"""
Test parser + normalize KHÔNG cần database, KHÔNG cần internet.
Chạy: python tests/test_parse_and_normalize.py
"""

import sys
import os
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adapters.topcv import TopCVAdapter
from adapters.careerviet import CareerVietAdapter
import normalize
from province_alias import resolve_province_alias

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixture_topcv_listing.html")
JOB_DETAIL_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixture_topcv_job_detail.html")
CAREERVIET_COMPANY_FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__), "fixture_careerviet_company_profile.html"
)


def test_listing_and_normalize() -> bool:
    with open(FIXTURE_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    adapter = TopCVAdapter()
    records = list(adapter._parse_listing_page(html, matching_industry="Data Analysis"))

    print(f"Parse được {len(records)} job từ fixture (kỳ vọng: 5)\n")

    ok = True
    for i, rec in enumerate(records, 1):
        salary = normalize.normalize_salary(rec.salary_text)
        level = normalize.infer_level(rec.experience_text, rec.job_title)
        print(f"--- Job {i} ---")
        print(f"  Title      : {rec.job_title}")
        print(f"  Company    : {rec.company_name}")
        print(f"  Source URL : {rec.source_url}")
        print(f"  Salary raw : '{rec.salary_text}' -> {salary}")
        print(f"  Province   : {rec.province_text}")
        print(f"  Experience : '{rec.experience_text}' -> level = {level}")
        print()

        if not rec.job_title or not rec.company_name or not rec.source_url:
            ok = False
            print("  !! THIẾU DỮ LIỆU BẮT BUỘC !!")

    if len(records) != 5:
        ok = False

    return ok


def test_job_full_detail() -> bool:
    """Test fetch_job_full_detail() (work_type, deadline_text,
    job_description, requirements, perks, required_skills) +
    normalize_deadline() + normalize_work_type()."""
    print("--- Test fetch_job_full_detail() ---")
    ok = True

    with open(JOB_DETAIL_FIXTURE_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    adapter = TopCVAdapter()
    adapter._fetch_html = lambda url: html  # tránh gọi internet thật khi test

    detail = adapter.fetch_job_full_detail("https://www.topcv.vn/viec-lam/fake/123.html")
    print(f"  work_type       : {detail.get('work_type')!r}")
    print(f"  deadline_text   : {detail.get('deadline_text')!r}")
    print(f"  job_description : {detail.get('job_description')[:60]!r}...")
    print(f"  requirements    : {detail.get('requirements')[:60]!r}...")
    print(f"  perks           : {detail.get('perks')[:60]!r}...")
    print(f"  required_skills : {detail.get('required_skills')!r}")

    checks = [
        (detail.get("work_type") == "Toàn thời gian", "work_type parse SAI"),
        (detail.get("deadline_text") == "23/08/2026", "deadline_text parse SAI"),
        ("Dashboard" in detail.get("job_description", ""), "job_description rỗng/SAI"),
        ("Đại học" in detail.get("requirements", ""), "requirements rỗng/SAI"),
        ("Lương cứng" in detail.get("perks", ""), "perks rỗng/SAI"),
        (detail.get("required_skills") == [
            "Python", "SQL", "Excel", "Tư duy logic", "Power BI",
            "Business Thinking", "Data visualization",
        ], "required_skills parse SAI"),
    ]
    for passed, msg in checks:
        if not passed:
            ok = False
            print(f"  !! {msg} !!")

    parsed_deadline = normalize.normalize_deadline(detail.get("deadline_text", ""))
    print(f"  normalize_deadline -> {parsed_deadline}")
    if parsed_deadline != date(2026, 8, 23):
        ok = False
        print("  !! normalize_deadline SAI !!")

    parsed_work_type = normalize.normalize_work_type(detail.get("work_type", ""))
    print(f"  normalize_work_type -> {parsed_work_type}")
    if parsed_work_type != "FULL_TIME":
        ok = False
        print("  !! normalize_work_type SAI !!")

    # Case rỗng / hỏng -> phải trả None, không crash
    if normalize.normalize_deadline("") is not None:
        ok = False
        print("  !! normalize_deadline('') phải trả None !!")
    if normalize.normalize_deadline("không có ngày") is not None:
        ok = False
        print("  !! normalize_deadline(text lạ) phải trả None !!")

    print()
    return ok


def test_normalize_work_type() -> bool:
    """Test normalize_work_type() — map text tiếng Việt sang work_type_enum,
    trả None cho text lạ/rỗng thay vì insert thẳng text thô (tránh rác dữ
    liệu kiểu nhiều biến thể của cùng 1 giá trị)."""
    print("--- Test normalize_work_type() ---")
    ok = True

    cases = [
        ("Toàn thời gian", "FULL_TIME"),
        ("Bán thời gian", "PART_TIME"),
        ("Thực tập", "INTERNSHIP"),
        ("Khác", "OTHER"),
        ("  Toàn thời gian  ", "FULL_TIME"),  # dư khoảng trắng
        ("TOÀN THỜI GIAN", "FULL_TIME"),       # khác hoa/thường
        ("", None),                             # rỗng
        ("Freelance", None),                    # giá trị lạ, không có trong enum
        (None, None),                            # None input
    ]

    for input_text, expected in cases:
        result = normalize.normalize_work_type(input_text)
        print(f"  normalize_work_type({input_text!r}) = {result!r}")
        if result != expected:
            ok = False
            print(f"  !! SAI, kỳ vọng {expected!r} !!")

    print()
    return ok


def test_normalize_salary() -> bool:
    """Test normalize_salary() — đặc biệt case chu kỳ trả lương
    (salary_period), bug thật đã sửa 08/2026: text "200tr-500tr ₫/năm" bị
    hiểu nhầm thành lương/tháng (xem docstring _YEARLY_SALARY_MARKER
    trong normalize.py). Test cả các case cũ (đơn vị VNĐ/USD, RANGE/EXACT/
    UPTO/STARTING_FROM/NEGOTIABLE) để đảm bảo không hồi quy khi thêm
    salary_period."""
    print("--- Test normalize_salary() ---")
    ok = True

    # (input_text, expected_currency, expected_min, expected_max, expected_type, expected_period)
    cases = [
        # Case bug thật đã sửa: lương NĂM bị hiểu nhầm lương/tháng
        ("200tr-500tr ₫/năm", "VNĐ", 200_000_000, 500_000_000, "RANGE", "YEAR"),
        ("15 triệu/năm", "VNĐ", 15_000_000, 15_000_000, "EXACT", "YEAR"),
        ("$ 3,000-5,000 per year", "USD", 3_000, 5_000, "RANGE", "YEAR"),
        ("Annual 500tr", "VNĐ", 500_000_000, 500_000_000, "EXACT", "YEAR"),
        # "/year" (KHÔNG có "per") KHÔNG được _YEARLY_SALARY_MARKER coi là
        # tín hiệu năm — chỉ khớp "/năm" (tiếng Việt), "annual", "per
        # year", "yearly". Test rõ ràng để tránh hồi quy nếu sau này mở
        # rộng regex mà không cập nhật lại test case này.
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
        # "năm" đứng LẺ (không đi sau "/") KHÔNG được tính là tín hiệu
        # lương năm — chỉ bắt khi đi sau "/" hoặc là 1 trong các cụm
        # annual/per year/yearly cố định. Dùng câu KHÔNG có số nào khác
        # ngoài mức lương, để cô lập đúng hành vi salary_period (nếu lỡ
        # có số khác trong câu, numbers[] sẽ bắt luôn số đó, gây nhiễu
        # kết quả không liên quan tới bug đang test ở đây).
        ("15 triệu (đã làm nhiều năm trong ngành)", "VNĐ", 15_000_000, 15_000_000, "EXACT", "MONTH"),
    ]

    for input_text, exp_cur, exp_min, exp_max, exp_type, exp_period in cases:
        result = normalize.normalize_salary(input_text)
        print(f"  normalize_salary({input_text!r}) = {result}")
        if (result.currency, result.salary_min, result.salary_max,
                result.salary_type, result.salary_period) != (exp_cur, exp_min, exp_max, exp_type, exp_period):
            ok = False
            print(f"  !! SAI, kỳ vọng currency={exp_cur!r} min={exp_min!r} "
                  f"max={exp_max!r} type={exp_type!r} period={exp_period!r} !!")

    print()
    return ok


def test_clean_company_name() -> bool:
    """Test clean_company_name() — cắt URL dính đuôi tên công ty (bug thật
    phát hiện qua đối chiếu dữ liệu đã crawl, 08/2026: company_id
    42df93a8-ba25-4ae6-8246-885441b16099 -> "Bắc Á Bank -
    Https://tuyendung.baca-Bank.vn/"; 49ae404e-52a9-4f47-9eb5-d9b02cc3a028
    -> "VPBank - Https://tuyendung.vpbank.com.vn/"), đồng thời không cắt
    nhầm tên công ty hợp lệ không chứa URL."""
    print("--- Test clean_company_name() ---")
    ok = True

    cases = [
        ("Bắc Á Bank - Https://tuyendung.baca-Bank.vn/", "Bắc Á Bank"),
        ("VPBank - Https://tuyendung.vpbank.com.vn/", "VPBank"),
        ("Some Co | https://example.com", "Some Co"),
        ("Some Co https://example.com", "Some Co"),
        ("  FPT   Software  ", "FPT Software"),   # dư khoảng trắng, không có URL
        ("Procter & Gamble", "Procter & Gamble"),  # không có URL, giữ nguyên
        ("", ""),
        (None, ""),
    ]

    for input_text, expected in cases:
        result = normalize.clean_company_name(input_text)
        print(f"  clean_company_name({input_text!r}) = {result!r}")
        if result != expected:
            ok = False
            print(f"  !! SAI, kỳ vọng {expected!r} !!")

    print()
    return ok


def test_resolve_province_alias() -> bool:
    """Test resolve_province_alias() — quy đổi tên tỉnh CŨ (trước sáp
    nhập 07/2025) sang tên tỉnh MỚI, dùng khi doanh nghiệp đăng tin vẫn
    ghi địa chỉ theo tỉnh cũ (chưa cập nhật theo địa giới hành chính
    mới)."""
    print("--- Test resolve_province_alias() ---")
    ok = True

    cases = [
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
    ]

    for input_text, expected in cases:
        result = resolve_province_alias(input_text)
        print(f"  resolve_province_alias({input_text!r}) = {result!r}")
        if result != expected:
            ok = False
            print(f"  !! SAI, kỳ vọng {expected!r} !!")

    print()
    return ok


def test_careerviet_company_profile() -> bool:
    """Test CareerVietAdapter.fetch_company_profile() bằng mẫu HTML thật
    (fixture_careerviet_company_profile.html, trang công ty FPT Long
    Châu, fetch 08/2026 — xem docstring fetch_company_profile() để biết
    cấu trúc DOM thật đang bám vào)."""
    print("--- Test CareerVietAdapter.fetch_company_profile() ---")
    ok = True

    with open(CAREERVIET_COMPANY_FIXTURE_PATH, "r", encoding="utf-8") as f:
        html = f.read()

    adapter = CareerVietAdapter.__new__(CareerVietAdapter)  # bỏ qua __init__, không cần session
    adapter._fetch_html = lambda url: html  # tránh gọi internet thật khi test

    result = adapter.fetch_company_profile(
        "https://careerviet.vn/vi/nha-tuyen-dung/fake.html"
    )
    print(f"  address       : {result.get('address')!r}")
    print(f"  company_size  : {result.get('company_size')!r}")
    print(f"  real_website  : {result.get('real_website')!r}")
    print(f"  industry      : {result.get('industry')!r}")
    print(f"  tax_id        : {result.get('tax_id')!r}")
    print(f"  description   : {result.get('description')[:60]!r}...")

    checks = [
        (
            result.get("address")
            == "379-381 Hai Bà Trưng, Phường Võ Thị Sáu, Quận 3, Thành phố Hồ Chí Minh",
            "address parse SAI",
        ),
        (result.get("company_size") == "10.000-19.999", "company_size parse SAI"),
        (result.get("real_website") == "https://tuyendung.frt.vn/", "real_website parse SAI"),
        ("Long Châu" in result.get("description", ""), "description rỗng/SAI"),
        # Mẫu thật KHÔNG có nhãn "Lĩnh vực hoạt động"/"Mã số thuế" -> phải
        # rỗng "" (an toàn), KHÔNG được bịa dữ liệu.
        (result.get("industry") == "", "industry phải rỗng khi mẫu không có nhãn này"),
        (result.get("tax_id") == "", "tax_id phải rỗng khi mẫu không có nhãn này"),
    ]
    for passed, msg in checks:
        if not passed:
            ok = False
            print(f"  !! {msg} !!")

    # company_url rỗng -> trả dict rỗng an toàn, không crash / không fetch
    empty = adapter.fetch_company_profile("")
    if any(v for v in empty.values()):
        ok = False
        print("  !! fetch_company_profile('') phải trả toàn bộ field rỗng !!")

    print()
    return ok


def main():
    ok = True
    ok = test_listing_and_normalize() and ok
    ok = test_job_full_detail() and ok
    ok = test_normalize_salary() and ok
    ok = test_normalize_work_type() and ok
    ok = test_clean_company_name() and ok
    ok = test_resolve_province_alias() and ok
    ok = test_careerviet_company_profile() and ok

    print("=" * 50)
    print("KẾT QUẢ: " + ("✅ PASS" if ok else "❌ FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
