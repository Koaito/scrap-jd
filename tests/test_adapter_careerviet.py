"""
Test CareerVietAdapter (listing + trang JD + fetch_jobs) — KHÔNG cần
database, KHÔNG cần internet.

tests/fixture_careerviet_listing.html và fixture_careerviet_job_detail.html
là fixture TỔNG HỢP dựng theo cấu trúc đã mô tả trong docstring
scrapjd/adapters/careerviet.py, KHÔNG phải HTML thật lưu từ site. Riêng
fixture_careerviet_company_profile.html là HTML thật (test ở
test_parse_and_normalize.py). Khi có view-source thật của trang
listing/JD, thay vào để test bám sát site thật hơn.
"""

import os
import re
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scrapjd import normalize
from scrapjd.adapters.base import CrawlBlockedError
from scrapjd.adapters.careerviet import (
    BASE_URL,
    CareerVietAdapter,
    _extract_employment_type_text,
    _is_non_company_website,
    _iso_to_ddmmyyyy,
    _months_to_year_text,
)

HERE = os.path.dirname(__file__)
CATEGORY = "data-analyst"
SEARCH_URL = f"{BASE_URL}/viec-lam/{CATEGORY}-k-vi.html"
PAGE2_URL = f"{BASE_URL}/viec-lam/{CATEGORY}-k-trang-2-vi.html"
JOB_URLS = [
    f"{BASE_URL}/vi/tim-viec-lam/data-analyst.35C82AC4.html",
    f"{BASE_URL}/vi/tim-viec-lam/business-analyst.35C82AC5.html",
    f"{BASE_URL}/vi/tim-viec-lam/data-engineer.35C82AC6.html",
]


def _read(name: str) -> str:
    with open(os.path.join(HERE, name), "r", encoding="utf-8") as f:
        return f.read()


LISTING_HTML = _read("fixture_careerviet_listing.html")
DETAIL_HTML = _read("fixture_careerviet_job_detail.html")


def _stub_fetch(adapter, mapping):
    """Gắn _fetch_html giả: trả mapping[url] (mặc định None = fetch thất
    bại). Trả về list ghi lại các URL đã được yêu cầu."""
    requested = []

    def fake(url, max_retries=3):
        requested.append(url)
        return mapping.get(url)

    adapter._fetch_html = fake
    return requested


# ----------------------------------------------------------------------
# Listing
# ----------------------------------------------------------------------
def test_extract_job_detail_urls_dedupes_keeps_order_and_ignores_non_job_links():
    urls = CareerVietAdapter._extract_job_detail_urls(LISTING_HTML)
    # link trùng bị bỏ; "?utm..#apply" bị cắt; link công ty/phân trang không phải URL job
    assert urls == JOB_URLS


@pytest.mark.parametrize(
    "page, expected",
    [
        (1, SEARCH_URL),
        (2, PAGE2_URL),
        (3, f"{BASE_URL}/viec-lam/{CATEGORY}-k-trang-3-vi.html"),
    ],
)
def test_build_page_url(page, expected):
    assert CareerVietAdapter._build_page_url(SEARCH_URL, page) == expected


# ----------------------------------------------------------------------
# Trang JD
# ----------------------------------------------------------------------
def test_parse_detail_page_full_fixture():
    adapter = CareerVietAdapter()
    parsed = adapter._parse_detail_page(DETAIL_HTML, JOB_URLS[0])

    assert parsed is not None
    assert parsed["job_title"] == "Data Engineer"
    assert parsed["company_name"] == "CÔNG TY CP DƯỢC PHẨM FPT LONG CHÂU"
    assert parsed["company_url"].endswith("/vi/nha-tuyen-dung/fpt-long-chau.123.html")
    # khối info xuất hiện đầu tiên được ưu tiên, khối ẩn (display:none) bị bỏ qua
    assert parsed["salary_text"] == "Cạnh tranh"
    assert parsed["experience_text"] == "3 năm"
    assert parsed["deadline_text"] == "05/09/2026"
    assert parsed["posted_text"] == "01/08/2026"
    assert parsed["province_text"] == "Hồ Chí Minh"
    # employmentType có dấu ngoặc kép thừa ["\"FULL_TIME\""] vẫn map đúng
    assert parsed["work_type"] == "Toàn thời gian"
    assert parsed["job_description"] == "Xây dựng pipeline dữ liệu\nTối ưu truy vấn SQL"
    assert parsed["requirements"] == "Tốt nghiệp Đại học\nThành thạo Python"
    assert parsed["perks"] == "Thưởng tháng 13\nBảo hiểm sức khoẻ"
    assert parsed["required_skills"] == ["Python", "SQL", "Airflow", "Python"]


def test_parse_detail_page_output_feeds_normalize():
    parsed = CareerVietAdapter()._parse_detail_page(DETAIL_HTML, JOB_URLS[0])
    assert normalize.normalize_deadline(parsed["deadline_text"]) == date(2026, 9, 5)
    assert normalize.normalize_work_type(parsed["work_type"]) == "FULL_TIME"
    normalize.infer_level(parsed["experience_text"], parsed["job_title"])  # không crash


def test_parse_detail_page_jsonld_only_uses_fallbacks():
    """Không có section.job-detail-content: lương/kinh nghiệm/hạn nộp lấy
    từ JSON-LD, mô tả lấy từ 'description' gộp."""
    html = re.sub(r"<section.*?</section>", "", DETAIL_HTML, flags=re.S)
    parsed = CareerVietAdapter()._parse_detail_page(html, JOB_URLS[0])

    assert parsed is not None
    assert parsed["salary_text"] == "20000000 - 30000000"
    assert parsed["experience_text"] == "3 năm"       # monthsOfExperience=36
    assert parsed["deadline_text"] == "05/09/2026"    # validThrough ISO -> dd/mm/yyyy
    assert parsed["posted_text"] == "2026-08-01"
    assert parsed["job_description"] == "Mô tả gộp"
    assert parsed["requirements"] == ""
    assert parsed["province_text"] == "Hồ Chí Minh"   # Place đầu tiên của jobLocation dạng list


def test_parse_detail_page_joblocation_list_does_not_crash():
    """Regression bug thật 08/2026: jobLocation là LIST (job nhiều địa điểm)
    từng gây "'list' object has no attribute 'get'" làm crash cả lượt crawl."""
    html = re.sub(r'<div class="map">.*?</div>', "", DETAIL_HTML, flags=re.S)
    parsed = CareerVietAdapter()._parse_detail_page(html, JOB_URLS[0])
    assert parsed["province_text"] == "Hồ Chí Minh"


def test_parse_detail_page_without_jsonld_nor_section_returns_none():
    assert CareerVietAdapter()._parse_detail_page("<html><body>Blocked</body></html>", JOB_URLS[0]) is None


# ----------------------------------------------------------------------
# fetch_jobs
# ----------------------------------------------------------------------
def _mapping_with_details():
    mapping = {SEARCH_URL: LISTING_HTML}
    for u in JOB_URLS:
        mapping[u] = DETAIL_HTML
    return mapping


def test_fetch_jobs_yields_records_and_fills_detail_cache():
    adapter = CareerVietAdapter()
    mapping = _mapping_with_details()
    mapping[PAGE2_URL] = "<html><body>hết job</body></html>"  # 0 URL job -> dừng
    requested = _stub_fetch(adapter, mapping)

    records = list(adapter.fetch_jobs(CATEGORY, max_pages=3))

    assert [r.source_url for r in records] == JOB_URLS
    assert all(r.source_name == "CareerViet" for r in records)
    assert records[0].job_title == "Data Engineer"
    assert records[0].matching_industry == "Data Analysis"
    # đã đi theo pattern phân trang thật
    assert PAGE2_URL in requested

    # fetch_job_full_detail dùng cache, KHÔNG fetch thêm
    before = len(requested)
    detail = adapter.fetch_job_full_detail(JOB_URLS[0])
    assert len(requested) == before
    assert detail["work_type"] == "Toàn thời gian"
    assert detail["deadline_text"] == "05/09/2026"
    assert "Xây dựng pipeline" in detail["job_description"]


def test_fetch_jobs_first_page_failure_raises_blocked():
    """Regression: trước đây CareerViet chỉ break -> lượt bị chặn hoàn toàn
    hiện 'done, 0 job' thay vì lỗi."""
    adapter = CareerVietAdapter()
    _stub_fetch(adapter, {})  # mọi fetch đều None
    with pytest.raises(CrawlBlockedError):
        list(adapter.fetch_jobs(CATEGORY, max_pages=3))


def test_fetch_jobs_later_page_failure_is_not_blocked():
    """Trang 2 fetch thất bại: dừng bình thường, giữ job trang 1, không raise."""
    adapter = CareerVietAdapter()
    _stub_fetch(adapter, _mapping_with_details())  # PAGE2_URL không có -> None

    records = list(adapter.fetch_jobs(CATEGORY, max_pages=3))
    assert [r.source_url for r in records] == JOB_URLS


def test_fetch_jobs_skips_job_whose_detail_fetch_fails():
    adapter = CareerVietAdapter()
    mapping = _mapping_with_details()
    del mapping[JOB_URLS[1]]  # detail job thứ 2 fetch thất bại
    _stub_fetch(adapter, mapping)

    records = list(adapter.fetch_jobs(CATEGORY, max_pages=1))
    assert [r.source_url for r in records] == [JOB_URLS[0], JOB_URLS[2]]


def test_fetch_jobs_skips_known_urls_without_fetching_their_detail():
    """Đợt 2: job đã có trong DB (hook do pipeline đặt) KHÔNG bị fetch lại
    trang chi tiết — đây là phần tiết kiệm request của lượt crawl lặp lại."""
    adapter = CareerVietAdapter()
    requested = _stub_fetch(adapter, _mapping_with_details())
    known = {JOB_URLS[0], JOB_URLS[2]}
    adapter.set_known_url_checker(lambda url: url in known)

    records = list(adapter.fetch_jobs(CATEGORY, max_pages=1))

    assert [r.source_url for r in records] == [JOB_URLS[1]]
    assert not (set(requested) & known)          # URL đã biết không bị fetch chi tiết
    assert JOB_URLS[1] in requested
    assert adapter.skipped_known_count == 2


def test_fetch_jobs_known_urls_do_not_stop_pagination():
    """Trang toàn job đã biết vẫn phải đi tiếp trang sau (dừng chỉ khi trang
    không có URL job nào mới theo seen_urls, không phải theo 'đã biết')."""
    adapter = CareerVietAdapter()
    page2_job = f"{BASE_URL}/vi/tim-viec-lam/new-job.99999999.html"
    mapping = _mapping_with_details()
    mapping[PAGE2_URL] = f'<a href="{page2_job}">job</a>'
    mapping[page2_job] = DETAIL_HTML
    requested = _stub_fetch(adapter, mapping)
    adapter.set_known_url_checker(lambda url: url in JOB_URLS)  # cả trang 1 đã biết

    records = list(adapter.fetch_jobs(CATEGORY, max_pages=2))

    assert [r.source_url for r in records] == [page2_job]
    assert PAGE2_URL in requested
    assert adapter.skipped_known_count == len(JOB_URLS)


def test_fetch_jobs_checker_error_falls_back_to_fetching():
    adapter = CareerVietAdapter()
    _stub_fetch(adapter, _mapping_with_details())

    def broken(url):
        raise RuntimeError("DB lỗi")

    adapter.set_known_url_checker(broken)

    records = list(adapter.fetch_jobs(CATEGORY, max_pages=1))
    assert [r.source_url for r in records] == JOB_URLS  # không mất job nào
    assert adapter.skipped_known_count == 0


def test_set_known_url_checker_resets_counter_and_none_disables():
    adapter = CareerVietAdapter()
    _stub_fetch(adapter, _mapping_with_details())
    adapter.set_known_url_checker(lambda url: True)
    assert list(adapter.fetch_jobs(CATEGORY, max_pages=1)) == []
    assert adapter.skipped_known_count == len(JOB_URLS)

    adapter.set_known_url_checker(None)
    assert adapter.skipped_known_count == 0
    assert len(list(adapter.fetch_jobs(CATEGORY, max_pages=1))) == len(JOB_URLS)


def test_fetch_jobs_unknown_category():
    with pytest.raises(ValueError):
        list(CareerVietAdapter().fetch_jobs("khong-ton-tai", max_pages=1))


# ----------------------------------------------------------------------
# fetch_job_full_detail: cache miss
# ----------------------------------------------------------------------
def test_job_full_detail_cache_miss_fetches_live_and_caches():
    adapter = CareerVietAdapter()
    url = JOB_URLS[0]
    requested = _stub_fetch(adapter, {url: DETAIL_HTML})

    first = adapter.fetch_job_full_detail(url)
    second = adapter.fetch_job_full_detail(url)

    assert first is not None and first == second
    assert requested == [url]  # lần 2 dùng cache


def test_job_full_detail_fetch_failure_returns_none():
    adapter = CareerVietAdapter()
    _stub_fetch(adapter, {})
    assert adapter.fetch_job_full_detail(JOB_URLS[0]) is None


# ----------------------------------------------------------------------
# Helper thuần
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-09-05T23:59:00Z", "05/09/2026"),
        ("2026-09-05", "05/09/2026"),
        ("", ""),
        (None, ""),
        ("không phải ngày", ""),
    ],
)
def test_iso_to_ddmmyyyy(value, expected):
    assert _iso_to_ddmmyyyy(value) == expected


@pytest.mark.parametrize(
    "months, expected",
    [(36, "3 năm"), (6, "Dưới 1 năm"), (0, ""), (None, ""), ("abc", "")],
)
def test_months_to_year_text(months, expected):
    assert _months_to_year_text(months) == expected


@pytest.mark.parametrize(
    "jsonld, expected",
    [
        ({"employmentType": ['"FULL_TIME"']}, "Toàn thời gian"),
        ({"employmentType": "PART_TIME"}, "Bán thời gian"),
        ({"employmentType": "INTERN"}, "Thực tập"),
        ({"employmentType": "ALIEN"}, ""),
        ({}, ""),
    ],
)
def test_employment_type_text(jsonld, expected):
    assert _extract_employment_type_text(jsonld) == expected


@pytest.mark.parametrize(
    "url, expected",
    [
        ("https://www.linkedin.com/company/x", True),
        ("https://facebook.com/x", True),
        ("https://tuyendung.frt.vn/", False),
    ],
)
def test_is_non_company_website(url, expected):
    assert _is_non_company_website(url) is expected
