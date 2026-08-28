"""
Test cho db/email_templates.py — cụ thể hàm _parse_pg_enum_array(), bản
vá cho lỗi 500 (fastapi.exceptions.ResponseValidationError) khi psycopg2
không tự parse được cột recommended_for kiểu contact_status_enum[].

Đây là test THUẬT (không mock DB) — _parse_pg_enum_array là hàm thuần
(pure function, không chạm DB/network), nên test trực tiếp với các
chuỗi Postgres array literal thật, đúng dạng psycopg2 RealDictCursor
trả về khi đọc mảng enum tự định nghĩa (xem log lỗi gốc: input
'{UNCONTACTED}' bị Pydantic list[str] reject).

Chạy: pytest tests/test_db_email_templates.py -v
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.email_templates import _parse_pg_enum_array


def test_single_value_matches_original_bug_log():
    """Đúng input gây lỗi trong log gốc: '{UNCONTACTED}' -> ['UNCONTACTED']."""
    assert _parse_pg_enum_array("{UNCONTACTED}") == ["UNCONTACTED"]


def test_multiple_values():
    assert _parse_pg_enum_array("{UNCONTACTED,RESPONDED}") == ["UNCONTACTED", "RESPONDED"]
    assert _parse_pg_enum_array("{EMAIL_SENT,IN_PARTNERSHIP,RESPONDED}") == [
        "EMAIL_SENT",
        "IN_PARTNERSHIP",
        "RESPONDED",
    ]


def test_empty_array_literal():
    """Mẫu không gợi ý riêng trạng thái nào (2/6 mẫu gốc trong migration)."""
    assert _parse_pg_enum_array("{}") == []


def test_none_input():
    """Cột NOT NULL DEFAULT '{}' nên None hiếm khi xảy ra, nhưng vẫn an
    toàn trả [] thay vì crash."""
    assert _parse_pg_enum_array(None) == []


def test_already_a_list_passthrough():
    """Nếu psycopg2/driver đã tự parse thành list (vd cấu hình/khác
    version), hàm không xử lý gì thêm — tránh double-parse."""
    assert _parse_pg_enum_array(["UNCONTACTED", "RESPONDED"]) == ["UNCONTACTED", "RESPONDED"]
    assert _parse_pg_enum_array([]) == []


def test_quoted_values():
    """Postgres đôi khi bọc quote nếu giá trị có ký tự đặc biệt — enum
    values ở đây không có, nhưng hàm vẫn nên strip quote thừa an toàn."""
    assert _parse_pg_enum_array('{"UNCONTACTED","RESPONDED"}') == ["UNCONTACTED", "RESPONDED"]


def test_all_real_enum_values_from_schema():
    """Toàn bộ giá trị hợp lệ của contact_status_enum theo sql/schema.sql,
    đảm bảo parse đúng dù enum có thêm giá trị dài trong tương lai."""
    raw = "{UNCONTACTED,EMAIL_SENT,RESPONDED,IN_PARTNERSHIP}"
    assert _parse_pg_enum_array(raw) == [
        "UNCONTACTED",
        "EMAIL_SENT",
        "RESPONDED",
        "IN_PARTNERSHIP",
    ]


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
