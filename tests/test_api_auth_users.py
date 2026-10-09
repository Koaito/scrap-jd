"""
Tests cho GET /auth/users/{ss_user_id} (api/routers/auth_users.py::get_user,
thêm 10/2026) — thay cho việc frontend tải cả GET /auth/users rồi lọc theo id.

Cùng convention với tests/test_api_messages.py: gọi thẳng hàm route (không
qua HTTP), mock db_module bằng unittest.mock.patch. Route này không có
@limiter.limit nên không cần fake_request.

KHÔNG TEST ở đây: câu SQL thật của db.get_user_summary_by_id (cần Postgres
thật, xem tests/test_pg_integration.py) — chỉ test logic route + chống lệch
shape/cột giữa danh sách và chi tiết.
"""
import inspect
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from api import error_codes
from api.schemas import UserOut


def _row(role: str = "user", **overrides) -> dict:
    base = {
        "ss_user_id": str(uuid.uuid4()),
        "full_name": "Nguyễn Văn A",
        "email": "a@mindx.edu.vn",
        "role": role,
        "is_active": True,
        "must_change_password": False,
        "last_login_at": None,
        "created_at": datetime.now(timezone.utc),
        "phone": "0900000000",
        "track": "Python",
    }
    base.update(overrides)
    return base


@pytest.fixture
def staff():
    return {"sub": str(uuid.uuid4()), "email": "staff@mindx.edu.vn", "role": "ss_team"}


def test_get_user_found_returns_row(mock_conn, staff):
    row = _row()
    with patch("api.routers.auth_users.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        mock_db.get_user_summary_by_id.return_value = row

        from api.routers.auth_users import get_user

        result = get_user(ss_user_id=row["ss_user_id"], user=staff, conn=mock_conn)

        assert result == row
        mock_db.get_user_summary_by_id.assert_called_once_with(mock_conn, row["ss_user_id"])


def test_get_user_not_found_404(mock_conn, staff):
    with patch("api.routers.auth_users.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        mock_db.get_user_summary_by_id.return_value = None

        from api.routers.auth_users import get_user

        with pytest.raises(HTTPException) as exc_info:
            get_user(ss_user_id=str(uuid.uuid4()), user=staff, conn=mock_conn)

        assert exc_info.value.status_code == 404
        assert exc_info.value.detail["error_code"] == error_codes.USER_ACCOUNT_NOT_FOUND


def test_get_user_invalid_uuid_400_before_touching_db(mock_conn, staff):
    with patch("api.routers.auth_users.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = False

        from api.routers.auth_users import get_user

        with pytest.raises(HTTPException) as exc_info:
            get_user(ss_user_id="abc", user=staff, conn=mock_conn)

        assert exc_info.value.status_code == 400
        assert exc_info.value.detail["error_code"] == error_codes.USER_SS_USER_ID_INVALID_UUID
        assert exc_info.value.detail["params"] == {"value": "abc"}
        mock_db.get_user_summary_by_id.assert_not_called()


def test_get_user_requires_ss_team_role():
    """Soi dependency thật của tham số `user`: học viên bị 403, ss_team và
    admin đi qua — cùng quyền với GET /auth/users."""
    from api.routers.auth_users import get_user

    dep = inspect.signature(get_user).parameters["user"].default.dependency

    with pytest.raises(HTTPException) as exc_info:
        dep(user={"sub": "x", "role": "user"})
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error_code"] == error_codes.AUTH_INSUFFICIENT_ROLE

    assert dep(user={"sub": "x", "role": "ss_team"})["role"] == "ss_team"
    assert dep(user={"sub": "x", "role": "admin"})["role"] == "admin"


def test_get_user_response_model_is_user_out_and_never_leaks_password_hash():
    """response_model phải là UserOut (cùng shape phần tử của GET /auth/users)
    và UserOut không có field password_hash — dù db trả dư cột."""
    from api.routers import auth_users

    route = next(r for r in auth_users.router.routes if r.path == "/auth/users/{ss_user_id}" and "GET" in r.methods)
    assert route.response_model is UserOut
    assert "password_hash" not in UserOut.model_fields

    leaked = _row(password_hash="$argon2id$secret")
    validated = UserOut.model_validate(leaked)
    assert "password_hash" not in validated.model_dump()


def test_list_and_detail_share_same_columns():
    """Chống lệch shape: danh sách và chi tiết cùng dùng USER_SUMMARY_COLUMNS
    và mọi cột đó đều có mặt trong UserOut."""
    from scrapjd import db
    from scrapjd.db.auth import USER_SUMMARY_COLUMNS

    cols = [c.strip() for c in USER_SUMMARY_COLUMNS.split(",")]
    assert "password_hash" not in cols
    assert set(cols) <= set(UserOut.model_fields)
    assert set(UserOut.model_fields) <= set(cols)

    from unittest.mock import MagicMock

    for fn, arg in ((db.list_users, ()), (db.get_user_summary_by_id, (str(uuid.uuid4()),))):
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        fn(conn, *arg)
        sql = cur.execute.call_args[0][0]
        assert USER_SUMMARY_COLUMNS in sql
        assert "SELECT *" not in sql
