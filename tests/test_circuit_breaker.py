"""
Ngắt mạch khi bị chặn GIỮA CHỪNG (đợt 3, 10/2026) — KHÔNG cần database, KHÔNG
cần internet.

Hợp đồng (adapters/base.py::BaseAdapter._note_fetch_failure):
  - Mỗi lần fetch THẤT BẠI sau khi hết retry (403/429/lỗi kết nối) tăng bộ đếm;
    1 request thành công đặt lại về 0.
  - Đủ CRAWL_BLOCK_CONSECUTIVE_FAILURES lần LIÊN TIẾP -> raise CrawlBlockedError.
  - 404/410 (tin đã gỡ) KHÔNG retry, KHÔNG tính vào bộ đếm.
  - Ngưỡng 0 = tắt ngắt mạch.
Áp dụng cho cả _fetch_html() (GET, dùng chung) lẫn
VietnamWorksAdapter._post_json() (POST JSON riêng).
"""

import os
import sys
import time

import pytest
from curl_cffi import requests as curl_requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scrapjd.adapters.base import BaseAdapter, CrawlBlockedError
from scrapjd.adapters.vietnamworks import VietnamWorksAdapter
from scrapjd.config import VNW_SEARCH_URL

URL = "https://example.test/job/1"


class FakeResponse:
    def __init__(self, status_code=200, text="<html>ok</html>", payload=None, bad_json=False):
        self.status_code = status_code
        self.text = text
        self._payload = payload
        self._bad_json = bad_json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise curl_requests.exceptions.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    """Mỗi lần .get()/.post() lấy 1 phần tử kế tiếp trong `script`.
    Exception -> raise; FakeResponse -> trả về."""

    def __init__(self, script):
        self.headers = {}
        self._script = list(script)
        self.calls = 0

    def _next(self):
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, timeout=None):
        return self._next()

    def post(self, url, json=None, timeout=None):  # noqa: A002 - khớp chữ ký curl_cffi
        return self._next()


class DummyAdapter(BaseAdapter):
    source_name = "Dummy"

    def fetch_jobs(self, category_key, max_pages):
        return iter(())


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(time, "sleep", lambda s: recorded.append(s))
    return recorded


def _adapter(script, threshold=3):
    session = FakeSession(script)
    adapter = DummyAdapter(session=session, delay_seconds=0.0)
    adapter._block_threshold = threshold
    return adapter, session


def _exhausted(n_fetches, status=403):
    """Kịch bản n lần fetch, MỖI lần hết 3 retry đều bị chặn."""
    return [FakeResponse(status) for _ in range(3 * n_fetches)]


# ----------------------------------------------------------------------
# GET: _fetch_html
# ----------------------------------------------------------------------
def test_one_exhausted_fetch_returns_none_without_raising(sleeps):
    adapter, session = _adapter(_exhausted(1))
    assert adapter._fetch_html(URL) is None
    assert session.calls == 3
    assert adapter._consecutive_failures == 1


def test_raises_after_threshold_consecutive_exhausted_fetches(sleeps):
    adapter, session = _adapter(_exhausted(3))

    assert adapter._fetch_html(URL) is None
    assert adapter._fetch_html(URL) is None
    with pytest.raises(CrawlBlockedError) as exc_info:
        adapter._fetch_html(URL)

    # Lần thứ 3 vẫn đã retry đủ 3 lượt trước khi kết luận.
    assert session.calls == 9
    message = str(exc_info.value)
    assert "3 lần" in message
    assert URL in message
    assert "Dummy" in message
    assert "HTTP 403" in message


def test_success_resets_the_streak(sleeps):
    script = (
        _exhausted(2)
        + [FakeResponse(200, text="<html>fine</html>")]
        + _exhausted(2)
    )
    adapter, _ = _adapter(script)

    assert adapter._fetch_html(URL) is None
    assert adapter._fetch_html(URL) is None
    assert adapter._fetch_html(URL) == "<html>fine</html>"
    assert adapter._consecutive_failures == 0
    # 2 lỗi nữa sau khi đã reset -> vẫn dưới ngưỡng 3, KHÔNG raise
    assert adapter._fetch_html(URL) is None
    assert adapter._fetch_html(URL) is None


def test_connection_errors_count_too(sleeps):
    err = curl_requests.exceptions.RequestException("HTTP/2 stream reset")
    adapter, session = _adapter([err] * 9)

    assert adapter._fetch_html(URL) is None
    assert adapter._fetch_html(URL) is None
    with pytest.raises(CrawlBlockedError) as exc_info:
        adapter._fetch_html(URL)
    assert "stream reset" in str(exc_info.value)
    assert session.calls == 9


def test_404_and_410_are_not_retried_and_never_trip_the_breaker(sleeps):
    """Tin đã gỡ: gọi lại cũng ra 404. Trước đây bị coi như lỗi kết nối và
    retry 3 lần (TopCV ~168s/tin) — và sẽ làm ngắt mạch báo nhầm bị chặn."""
    script = [FakeResponse(404), FakeResponse(410)] * 5
    adapter, session = _adapter(script)

    for _ in range(10):
        assert adapter._fetch_html(URL) is None

    assert session.calls == 10   # đúng 1 request mỗi lần, không retry
    assert sleeps == []          # không backoff
    assert adapter._consecutive_failures == 0


def test_404_resets_the_streak_because_site_is_clearly_answering(sleeps):
    script = _exhausted(2) + [FakeResponse(404)] + _exhausted(2)
    adapter, _ = _adapter(script)

    assert adapter._fetch_html(URL) is None   # lỗi 1
    assert adapter._fetch_html(URL) is None   # lỗi 2
    assert adapter._fetch_html(URL) is None   # 404 -> reset
    assert adapter._fetch_html(URL) is None   # lỗi 1
    assert adapter._fetch_html(URL) is None   # lỗi 2 — vẫn chưa raise


def test_threshold_zero_disables_the_breaker(sleeps):
    adapter, _ = _adapter(_exhausted(10), threshold=0)
    for _ in range(10):
        assert adapter._fetch_html(URL) is None


def test_retry_then_success_still_works_and_is_not_a_failure(sleeps):
    adapter, session = _adapter([FakeResponse(429), FakeResponse(200, text="<p>ok</p>")])
    assert adapter._fetch_html(URL) == "<p>ok</p>"
    assert session.calls == 2
    assert adapter._consecutive_failures == 0


def test_default_threshold_comes_from_config():
    from scrapjd.config import CRAWL_BLOCK_CONSECUTIVE_FAILURES

    adapter = DummyAdapter(session=FakeSession([]), delay_seconds=0.0)
    assert adapter._block_threshold == CRAWL_BLOCK_CONSECUTIVE_FAILURES
    assert CRAWL_BLOCK_CONSECUTIVE_FAILURES == 3


# ----------------------------------------------------------------------
# POST: VietnamWorksAdapter._post_json (cùng hợp đồng)
# ----------------------------------------------------------------------
def _vnw(script, threshold=3):
    session = FakeSession(script)
    adapter = VietnamWorksAdapter(session=session)
    adapter._delay_seconds = 0.0
    adapter._block_threshold = threshold
    return adapter, session


def test_post_json_raises_after_threshold(sleeps):
    adapter, session = _vnw(_exhausted(3, status=429))

    assert adapter._post_json(VNW_SEARCH_URL, {}) is None
    assert adapter._post_json(VNW_SEARCH_URL, {}) is None
    with pytest.raises(CrawlBlockedError):
        adapter._post_json(VNW_SEARCH_URL, {})
    assert session.calls == 9


def test_post_json_invalid_json_counts_as_failure(sleeps):
    """200 nhưng không phải JSON thường là trang challenge của WAF."""
    adapter, session = _vnw([FakeResponse(200, bad_json=True)] * 3)

    assert adapter._post_json(VNW_SEARCH_URL, {}) is None
    assert adapter._post_json(VNW_SEARCH_URL, {}) is None
    with pytest.raises(CrawlBlockedError) as exc_info:
        adapter._post_json(VNW_SEARCH_URL, {})
    assert "JSON" in str(exc_info.value)
    assert session.calls == 3   # lỗi JSON không retry


def test_post_json_success_resets_the_streak(sleeps):
    script = _exhausted(2, 429) + [FakeResponse(200, payload={"data": []})] + _exhausted(2, 429)
    adapter, _ = _vnw(script)

    assert adapter._post_json(VNW_SEARCH_URL, {}) is None
    assert adapter._post_json(VNW_SEARCH_URL, {}) is None
    assert adapter._post_json(VNW_SEARCH_URL, {}) == {"data": []}
    assert adapter._post_json(VNW_SEARCH_URL, {}) is None
    assert adapter._post_json(VNW_SEARCH_URL, {}) is None
