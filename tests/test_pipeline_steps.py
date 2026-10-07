"""
Đợt B2: vòng lặp _process_jobs() được tách thành các bước nhỏ
(_process_job -> _import_new_job -> _import_repost / _insert_new_job).
Test từng bước riêng, KHÔNG cần database (mock module db), KHÔNG cần internet.

Tính "không đổi hành vi" của cả pipeline đã có test_pipeline_stats.py,
test_pipeline_repost.py, test_pipeline_blocked_degraded.py và các test_pg_*;
file này khoá ranh giới giữa các bước để sau này sửa 1 bước không làm vỡ bước khác.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import normalize
import pipeline
from adapters.base import BaseAdapter, CrawlBlockedError
from field_stats import EmptyFieldCounter
from models import RawJobRecord
from pipeline_stats import PipelineStats

DETAIL = {
    "work_type": "Toàn thời gian", "deadline_text": "05/09/2026",
    "job_description": "mô tả", "requirements": "yêu cầu", "perks": "",
    "required_skills": ["SQL"],
}


def _raw(url="u-1", **kw):
    base = dict(job_title="Data Analyst", company_name="ACME", source_url=url,
                source_name="Fake", salary_text="10-15 triệu", province_text="Hà Nội",
                experience_text="2 năm")
    base.update(kw)
    return RawJobRecord(**base)


class StubAdapter(BaseAdapter):
    source_name = "Fake"

    def __init__(self, detail=DETAIL):
        super().__init__()
        self.detail = detail
        self.detail_calls = []

    def fetch_jobs(self, category_key, max_pages):
        yield from ()

    def fetch_job_full_detail(self, source_url):
        self.detail_calls.append(source_url)
        return dict(self.detail) if self.detail is not None else None


def _candidate(job_id, status="OPEN", closed_reason=None):
    """Kết quả db.find_repost_candidate() cho test (mock db)."""
    return {"job_id": job_id, "job_status": status, "level_id": None, "deadline": None,
            "closed_reason": closed_reason}


@pytest.fixture
def fake_db(monkeypatch):
    fdb = MagicMock()
    fdb.get_job_probe_by_source_url.return_value = None
    fdb.find_company_probe.return_value = None
    fdb.probe_needs_enrichment.return_value = False
    fdb.get_or_create_company_by_profile.return_value = "company-1"
    fdb.get_or_create_province.return_value = 7
    fdb.get_level_id.return_value = 3
    fdb.find_repost_candidate.return_value = None
    fdb.extend_job_deadline.return_value = False
    monkeypatch.setattr(pipeline, "db", fdb)
    return fdb


def _run_step(adapter, conn, raw=None):
    stats = PipelineStats()
    pipeline._process_job(adapter, conn, raw or _raw(), stats, EmptyFieldCounter())
    return stats


# ----------------------------------------------------------------------
# _process_job: chọn nhánh theo source_url
# ----------------------------------------------------------------------
def test_known_url_goes_to_existing_branch_and_never_imports_new(fake_db, monkeypatch):
    fake_db.get_job_probe_by_source_url.return_value = ("job-1", "Toàn thời gian", "2026-09-05", {"a": 1}, None)
    fake_db.job_needs_detail_enrichment.return_value = False
    spy = MagicMock()
    monkeypatch.setattr(pipeline, "_import_new_job", spy)

    stats = _run_step(StubAdapter(), MagicMock())

    assert stats.skipped_duplicate == 1
    spy.assert_not_called()
    fake_db.insert_job.assert_not_called()


def test_unknown_url_goes_to_import_new_job(fake_db, monkeypatch):
    spy = MagicMock()
    monkeypatch.setattr(pipeline, "_import_new_job", spy)
    conn, adapter, raw = MagicMock(), StubAdapter(), _raw()

    _run_step(adapter, conn, raw)

    spy.assert_called_once()
    assert spy.call_args.args[:3] == (adapter, conn, raw)


# ----------------------------------------------------------------------
# _import_new_job: các điểm dừng sớm
# ----------------------------------------------------------------------
def test_anonymous_employer_stops_before_any_fetch_or_db_write(fake_db):
    adapter = StubAdapter()
    stats = _run_step(adapter, MagicMock(), _raw(company_name="Vietnamworks' Client"))

    assert stats.skipped_anonymous_employer == 1
    assert adapter.detail_calls == []  # không tốn request fetch chi tiết
    fake_db.get_or_create_province.assert_not_called()
    fake_db.insert_job.assert_not_called()


def test_failed_detail_fetch_stops_before_company_and_insert(fake_db):
    conn = MagicMock()
    stats = _run_step(StubAdapter(detail=None), conn)

    assert stats.skipped_fetch_failed == 1
    fake_db.get_or_create_company_by_profile.assert_not_called()
    fake_db.insert_job.assert_not_called()
    conn.commit.assert_not_called()


# ----------------------------------------------------------------------
# Nhánh insert và nhánh đăng lại
# ----------------------------------------------------------------------
def test_new_job_is_inserted_then_committed_once(fake_db):
    conn = MagicMock()
    stats = _run_step(StubAdapter(), conn)

    assert stats.inserted == 1
    assert stats.skipped_duplicate_repost == 0
    fake_db.insert_job.assert_called_once()
    kwargs = fake_db.insert_job.call_args.kwargs
    assert kwargs["company_id"] == "company-1"
    assert kwargs["level_id"] == 3 and kwargs["province_id"] == 7
    assert kwargs["detail_fetched"] is True
    assert kwargs["source_url"] == "u-1"
    assert kwargs["parsed_content"]["required_skills"] == ["SQL"]
    conn.commit.assert_called_once()


def test_repost_links_source_and_commits_without_insert(fake_db):
    fake_db.find_repost_candidate.return_value = _candidate("old-job")
    fake_db.extend_job_deadline.return_value = True
    conn = MagicMock()

    stats = _run_step(StubAdapter(), conn)

    assert stats.inserted == 0
    assert stats.skipped_duplicate_repost == 1
    assert stats.repost_deadline_extended == 1
    fake_db.insert_job.assert_not_called()
    fake_db.link_repost_source.assert_called_once()
    assert fake_db.link_repost_source.call_args.args[1] == "old-job"
    conn.commit.assert_called_once()


def test_repost_without_later_deadline_does_not_count_extension(fake_db):
    fake_db.find_repost_candidate.return_value = _candidate("old-job")
    fake_db.extend_job_deadline.return_value = False

    stats = _run_step(StubAdapter(), MagicMock())

    assert stats.skipped_duplicate_repost == 1
    assert stats.repost_deadline_extended == 0


# ----------------------------------------------------------------------
# Lỗi ở bất kỳ bước nào do _process_jobs() xử lý (rollback + đếm), không
# bị nuốt ở bước con.
# ----------------------------------------------------------------------
class OneJobAdapter(StubAdapter):
    def fetch_jobs(self, category_key, max_pages):
        yield _raw("u-1")


def _run_loop(adapter, conn):
    stats = PipelineStats()
    progress = MagicMock()
    pipeline._process_jobs(adapter, conn, "da", 1, None, stats, EmptyFieldCounter(), progress)
    return stats, progress


def test_error_inside_insert_step_is_rolled_back_counted_and_heartbeat_still_fires(fake_db):
    fake_db.insert_job.side_effect = RuntimeError("boom")
    conn = MagicMock()

    stats, progress = _run_loop(OneJobAdapter(), conn)

    assert stats.errors == 1 and stats.inserted == 0
    # 2 lần: 1 lần đóng transaction đọc sau câu probe + 1 lần huỷ phần ghi dở do lỗi
    assert conn.rollback.call_count == 2
    conn.commit.assert_not_called()
    progress.assert_called_once()


def test_error_inside_repost_step_is_rolled_back_and_counted(fake_db):
    fake_db.find_repost_candidate.return_value = _candidate("old-job")
    fake_db.link_repost_source.side_effect = RuntimeError("boom")
    conn = MagicMock()

    stats, _ = _run_loop(OneJobAdapter(), conn)

    assert stats.errors == 1
    # 2 lần: 1 lần đóng transaction đọc sau câu probe + 1 lần huỷ phần ghi dở do lỗi
    assert conn.rollback.call_count == 2
    conn.commit.assert_not_called()


def test_block_inside_nested_step_rolls_back_and_propagates(fake_db):
    class BlockedAdapter(OneJobAdapter):
        def fetch_job_full_detail(self, source_url):
            raise CrawlBlockedError("bị chặn")

    conn = MagicMock()
    stats = PipelineStats()
    progress = MagicMock()
    with pytest.raises(CrawlBlockedError):
        pipeline._process_jobs(BlockedAdapter(), conn, "da", 1, None, stats, EmptyFieldCounter(), progress)

    # 2 lần: 1 lần đóng transaction đọc sau câu probe + 1 lần huỷ phần ghi dở do lỗi
    assert conn.rollback.call_count == 2
    assert stats.errors == 0  # bị chặn không bị đếm như lỗi từng job
    progress.assert_called_once()


def test_normalize_is_used_for_level_and_company_name(fake_db):
    """Khoá việc bước insert nhận level/công ty đã chuẩn hoá chứ không phải raw."""
    raw = _raw(company_name="  ACME  Co  ", experience_text="5 năm", job_title="Senior Data Analyst")
    _run_step(StubAdapter(), MagicMock(), raw)

    expected_level = normalize.infer_level(raw.experience_text, raw.job_title)
    fake_db.get_level_id.assert_called_once_with(fake_db.get_level_id.call_args.args[0], expected_level)
    assert fake_db.get_or_create_company_by_profile.call_args.args[1] == normalize.clean_company_name(raw.company_name)


def test_level_hint_from_source_reaches_infer_level(fake_db):
    """Pipeline phải truyền raw.level_hint vào infer_level (VietnamWorks dùng
    jobLevel làm dự phòng khi không đọc được số năm)."""
    raw = _raw(experience_text="", job_title="Quản lý vận hành", level_hint="Manager")
    _run_step(StubAdapter(), MagicMock(), raw)
    assert fake_db.get_level_id.call_args.args[1] == "Manager"

