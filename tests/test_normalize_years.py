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


def test_infer_level_tren_5_nam_la_lead():
    """Nhãn "Trên 5 năm" của TopCV phải ra Lead, không phải Senior.

    Lỗi cũ: regex "(\\d+) năm" bắt "5 năm" trong cụm này trước nên ra Senior,
    nhánh Lead phía dưới không bao giờ chạy tới."""
    assert normalize.infer_level("Trên 5 năm") == "Lead"
    assert normalize.infer_level("trên 5 năm") == "Lead"
    # đúng 5 năm vẫn là Senior, ranh giới không bị lệch
    assert normalize.infer_level("5 năm") == "Senior"
    assert normalize.infer_level("4 năm") == "Senior"
    assert normalize.infer_level("6 năm") == "Lead"


def test_infer_level_tren_5_nam_title_van_uu_tien():
    """Từ khoá trong tiêu đề vẫn thắng số năm, kể cả với "Trên 5 năm"."""
    assert normalize.infer_level("Trên 5 năm", "Senior Data Engineer") == "Senior"
    assert normalize.infer_level("Trên 5 năm", "Giám đốc kinh doanh") == "Manager"
    assert normalize.infer_level("Trên 5 năm", "Intern") == "Intern"


# ----------------------------------------------------------------------
# Từ khoá cấp bậc trong tiêu đề khớp theo TỪ, không khớp chuỗi con.
# Các tiêu đề dưới đây lấy từ dữ liệu thật (TopCV/VietnamWorks/CareerViet).
# ----------------------------------------------------------------------
@pytest.mark.parametrize("title", [
    "Digital Leadership - ID10550",
    "Digital Leadership Opportunities At VPBank - ID10550",
    "Leadership Development Program (LDP) 2026 – Milwaukee Power Tool Vietnam",
    "Lead Generation Specialist",
])
def test_title_leadership_is_not_lead(title):
    assert normalize.infer_level("3 năm", title) == "Middle"      # rơi về số năm


@pytest.mark.parametrize("title", [
    "ROX Global Leaders - Giám Đốc Công Ty Năng Lượng Tái Tạo Tại Quốc Gia (Tuyển Dụng Nước Ngoài)",
])
def test_title_global_leaders_brand_falls_through_to_manager(title):
    assert normalize.infer_level("", title) == "Manager"


@pytest.mark.parametrize("title", [
    "Java Techlead",
    "Techlead .NET (Government Domain)",
    "Fullstack Developer -  Techlead Track",
    "Angular Team Lead",
    "Business Analyst (2-3 Năm Kinh Nghiệm) - Teamlead",
    "Android Developer (Leader)",
    "BA Lead (Dự Án GOV)",
    "Data Engineer (Senior/Leader)",
    "Workforce Planning Lead",
    "Trưởng nhóm phát triển",
])
def test_title_real_lead_titles_stay_lead(title):
    assert normalize.infer_level("", title) == "Lead"


@pytest.mark.parametrize("title", [
    "International Sales Executive",
    "Internal Audit Specialist",
    "Internet Marketing Executive",
])
def test_title_international_internal_internet_are_not_intern(title):
    assert normalize.infer_level("3 năm", title) == "Middle"


@pytest.mark.parametrize("title", [
    "Backend Intern", "Marketing Interns", "Internship Program 2026", "Thực tập sinh Data",
])
def test_title_real_intern_titles_stay_intern(title):
    assert normalize.infer_level("3 năm", title) == "Intern"


def test_title_assistant_to_director_is_not_manager():
    assert normalize.infer_level("3 năm", "Trợ Lý Giám Đốc Kinh Doanh") == "Middle"
    assert normalize.infer_level("3 năm", "Trợ Lý Ban Tổng Giám Đốc") == "Middle"     # dữ liệu thật VietnamWorks
    assert normalize.infer_level("3 năm", "Trợ Lý Giám Đốc Sản Xuất") == "Middle"
    assert normalize.infer_level("", "Thư ký Phó Giám đốc") == "Junior"
    # giữ nguyên: người giữ chức vụ thật vẫn là Manager
    assert normalize.infer_level("", "Giám Đốc Kinh Doanh") == "Manager"
    assert normalize.infer_level("", "Phó Giám Đốc Trung Tâm") == "Manager"
    assert normalize.infer_level("", "Trưởng Phòng Pháp Chế") == "Manager"


def test_title_manager_and_senior_match_whole_words():
    assert normalize.infer_level("", "Sales Manager - Hyundai Electric") == "Manager"
    assert normalize.infer_level("", "Senior Data Engineer") == "Senior"
    assert normalize.infer_level("", "Fresher Data Analyst") == "Fresher"
    # "Refresher" không phải Fresher
    assert normalize.infer_level("3 năm", "Refresher Course Coordinator") == "Middle"
