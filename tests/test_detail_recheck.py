"""
Điều kiện "job đã có còn cần fetch lại trang chi tiết không" (mục 2, 10/2026).

Trước đây job thiếu work_type/deadline/parsed_content LUÔN bị fetch lại, nên
tin nào nguồn không ghi hạn nộp thì tốn request ở mọi lượt crawl, mãi mãi.
Giờ có thêm dấu detail_checked_at: đã fetch rồi thì chỉ fetch lại sau
DETAIL_RECHECK_DAYS ngày. Test không cần DB (hàm thuần + pipeline mock).
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db
import pipeline
from adapters.base import BaseAdapter
from models import RawJobRecord

NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)
COMPLETE = ("j", "FULL_TIME", "2026-11-01", {"job_description": "x"})
NO_DEADLINE = ("j", "FULL_TIME", None, {"job_description": "x"})


def _probe(base, checked_at):
    return (*base, checked_at)


# ------------------------------------------------- job_needs_detail_enrichment

def test_unknown_job_needs_detail():
    assert db.job_needs_detail_enrichment(None) is True


@pytest.mark.parametrize("checked_at", [None, NOW, NOW - timedelta(days=400)])
def test_complete_job_never_needs_detail(checked_at):
    assert db.job_needs_detail_enrichment(_probe(COMPLETE, checked_at), now=NOW, recheck_days=7) is False


def test_incomplete_job_never_recorded_needs_detail():
    # Job cũ (trước khi có cột): chưa từng ghi nhận -> vá ngay như trước đây.
    assert db.job_needs_detail_enrichment(_probe(NO_DEADLINE, None), now=NOW, recheck_days=7) is True


def test_incomplete_job_checked_recently_is_not_refetched():
    probe = _probe(NO_DEADLINE, NOW - timedelta(days=1))
    assert db.job_needs_detail_enrichment(probe, now=NOW, recheck_days=7) is False


def test_incomplete_job_is_refetched_once_recheck_window_passed():
    just_before = _probe(NO_DEADLINE, NOW - timedelta(days=7) + timedelta(seconds=1))
    exactly = _probe(NO_DEADLINE, NOW - timedelta(days=7))
    assert db.job_needs_detail_enrichment(just_before, now=NOW, recheck_days=7) is False
    assert db.job_needs_detail_enrichment(exactly, now=NOW, recheck_days=7) is True


def test_recheck_days_zero_restores_old_behaviour():
    probe = _probe(NO_DEADLINE, NOW)
    assert db.job_needs_detail_enrichment(probe, now=NOW, recheck_days=0) is True


def test_naive_timestamp_is_treated_as_utc():
    probe = _probe(NO_DEADLINE, datetime(2026, 9, 1, 12, 0))
    assert db.job_needs_detail_enrichment(probe, now=NOW, recheck_days=7) is True


def test_default_recheck_days_comes_from_config(monkeypatch):
    probe = _probe(NO_DEADLINE, NOW - timedelta(days=3))
    monkeypatch.setattr("db.jobs.DETAIL_RECHECK_DAYS", 2)
    assert db.job_needs_detail_enrichment(probe, now=NOW) is True
    monkeypatch.setattr("db.jobs.DETAIL_RECHECK_DAYS", 30)
    assert db.job_needs_detail_enrichment(probe, now=NOW) is False


# -------------------------------------------------------- pipeline (mock db)

DETAIL = {
    "work_type": "Toàn thời gian", "deadline_text": "",
    "job_description": "mô tả", "requirements": "", "perks": "", "required_skills": [],
}


class FakeAdapter(BaseAdapter):
    source_name = "Fake"

    def __init__(self, url, detail=DETAIL):
        super().__init__()
        self.url = url
        self.detail = detail
        self.detail_calls = 0

    def fetch_jobs(self, category_key, max_pages):
        yield RawJobRecord(job_title="DA", company_name="ACME", source_url=self.url,
                           source_name="Fake", province_text="Hà Nội", experience_text="2 năm")

    def fetch_job_full_detail(self, source_url):
        self.detail_calls += 1
        return None if self.detail is None else dict(self.detail)


@pytest.fixture
def fake_db(monkeypatch):
    fdb = MagicMock()
    fdb.job_needs_detail_enrichment.side_effect = db.job_needs_detail_enrichment
    fdb.find_company_probe.return_value = None
    fdb.probe_needs_enrichment.return_value = False
    fdb.get_or_create_company_by_profile.return_value = "company-1"
    fdb.find_repost_candidate.return_value = None
    monkeypatch.setattr(pipeline, "db", fdb)
    return fdb


def _run(adapter):
    return pipeline.run_pipeline(adapter, MagicMock(), "data-analyst", 1)


def test_existing_incomplete_job_recently_checked_is_not_fetched(fake_db):
    fake_db.get_job_probe_by_source_url.return_value = _probe(
        NO_DEADLINE, datetime.now(timezone.utc) - timedelta(hours=1))
    adapter = FakeAdapter("https://x/a")

    stats = _run(adapter)

    assert adapter.detail_calls == 0
    assert stats["skipped_duplicate"] == 1
    fake_db.mark_source_detail_checked.assert_not_called()


def test_existing_incomplete_job_due_is_fetched_and_stamped(fake_db):
    fake_db.get_job_probe_by_source_url.return_value = _probe(
        NO_DEADLINE, datetime.now(timezone.utc) - timedelta(days=30))
    adapter = FakeAdapter("https://x/a")

    _run(adapter)

    assert adapter.detail_calls == 1
    fake_db.mark_source_detail_checked.assert_called_once()
    assert fake_db.mark_source_detail_checked.call_args.args[1] == "https://x/a"


def test_failed_fetch_is_not_stamped_so_it_retries_next_crawl(fake_db):
    fake_db.get_job_probe_by_source_url.return_value = _probe(NO_DEADLINE, None)
    adapter = FakeAdapter("https://x/a", detail=None)

    stats = _run(adapter)

    assert adapter.detail_calls == 1
    assert stats["skipped_fetch_failed"] == 1
    fake_db.mark_source_detail_checked.assert_not_called()


def test_new_job_insert_is_marked_as_detail_fetched(fake_db):
    fake_db.get_job_probe_by_source_url.return_value = None
    fake_db.insert_job.return_value = "new-job"

    _run(FakeAdapter("https://x/new"))

    assert fake_db.insert_job.call_args.kwargs["detail_fetched"] is True
