"""
Test cho POST /auth/refresh — tập trung vào GRACE PERIOD tái sử dụng
refresh token vừa xoay vòng (security.REFRESH_REUSE_GRACE_SECONDS, thêm
khi migrate Next.js — xem docstring hằng số này ở api/security.py và
docstring refresh() ở api/routers/auth_session.py).

Gọi route TRỰC TIẾP (không qua TestClient) và patch thẳng
api.routers.auth_session.db_module — cùng pattern với
tests/test_api_contacts.py (xem docstring ở đó), vì các route này có
@limiter.limit(...) cần 1 Request thật (dùng fixture fake_request từ
conftest.py), không phải MagicMock.

4 tình huống grace period đối chiếu với chat237.txt (bảng "Chặt/Nới"):
  - Race hợp lệ (2 request cùng gửi token A, token thay thế B còn sống)
    -> CHO QUA, cấp thêm 1 cặp mới.
  - Vừa logout (B đã bị revoke) -> CHẶN.
  - Vừa đổi mật khẩu / admin reset mật khẩu (B đã bị revoke qua
    revoke_all_refresh_tokens_for_user) -> CHẶN.
  - Vừa đăng nhập ở máy khác / single-session (B đã bị revoke qua cùng
    cơ chế) -> CHẶN.
  - Ngoài khung giờ REFRESH_REUSE_GRACE_SECONDS (dù B còn sống) -> CHẶN.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from api import error_codes, security
from api.routers.auth_session import refresh
from api.schemas import RefreshRequest


def make_user_record(ss_user_id: str, **overrides) -> dict:
    """Helper tạo dict giả lập row app_users — riêng cho file test này vì
    conftest.py hiện chưa có helper cho app_users (chỉ có các fixture
    user dạng payload JWT như ss_team_user)."""
    base = {
        "ss_user_id": uuid.UUID(ss_user_id) if isinstance(ss_user_id, str) else ss_user_id,
        "email": "staff@mindx.edu.vn",
        "role": "ss_team",
        "is_active": True,
        "active_session_id": str(uuid.uuid4()),
        "must_change_password": False,
    }
    base.update(overrides)
    return base


def make_token_record(
    refresh_token_id: str,
    ss_user_id: str,
    revoked_at=None,
    replaced_by_token_id: str | None = None,
    expires_at=None,
    **overrides,
) -> dict:
    """Helper tạo dict giả lập row auth_refresh_tokens."""
    base = {
        "refresh_token_id": uuid.UUID(refresh_token_id) if isinstance(refresh_token_id, str) else refresh_token_id,
        "ss_user_id": uuid.UUID(ss_user_id) if isinstance(ss_user_id, str) else ss_user_id,
        "token_hash": "x" * 64,
        "expires_at": expires_at or (datetime.now(timezone.utc) + timedelta(days=30)),
        "revoked_at": revoked_at,
        "replaced_by_token_id": uuid.UUID(replaced_by_token_id) if isinstance(replaced_by_token_id, str) else replaced_by_token_id,
        "user_agent": None,
        "ip_address": None,
        "created_at": datetime.now(timezone.utc),
    }
    base.update(overrides)
    return base


@pytest.fixture
def refresh_payload():
    return RefreshRequest(refresh_token="a" * 64)


class TestRefreshGracePeriodAllowed:
    """Race hợp lệ — token thay thế B còn sống, trong khung grace -> cho qua."""

    def test_reissues_new_pair_when_replacement_alive_within_grace(
        self, mock_conn, fake_request, refresh_payload
    ):
        user_id = str(uuid.uuid4())
        token_a_id = str(uuid.uuid4())
        token_b_id = str(uuid.uuid4())
        user = make_user_record(user_id)

        token_a = make_token_record(
            token_a_id, user_id,
            revoked_at=datetime.now(timezone.utc) - timedelta(seconds=2),  # trong 10s
            replaced_by_token_id=token_b_id,
        )
        token_b = make_token_record(token_b_id, user_id, revoked_at=None)

        with patch("api.routers.auth_session.db_module") as mock_db:
            mock_db.get_refresh_token_by_hash.return_value = token_a
            mock_db.get_refresh_token_by_id.return_value = token_b
            mock_db.get_user_by_id.return_value = user

            result = refresh(refresh_payload, fake_request, conn=mock_conn)

        assert result.access_token
        assert result.refresh_token
        # Không được revoke B, không được thu hồi toàn bộ token của user.
        mock_db.revoke_all_refresh_tokens_for_user.assert_not_called()
        mock_db.revoke_refresh_token.assert_not_called()
        mock_conn.commit.assert_called_once()

    def test_grace_boundary_just_inside_limit_still_allowed(
        self, mock_conn, fake_request, refresh_payload
    ):
        """revoked_at chỉ 0.1s trước khi hết khung
        REFRESH_REUSE_GRACE_SECONDS -> vẫn còn nằm trong khung, cho qua.
        (Không test đúng ranh giới bằng =, vì thời gian trôi qua giữa
        lúc tạo revoked_at trong test và lúc refresh() tự tính lại
        datetime.now() khiến test đúng-ranh-giới dễ flaky theo tốc độ
        máy chạy — 0.1s đủ ổn định mà vẫn xác nhận đúng nhánh <=.)"""
        user_id = str(uuid.uuid4())
        token_a_id = str(uuid.uuid4())
        token_b_id = str(uuid.uuid4())
        user = make_user_record(user_id)

        token_a = make_token_record(
            token_a_id, user_id,
            revoked_at=datetime.now(timezone.utc) - timedelta(
                seconds=security.REFRESH_REUSE_GRACE_SECONDS - 0.1
            ),
            replaced_by_token_id=token_b_id,
        )
        token_b = make_token_record(token_b_id, user_id, revoked_at=None)

        with patch("api.routers.auth_session.db_module") as mock_db:
            mock_db.get_refresh_token_by_hash.return_value = token_a
            mock_db.get_refresh_token_by_id.return_value = token_b
            mock_db.get_user_by_id.return_value = user

            result = refresh(refresh_payload, fake_request, conn=mock_conn)

        assert result.access_token
        mock_db.revoke_all_refresh_tokens_for_user.assert_not_called()


class TestRefreshGracePeriodBlocked:
    """4 tình huống phải bị chặn dù nằm trong khung grace."""

    def test_blocked_when_replacement_also_revoked_logout(
        self, mock_conn, fake_request, refresh_payload
    ):
        """Mô phỏng vừa logout: token B (thay thế A) đã bị revoke
        (logout() revoke đúng token hiện tại) -> chặn, thu hồi hết."""
        user_id = str(uuid.uuid4())
        token_a_id = str(uuid.uuid4())
        token_b_id = str(uuid.uuid4())

        token_a = make_token_record(
            token_a_id, user_id,
            revoked_at=datetime.now(timezone.utc) - timedelta(seconds=2),
            replaced_by_token_id=token_b_id,
        )
        token_b = make_token_record(
            token_b_id, user_id,
            revoked_at=datetime.now(timezone.utc) - timedelta(seconds=1),  # đã bị revoke
        )

        with patch("api.routers.auth_session.db_module") as mock_db:
            mock_db.get_refresh_token_by_hash.return_value = token_a
            mock_db.get_refresh_token_by_id.return_value = token_b
            mock_db.revoke_all_refresh_tokens_for_user.return_value = 2

            with pytest.raises(HTTPException) as exc_info:
                refresh(refresh_payload, fake_request, conn=mock_conn)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error_code"] == error_codes.AUTH_REFRESH_TOKEN_ALREADY_REVOKED
        mock_db.revoke_all_refresh_tokens_for_user.assert_called_once_with(mock_conn, user_id)

    def test_blocked_when_replacement_also_revoked_password_change(
        self, mock_conn, fake_request, refresh_payload
    ):
        """Mô phỏng vừa đổi mật khẩu / admin reset mật khẩu: B cũng đã bị
        revoke qua revoke_all_refresh_tokens_for_user() -> chặn. Cùng
        đường code với test logout ở trên (server không phân biệt được
        LÝ DO B bị revoke, chỉ cần biết B đã revoked_at != NULL), viết
        riêng để tài liệu hoá rõ từng tình huống trong bảng chat237.txt."""
        user_id = str(uuid.uuid4())
        token_a_id = str(uuid.uuid4())
        token_b_id = str(uuid.uuid4())

        token_a = make_token_record(
            token_a_id, user_id,
            revoked_at=datetime.now(timezone.utc) - timedelta(seconds=3),
            replaced_by_token_id=token_b_id,
        )
        token_b = make_token_record(
            token_b_id, user_id,
            revoked_at=datetime.now(timezone.utc) - timedelta(seconds=2),
        )

        with patch("api.routers.auth_session.db_module") as mock_db:
            mock_db.get_refresh_token_by_hash.return_value = token_a
            mock_db.get_refresh_token_by_id.return_value = token_b
            mock_db.revoke_all_refresh_tokens_for_user.return_value = 1

            with pytest.raises(HTTPException) as exc_info:
                refresh(refresh_payload, fake_request, conn=mock_conn)

        assert exc_info.value.status_code == 401
        assert exc_info.value.detail["error_code"] == error_codes.AUTH_REFRESH_TOKEN_ALREADY_REVOKED

    def test_blocked_when_no_replacement_token(
        self, mock_conn, fake_request, refresh_payload
    ):
        """Không có replaced_by_token_id (token cũ trước khi có cơ chế
        này, hoặc bị revoke qua đường khác không nối token thay thế) ->
        không có gì để kiểm tra "còn sống" -> chặn như cũ."""
        user_id = str(uuid.uuid4())
        token_a_id = str(uuid.uuid4())

        token_a = make_token_record(
            token_a_id, user_id,
            revoked_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            replaced_by_token_id=None,
        )

        with patch("api.routers.auth_session.db_module") as mock_db:
            mock_db.get_refresh_token_by_hash.return_value = token_a
            mock_db.revoke_all_refresh_tokens_for_user.return_value = 1

            with pytest.raises(HTTPException) as exc_info:
                refresh(refresh_payload, fake_request, conn=mock_conn)

        assert exc_info.value.status_code == 401
        mock_db.get_refresh_token_by_id.assert_not_called()

    def test_blocked_when_outside_grace_window(
        self, mock_conn, fake_request, refresh_payload
    ):
        """B còn sống nhưng A đã bị revoke quá lâu (ngoài khung
        REFRESH_REUSE_GRACE_SECONDS) -> chặn, không tra B nữa (đỡ 1
        lượt query khi chắc chắn sẽ chặn)."""
        user_id = str(uuid.uuid4())
        token_a_id = str(uuid.uuid4())
        token_b_id = str(uuid.uuid4())

        token_a = make_token_record(
            token_a_id, user_id,
            revoked_at=datetime.now(timezone.utc) - timedelta(
                seconds=security.REFRESH_REUSE_GRACE_SECONDS + 5
            ),
            replaced_by_token_id=token_b_id,
        )

        with patch("api.routers.auth_session.db_module") as mock_db:
            mock_db.get_refresh_token_by_hash.return_value = token_a
            mock_db.revoke_all_refresh_tokens_for_user.return_value = 1

            with pytest.raises(HTTPException) as exc_info:
                refresh(refresh_payload, fake_request, conn=mock_conn)

        assert exc_info.value.status_code == 401
        mock_db.get_refresh_token_by_id.assert_not_called()

    def test_blocked_when_replacement_row_missing(
        self, mock_conn, fake_request, refresh_payload
    ):
        """replaced_by_token_id có giá trị nhưng tra không ra dòng nào
        (trường hợp cực hiếm/dữ liệu hỏng) -> chặn, không crash."""
        user_id = str(uuid.uuid4())
        token_a_id = str(uuid.uuid4())
        token_b_id = str(uuid.uuid4())

        token_a = make_token_record(
            token_a_id, user_id,
            revoked_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            replaced_by_token_id=token_b_id,
        )

        with patch("api.routers.auth_session.db_module") as mock_db:
            mock_db.get_refresh_token_by_hash.return_value = token_a
            mock_db.get_refresh_token_by_id.return_value = None
            mock_db.revoke_all_refresh_tokens_for_user.return_value = 1

            with pytest.raises(HTTPException) as exc_info:
                refresh(refresh_payload, fake_request, conn=mock_conn)

        assert exc_info.value.status_code == 401


class TestRefreshGracePeriodEdgeCases:
    def test_blocked_when_user_inactive_during_grace_reissue(
        self, mock_conn, fake_request, refresh_payload
    ):
        """Nằm trong nhánh grace hợp lệ, nhưng tài khoản đã bị vô hiệu
        hoá giữa chừng -> vẫn phải chặn (403), không cấp token mới."""
        user_id = str(uuid.uuid4())
        token_a_id = str(uuid.uuid4())
        token_b_id = str(uuid.uuid4())
        user = make_user_record(user_id, is_active=False)

        token_a = make_token_record(
            token_a_id, user_id,
            revoked_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            replaced_by_token_id=token_b_id,
        )
        token_b = make_token_record(token_b_id, user_id, revoked_at=None)

        with patch("api.routers.auth_session.db_module") as mock_db:
            mock_db.get_refresh_token_by_hash.return_value = token_a
            mock_db.get_refresh_token_by_id.return_value = token_b
            mock_db.get_user_by_id.return_value = user

            with pytest.raises(HTTPException) as exc_info:
                refresh(refresh_payload, fake_request, conn=mock_conn)

        assert exc_info.value.status_code == 403
        assert exc_info.value.detail["error_code"] == error_codes.AUTH_ACCOUNT_INACTIVE

    def test_self_heals_null_session_id_during_grace_reissue(
        self, mock_conn, fake_request, refresh_payload
    ):
        """active_session_id NULL (user cũ từ trước migration
        single-session) ngay trong nhánh grace -> vẫn phải tự sinh
        session_id mới như nhánh refresh bình thường, không crash."""
        user_id = str(uuid.uuid4())
        token_a_id = str(uuid.uuid4())
        token_b_id = str(uuid.uuid4())
        user = make_user_record(user_id, active_session_id=None)

        token_a = make_token_record(
            token_a_id, user_id,
            revoked_at=datetime.now(timezone.utc) - timedelta(seconds=1),
            replaced_by_token_id=token_b_id,
        )
        token_b = make_token_record(token_b_id, user_id, revoked_at=None)

        with patch("api.routers.auth_session.db_module") as mock_db:
            mock_db.get_refresh_token_by_hash.return_value = token_a
            mock_db.get_refresh_token_by_id.return_value = token_b
            mock_db.get_user_by_id.return_value = user

            result = refresh(refresh_payload, fake_request, conn=mock_conn)

        assert result.access_token
        mock_db.set_active_session_id.assert_called_once()
        called_args = mock_db.set_active_session_id.call_args[0]
        assert called_args[0] is mock_conn
        assert called_args[1] == user_id


class TestRefreshNormalFlowUnaffected:
    """Đảm bảo nhánh refresh bình thường (token còn sống, chưa từng bị
    revoke) không bị ảnh hưởng bởi thay đổi grace period."""

    def test_normal_refresh_still_works(self, mock_conn, fake_request, refresh_payload):
        user_id = str(uuid.uuid4())
        token_a_id = str(uuid.uuid4())
        new_token_id = str(uuid.uuid4())
        user = make_user_record(user_id)

        token_a = make_token_record(token_a_id, user_id, revoked_at=None)
        new_token_row = make_token_record(new_token_id, user_id, revoked_at=None)

        with patch("api.routers.auth_session.db_module") as mock_db:
            mock_db.get_refresh_token_by_hash.side_effect = [token_a, new_token_row]
            mock_db.get_user_by_id.return_value = user

            result = refresh(refresh_payload, fake_request, conn=mock_conn)

        assert result.access_token
        assert result.refresh_token
        mock_db.revoke_refresh_token.assert_called_once()
        mock_db.get_refresh_token_by_id.assert_not_called()
