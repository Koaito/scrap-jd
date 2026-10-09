"""
normalize.derive_level() (level + căn cứ) và các từ khoá tiêu đề thêm 10/2026:
"Sr.", "Head of", "Phó phòng"/"Deputy Head", "Junior".

Hàm thuần, không cần DB. Các tiêu đề dưới đây lấy từ dữ liệu thật trong DB
(kết quả soát level 10/2026), kể cả những tiêu đề từng bị gán sai.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scrapjd import normalize


# ----------------------------------------------------------------------
# Căn cứ (source) của từng nhánh
# ----------------------------------------------------------------------
@pytest.mark.parametrize("args,level,source", [
    # tiêu đề thắng tất cả
    (("", "Senior Dev", ""), "Senior", "title"),
    (("1 năm", "Trưởng nhóm kỹ thuật", "Fresher"), "Lead", "title"),
    # nhãn cố định của nguồn
    (("Không yêu cầu", "Kế toán"), "Fresher", "label"),
    (("Dưới 1 năm", "Kế toán"), "Fresher", "label"),
    (("Trên 5 năm", "Data Analyst"), "Lead", "label"),
    # số năm
    (("1 năm", "Data Analyst"), "Junior", "years"),
    (("3 năm", "Data Analyst"), "Middle", "years"),
    (("5 năm", "Data Analyst"), "Senior", "years"),
    (("8 năm", "Data Analyst"), "Lead", "years"),
    # số năm thắng nhãn cấp bậc của nguồn
    (("3 năm", "Data Modeler", "Manager"), "Middle", "years"),
    # nhãn cấp bậc chỉ là dự phòng
    (("", "Kế toán tổng hợp", "Manager"), "Manager", "hint"),
    (("", "Data Analyst", "Giám đốc"), "Junior", "default"),  # nhãn không thuộc LEVEL_ORDER
    # không có căn cứ nào
    (("", "Data Analyst", ""), "Junior", "default"),
    (("",), "Junior", "default"),
])
def test_derive_level_reports_source(args, level, source):
    decision = normalize.derive_level(*args)
    assert decision.level == level
    assert decision.source == source


def test_infer_level_is_derive_level_level():
    """infer_level giữ nguyên chữ ký cũ, luôn trùng derive_level().level."""
    cases = [
        ("", "Senior Dev", ""), ("Trên 5 năm", "Intern", ""), ("2 năm", "", ""),
        ("", "Head of Sales", ""), ("", "", "Lead"), ("", "[Junior+] Embedded Engineer", ""),
        ("Không yêu cầu", "Phó phòng Dữ liệu", ""),
    ]
    for args in cases:
        assert normalize.infer_level(*args) == normalize.derive_level(*args).level


def test_derive_level_never_returns_manual_source():
    """"manual" do tầng ứng dụng đặt khi người dùng sửa tay, hàm suy luận không bao giờ trả."""
    for title in ("", "Senior Dev", "Head of Data", "Junior Dev", "Intern"):
        for exp in ("", "3 năm", "Trên 5 năm", "Không yêu cầu"):
            for hint in ("", "Manager"):
                assert normalize.derive_level(exp, title, hint).source != "manual"


def test_level_constants():
    assert isinstance(normalize.LEVEL_RULE_VERSION, int) and normalize.LEVEL_RULE_VERSION >= 1
    assert "manual" in normalize.LEVEL_SOURCES
    assert len(set(normalize.LEVEL_SOURCES)) == len(normalize.LEVEL_SOURCES)
    # mọi nguồn derive_level có thể trả đều nằm trong danh sách (cột DB sẽ CHECK theo danh sách này)
    for src in ("title", "label", "years", "title_range", "hint", "default"):
        assert src in normalize.LEVEL_SOURCES


# ----------------------------------------------------------------------
# "Sr." = Senior
# ----------------------------------------------------------------------
@pytest.mark.parametrize("title", [
    "Sr. Specialist, Culture And Engagement",
    "Sr Software Engineer",
    "SR. Accountant",
    "Sr.Developer",
])
def test_sr_is_senior(title):
    assert normalize.level_from_title(title) == "Senior"


@pytest.mark.parametrize("title", [
    "Technician (SR-001)",        # mã tin
    "Kỹ sư SR 2026",              # sau "SR" là số
    "Sr",                         # đứng một mình
    "Srinivas Data Engineer",     # chuỗi con của một từ khác
])
def test_sr_does_not_match_codes_or_substrings(title):
    assert normalize.level_from_title(title) is None


# ----------------------------------------------------------------------
# "Head of" -> Manager; "Phó phòng"/"Deputy Head" -> Lead
# ----------------------------------------------------------------------
@pytest.mark.parametrize("title,expected", [
    ("Head Of Export Sales (Attractive Salary)", "Manager"),
    ("Head of AI & Data (Data Foundation)", "Manager"),
    ("Head Of Data (Loyalty/Fintech)", "Manager"),
    ("Head Of Data Tại UpBase-AI & Big Data Ecom Partner", "Manager"),
    ("Deputy Head of Machine Learning Engineering", "Lead"),
    ("Phó phòng Công nghệ Mô hình - Deputy Head of Machine Learning Engineering - Khối Dữ liệu (2026TD456609)", "Lead"),
    ("[Hà Nội] Phó Phòng Dữ Liệu và Phân Tích", "Lead"),
    ("Phó trưởng phòng Kế toán", "Lead"),            # không bị "trưởng phòng" kéo thành Manager
    ("Head of Data Team Lead", "Lead"),              # Lead vẫn thắng Manager như trước
    ("Trưởng phòng Kinh doanh", "Manager"),          # không đổi
])
def test_head_of_and_deputy(title, expected):
    assert normalize.level_from_title(title) == expected


@pytest.mark.parametrize("title", [
    "Operation Officer (Ha Noi Head Office - 629)",  # "Head Office" không phải "Head of"
    "Leasing Officer (Hanoi Head Office - 646)",
    "Ahead of the curve Analyst",                    # "ahead of": không có ranh giới từ trước "head"
    "Cửa hàng phó (ưu tiên kinh nghiệm siêu thị bán lẻ)",  # "phó" nhưng không phải phó phòng
    "Trợ lý Phó phòng Kế hoạch",                     # người hỗ trợ, không phải người giữ chức
    "Assistant to Head of Sales",
    "Secretary to the Head of Finance",
])
def test_head_of_and_deputy_false_positives(title):
    assert normalize.level_from_title(title) is None


# ----------------------------------------------------------------------
# "Junior" trong tiêu đề
# ----------------------------------------------------------------------
@pytest.mark.parametrize("title", [
    "[HCM - Thu Duc] (Junior) Business Analyst",     # từng bị lưu Lead
    "Business Analyst (IT BA) - Thiết Kế Giải Pháp - Junior",
    "Junior Business Analyst",
    "Junior Java Developer",
])
def test_junior_in_title(title):
    assert normalize.level_from_title(title) == "Junior"


@pytest.mark.parametrize("title", [
    "[Junior+] Automotive Embedded Software Engineer",  # "từ Junior trở lên": để số năm quyết định
    "Developer (Junior+)",
    "Developer (Junior/Middle)",
    "Developer (Junior - Mid)",
    "Junior to Middle Backend Engineer",
])
def test_junior_ambiguous_titles_fall_through(title):
    assert normalize.level_from_title(title) is None


def test_junior_plus_falls_back_to_years():
    title = "[Junior+] Automotive Embedded Software Engineer"
    assert normalize.derive_level("3 năm", title) == normalize.LevelDecision("Middle", "years")
    assert normalize.derive_level("", title) == normalize.LevelDecision("Junior", "default")


@pytest.mark.parametrize("title,expected", [
    # chức danh GHÉP chỉ là một cấp: tiêu đề vẫn quyết định, cấp cao hơn thắng
    ("Junior Team Lead", "Lead"),
    ("Senior Team Lead", "Lead"),
    ("Senior Product Manager", "Manager"),
    ("Senior Developer (Lead group)", "Lead"),
    ("Head of Data Team Lead", "Lead"),
])
def test_compound_titles_are_one_level(title, expected):
    assert normalize.level_from_title(title) == expected


@pytest.mark.parametrize("title", [
    "Business Analyst (Junior - Senior)",
    "FullStack Developer (Junior, Senior) - Khối Công nghệ thông tin (2025TD922)",
    "Business Analyst (Fresher- Junior) Lương 8-12 Triệu",
    "Middle/Senior Backend Developer",
    "Lead/Senior/Middle Engineer",
    "Intern/Fresher Marketing",
    "Developer (Junior & Senior)",
    "Developer (Junior | Senior)",
    "Developer (Junior or Senior)",
    "Developer (Junior and Middle)",
    "Developer (Junior hoặc Senior)",
    "Senior - Team Lead",
])
def test_range_titles_do_not_decide(title):
    """Tiêu đề liệt kê nhiều cấp không xác định được cấp nào -> None, số năm quyết định."""
    assert normalize.level_from_title(title) is None


def test_range_title_falls_back_to_years():
    title = "FullStack Developer (Junior, Senior) - Khối Công nghệ thông tin (2025TD922)"
    assert normalize.derive_level("2 năm", title) == normalize.LevelDecision("Middle", "years")
    assert normalize.derive_level("6 năm", title) == normalize.LevelDecision("Lead", "years")
    # không có số năm: cấp THẤP nhất trong khoảng tiêu đề nêu, đứng trước hint
    assert normalize.derive_level("", title) == normalize.LevelDecision("Junior", "title_range")
    assert normalize.derive_level("", title, "Lead") == normalize.LevelDecision("Junior", "title_range")


@pytest.mark.parametrize("title,floor", [
    ("Data Engineer (Senior/Leader)", "Senior"),       # không thể rơi về Junior mặc định
    ("Middle/Senior Backend Developer", "Middle"),
    ("Lead/Senior/Middle Engineer", "Middle"),
    ("Business Analyst (Fresher- Junior) Lương 8-12 Triệu", "Fresher"),
    ("Developer (Junior to Middle)", "Junior"),
])
def test_range_title_without_years_uses_lowest_level(title, floor):
    assert normalize.derive_level("", title) == normalize.LevelDecision(floor, "title_range")


def test_range_title_label_beats_range_floor():
    """Nhãn cố định của nguồn ("Trên 5 năm") đứng trước cận dưới của khoảng."""
    assert normalize.derive_level("Trên 5 năm", "Developer (Junior, Senior)") == normalize.LevelDecision("Lead", "label")


def test_single_level_title_still_beats_years():
    """Tiêu đề nêu đúng một cấp thì thắng số năm (nhà tuyển dụng đã nói rõ cấp)."""
    assert normalize.derive_level("2 năm", "Senior Dev") == normalize.LevelDecision("Senior", "title")


def test_middle_alone_does_not_decide():
    assert normalize.level_from_title("Middle Backend Developer") is None


def test_junior_title_beats_years():
    assert normalize.derive_level("8 năm", "Junior Developer") == normalize.LevelDecision("Junior", "title")


# ----------------------------------------------------------------------
# Hồi quy: các lỗi level cũ đã thấy trong DB (xem log soát 10/2026)
# ----------------------------------------------------------------------
@pytest.mark.parametrize("title,expected", [
    ("Internal Audit Officer", None),                 # từng bị Intern (khớp chuỗi con)
    ("Fullstack (Lead Internal)", "Lead"),            # từng bị Intern
    ("Senior Culture & Internal Communications Executive", "Senior"),
    ("Leadership Development Program (LDP) 2026", None),
    ("Lead Generation Specialist", None),
])
def test_regressions_from_audit(title, expected):
    assert normalize.level_from_title(title) == expected


def test_rox_global_leaders_is_manager_not_lead():
    title = "ROX Global Leaders - Giám Đốc Công Ty Năng Lượng Tái Tạo Tại Quốc Gia (Tuyển Dụng Nước Ngoài)"
    assert normalize.derive_level("", title) == normalize.LevelDecision("Manager", "title")
