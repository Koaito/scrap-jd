"""
Mục 3, Pha A — test KHÔNG cần DB cho session mờ:

  - helper trong security.py (định dạng token, hash, tiền tố) và kiểm tra cấu hình hạn lúc import;
  - ánh xạ mã lỗi của get_current_user() cho session token (DB giả);
  - khoá rate limit theo phiên;
  - job dọn phiên hết hạn;
  - schema đăng nhập/đăng xuất.

Hành vi trên Postgres thật (hạn, thu hồi, commit, song song) nằm ở tests/test_pg_auth_sessions.py.
"""

import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import ValidationError

from scrapjd.api import error_codes, security
from scrapjd.api.deps import get_current_user
from scrapjd.api.rate_limit import get_user_id_or_ip
from scrapjd.api.schemas import LogoutRequest, TokenPairOut
from scrapjd.api.services import session_cleanup

from conftest import make_fake_request

_REPO = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------
# security.py
# ----------------------------------------------------------------------
class TestSessionTokenHelpers:
    def test_dinh_dang_token(self):
        token = security.generate_session_token()
        assert token.startswith("s1.")
        assert len(token) >= 3 + 64  # 48 byte urlsafe = 64 ký tự, đủ entropy
        assert security.is_session_token(token)

    def test_token_khong_trung_nhau(self):
        assert len({security.generate_session_token() for _ in range(500)}) == 500

    def test_hash_la_sha256_hex_va_on_dinh(self):
        token = security.generate_session_token()
        digest = security.hash_session_token(token)
        assert len(digest) == 64 and digest == digest.lower()
        int(digest, 16)  # là hex hợp lệ
        assert digest == security.hash_session_token(token)
        assert token not in digest
        assert digest != security.hash_session_token(security.generate_session_token())

    def test_jwt_khong_bao_gio_bi_nhan_nham_la_session_token(self):
        access = security.create_access_token(
            ss_user_id=str(uuid.uuid4()), role="user", email="a@b.c", session_id=str(uuid.uuid4()))
        assert access.startswith("eyJ")
        assert not security.is_session_token(access)

    @pytest.mark.parametrize("value", ["", "S1.abc", "s1", "xs1.abc", "Bearer s1.abc"])
    def test_chuoi_khong_co_tien_to_dung_khong_phai_session_token(self, value):
        assert not security.is_session_token(value)


class TestSessionConfigValidation:
    """Cấu hình hạn sai phải làm ứng dụng DỪNG lúc import (fail-closed), không âm thầm dùng giá trị khác.
    Chạy trong subprocess vì kiểm tra nằm ở cấp module, import lại trong process test sẽ làm bẩn module."""

    def _import_with(self, **env):
        full = {**os.environ, "JWT_SECRET_KEY": "ci_placeholder", **env}
        return subprocess.run(
            [sys.executable, "-c",
             "from scrapjd.api import security as s; print(s.SESSION_ABSOLUTE_DAYS, s.SESSION_IDLE_DAYS)"],
            cwd=_REPO, env=full, capture_output=True, text=True, timeout=60,
        )

    def test_mac_dinh_30_va_7_ngay(self):
        env = {"SESSION_ABSOLUTE_DAYS": "", "SESSION_IDLE_DAYS": ""}
        r = self._import_with(**env)
        assert r.returncode == 0, r.stderr
        assert r.stdout.split() == ["30", "7"]

    def test_doc_duoc_tu_bien_moi_truong(self):
        r = self._import_with(SESSION_ABSOLUTE_DAYS="14", SESSION_IDLE_DAYS="3")
        assert r.returncode == 0, r.stderr
        assert r.stdout.split() == ["14", "3"]

    @pytest.mark.parametrize("env", [
        {"SESSION_ABSOLUTE_DAYS": "abc"},
        {"SESSION_ABSOLUTE_DAYS": "0"},
        {"SESSION_IDLE_DAYS": "-1"},
        {"SESSION_ABSOLUTE_DAYS": "5", "SESSION_IDLE_DAYS": "10"},  # hạn trượt dài hơn hạn tuyệt đối
    ])
    def test_gia_tri_sai_lam_ung_dung_dung_ngay(self, env):
        r = self._import_with(**env)
        assert r.returncode != 0
        assert "RuntimeError" in r.stderr


# ----------------------------------------------------------------------
# get_current_user — nhánh session token (DB giả)
# ----------------------------------------------------------------------
def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _row(**over):
    base = {
        "session_id": uuid.uuid4(), "ss_user_id": uuid.uuid4(),
        "revoked_at": None, "revoked_reason": None,
        "absolute_expired": False, "idle_expired": False, "needs_touch": False,
        "email": "u@mindx.edu.vn", "role": "user", "is_active": True,
    }
    base.update(over)
    return base


def _run(row, token=None):
    """Chạy get_current_user với get_session_for_auth trả `row`. Trả (kết quả|HTTPException, db mock, conn)."""
    token = token or security.generate_session_token()
    conn = MagicMock()
    with patch("scrapjd.api.deps.db_module") as db_mock:
        db_mock.get_session_for_auth.return_value = row
        try:
            result = get_current_user(_creds(token), conn)
        except HTTPException as exc:
            result = exc
    return result, db_mock, conn


class TestGetCurrentUserWithSessionToken:
    def test_phien_hop_le_tra_dung_dang_dict_cu(self):
        row = _row(role="ss_team")
        user, _, _ = _run(row)
        assert user["sub"] == str(row["ss_user_id"])
        assert user["role"] == "ss_team" and user["email"] == "u@mindx.edu.vn"
        assert user["is_active"] is True and user["sid"] == str(row["session_id"])

    def test_tra_theo_hash_chu_khong_phai_token_tho(self):
        token = security.generate_session_token()
        _, db_mock, _ = _run(_row(), token=token)
        args = db_mock.get_session_for_auth.call_args
        assert args.args[1] == security.hash_session_token(token)
        assert token not in repr(args)

    def test_khong_co_dong_nao_la_token_expired(self):
        err, _, _ = _run(None)
        assert err.status_code == 401 and err.detail["error_code"] == error_codes.TOKEN_EXPIRED

    @pytest.mark.parametrize("reason,code", [
        ("replaced", error_codes.SESSION_REPLACED),
        ("logout", error_codes.SESSION_REVOKED),
        ("password_changed", error_codes.SESSION_REVOKED),
        ("admin", error_codes.SESSION_REVOKED),
    ])
    def test_anh_xa_ly_do_thu_hoi_sang_ma_loi(self, reason, code):
        err, _, _ = _run(_row(revoked_at=datetime.now(timezone.utc), revoked_reason=reason))
        assert err.status_code == 401 and err.detail["error_code"] == code

    def test_thu_hoi_do_khoa_tai_khoan_giu_ma_403_cu_neu_van_bi_khoa(self):
        err, _, _ = _run(_row(revoked_at=datetime.now(timezone.utc), revoked_reason="inactive", is_active=False))
        assert err.status_code == 403 and err.detail["error_code"] == error_codes.AUTH_ACCOUNT_INACTIVE

    def test_thu_hoi_do_khoa_nhung_da_mo_khoa_la_session_revoked(self):
        err, _, _ = _run(_row(revoked_at=datetime.now(timezone.utc), revoked_reason="inactive", is_active=True))
        assert err.status_code == 401 and err.detail["error_code"] == error_codes.SESSION_REVOKED

    @pytest.mark.parametrize("flag", ["absolute_expired", "idle_expired"])
    def test_het_han_giu_chuoi_token_expired_cu(self, flag):
        err, _, _ = _run(_row(**{flag: True}))
        assert err.status_code == 401 and err.detail["error_code"] == error_codes.TOKEN_EXPIRED
        assert error_codes.TOKEN_EXPIRED == "token_expired"  # frontend cũ rẽ nhánh theo đúng chuỗi này

    def test_tai_khoan_bi_khoa_la_403(self):
        err, _, _ = _run(_row(is_active=False))
        assert err.status_code == 403 and err.detail["error_code"] == error_codes.AUTH_ACCOUNT_INACTIVE

    def test_thu_hoi_duoc_xet_truoc_het_han(self):
        err, _, _ = _run(_row(
            revoked_at=datetime.now(timezone.utc), revoked_reason="replaced", idle_expired=True))
        assert err.detail["error_code"] == error_codes.SESSION_REPLACED

    def test_ghi_last_seen_va_commit_khi_can(self):
        row = _row(needs_touch=True)
        _, db_mock, conn = _run(row)
        db_mock.touch_session.assert_called_once()
        assert db_mock.touch_session.call_args.args[1] == str(row["session_id"])
        conn.commit.assert_called_once()

    def test_khong_ghi_khi_chua_toi_nguong(self):
        _, db_mock, conn = _run(_row(needs_touch=False))
        db_mock.touch_session.assert_not_called()
        conn.commit.assert_not_called()

    @pytest.mark.parametrize("row", [
        None,
        _row(revoked_at=datetime.now(timezone.utc), revoked_reason="logout", needs_touch=True),
        _row(absolute_expired=True, needs_touch=True),
        _row(idle_expired=True, needs_touch=True),
        _row(is_active=False, needs_touch=True),
    ])
    def test_phien_bi_tu_choi_thi_khong_bao_gio_duoc_gia_han(self, row):
        err, db_mock, conn = _run(row)
        assert isinstance(err, HTTPException)
        db_mock.touch_session.assert_not_called()
        conn.commit.assert_not_called()

    def test_token_hinh_session_nhung_gia_van_ra_token_expired_khong_roi_ve_nhanh_jwt(self):
        with patch("scrapjd.api.deps.security.decode_access_token") as decode:
            err, _, _ = _run(None, token="s1.khong-phai-token-that")
        decode.assert_not_called()
        assert err.detail["error_code"] == error_codes.TOKEN_EXPIRED

    def test_jwt_van_di_nhanh_cu_khong_cham_bang_session(self):
        jwt = security.create_access_token(
            ss_user_id=str(uuid.uuid4()), role="user", email="a@b.c", session_id=str(uuid.uuid4()))
        conn = MagicMock()
        with patch("scrapjd.api.deps.db_module") as db_mock:
            db_mock.get_user_by_id.return_value = None
            with pytest.raises(HTTPException):
                get_current_user(_creds(jwt), conn)
        db_mock.get_session_for_auth.assert_not_called()


# ----------------------------------------------------------------------
# rate limit
# ----------------------------------------------------------------------
class TestRateLimitKeyForSessionToken:
    def _req(self, token):
        return make_fake_request({"authorization": f"Bearer {token}"})

    def test_khoa_theo_phien_va_khong_chua_token_tho(self):
        token = security.generate_session_token()
        key = get_user_id_or_ip(self._req(token))
        assert key == f"session:{security.hash_session_token(token)[:16]}"
        assert token not in key and token[3:] not in key

    def test_hai_phien_khac_nhau_cho_hai_khoa_khac_nhau(self):
        a = get_user_id_or_ip(self._req(security.generate_session_token()))
        b = get_user_id_or_ip(self._req(security.generate_session_token()))
        assert a != b

    def test_cung_phien_cho_cung_khoa_du_gui_tu_ip_khac(self):
        token = security.generate_session_token()
        assert get_user_id_or_ip(self._req(token)) == get_user_id_or_ip(self._req(token))


# ----------------------------------------------------------------------
# job dọn phiên
# ----------------------------------------------------------------------
class TestSessionCleanup:
    def test_xoa_commit_va_tra_connection(self):
        conn = MagicMock()
        with patch.object(session_cleanup, "db_module") as db_mock:
            db_mock.get_pooled_connection.return_value = conn
            db_mock.delete_stale_sessions.return_value = 3
            session_cleanup.run_session_cleanup_once()
        db_mock.delete_stale_sessions.assert_called_once_with(conn)
        conn.commit.assert_called_once()
        db_mock.release_connection.assert_called_once_with(conn)

    def test_loi_khi_don_khong_lam_sap_scheduler_va_van_tra_connection(self):
        conn = MagicMock()
        with patch.object(session_cleanup, "db_module") as db_mock:
            db_mock.get_pooled_connection.return_value = conn
            db_mock.delete_stale_sessions.side_effect = RuntimeError("DB chết")
            session_cleanup.run_session_cleanup_once()  # không được ném ra ngoài
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()
        db_mock.release_connection.assert_called_once_with(conn)

    def test_duoc_dang_ky_trong_scheduler_cua_app(self):
        src = (_REPO / "scrapjd" / "api" / "app.py").read_text(encoding="utf-8")
        assert "run_session_cleanup_once" in src
        assert "SESSION_CLEANUP_INTERVAL_MINUTES" in src


# ----------------------------------------------------------------------
# schema
# ----------------------------------------------------------------------
class TestSchemas:
    def test_response_dang_nhap_co_session_token_va_van_giu_cap_cu(self):
        out = TokenPairOut(
            access_token="a", refresh_token="r", session_token="s1.x",
            session_expires_at=datetime.now(timezone.utc))
        dumped = out.model_dump()
        assert {"access_token", "refresh_token", "token_type", "must_change_password",
                "session_token", "session_expires_at"} <= set(dumped)

    def test_logout_body_tuy_chon(self):
        assert LogoutRequest().refresh_token is None
        assert LogoutRequest(refresh_token="abc").refresh_token == "abc"

    def test_logout_tu_choi_chuoi_rong_va_field_la(self):
        with pytest.raises(ValidationError):
            LogoutRequest(refresh_token="")
        with pytest.raises(ValidationError):
            LogoutRequest(**{"session_token": "s1.x"})
