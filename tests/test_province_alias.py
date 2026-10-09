"""Test resolve_province_alias() (scrapjd/province_alias.py).

Chạy thuần Python, không cần DB. Bổ sung cho test_resolve_province_alias()
cũ trong test_parse_and_normalize.py (giữ nguyên) bằng:
- kiểm tra chuẩn hoá (hoa/thường, bỏ dấu, tiền tố, gạch nối) và các viết
  tắt/tên tiếng Anh phổ biến;
- khoá bất biến: mọi alias phải trỏ tới tỉnh CÓ THẬT trong seed
  sql/schema.sql, và bỏ dấu không làm 2 tỉnh khác nhau trùng key.

Bối cảnh: "Thừa Thiên Huế", "TP.HCM", "Ho Chi Minh" từng trả "" nên rơi
vào "Khác" (xem scrapjd/db/lookups.py::get_province_id).
"""
import os
import re

import pytest

from scrapjd import province_alias
from scrapjd.province_alias import PROVINCE_ALIAS_MAP, resolve_province_alias

_SCHEMA = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")


def _seeded_provinces() -> set[str]:
    """Đọc danh sách tỉnh seed trong schema.sql (khối INSERT INTO provinces)."""
    text = open(_SCHEMA, encoding="utf-8").read()
    block = text.split("INSERT INTO provinces", 1)[1].split("ON CONFLICT", 1)[0]
    return set(re.findall(r"'([^']+)'", block))


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Các ca từng rơi vào "Khác"
        ("Thừa Thiên Huế", "Huế"),
        ("Thừa Thiên - Huế", "Huế"),
        ("TT Huế", "Huế"),
        ("TP.HCM", "Hồ Chí Minh"),
        ("TP HCM", "Hồ Chí Minh"),
        ("TPHCM", "Hồ Chí Minh"),
        ("HCM", "Hồ Chí Minh"),
        ("Ho Chi Minh", "Hồ Chí Minh"),
        ("Ho Chi Minh City", "Hồ Chí Minh"),
        ("Sài Gòn", "Hồ Chí Minh"),
        ("Hanoi", "Hà Nội"),
        ("HN", "Hà Nội"),
        # Hoa/thường, không dấu
        ("HÀ NỘI", "Hà Nội"),
        ("ha noi", "Hà Nội"),
        ("Dak Lak", "Đắk Lắk"),
        ("da nang", "Đà Nẵng"),
        # Tiền tố
        ("Thành phố Hồ Chí Minh", "Hồ Chí Minh"),
        ("Tỉnh Bình Dương", "Hồ Chí Minh"),
        ("Tp. Cần Thơ", "Cần Thơ"),
        # Dấu gạch nối / khoảng trắng lệch
        ("Bà Rịa-Vũng Tàu", "Hồ Chí Minh"),
        ("  Bà Rịa - Vũng Tàu  ", "Hồ Chí Minh"),
        # Đã đúng từ trước, không được hỏng
        ("Bình Dương", "Hồ Chí Minh"),
        ("Hòa Bình", "Phú Thọ"),
        ("Huế", "Huế"),
        ("Khác", "Khác"),
        ("Remote", "Remote"),
    ],
)
def test_resolve_known_variants(raw, expected):
    assert resolve_province_alias(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "   ", "Xyz Không Tồn Tại", "Vũng Tàu", "TP."])
def test_resolve_unknown_returns_empty(raw):
    """Tên lạ/mơ hồ -> "" để nơi gọi fallback "Khác", không đoán bừa."""
    assert resolve_province_alias(raw) == ""


def test_every_alias_target_exists_in_seed():
    """Alias trỏ tới tên không có trong bảng provinces sẽ rơi về "Khác" âm
    thầm (get_province_id không tìm thấy dòng)."""
    seeded = _seeded_provinces()
    targets = set(PROVINCE_ALIAS_MAP.values()) | set(province_alias._EXTRA_ALIASES.values())
    assert targets <= seeded, f"Alias trỏ tới tỉnh không có trong seed: {targets - seeded}"


def test_every_map_key_resolves_to_its_value():
    """Chuẩn hoá không được làm hỏng bất kỳ key nào đã có."""
    for raw, expected in PROVINCE_ALIAS_MAP.items():
        assert resolve_province_alias(raw) == expected, raw


def test_accent_stripping_does_not_merge_different_provinces():
    """Bỏ dấu có thể làm 2 tên khác nhau trùng key (vd Bạc Liêu / Bắc Liêu
    nếu tồn tại). Khoá bất biến này để sau thêm tên mới không ngầm ghi đè."""
    seen: dict[str, str] = {}
    for raw, target in PROVINCE_ALIAS_MAP.items():
        key = province_alias._normalize_key(raw)
        assert seen.setdefault(key, target) == target, f"{raw!r} trùng key {key!r}"
    for key, target in province_alias._EXTRA_ALIASES.items():
        assert seen.setdefault(key, target) == target, f"alias bổ sung {key!r} xung đột"
