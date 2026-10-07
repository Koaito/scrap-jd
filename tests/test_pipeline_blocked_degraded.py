"""
Đợt 3 của pipeline.run_pipeline(): (a) bị chặn GIỮA CHỪNG không bị nuốt vào
except từng job, giữ lại stats tạm; (b) cờ stats["degraded"] khi dữ liệu nhiều
khả năng sai — KHÔNG cần database (mock module db), KHÔNG cần internet.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pipeline
from adapters.base import BaseAdapter, CrawlBlockedError
from field_stats import PLACEHOLDER_VALUES, degraded_reasons, is_empty, EmptyFieldCounter
from models import RawJobRecord

GOOD_DETAIL = {
    "work_type": "Toàn thời gian", "deadline_text": "05/09/2026",
    "job_description": "mô tả", "requirements": "yêu cầu", "perks": "",
    "required_skills": [],
}
BLANK_DETAIL = {
    "work_type": "", "deadline_text": "", "job_description": "",
    "requirements": "", "perks": "", "required_skills": [],
}


def _raw(i, **kw):
    base = dict(job_title=f"Job {i}", company_name="ACME", source_url=f"u-{i}",
                source_name="Fake", salary_text="", province_text="Hà Nội",
                experience_text="2 năm")
    base.update(kw)
    return RawJobRecord(**base)


class ScriptedAdapter(BaseAdapter):
    """Trả `n` job; detail do `detail_for(index)` quyết định (dict/None) hoặc
    raise nếu là Exception."""

    source_name = "Fake"

    def __init__(self, n, detail_for=lambda i: GOOD_DETAIL, raw_for=_raw):
        super().__init__()
        self.n = n
        self.detail_for = detail_for
        self.raw_for = raw_for

    def fetch_jobs(self, category_key, max_pages):
        for i in range(self.n):
            yield self.raw_for(i)

    def fetch_job_full_detail(self, source_url):
        index = int(source_url.split("-")[1])
        result = self.detail_for(index)
        if isinstance(result, Exception):
            raise result
        return dict(result) if result is not None else None



# ----------------------------------------------------------------------
# (a) bị chặn giữa chừng
# ----------------------------------------------------------------------
def test_block_during_detail_fetch_is_not_swallowed_and_keeps_partial_stats(pipeline_db):
    """Ngắt mạch raise TRONG fetch_job_full_detail() của job thứ 3 — nằm trong
    try của từng job. Phải lan ra ngoài (không chạy tiếp job 4, 5...), rollback
    job dở dang, và mang theo số liệu của 2 job đã lưu."""
    blocked = CrawlBlockedError("3 lần fetch liên tiếp thất bại")
    adapter = ScriptedAdapter(
        5, detail_for=lambda i: blocked if i == 2 else GOOD_DETAIL,
    )
    conn = MagicMock()

    with pytest.raises(CrawlBlockedError) as exc_info:
        pipeline.run_pipeline(adapter, conn, "data-analyst", 3)

    stats = exc_info.value.stats
    assert stats["blocked"] is True
    assert stats["fetched"] == 3        # job 0, 1 xong; job 2 bị chặn
    assert stats["inserted"] == 2
    assert stats["errors"] == 0         # KHÔNG bị tính là "lỗi từng job" rồi đi tiếp
    assert pipeline_db.insert_job.call_count == 2   # job 3, 4 chưa bao giờ được xử lý
    conn.rollback.assert_called()


def test_block_in_generator_still_carries_stats_with_field_summary(pipeline_db):
    class BlockedAfterTwo(ScriptedAdapter):
        def fetch_jobs(self, category_key, max_pages):
            yield self.raw_for(0)
            yield self.raw_for(1)
            raise CrawlBlockedError("trang 2 bị chặn")

    with pytest.raises(CrawlBlockedError) as exc_info:
        pipeline.run_pipeline(BlockedAfterTwo(2), MagicMock(), "data-analyst", 3)

    stats = exc_info.value.stats
    assert stats["blocked"] is True
    assert stats["inserted"] == 2
    assert stats["field_empty"]["listing"]["total"] == 2


def test_block_before_any_job_still_has_stats(pipeline_db):
    class BlockedAtStart(BaseAdapter):
        source_name = "Fake"

        def fetch_jobs(self, category_key, max_pages):
            raise CrawlBlockedError("trang đầu bị chặn")
            yield  # pragma: no cover

    with pytest.raises(CrawlBlockedError) as exc_info:
        pipeline.run_pipeline(BlockedAtStart(), MagicMock(), "data-analyst", 3)

    stats = exc_info.value.stats
    assert stats["blocked"] is True
    assert stats["fetched"] == 0
    assert "field_empty" not in stats


def test_ordinary_job_error_still_does_not_stop_the_run(pipeline_db):
    """Chỉ CrawlBlockedError mới dừng cả lượt; lỗi thường của 1 job vẫn chỉ
    tính vào stats["errors"] như trước."""
    adapter = ScriptedAdapter(
        4, detail_for=lambda i: ValueError("lỗi parse") if i == 1 else GOOD_DETAIL,
    )
    stats = pipeline.run_pipeline(adapter, MagicMock(), "data-analyst", 3)

    assert stats["errors"] == 1
    assert stats["inserted"] == 3
    assert "blocked" not in stats


# ----------------------------------------------------------------------
# (b) degraded
# ----------------------------------------------------------------------
def test_clean_run_has_no_degraded_key(pipeline_db):
    stats = pipeline.run_pipeline(ScriptedAdapter(12), MagicMock(), "data-analyst", 3)
    assert "degraded" not in stats


def test_degraded_when_detail_description_empty_almost_everywhere(pipeline_db):
    adapter = ScriptedAdapter(12, detail_for=lambda i: BLANK_DETAIL)
    stats = pipeline.run_pipeline(adapter, MagicMock(), "data-analyst", 3)

    reasons = stats["degraded"]["reasons"]
    assert {"group": "detail", "field": "job_description"}.items() <= reasons[0].items()
    assert reasons[0]["type"] == "field_empty"
    assert reasons[0]["empty"] == 12 and reasons[0]["total"] == 12
    assert stats["inserted"] == 12  # chỉ cảnh báo, KHÔNG chặn insert


def test_not_degraded_when_only_some_descriptions_are_empty(pipeline_db):
    # 5/12 ~ 42% rỗng: dưới ngưỡng 90%
    adapter = ScriptedAdapter(12, detail_for=lambda i: BLANK_DETAIL if i < 5 else GOOD_DETAIL)
    stats = pipeline.run_pipeline(adapter, MagicMock(), "data-analyst", 3)
    assert "degraded" not in stats


def test_not_degraded_with_too_few_samples(pipeline_db):
    """Crawl thử 5 job toàn rỗng: mẫu quá nhỏ (< 10) nên không kết luận."""
    adapter = ScriptedAdapter(5, detail_for=lambda i: BLANK_DETAIL)
    stats = pipeline.run_pipeline(adapter, MagicMock(), "data-analyst", 3)
    assert "degraded" not in stats


def test_placeholder_company_name_counts_as_empty(pipeline_db):
    """TopCV điền "Chưa xác định" khi không bắt được tên công ty — nếu không
    coi là rỗng thì company_name không bao giờ hiện hỏng."""
    adapter = ScriptedAdapter(
        12, raw_for=lambda i: _raw(i, company_name="Chưa xác định"),
    )
    stats = pipeline.run_pipeline(adapter, MagicMock(), "data-analyst", 3)

    fields = [(r["group"], r["field"]) for r in stats["degraded"]["reasons"]]
    assert ("listing", "company_name") in fields


def test_listing_anomaly_marks_run_degraded(pipeline_db):
    class EmptyFirstPage(BaseAdapter):
        source_name = "Fake"

        def fetch_jobs(self, category_key, max_pages):
            self._flag_listing_anomaly("first_page_no_jobs")
            return iter(())

    stats = pipeline.run_pipeline(EmptyFirstPage(), MagicMock(), "data-analyst", 3)
    assert stats["degraded"]["reasons"] == [{"type": "first_page_no_jobs"}]
    assert stats["fetched"] == 0


def test_adapter_without_anomaly_attribute_is_fine(pipeline_db):
    """Adapter tự viết, không kế thừa BaseAdapter (như BlockedAdapter ở
    test_crawl_blocked.py) không có listing_anomalies — không được lỗi."""

    class Plain:
        def fetch_jobs(self, category_key, max_pages):
            return iter(())

    stats = pipeline.run_pipeline(Plain(), MagicMock(), "data-analyst", 3)
    assert "degraded" not in stats


# ----------------------------------------------------------------------
# field_stats: placeholder + degraded_reasons (mức hàm)
# ----------------------------------------------------------------------
def test_placeholder_is_empty_case_insensitive():
    assert "chưa xác định" in PLACEHOLDER_VALUES
    assert is_empty("Chưa xác định") is True
    assert is_empty("  CHƯA XÁC ĐỊNH ") is True
    assert is_empty("Công ty ABC") is False


def _summary(group, field, empty, total):
    counter = EmptyFieldCounter()
    for i in range(total):
        counter.record(group, {field: "" if i < empty else "x"}, [field])
    return counter.summary()


@pytest.mark.parametrize("empty, total, expected", [
    (9, 10, True),    # đúng ngưỡng 90%
    (8, 10, False),   # 80% < 90%
    (10, 10, True),
    (9, 9, False),    # < 10 mẫu
])
def test_degraded_reasons_threshold_and_min_samples(empty, total, expected):
    summary = _summary("detail", "job_description", empty, total)
    reasons = degraded_reasons(summary, rate_threshold=0.9)
    assert bool(reasons) is expected


def test_degraded_reasons_ignores_non_critical_fields():
    """salary_text rỗng 100% là bình thường (nhiều tin không ghi lương) —
    không được làm lượt crawl bị đánh dấu degraded."""
    summary = _summary("listing", "salary_text", 20, 20)
    assert degraded_reasons(summary, rate_threshold=0.9) == []
