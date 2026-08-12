"""
Test parser + normalize KHÔNG cần database, KHÔNG cần internet.
Chạy: python tests/test_parse_and_normalize.py
"""

import sys
import os
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from adapters.topcv import TopCVAdapter
import normalize

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixture_topcv_listing.html")
JOB_DETAIL_FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixture_topcv_job_detail.html")


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


def main():
    ok = True
    ok = test_listing_and_normalize() and ok
    ok = test_job_full_detail() and ok
    ok = test_normalize_work_type() and ok

    print("=" * 50)
    print("KẾT QUẢ: " + ("✅ PASS" if ok else "❌ FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
