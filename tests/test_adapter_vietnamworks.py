"""
Test VietnamWorksAdapter — KHÔNG cần database, KHÔNG cần internet.

Dùng tests/fixture_vietnamworks_search.json: fixture TỔNG HỢP dựng theo
cấu trúc response đã xác nhận trong docstring adapters/vietnamworks.py,
KHÔNG phải response thật lưu từ API (xem field "_note" trong file). Khi
có 1 response search thật, thay vào để test bám sát dữ liệu thật hơn.
"""

import copy
import json
import os
import sys
import time

import pytest
from curl_cffi import requests as curl_requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adapters.base import CrawlBlockedError
from adapters.vietnamworks import (
    VietnamWorksAdapter,
    _build_company_url,
    _slugify_company_name,
    _work_type_text_from_id,
)
from config import VNW_SEARCH_URL

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixture_vietnamworks_search.json")
CATEGORY = "data-analyst"


def _load_fixture() -> dict:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, bad_json=False):
        self.status_code = status_code
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
    """Session giả: mỗi lần .post() lấy 1 phần tử kế tiếp trong `script`.
    Phần tử là Exception -> raise; là FakeResponse -> trả về."""

    def __init__(self, script):
        self.headers = {}
        self._script = list(script)
        self.post_calls = 0

    def post(self, url, json=None, timeout=None):  # noqa: A002 - khớp chữ ký curl_cffi
        self.post_calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def sleeps(monkeypatch):
    """Chặn time.sleep thật (test không được chờ), ghi lại các lần chờ."""
    recorded = []
    monkeypatch.setattr(time, "sleep", lambda s: recorded.append(s))
    return recorded


def _adapter_with(script, delay=2.0):
    session = FakeSession(script)
    adapter = VietnamWorksAdapter(session=session)
    adapter._delay_seconds = delay
    return adapter, session


# ----------------------------------------------------------------------
# _post_json: retry
# ----------------------------------------------------------------------
def test_post_json_retries_connection_error_then_succeeds(sleeps):
    """Regression: trước đây lỗi kết nối (vd HTTP/2 stream reset) trả None
    ngay ở lần đầu, không retry như BaseAdapter._fetch_html()."""
    err = curl_requests.exceptions.RequestException("stream reset")
    ok = FakeResponse(200, {"meta": {}, "data": []})
    adapter, session = _adapter_with([err, err, ok], delay=2.0)

    result = adapter._post_json(VNW_SEARCH_URL, {"page": 0})

    assert result == {"meta": {}, "data": []}
    assert session.post_calls == 3
    # backoff theo self._delay_seconds của adapter (2.0), KHÔNG phải hằng cứng:
    # 2*2^1, 2*2^2
    assert sleeps == [4.0, 8.0]


def test_post_json_gives_up_after_max_retries(sleeps):
    err = curl_requests.exceptions.RequestException("timeout")
    adapter, session = _adapter_with([err, err, err])

    assert adapter._post_json(VNW_SEARCH_URL, {}) is None
    assert session.post_calls == 3


def test_post_json_retries_on_429_and_403(sleeps):
    adapter, session = _adapter_with(
        [FakeResponse(429), FakeResponse(403), FakeResponse(200, {"data": []})],
        delay=1.0,
    )
    assert adapter._post_json(VNW_SEARCH_URL, {}) == {"data": []}
    assert session.post_calls == 3
    assert sleeps == [2.0, 4.0]


def test_post_json_does_not_retry_invalid_json(sleeps):
    """Response 200 nhưng không phải JSON: gọi lại cũng ra y hệt -> không retry."""
    adapter, session = _adapter_with([FakeResponse(200, bad_json=True)])
    assert adapter._post_json(VNW_SEARCH_URL, {}) is None
    assert session.post_calls == 1
    assert sleeps == []


def test_post_json_uses_adapter_delay_not_global_constant(sleeps):
    adapter, _ = _adapter_with([FakeResponse(429), FakeResponse(200, {"data": []})], delay=0.5)
    adapter._post_json(VNW_SEARCH_URL, {})
    assert sleeps == [1.0]  # 0.5 * 2^1; nếu dùng REQUEST_DELAY_SECONDS=5.0 sẽ là 10.0


# ----------------------------------------------------------------------
# fetch_jobs
# ----------------------------------------------------------------------
def test_fetch_jobs_parses_fixture_and_skips_invalid_and_duplicates():
    fixture = _load_fixture()
    empty_page = {"meta": {"nbPages": 3}, "data": []}
    pages = {0: fixture, 1: empty_page}

    adapter = VietnamWorksAdapter()
    adapter._post_json = lambda url, body, **kw: pages[body["page"]]

    records = list(adapter.fetch_jobs(CATEGORY, max_pages=3))

    # 5 job trong fixture: 1004 thiếu companyName (bỏ), 1005 trùng URL với 1001 (bỏ)
    assert [r.source_url for r in records] == [
        "https://www.vietnamworks.com/data-analyst-1001-jv",
        "https://www.vietnamworks.com/chuyen-vien-ban-hang-1002-jv",  # URL tương đối được nối BASE_URL
        "https://www.vietnamworks.com/thuc-tap-sinh-data-1003-jv",
    ]


def test_fetch_jobs_maps_fields_of_first_job():
    fixture = _load_fixture()
    adapter = VietnamWorksAdapter()
    adapter._post_json = lambda url, body, **kw: {"meta": {"nbPages": 1}, "data": fixture["data"][:1]}

    (rec,) = list(adapter.fetch_jobs(CATEGORY, max_pages=3))

    assert rec.job_title == "Data Analyst"
    assert rec.company_name == "Ngân Hàng TMCP Công Thương Việt Nam (VietinBank)"
    assert rec.salary_text == "15 - 25 triệu"
    assert rec.province_text == "Hồ Chí Minh"
    assert rec.experience_text == "2 năm"
    assert rec.work_type_text == "Toàn thời gian"
    assert rec.deadline_text == "13/08/2026"
    assert rec.company_url == (
        "https://www.vietnamworks.com/nha-tuyen-dung/"
        "ngan-hang-tmcp-cong-thuong-viet-nam-vietinbank-c34511"
    )


def test_fetch_jobs_edge_case_fields():
    """Job 1002: ẩn lương, yearsOfExperience=0, typeWorkingId lạ, tên địa
    điểm là địa chỉ chi tiết (bug thật 08/2026: từng bị lưu làm 'tỉnh')."""
    fixture = _load_fixture()
    adapter = VietnamWorksAdapter()
    adapter._post_json = lambda url, body, **kw: {"meta": {"nbPages": 1}, "data": [fixture["data"][1]]}

    (rec,) = list(adapter.fetch_jobs(CATEGORY, max_pages=1))

    assert rec.salary_text == ""            # isSalaryVisible=False
    assert rec.experience_text == ""        # 0 năm -> để trống, không đoán
    assert rec.work_type_text == "Khác"     # typeWorkingId ngoài {0,1,3}
    assert rec.province_text == ""          # địa chỉ chi tiết bị loại, không tạo "tỉnh" rác


def test_fetch_jobs_stops_at_meta_nb_pages():
    fixture = _load_fixture()
    page = copy.deepcopy(fixture)
    page["meta"]["nbPages"] = 1
    calls = []

    adapter = VietnamWorksAdapter()
    adapter._post_json = lambda url, body, **kw: (calls.append(body["page"]), page)[1]

    list(adapter.fetch_jobs(CATEGORY, max_pages=5))
    assert calls == [0]


def test_fetch_jobs_first_page_failure_raises_blocked():
    adapter = VietnamWorksAdapter()
    adapter._post_json = lambda url, body, **kw: None
    with pytest.raises(CrawlBlockedError):
        list(adapter.fetch_jobs(CATEGORY, max_pages=3))


def test_fetch_jobs_later_page_failure_is_not_blocked():
    """Trang sau thất bại = coi là dừng bình thường, giữ job đã lấy được."""
    fixture = _load_fixture()
    pages = {0: fixture, 1: None}
    adapter = VietnamWorksAdapter()
    adapter._post_json = lambda url, body, **kw: pages[body["page"]]

    records = list(adapter.fetch_jobs(CATEGORY, max_pages=3))
    assert len(records) == 3


def test_fetch_jobs_unknown_category():
    with pytest.raises(ValueError):
        list(VietnamWorksAdapter().fetch_jobs("khong-ton-tai", max_pages=1))


# ----------------------------------------------------------------------
# fetch_job_full_detail (cache từ search API)
# ----------------------------------------------------------------------
def test_job_full_detail_comes_from_search_cache():
    fixture = _load_fixture()
    adapter = VietnamWorksAdapter()
    adapter._post_json = lambda url, body, **kw: {"meta": {"nbPages": 1}, "data": fixture["data"][:1]}
    (rec,) = list(adapter.fetch_jobs(CATEGORY, max_pages=1))

    detail = adapter.fetch_job_full_detail(rec.source_url)

    assert detail is not None
    assert detail["work_type"] == "Toàn thời gian"
    assert detail["deadline_text"] == "13/08/2026"
    assert detail["job_description"] == "Phân tích dữ liệu\nXây dựng dashboard"
    assert detail["requirements"] == "Tốt nghiệp Đại học"
    # benefits: "<NameVI>: <value>"; thiếu NameVI thì lấy benefitName (tiếng Anh).
    # KHÔNG được là repr dict kiểu "{'benefitId': 1, ...}" (bug cũ).
    assert detail["perks"] == "Lương thưởng: Thưởng tháng 13\nHealth: Bảo hiểm sức khoẻ"
    assert "benefitId" not in detail["perks"]
    # skills hỗn hợp dict{skillName}/str đều được lấy
    assert detail["required_skills"] == ["SQL", "Power BI", "Python"]


def test_job_full_detail_cache_miss_returns_none():
    assert VietnamWorksAdapter().fetch_job_full_detail("https://www.vietnamworks.com/khong-co-1-jv") is None


# ----------------------------------------------------------------------
# Helper thuần
# ----------------------------------------------------------------------
def test_slugify_company_name_matches_confirmed_real_url():
    # Đối chiếu bằng URL thật đã xác nhận 08/2026 (xem docstring adapter)
    assert (
        _slugify_company_name("Ngân Hàng TMCP Công Thương Việt Nam (VietinBank)")
        == "ngan-hang-tmcp-cong-thuong-viet-nam-vietinbank"
    )


def test_build_company_url_requires_id():
    assert _build_company_url("Công ty ABC", None) == ""
    assert _build_company_url("Công ty ABC", 0) == ""
    assert _build_company_url("", 12) == ""
    assert _build_company_url("Công ty ABC", 12).endswith("/nha-tuyen-dung/cong-ty-abc-c12")


@pytest.mark.parametrize(
    "type_id, expected",
    [(1, "Toàn thời gian"), (3, "Thực tập"), (0, ""), (None, ""), (2, "Khác"), (99, "Khác")],
)
def test_work_type_text_from_id(type_id, expected):
    assert _work_type_text_from_id(type_id) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-08-13T23:59:59+07:00", "13/08/2026"),
        ("2026-08-13", "13/08/2026"),
        ("13/08/2026", "13/08/2026"),
        ("", ""),
        (None, ""),
        ("không phải ngày", ""),
    ],
)
def test_format_deadline(value, expected):
    assert VietnamWorksAdapter._format_deadline(value) == expected
