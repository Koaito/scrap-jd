"""
Test các phần đợt 2 của pipeline.run_pipeline(): hook "URL đã biết", heartbeat
sau mỗi job (kể cả job bị bỏ qua), stats["skipped_known_url"] và
stats["field_empty"] — KHÔNG cần database (mock module db), KHÔNG cần internet.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pipeline
from adapters.base import BaseAdapter
from models import RawJobRecord

COMPLETE_PROBE = ("job-complete", "Toàn thời gian", "2026-09-05", {"job_description": "x"}, None)
NEEDS_PATCH_PROBE = ("job-old", None, None, None, None)  # job cũ chưa ghi nhận fetch + thiếu field -> phải vá

DETAIL = {
    "work_type": "Toàn thời gian", "deadline_text": "05/09/2026",
    "job_description": "mô tả", "requirements": "", "perks": "",
    "required_skills": [],
}


def _raw(url, **kw):
    base = dict(job_title=f"Job {url}", company_name="ACME", source_url=url,
                source_name="Fake", salary_text="", province_text="Hà Nội",
                experience_text="2 năm")
    base.update(kw)
    return RawJobRecord(**base)


class FakeAdapter(BaseAdapter):
    """Giống CareerViet: hỏi _is_known_url() TRƯỚC khi "fetch" từng URL."""

    source_name = "Fake"

    def __init__(self, urls):
        super().__init__()
        self.urls = urls

    def fetch_jobs(self, category_key, max_pages):
        for url in self.urls:
            if self._is_known_url(url):
                continue
            yield _raw(url)

    def fetch_job_full_detail(self, source_url):
        return dict(DETAIL)



# ----------------------------------------------------------------------
# _make_known_url_checker
# ----------------------------------------------------------------------
@pytest.fixture
def probes(pipeline_db):
    """Bảng probe tra theo URL cho db.get_job_probe_by_source_url; test điền vào bằng .update()."""
    table = {}
    pipeline_db.get_job_probe_by_source_url.side_effect = lambda conn, url: table.get(url)
    return table


def test_known_url_checker_semantics(pipeline_db, probes):
    probes.update({"complete": COMPLETE_PROBE, "old": NEEDS_PATCH_PROBE})
    check = pipeline._make_known_url_checker(MagicMock())

    assert check("complete") is True   # đã có và đủ field -> bỏ qua an toàn
    assert check("old") is False       # đã có nhưng thiếu field -> vẫn phải vá
    assert check("brand-new") is False  # chưa có


def test_known_url_checker_db_error_means_not_known_and_rolls_back(pipeline_db):
    pipeline_db.get_job_probe_by_source_url.side_effect = RuntimeError("DB mất kết nối")
    conn = MagicMock()
    check = pipeline._make_known_url_checker(conn)

    assert check("any") is False
    conn.rollback.assert_called_once()


# ----------------------------------------------------------------------
# run_pipeline
# ----------------------------------------------------------------------
def test_run_pipeline_skips_known_urls_and_reports_count(pipeline_db, probes):
    probes.update({
        "u-known": COMPLETE_PROBE,
        "u-old": NEEDS_PATCH_PROBE,
    })
    adapter = FakeAdapter(["u-known", "u-old", "u-new"])

    stats = pipeline.run_pipeline(adapter, MagicMock(), "data-analyst", 1)

    assert stats["skipped_known_url"] == 1      # u-known không tới được pipeline
    assert stats["fetched"] == 2                # chỉ u-old và u-new
    assert stats["updated_existing"] == 1       # u-old vẫn được vá như trước
    assert stats["inserted"] == 1               # u-new được insert
    pipeline_db.update_job_fields.assert_called_once()
    pipeline_db.insert_job.assert_called_once()


def test_run_pipeline_works_with_adapter_without_hook(pipeline_db):
    """Adapter không kế thừa BaseAdapter (không có set_known_url_checker)
    vẫn chạy bình thường, skipped_known_url = 0."""

    class PlainAdapter:
        def fetch_jobs(self, category_key, max_pages):
            yield _raw("u-new")

        def fetch_job_full_detail(self, source_url):
            return dict(DETAIL)

    stats = pipeline.run_pipeline(PlainAdapter(), MagicMock(), "data-analyst", 1)
    assert stats["inserted"] == 1
    assert stats["skipped_known_url"] == 0


def test_run_pipeline_reports_field_empty_rates(pipeline_db):
    adapter = FakeAdapter(["u-1", "u-2"])

    stats = pipeline.run_pipeline(adapter, MagicMock(), "data-analyst", 1)

    field_empty = stats["field_empty"]
    assert field_empty["listing"]["total"] == 2
    assert field_empty["listing"]["fields"]["salary_text"] == {"empty": 2, "rate": 1.0}
    assert field_empty["listing"]["fields"]["province_text"] == {"empty": 0, "rate": 0.0}
    assert field_empty["detail"]["total"] == 2
    assert field_empty["detail"]["fields"]["requirements"]["rate"] == 1.0
    assert field_empty["detail"]["fields"]["job_description"]["rate"] == 0.0


def test_run_pipeline_failed_detail_fetch_not_counted_in_detail_total(pipeline_db):
    class FailingDetail(FakeAdapter):
        def fetch_job_full_detail(self, source_url):
            return None

    stats = pipeline.run_pipeline(FailingDetail(["u-1"]), MagicMock(), "data-analyst", 1)

    assert stats["skipped_fetch_failed"] == 1
    assert "detail" not in stats["field_empty"]
    assert stats["field_empty"]["listing"]["total"] == 1


def test_run_pipeline_stats_are_json_serializable(pipeline_db):
    import json

    stats = pipeline.run_pipeline(FakeAdapter(["u-1"]), MagicMock(), "data-analyst", 1)
    json.dumps(stats)


def test_heartbeat_fires_for_every_job_even_when_skipped(pipeline_db, probes):
    """Regression: on_progress từng nằm SAU khối try nên mọi `continue` (job
    trùng URL, ẩn danh, đăng lại, fetch lỗi) bỏ qua heartbeat -> watchdog
    tính theo tiến độ tưởng lượt crawl đang treo."""
    probes.update({"u-1": COMPLETE_PROBE, "u-2": COMPLETE_PROBE})

    class NoHookAdapter(FakeAdapter):
        def set_known_url_checker(self, checker):  # tắt hook để job đi vào nhánh trùng URL
            pass

    calls = []
    stats = pipeline.run_pipeline(
        NoHookAdapter(["u-1", "u-2"]), MagicMock(), "data-analyst", 1,
        on_progress=lambda p: calls.append(dict(p)),
    )

    assert stats["skipped_duplicate"] == 2
    assert len(calls) == 2
    assert calls[-1] == {"fetched": 2, "inserted": 0}


def test_heartbeat_error_does_not_break_crawl(pipeline_db):
    def boom(_):
        raise RuntimeError("DB tạm mất kết nối lúc ghi progress")

    stats = pipeline.run_pipeline(
        FakeAdapter(["u-1", "u-2"]), MagicMock(), "data-analyst", 1, on_progress=boom,
    )
    assert stats["inserted"] == 2


def test_heartbeat_fires_when_adapter_skips_known_urls(pipeline_db, probes):
    """Trang toàn job đã biết: adapter bỏ qua hết nên pipeline không nhận
    record nào. Vẫn phải có heartbeat, nếu không watchdog tưởng bị treo."""
    probes.update({"u-1": COMPLETE_PROBE, "u-2": COMPLETE_PROBE, "u-3": COMPLETE_PROBE})
    calls = []

    stats = pipeline.run_pipeline(
        FakeAdapter(["u-1", "u-2", "u-3"]), MagicMock(), "data-analyst", 1,
        on_progress=lambda p: calls.append(dict(p)),
    )

    assert stats["fetched"] == 0 and stats["skipped_known_url"] == 3
    assert len(calls) == 3
