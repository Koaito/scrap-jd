"""
Test parser + normalize KHÔNG cần database, KHÔNG cần internet.
Chạy: pytest tests/test_parse_and_normalize.py

10/2026: đổi từ kiểu "return True/False + print" sang assert thật. Bản cũ
trả bool nên pytest chỉ cảnh báo PytestReturnNotNoneWarning chứ KHÔNG
fail khi parser hỏng -> CI luôn xanh dù kết quả sai. Các ca kiểm tra giữ
nguyên nội dung, chỉ đổi cơ chế báo lỗi.
"""

import sys
import os
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adapters.topcv import TopCVAdapter
from adapters.careerviet import CareerVietAdapter
import normalize
from province_alias import resolve_province_alias

HERE = os.path.dirname(__file__)
FIXTURE_PATH = os.path.join(HERE, "fixture_topcv_listing.html")
JOB_DETAIL_FIXTURE_PATH = os.path.join(HERE, "fixture_topcv_job_detail.html")
CAREERVIET_COMPANY_FIXTURE_PATH = os.path.join(HERE, "fixture_careerviet_company_profile.html")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_listing_and_normalize():
    adapter = TopCVAdapter()
    records = list(adapter._parse_listing_page(_read(FIXTURE_PATH), matching_industry="Data Analysis"))

    assert len(records) == 5, f"Parse được {len(records)} job từ fixture (kỳ vọng: 5)"

    for i, rec in enumerate(records, 1):
        assert rec.job_title, f"Job {i}: thiếu job_title"
        assert rec.company_name, f"Job {i}: thiếu company_name"
        assert rec.source_url, f"Job {i}: thiếu source_url"
        # normalize phải chạy được trên dữ liệu thật của fixture, không crash
        normalize.normalize_salary(rec.salary_text)
        normalize.infer_level(rec.experience_text, rec.job_title)


def test_job_full_detail():
    """fetch_job_full_detail() (work_type, deadline_text, job_description,
    requirements, perks, required_skills) + normalize_deadline() +
    normalize_work_type()."""
    html = _read(JOB_DETAIL_FIXTURE_PATH)
    adapter = TopCVAdapter()
    adapter._fetch_html = lambda url: html  # tránh gọi internet thật khi test

    detail = adapter.fetch_job_full_detail("https://www.topcv.vn/viec-lam/fake/123.html")
    assert detail is not None, "fetch_job_full_detail() trả None dù fixture hợp lệ"

    assert detail.get("work_type") == "Toàn thời gian", "work_type parse SAI"
    assert detail.get("deadline_text") == "23/08/2026", "deadline_text parse SAI"
    assert "Dashboard" in detail.get("job_description", ""), "job_description rỗng/SAI"
    assert "Đại học" in detail.get("requirements", ""), "requirements rỗng/SAI"
    assert "Lương cứng" in detail.get("perks", ""), "perks rỗng/SAI"
    assert detail.get("required_skills") == [
        "Python", "SQL", "Excel", "Tư duy logic", "Power BI",
        "Business Thinking", "Data visualization",
    ], "required_skills parse SAI"

    assert normalize.normalize_deadline(detail.get("deadline_text", "")) == date(2026, 8, 23)
    assert normalize.normalize_work_type(detail.get("work_type", "")) == "FULL_TIME"

    # Case rỗng / hỏng -> phải trả None, không crash
    assert normalize.normalize_deadline("") is None
    assert normalize.normalize_deadline("không có ngày") is None


@pytest.mark.parametrize(
    "input_text, expected",
    [
        ("Toàn thời gian", "FULL_TIME"),
        ("Bán thời gian", "PART_TIME"),
        ("Thực tập", "INTERNSHIP"),
        ("Khác", "OTHER"),
        ("  Toàn thời gian  ", "FULL_TIME"),  # dư khoảng trắng
        ("TOÀN THỜI GIAN", "FULL_TIME"),       # khác hoa/thường
        ("", None),                             # rỗng
        ("Freelance", None),                    # giá trị lạ, không có trong enum
        (None, None),                           # None input
    ],
)
def test_normalize_work_type(input_text, expected):
    """map text tiếng Việt sang work_type_enum, trả None cho text lạ/rỗng
    thay vì insert thẳng text thô (tránh rác dữ liệu)."""
    assert normalize.normalize_work_type(input_text) == expected


@pytest.mark.parametrize(
    "input_text, exp_cur, exp_min, exp_max, exp_type, exp_period",
    [
        # Case bug thật đã sửa 08/2026: lương NĂM bị hiểu nhầm lương/tháng
        ("200tr-500tr ₫/năm", "VNĐ", 200_000_000, 500_000_000, "RANGE", "YEAR"),
        ("15 triệu/năm", "VNĐ", 15_000_000, 15_000_000, "EXACT", "YEAR"),
        ("$ 3,000-5,000 per year", "USD", 3_000, 5_000, "RANGE", "YEAR"),
        ("Annual 500tr", "VNĐ", 500_000_000, 500_000_000, "EXACT", "YEAR"),
        # "/year" (KHÔNG có "per") KHÔNG được coi là tín hiệu năm — chỉ khớp
        # "/năm", "annual", "per year", "yearly". Test để tránh hồi quy nếu
        # sau này mở rộng regex mà không cập nhật lại case này.
        ("$ 3,000-5,000 /year", "USD", 3_000, 5_000, "RANGE", "MONTH"),
        # Mặc định MONTH khi không có tín hiệu năm (khớp hành vi cũ)
        ("15tr-30tr ₫/tháng", "VNĐ", 15_000_000, 30_000_000, "RANGE", "MONTH"),
        ("12,000-30,000 ₫/tháng", "VNĐ", 12_000_000, 30_000_000, "RANGE", "MONTH"),
        ("$ 3,000-5,000 /tháng", "USD", 3_000, 5_000, "RANGE", "MONTH"),
        ("$ 13tr-15tr /tháng", "VNĐ", 13_000_000, 15_000_000, "RANGE", "MONTH"),
        ("Tới 3,000 USD", "USD", None, 3_000, "UPTO", "MONTH"),
        ("Từ 12 triệu", "VNĐ", 12_000_000, None, "STARTING_FROM", "MONTH"),
        ("Thoả thuận", "VNĐ", None, None, "NEGOTIABLE", "MONTH"),
        ("", "VNĐ", None, None, "NEGOTIABLE", "MONTH"),
        # "năm" đứng LẺ (không đi sau "/") KHÔNG được tính là tín hiệu lương
        # năm. Câu không có số nào khác ngoài mức lương để cô lập đúng hành
        # vi salary_period.
        ("15 triệu (đã làm nhiều năm trong ngành)", "VNĐ", 15_000_000, 15_000_000, "EXACT", "MONTH"),
    ],
)
def test_normalize_salary(input_text, exp_cur, exp_min, exp_max, exp_type, exp_period):
    """normalize_salary() — đặc biệt salary_period (bug thật 08/2026: text
    "200tr-500tr ₫/năm" bị hiểu nhầm thành lương/tháng)."""
    r = normalize.normalize_salary(input_text)
    assert (r.currency, r.salary_min, r.salary_max, r.salary_type, r.salary_period) == (
        exp_cur, exp_min, exp_max, exp_type, exp_period
    ), f"normalize_salary({input_text!r}) = {r}"


@pytest.mark.parametrize(
    "input_text, expected",
    [
        ("Bắc Á Bank - Https://tuyendung.baca-Bank.vn/", "Bắc Á Bank"),
        ("VPBank - Https://tuyendung.vpbank.com.vn/", "VPBank"),
        ("Some Co | https://example.com", "Some Co"),
        ("Some Co https://example.com", "Some Co"),
        ("  FPT   Software  ", "FPT Software"),      # dư khoảng trắng, không có URL
        ("Procter & Gamble", "Procter & Gamble"),    # không có URL, giữ nguyên
        ("", ""),
        (None, ""),
    ],
)
def test_clean_company_name(input_text, expected):
    """clean_company_name() — cắt URL dính đuôi tên công ty (bug thật
    08/2026), đồng thời không cắt nhầm tên công ty hợp lệ."""
    assert normalize.clean_company_name(input_text) == expected


@pytest.mark.parametrize(
    "input_text, expected",
    [
        ("Bình Dương", "Hồ Chí Minh"),      # tỉnh cũ, đã sáp nhập
        ("Hòa Bình", "Phú Thọ"),             # tỉnh cũ, đã sáp nhập
        ("Bắc Giang", "Bắc Ninh"),           # tỉnh cũ, đã sáp nhập
        ("TP. Hồ Chí Minh", "Hồ Chí Minh"),  # tên mới, có tiền tố "TP."
        ("Hồ Chí Minh", "Hồ Chí Minh"),      # tên mới, tự map về chính nó
        ("Hà Nội", "Hà Nội"),                # tỉnh giữ nguyên (không sáp nhập)
        ("Khác", "Khác"),
        ("Remote", "Remote"),
        ("", ""),
        (None, ""),
        ("Xyz Không Tồn Tại", ""),           # tên lạ -> không nhận diện được
    ],
)
def test_resolve_province_alias(input_text, expected):
    """quy đổi tên tỉnh CŨ (trước sáp nhập 07/2025) sang tên MỚI."""
    assert resolve_province_alias(input_text) == expected


def test_careerviet_company_profile():
    """CareerVietAdapter.fetch_company_profile() bằng mẫu HTML thật
    (fixture_careerviet_company_profile.html, trang công ty FPT Long Châu,
    fetch 08/2026 — xem docstring fetch_company_profile())."""
    html = _read(CAREERVIET_COMPANY_FIXTURE_PATH)

    adapter = CareerVietAdapter.__new__(CareerVietAdapter)  # bỏ qua __init__, không cần session
    adapter._fetch_html = lambda url: html  # tránh gọi internet thật khi test

    result = adapter.fetch_company_profile("https://careerviet.vn/vi/nha-tuyen-dung/fake.html")

    assert result.get("address") == "379-381 Hai Bà Trưng, Phường Võ Thị Sáu, Quận 3, Thành phố Hồ Chí Minh"
    assert result.get("company_size") == "10.000-19.999"
    assert result.get("real_website") == "https://tuyendung.frt.vn/"
    assert "Long Châu" in result.get("description", ""), "description rỗng/SAI"
    # Mẫu thật KHÔNG có nhãn "Lĩnh vực hoạt động"/"Mã số thuế" -> phải rỗng
    # "" (an toàn), KHÔNG được bịa dữ liệu.
    assert result.get("industry") == "", "industry phải rỗng khi mẫu không có nhãn này"
    assert result.get("tax_id") == "", "tax_id phải rỗng khi mẫu không có nhãn này"

    # company_url rỗng -> trả dict rỗng an toàn, không crash / không fetch
    empty = adapter.fetch_company_profile("")
    assert not any(empty.values()), "fetch_company_profile('') phải trả toàn bộ field rỗng"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
