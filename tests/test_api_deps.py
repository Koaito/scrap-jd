"""
Test cho get_current_user() / require_role() ở api/deps.py — tập trung
vào bản sửa Phần 1 mục 3.10 của plan migrate Next.js: role/is_active
phải lấy từ DB (user_row) ở request hiện tại, không phải giá trị đóng
băng trong JWT payload lúc phát hành token.

Trước bản sửa: admin đổi role / khoá 1 tài khoản qua /staff-accounts
không có hiệu lực ngay — tài khoản đó vẫn authorize theo role CŨ (hoặc
vẫn qua được dù is_active=false) tới khi access token tự hết hạn (tối
đa 30 phút). Các test dưới đây chặn việc revert nhầm bản sửa đó.

Gọi hàm TRỰC TIẾP (không qua TestClient), patch thẳng
api.deps.db_module.get_user_by_id — cùng pattern với
tests/test_api_auth.py. Token được ký THẬT bằng security.create_access_token
(JWT_SECRET_KEY lấy từ môi trường, xem .github/workflows/test.yml) để
đi qua đúng decode_access_token(), không mock lớp giải mã.
"""
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from scrapjd.api import error_codes, security
from scrapjd.api.deps import get_current_user, require_role


def _make_token(user_id: str, *, role: str = "ss_team", session_id: str) -> str:
    return security.create_access_token(
        ss_user_id=user_id,
        role=role,
        email="staff@mindx.edu.vn",
        session_id=session_id,
    )


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _user_row(user_id: str, session_id: str, **overrides) -> dict:
    base = {
        "ss_user_id": uuid.UUID(user_id),
        "email": "staff@mindx.edu.vn",
        "role": "ss_team",
        "is_active": True,
        "active_session_id": session_id,
    }
    base.update(overrides)
    return base


@pytest.fixture
def user_id() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def session_id() -> str:
    return str(uuid.uuid4())


class TestGetCurrentUserInactive:
    def test_blocks_inactive_account_even_with_valid_token(self, user_id, session_id):
        """Token còn hạn, sid khớp, nhưng admin vừa khoá tài khoản ->
        403 AUTH_ACCOUNT_INACTIVE ngay, không đợi token hết hạn."""
        token = _make_token(user_id, session_id=session_id)
        row = _user_row(user_id, session_id, is_active=False)

        with patch("scrapjd.api.deps.db_module") as mock_db:
            mock_db.get_user_by_id.return_value = row
            with pytest.raises(HTTPException) as exc_info:
                get_current_user(_creds(token), conn=MagicMock())

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["error_code"] == error_codes.AUTH_ACCOUNT_INACTIVE

    def test_allows_active_account(self, user_id, session_id):
        token = _make_token(user_id, session_id=session_id)
        row = _user_row(user_id, session_id, is_active=True)

        with patch("scrapjd.api.deps.db_module") as mock_db:
            mock_db.get_user_by_id.return_value = row
            result = get_current_user(_creds(token), conn=MagicMock())

        assert result["sub"] == user_id
        assert result["is_active"] is True


class TestGetCurrentUserRoleFromDb:
    def test_role_comes_from_db_not_jwt(self, user_id, session_id):
        """JWT ký role='admin' lúc phát hành, nhưng DB đã hạ xuống 'user'
        -> payload trả về PHẢI mang role='user' (giá trị mới nhất)."""
        token = _make_token(user_id, role="admin", session_id=session_id)
        row = _user_row(user_id, session_id, role="user")

        with patch("scrapjd.api.deps.db_module") as mock_db:
            mock_db.get_user_by_id.return_value = row
            result = get_current_user(_creds(token), conn=MagicMock())

        assert result["role"] == "user"
        # Các claim khác của JWT vẫn được giữ nguyên.
        assert result["sub"] == user_id
        assert result["sid"] == session_id

    def test_demoted_admin_is_rejected_by_require_role(self, user_id, session_id):
        """Đầu-cuối: token cũ mang role='admin' nhưng DB đã hạ xuống
        'ss_team' -> require_role('admin') phải trả 403, không cho qua
        theo role cũ trong JWT."""
        token = _make_token(user_id, role="admin", session_id=session_id)
        row = _user_row(user_id, session_id, role="ss_team")

        with patch("scrapjd.api.deps.db_module") as mock_db:
            mock_db.get_user_by_id.return_value = row
            user = get_current_user(_creds(token), conn=MagicMock())

        with pytest.raises(HTTPException) as exc_info:
            require_role("admin")(user=user)

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["error_code"] == error_codes.AUTH_INSUFFICIENT_ROLE

    def test_promoted_user_is_accepted_by_require_role(self, user_id, session_id):
        """Chiều ngược lại: JWT cũ mang role='user', DB đã nâng lên
        'ss_team' -> require_role('ss_team') cho qua ngay."""
        token = _make_token(user_id, role="user", session_id=session_id)
        row = _user_row(user_id, session_id, role="ss_team")

        with patch("scrapjd.api.deps.db_module") as mock_db:
            mock_db.get_user_by_id.return_value = row
            user = get_current_user(_creds(token), conn=MagicMock())

        assert require_role("ss_team")(user=user)["role"] == "ss_team"


class TestGetCurrentUserSession:
    def test_session_replaced_when_sid_mismatch(self, user_id, session_id):
        """sid trong token khác active_session_id trong DB -> tài khoản
        vừa đăng nhập ở nơi khác -> 401 SESSION_REPLACED."""
        token = _make_token(user_id, session_id=session_id)
        row = _user_row(user_id, str(uuid.uuid4()))  # session khác

        with patch("scrapjd.api.deps.db_module") as mock_db:
            mock_db.get_user_by_id.return_value = row
            with pytest.raises(HTTPException) as exc_info:
                get_current_user(_creds(token), conn=MagicMock())

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error_code"] == error_codes.SESSION_REPLACED

    def test_session_revoked_when_active_session_cleared(self, user_id, session_id):
        """active_session_id = NULL (logout / đổi mật khẩu) -> 401
        SESSION_REVOKED, khác SESSION_REPLACED."""
        token = _make_token(user_id, session_id=session_id)
        row = _user_row(user_id, session_id, active_session_id=None)

        with patch("scrapjd.api.deps.db_module") as mock_db:
            mock_db.get_user_by_id.return_value = row
            with pytest.raises(HTTPException) as exc_info:
                get_current_user(_creds(token), conn=MagicMock())

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error_code"] == error_codes.SESSION_REVOKED

    def test_session_revoked_when_user_deleted(self, user_id, session_id):
        token = _make_token(user_id, session_id=session_id)

        with patch("scrapjd.api.deps.db_module") as mock_db:
            mock_db.get_user_by_id.return_value = None
            with pytest.raises(HTTPException) as exc_info:
                get_current_user(_creds(token), conn=MagicMock())

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error_code"] == error_codes.SESSION_REVOKED

    def test_inactive_check_runs_after_session_check(self, user_id, session_id):
        """Vừa sai sid vừa bị khoá -> ưu tiên trả lỗi session (401), giữ
        đúng thứ tự kiểm tra hiện tại trong get_current_user()."""
        token = _make_token(user_id, session_id=session_id)
        row = _user_row(user_id, str(uuid.uuid4()), is_active=False)

        with patch("scrapjd.api.deps.db_module") as mock_db:
            mock_db.get_user_by_id.return_value = row
            with pytest.raises(HTTPException) as exc_info:
                get_current_user(_creds(token), conn=MagicMock())

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error_code"] == error_codes.SESSION_REPLACED


class TestGetCurrentUserTokenErrors:
    def test_missing_credentials(self):
        with pytest.raises(HTTPException) as exc_info:
            get_current_user(None, conn=MagicMock())

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error_code"] == error_codes.MISSING_AUTH_HEADER

    def test_invalid_token(self):
        with pytest.raises(HTTPException) as exc_info:
            get_current_user(_creds("not-a-jwt"), conn=MagicMock())

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error_code"] == error_codes.TOKEN_EXPIRED
