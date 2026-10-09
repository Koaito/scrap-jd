"""
Đợt 1 rà soát pipeline (10/2026): lượt crawl không được "xong" một cách im lặng
khi thực ra mất job — KHÔNG cần database, KHÔNG cần internet.

Phủ 3 chỗ trước đây không lộ lên stats:
  1. Job adapter tự bỏ trong fetch_jobs() vì không tải/parse được trang chi tiết
     (CareerViet, VietnamWorks) -> stats.skipped_detail_unavailable + degraded
     khi gần hết job bị bỏ.
  2. Trang listing SAU trang đầu tải thất bại (cả 3 adapter) -> cờ degraded
     listing_page_failed thay vì "xong" như đã crawl đủ.
  3. Lỗi bất ngờ làm chết cả generator fetch_jobs() -> exc.stats giữ số liệu đã
     có, crawl_runner ghi vào crawl_runs.stats thay vì để trống.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.crawl_runner as runner
import pipeline
from scrapjd.adapters.base import ANOMALY_LISTING_PAGE_FAILED, BaseAdapter
from scrapjd.adapters.careerviet import CareerVietAdapter
from scrapjd.adapters.topcv import TopCVAdapter
from scrapjd.adapters.vietnamworks import VietnamWorksAdapter
from scrapjd.config import TOPCV_CATEGORIES
from scrapjd.models import RawJobRecord
from pipeline_stats import PipelineStats
from vnw_page_builder import build_detail_html

HERE = os.path.dirname(__file__)
CV_SEARCH = "https://careerviet.vn/viec-lam/data-analyst-k-vi.html"


def _read(name: str) -> str:
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


# ----------------------------------------------------------------------
# PipelineStats: khoá mới chỉ xuất hiện khi > 0
# ----------------------------------------------------------------------
def test_skipped_detail_unavailable_is_omitted_when_zero_and_present_when_set():
    assert "skipped_detail_unavailable" not in PipelineStats().to_dict()
    assert PipelineStats(skipped_detail_unavailable=3).to_dict()["skipped_detail_unavailable"] == 3


# ----------------------------------------------------------------------
# 2) Trang listing SAU trang đầu thất bại -> cờ degraded
# ----------------------------------------------------------------------
def test_topcv_later_page_failure_flags_anomaly_but_keeps_first_page_jobs():
    adapter = TopCVAdapter()
    page1 = _read("fixture_topcv_listing.html")
    base_url = TOPCV_CATEGORIES["data-analyst"]["url"]
    adapter._fetch_html = lambda url, max_retries=3: page1 if url == base_url else None

    records = list(adapter.fetch_jobs("data-analyst", 3))

    assert records                                    # job trang 1 vẫn được giữ
    assert adapter.listing_anomalies == [ANOMALY_LISTING_PAGE_FAILED]


def test_careerviet_later_page_failure_flags_anomaly():
    adapter = CareerVietAdapter()
    listing = _read("fixture_careerviet_listing.html")
    detail = _read("fixture_careerviet_job_detail.html")
    urls = CareerVietAdapter._extract_job_detail_urls(listing)
    pages = {CV_SEARCH: listing, **{u: detail for u in urls}}   # trang 2 -> None
    adapter._fetch_html = lambda url, max_retries=3: pages.get(url)

    assert list(adapter.fetch_jobs("data-analyst", 3))
    assert adapter.listing_anomalies == [ANOMALY_LISTING_PAGE_FAILED]


def test_vietnamworks_later_page_failure_flags_anomaly():
    adapter = VietnamWorksAdapter()
    job = {
        "jobId": 1, "jobTitle": "Data Analyst", "companyName": "ACME",
        "jobUrl": "https://www.vietnamworks.com/da-1-jv", "jobRequirement": "<p>2 năm kinh nghiệm</p>",
    }
    pages = {0: {"meta": {"nbPages": 3}, "data": [job]}, 1: None}
    adapter._post_json = lambda url, body, **kw: pages[body["page"]]
    adapter._fetch_html = lambda url, max_retries=3: build_detail_html(job)

    assert len(list(adapter.fetch_jobs("data-analyst", 3))) == 1
    assert adapter.listing_anomalies == [ANOMALY_LISTING_PAGE_FAILED]


def test_no_anomaly_when_pagination_ends_normally():
    """Trang cuối hết job là bình thường, KHÔNG được báo cờ."""
    adapter = CareerVietAdapter()
    listing = _read("fixture_careerviet_listing.html")
    detail = _read("fixture_careerviet_job_detail.html")
    urls = CareerVietAdapter._extract_job_detail_urls(listing)
    pages = {CV_SEARCH: listing, **{u: detail for u in urls}}
    # trang 2 tải được nhưng không có job nào -> hết trang thật
    page2 = "https://careerviet.vn/viec-lam/data-analyst-k-trang-2-vi.html"
    pages[page2] = "<html><body>Không có việc làm</body></html>"
    adapter._fetch_html = lambda url, max_retries=3: pages.get(url)

    list(adapter.fetch_jobs("data-analyst", 3))
    assert adapter.listing_anomalies == []


# ----------------------------------------------------------------------
# 1) Job adapter tự bỏ -> được đếm
# ----------------------------------------------------------------------
def test_careerviet_counts_jobs_dropped_when_detail_cannot_be_fetched_or_parsed():
    adapter = CareerVietAdapter()
    listing = _read("fixture_careerviet_listing.html")
    detail = _read("fixture_careerviet_job_detail.html")
    urls = CareerVietAdapter._extract_job_detail_urls(listing)
    assert len(urls) >= 3
    pages = {CV_SEARCH: listing, urls[0]: None, urls[1]: "<html>trang đổi cấu trúc</html>"}
    pages.update({u: detail for u in urls[2:]})
    adapter._fetch_html = lambda url, max_retries=3: pages.get(url)

    records = list(adapter.fetch_jobs("data-analyst", 1))

    assert len(records) == len(urls) - 2
    assert adapter.skipped_detail_unavailable_count == 2


def test_vietnamworks_counts_jobs_dropped_when_detail_page_cannot_be_fetched():
    adapter = VietnamWorksAdapter()
    job = {"jobId": 1, "jobTitle": "Data Analyst", "companyName": "ACME",
           "jobUrl": "https://www.vietnamworks.com/da-1-jv"}
    adapter._post_json = lambda url, body, **kw: {"meta": {"nbPages": 1}, "data": [job]}
    adapter._fetch_html = lambda url, max_retries=3: None

    assert list(adapter.fetch_jobs("data-analyst", 1)) == []
    assert adapter.skipped_detail_unavailable_count == 1


def test_vietnamworks_job_with_missing_required_fields_is_not_counted_as_dropped():
    """Job thiếu title/company/url là dữ liệu listing hỏng, KHÔNG phải trang chi tiết lỗi."""
    adapter = VietnamWorksAdapter()
    adapter._post_json = lambda url, body, **kw: {"meta": {"nbPages": 1}, "data": [{"jobId": 9}]}
    adapter._fetch_html = lambda url, max_retries=3: None
    assert list(adapter.fetch_jobs("data-analyst", 1)) == []
    assert adapter.skipped_detail_unavailable_count == 0


# ----------------------------------------------------------------------
# pipeline._finalize_stats: stats + degraded
# ----------------------------------------------------------------------
class DroppingAdapter(BaseAdapter):
    source_name = "Fake"

    def __init__(self, dropped, delivered, anomalies=()):
        super().__init__()
        self._dropped = dropped
        self._delivered = delivered
        self.listing_anomalies = list(anomalies)

    def fetch_jobs(self, category_key, max_pages):
        self.skipped_detail_unavailable_count = self._dropped
        for i in range(self._delivered):
            yield RawJobRecord(job_title=f"Job {i}", company_name="ACME", source_url=f"u-{i}",
                               source_name="Fake", province_text="Hà Nội", experience_text="2 năm")

    def fetch_job_full_detail(self, source_url):
        return {"work_type": "Toàn thời gian", "deadline_text": "", "job_description": "mô tả",
                "requirements": "", "perks": "", "required_skills": []}



def _run(adapter):
    return pipeline.run_pipeline(adapter, MagicMock(), "data-analyst", 1)


def _reason_types(stats):
    return [r["type"] for r in (stats.get("degraded") or {}).get("reasons", [])]


def test_dropped_count_reaches_stats_without_degraded_when_few(pipeline_db):
    stats = _run(DroppingAdapter(dropped=2, delivered=20))
    assert stats["skipped_detail_unavailable"] == 2
    assert "jobs_dropped" not in _reason_types(stats)


def test_most_jobs_dropped_marks_run_degraded(pipeline_db):
    """Parse trang chi tiết hỏng cả loạt: tải được trang nên circuit breaker không
    nổ; trước đây lượt này kết thúc 'done' không một cảnh báo."""
    stats = _run(DroppingAdapter(dropped=38, delivered=2))
    assert stats["skipped_detail_unavailable"] == 38
    (reason,) = [r for r in stats["degraded"]["reasons"] if r["type"] == "jobs_dropped"]
    assert reason["dropped"] == 38 and reason["total"] == 40 and reason["rate"] == 0.95


def test_small_sample_is_not_flagged_degraded(pipeline_db):
    """Crawl thử 3 job mà cả 3 bị bỏ: mẫu quá nhỏ, không báo nhầm."""
    stats = _run(DroppingAdapter(dropped=3, delivered=0))
    assert "degraded" not in stats


def test_half_dropped_is_not_flagged_degraded(pipeline_db):
    stats = _run(DroppingAdapter(dropped=20, delivered=20))
    assert "jobs_dropped" not in _reason_types(stats)


def test_listing_page_failed_anomaly_becomes_degraded_reason(pipeline_db):
    stats = _run(DroppingAdapter(dropped=0, delivered=3, anomalies=[ANOMALY_LISTING_PAGE_FAILED]))
    assert _reason_types(stats) == [ANOMALY_LISTING_PAGE_FAILED]


def test_normal_run_has_no_new_keys(pipeline_db):
    """Lượt bình thường giữ nguyên tập khoá cũ (hợp đồng với frontend/crawl_runs.stats)."""
    stats = _run(DroppingAdapter(dropped=0, delivered=3))
    assert "skipped_detail_unavailable" not in stats
    assert "degraded" not in stats


# ----------------------------------------------------------------------
# 3) Lỗi bất ngờ làm chết generator -> giữ stats
# ----------------------------------------------------------------------
class ExplodingAdapter(DroppingAdapter):
    def fetch_jobs(self, category_key, max_pages):
        yield from super().fetch_jobs(category_key, max_pages)
        raise KeyError("parser nổ ở trang 2")


def test_unexpected_adapter_error_keeps_partial_stats_on_exception(pipeline_db):
    conn = MagicMock()
    with pytest.raises(KeyError) as exc_info:
        pipeline.run_pipeline(ExplodingAdapter(dropped=1, delivered=3), conn, "data-analyst", 2)

    stats = exc_info.value.stats
    assert stats["fetched"] == 3 and stats["inserted"] == 3
    assert stats["skipped_detail_unavailable"] == 1
    assert "blocked" not in stats          # lỗi thường, không phải bị chặn
    conn.rollback.assert_called()


def test_error_while_building_partial_stats_does_not_hide_original_error(pipeline_db, monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("không dựng được stats")
    monkeypatch.setattr(pipeline, "_finalize_stats", boom)

    with pytest.raises(KeyError, match="parser nổ"):       # vẫn là lỗi gốc
        pipeline.run_pipeline(ExplodingAdapter(dropped=0, delivered=1), MagicMock(), "data-analyst", 2)


def test_crawl_runner_records_partial_stats_for_ordinary_error(monkeypatch):
    from test_crawl_runner_blocked import _install

    def fake(adapter, conn, category, pages, **kw):
        exc = KeyError("parser nổ")
        exc.stats = {"fetched": 3, "inserted": 3}
        raise exc
    fdb = _install(monkeypatch, pipeline=fake)

    runner._execute_one("run-1")

    args, kwargs = fdb.mark_crawl_run_error.call_args
    assert kwargs["stats"] == {"fetched": 3, "inserted": 3}
    assert "blocked" not in kwargs["stats"]
