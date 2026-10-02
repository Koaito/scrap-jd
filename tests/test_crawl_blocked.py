"""
Hợp đồng chung của CrawlBlockedError cho CẢ 3 adapter + pipeline — KHÔNG
cần database, KHÔNG cần internet.

Hợp đồng: trang ĐẦU TIÊN của 1 lượt crawl thất bại sau khi hết retry ->
adapter phải raise CrawlBlockedError (để api/crawl_runner.py ghi
status='error'), KHÔNG được im lặng kết thúc như "hết job" (status='done'
với 0 job). Test parametrize theo adapter để nguồn mới thêm sau này (chỉ
cần thêm 1 dòng vào ADAPTER_CASES) cũng bị ép tuân thủ hợp đồng này.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adapters.base import CrawlBlockedError
from adapters.careerviet import CareerVietAdapter
from adapters.topcv import TopCVAdapter
from adapters.vietnamworks import VietnamWorksAdapter
from pipeline import run_pipeline

# (lớp adapter, tên method HTTP cần giả lập thất bại)
ADAPTER_CASES = [
    pytest.param(TopCVAdapter, "_fetch_html", id="topcv"),
    pytest.param(VietnamWorksAdapter, "_post_json", id="vietnamworks"),
    pytest.param(CareerVietAdapter, "_fetch_html", id="careerviet"),
]


@pytest.mark.parametrize("adapter_cls, http_method", ADAPTER_CASES)
def test_first_page_failure_raises_crawl_blocked(adapter_cls, http_method):
    adapter = adapter_cls()
    setattr(adapter, http_method, lambda *a, **kw: None)  # hết retry -> None

    category_key = "data-analyst"
    with pytest.raises(CrawlBlockedError):
        list(adapter.fetch_jobs(category_key, 3))


def test_pipeline_does_not_swallow_crawl_blocked():
    """run_pipeline() KHÔNG được nuốt CrawlBlockedError vào except chung
    từng job — phải lan lên execute() để ghi status='error'."""

    class BlockedAdapter:
        def fetch_jobs(self, category_key, max_pages):
            raise CrawlBlockedError("bị chặn ở trang đầu")
            yield  # pragma: no cover - biến hàm thành generator

    conn = MagicMock()
    with pytest.raises(CrawlBlockedError):
        run_pipeline(BlockedAdapter(), conn, "data-analyst", 3)
