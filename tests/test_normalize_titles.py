"""
normalize.titles_similar / title_overlap — so khớp tiêu đề để biết hai tin cùng
mã job VietnamWorks còn là một vị trí hay đã bị đổi sang vị trí khác.

Dùng chung bởi scripts/backfill/backfill_vnw_detail.py và pipeline crawl. Không cần DB.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import normalize


@pytest.mark.parametrize("stored,page", [
    ("Kỹ Sư Dữ Liệu - Data Engineer", "Kỹ Sư Dữ Liệu (DE)"),
    ("Senior Tester (QA)", "Senior Tester (QA) (Open for Middle and Junior)"),
    ("School Sales Manager/Supervisor", "School Sales Supervisor"),
    ("AI & IT Specialist", "IT Business Analyst & AI Specialist"),
    ("Product Development Project Coordinator",
     "Product Development Project Coordinator (Background Automotive Engineer)"),
    ("Chuyên Gia Quản Trị Rủi Ro", "Chuyên Gia Quản Trị Rủi Ro / Tuyển Gấp"),
    ("Data Engineer", ""),                      # thiếu tiêu đề trang: không đủ cơ sở để từ chối
])
def test_titles_similar_accepts_light_edits(stored, page):
    assert normalize.titles_similar(stored, page) is True


@pytest.mark.parametrize("stored,page", [
    ("Account Manager", "Sales Assistant"),
    ("BACK-END DEVELOPER (NodeJS/Java/PHP)", "Kỹ Sư An Toàn Thông Tin"),
    ("Demand Planner & Analyst (Supply Chain Operation)", "Inventory Planning Analyst"),
    ("Chuyên Viên Bán Hàng Qua Điện Thoại (Tiếng Trung Giao Tiếp)",
     "Pre-Sales Officer (Tiếng Trung Hsk5/6)"),
])
def test_titles_similar_rejects_different_positions(stored, page):
    """Các cặp lấy từ dữ liệu thật: cùng mã job VietnamWorks nhưng đã đổi thành vị trí khác."""
    assert normalize.titles_similar(stored, page) is False


def test_title_overlap_ignores_case_and_accents():
    assert normalize.title_overlap("Kỹ Sư Dữ Liệu", "KY SU DU LIEU") == 1.0


def test_title_overlap_divides_by_the_shorter_title():
    # Thêm đuôi dài vào tiêu đề không được kéo điểm xuống.
    assert normalize.title_overlap("Data Engineer", "Data Engineer (Hà Nội) - Tuyển Gấp 2026") == 1.0


def test_title_overlap_is_zero_for_unrelated_titles():
    assert normalize.title_overlap("Account Manager", "Sales Assistant") == 0.0


@pytest.mark.parametrize("a,b", [("", "Data Engineer"), ("Data Engineer", ""), ("", "")])
def test_title_overlap_missing_title_counts_as_same(a, b):
    assert normalize.title_overlap(a, b) == 1.0


def test_titles_similar_threshold_is_the_shared_constant():
    assert normalize.MIN_TITLE_OVERLAP == 0.5
    # 1 từ chung trên 2 từ của tiêu đề ngắn = đúng 0,5 -> vẫn coi là giống.
    assert normalize.titles_similar("Data Analyst", "Data Scientist") is True
