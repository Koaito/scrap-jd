"""
Test normalize.extract_min_years() và tham số level_hint của infer_level() —
KHÔNG cần database, KHÔNG cần internet.

Các câu trong bảng test lấy từ phần yêu cầu của job VietnamWorks thật (mẫu
snapshot 10/2026), giữ nguyên lỗi chính tả/định dạng gốc ("Tối thiếu", "03–05",
">5 năm"...) vì đó chính là thứ bộ đọc phải chịu được.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import normalize


@pytest.mark.parametrize(
    "text, expected",
    [
        # --- tiếng Việt, một con số ---
        ("Có từ 5 năm kinh nghiệm trong Marketing, Growth Marketing", 5),
        ("Ít nhất 5 năm kinh nghiệm về chăm sóc khách hàng", 5),
        ("Có kinh nghiệm tối thiểu từ 2 năm làm Product Manager", 2),
        ("Tối thiểu 06 năm kinh nghiệm trong lĩnh vực Tài chính", 6),
        ("Có 5+ năm kinh nghiệm trong kế toán", 5),
        ("Tuổi từ 30 ~ 45 tuổi, kinh nghiệm >5 năm ở vị trí lập kế hoạch", 5),
        # --- khoảng: lấy SỐ NHỎ (quyết định của người dùng) ---
        ("Có tối thiểu 1 - 3 năm kinh nghiệm thực tế trong phát triển", 1),
        ("Có 03–05 năm kinh nghiệm trong lĩnh vực Data", 3),
        ("3–5 năm kinh nghiệm về Data Architect", 3),
        # --- nhiều đoạn: lấy đoạn ĐẦU TIÊN ---
        ("Tối thiểu 06 năm kinh nghiệm trong Tài chính. Tối thiểu 02 năm kinh nghiệm trong Product", 6),
        ("Có tối thiểu 10 năm kinh nghiệm trong đó 5 năm kinh nghiệm làm quản lý", 10),
        ("Tối thiểu 3 năm kinh nghiệm tuyển dụng; 1 năm ở vị trí trưởng nhóm", 3),
        # --- tiếng Anh ---
        ("- 3+ years of experience in Data Engineering, Backend Engineering", 3),
        ("At least 2-4 years of experience in analytics", 2),
        # --- không yêu cầu / dưới 1 năm -> 0 ---
        ("Có dưới 1 năm kinh nghiệm trong E-commerce", 0),
        ("Không yêu cầu kinh nghiệm, sẽ được đào tạo", 0),
        ("Có 6 tháng kinh nghiệm làm việc", 0),
        # "Không yêu cầu" đứng TRƯỚC con số nên thắng
        ("Không yêu cầu kinh nghiệm, ưu tiên có 2 năm kinh nghiệm", 0),
        # --- KHÔNG được bắt nhầm ---
        ("Sinh viên năm cuối hoặc đã tốt nghiệp Đại học", None),
        ("Sinh viên năm 3, năm 4 ngành CNTT", None),
        ("Tốt nghiệp đại học năm 2020", None),
        ("Làm việc 5 ngày/tuần, thành lập năm 2010", None),
        ("", None),
        (None, None),
    ],
)
def test_extract_min_years(text, expected):
    assert normalize.extract_min_years(text) == expected


def test_extract_min_years_ignores_implausible_numbers():
    assert normalize.extract_min_years("Công ty có 99 năm kinh nghiệm trên thị trường") is None


# ----------------------------------------------------------------------
# infer_level + level_hint
# ----------------------------------------------------------------------
def test_level_hint_used_only_when_no_years():
    assert normalize.infer_level("", "Kế toán tổng hợp", "Manager") == "Manager"
    assert normalize.infer_level("", "Kế toán tổng hợp", "Fresher") == "Fresher"


def test_level_hint_does_not_override_years():
    # Số năm đọc được thắng nhãn cấp bậc (nhãn do nhà tuyển dụng tự chọn, nhiễu).
    assert normalize.infer_level("3 năm", "Data Modeler", "Manager") == "Middle"


def test_level_hint_does_not_override_title_keyword():
    assert normalize.infer_level("", "Senior Data Engineer", "Fresher") == "Senior"


def test_level_hint_unknown_value_ignored():
    assert normalize.infer_level("", "Data Analyst", "Giám đốc") == "Junior"
    assert normalize.infer_level("", "Data Analyst", "") == "Junior"


def test_infer_level_without_hint_unchanged():
    """Hai tham số cũ vẫn cho kết quả như trước khi có level_hint."""
    assert normalize.infer_level("Không yêu cầu") == "Fresher"
    assert normalize.infer_level("1 năm") == "Junior"
    assert normalize.infer_level("2 năm") == "Middle"
    assert normalize.infer_level("5 năm") == "Senior"
    assert normalize.infer_level("8 năm") == "Lead"
    assert normalize.infer_level("", "Thực tập sinh Data") == "Intern"
    assert normalize.infer_level("") == "Junior"
