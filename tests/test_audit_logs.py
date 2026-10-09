"""
Tests cho audit log — diff_changed_fields (db/audit_logs.py) và danh sách
action/entity hợp lệ của GET /audit-logs (api/routers/audit_logs.py).

Coverage:
- diff_changed_fields: số so bằng GIÁ TRỊ (Decimal('10000000.00') ==
  10000000 == 10000000.0) -> không báo thay đổi giả; số cũ Decimal được
  ghi thành số thật (int/float) thay vì chuỗi; kiểu khác giữ cách so cũ.
- _VALID_ACTION_TYPES luôn khớp ACTION_LOG_RULES (không lệch khi thêm
  action mới như BULK_IMPORT_*/EMAIL_TEMPLATE).
- GET /audit-logs nhận action/entity mới, vẫn trả 400 với giá trị lạ.
"""
from datetime import date
from decimal import Decimal
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from scrapjd import db as db_module
from scrapjd.db.audit_logs import diff_changed_fields


# ---------------------------------------------------------------------
# diff_changed_fields
# ---------------------------------------------------------------------

@pytest.mark.parametrize("old, new", [
    (Decimal("10000000.00"), 10000000),
    (Decimal("10000000"), 10000000.0),
    (Decimal("1.50"), 1.5),
    (10000000, 10000000),
    (None, None),
    ("abc", "abc"),
    (date(2026, 9, 1), date(2026, 9, 1)),
])
def test_diff_khong_bao_thay_doi_gia_khi_gia_tri_bang_nhau(old, new):
    assert diff_changed_fields({"f": old}, {"f": new}) == {}


def test_diff_ghi_so_cu_dang_so_that_khong_phai_chuoi():
    changes = diff_changed_fields({"salary_min": Decimal("10000000.00")}, {"salary_min": 12000000})
    assert changes == {"salary_min": {"old": 10000000, "new": 12000000}}
    assert isinstance(changes["salary_min"]["old"], int)


def test_diff_so_le_cu_ghi_thanh_float():
    changes = diff_changed_fields({"x": Decimal("1.25")}, {"x": 2})
    assert changes == {"x": {"old": 1.25, "new": 2}}


def test_diff_cu_la_none_moi_co_gia_tri_la_thay_doi():
    assert diff_changed_fields({"deadline": None}, {"deadline": date(2026, 10, 1)}) == {
        "deadline": {"old": None, "new": date(2026, 10, 1)}
    }


def test_diff_bool_khong_bi_coi_la_so():
    # True == 1 theo Python, nhưng đây là 2 giá trị khác nhau về nghĩa.
    assert diff_changed_fields({"f": True}, {"f": 1}) != {}


def test_diff_chi_xet_field_co_trong_payload():
    old = {"a": 1, "b": 2}
    assert diff_changed_fields(old, {"a": 1}) == {}
    assert diff_changed_fields(old, {"a": 5}) == {"a": {"old": 1, "new": 5}}


# ---------------------------------------------------------------------
# Danh sách action / entity hợp lệ
# ---------------------------------------------------------------------

def test_valid_action_types_khop_action_log_rules():
    from scrapjd.api.routers.audit_logs import _VALID_ACTION_TYPES
    assert _VALID_ACTION_TYPES == set(db_module.ACTION_LOG_RULES)
    for action in (
        "BULK_IMPORT_JOB", "BULK_IMPORT_COMPANY", "BULK_IMPORT_CONTACT",
        "CREATE_EMAIL_TEMPLATE", "UPDATE_EMAIL_TEMPLATE", "DELETE_EMAIL_TEMPLATE",
    ):
        assert action in _VALID_ACTION_TYPES


def test_valid_entity_types_co_email_template():
    from scrapjd.api.routers.audit_logs import _VALID_ENTITY_TYPES
    assert "EMAIL_TEMPLATE" in _VALID_ENTITY_TYPES


def _call_list(mock_conn, ss_team_user, **kwargs):
    from scrapjd.api.routers.audit_logs import list_audit_logs
    params = dict(
        view="auto", entity_type=None, company_id=None, actor_id=None,
        action_type=None, pending_note=None, limit=50, offset=0,
        user=ss_team_user, conn=mock_conn,
    )
    params.update(kwargs)
    return list_audit_logs(**params)


@pytest.mark.parametrize("kwargs", [
    {"action_type": "BULK_IMPORT_JOB"},
    {"action_type": "UPDATE_EMAIL_TEMPLATE"},
    {"entity_type": "EMAIL_TEMPLATE"},
])
def test_list_audit_logs_nhan_action_va_entity_moi(mock_conn, ss_team_user, kwargs):
    with patch("scrapjd.api.routers.audit_logs.db_module") as mock_db:
        mock_db.ACTION_LOG_RULES = db_module.ACTION_LOG_RULES
        mock_db.list_audit_logs.return_value = ([], 0)
        result = _call_list(mock_conn, ss_team_user, **kwargs)
        assert result.total == 0
        mock_db.list_audit_logs.assert_called_once()


@pytest.mark.parametrize("kwargs", [
    {"action_type": "NOT_A_REAL_ACTION"},
    {"entity_type": "NOT_A_REAL_ENTITY"},
])
def test_list_audit_logs_van_tra_400_voi_gia_tri_la(mock_conn, ss_team_user, kwargs):
    with patch("scrapjd.api.routers.audit_logs.db_module") as mock_db:
        mock_db.ACTION_LOG_RULES = db_module.ACTION_LOG_RULES
        with pytest.raises(HTTPException) as exc:
            _call_list(mock_conn, ss_team_user, **kwargs)
        assert exc.value.status_code == 400
