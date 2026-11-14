"""
Mục 3, Pha A trên POSTGRES THẬT: session mờ lưu DB (bảng auth_sessions, migration 0045).

Ba nhóm test:
  1. Ràng buộc schema (CHECK/UNIQUE) — chặn lỗi ở chính DB, không chỉ ở code.
  2. Hàm db.sessions (hạn, throttle, thu hồi, dọn) — chạy SQL thật, kể cả bẫy "now() đứng yên" trong
     transaction mở dài.
  3. Luồng đầy đủ qua chính các route và get_current_user: đăng nhập, đăng nhập lần hai (replaced), logout bằng
     session token và bằng refresh token, đổi mật khẩu, đặt lại mật khẩu, khoá tài khoản, đổi role có hiệu lực
     ngay, JWT cũ vẫn chạy song song (Pha A không được phá client cũ), và gọi song song nhiều request.

Gọi thẳng hàm route (cùng convention tests/test_pg_application_routes.py). Cần TEST_DATABASE_URL trỏ tới DB RIÊNG
có chữ "test" (fixture DROP SCHEMA public). Không đặt thì cả file bị bỏ qua.
"""

import os
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import psycopg2
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scrapjd import db
from scrapjd.api import error_codes, security
from scrapjd.api.app import app
from scrapjd.api.deps import get_current_user, get_db
from scrapjd.api.rate_limit import limiter
from scrapjd.api.routers import auth_registration, auth_session, auth_users
from scrapjd.api.schemas import (
    ChangePasswordRequest, LoginRequest, LogoutRequest, RefreshRequest,
    ResetPasswordRequest, UserActiveStatusUpdate,
)

from conftest import make_fake_request

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")
_PASSWORD = "MatKhau-Test-123"


# ----------------------------------------------------------------------
# Fixture
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def pg_conn():
    dbname = urlparse(TEST_DATABASE_URL).path.lstrip("/")
    if "test" not in dbname.lower():
        pytest.fail(
            f"Từ chối chạy: database '{dbname}' không chứa 'test' trong tên "
            "(fixture này DROP SCHEMA public CASCADE)."
        )
    conn = psycopg2.connect(TEST_DATABASE_URL)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    conn.commit()
    db.apply_schema(conn, _SCHEMA_PATH)
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture(autouse=True)
def _clean(pg_conn, monkeypatch):
    """Mỗi test bắt đầu sạch. Tắt rate limit: login() có 20/phút theo IP và mọi test dùng chung 127.0.0.1."""
    monkeypatch.setattr(limiter, "enabled", False)
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM auth_sessions")
        cur.execute("DELETE FROM auth_refresh_tokens")
        cur.execute("DELETE FROM app_users")
    pg_conn.commit()
    yield
    pg_conn.rollback()


@pytest.fixture
def new_user(pg_conn):
    """Factory tạo tài khoản đã xác thực email, mật khẩu = _PASSWORD, must_change_password=False."""

    def _make(role: str = "user", **kw) -> dict:
        email = f"{uuid.uuid4().hex[:10]}@mindx.edu.vn"
        user_id = db.create_user(
            pg_conn, full_name="Người Test", email=email,
            password_hash=security.hash_password(_PASSWORD), role=role,
            must_change_password=False,
        )
        with pg_conn.cursor() as cur:
            cur.execute("UPDATE app_users SET email_verified = true WHERE ss_user_id = %s", (user_id,))
        pg_conn.commit()
        return {"id": user_id, "email": email, "role": role}

    return _make


def _login(conn, email):
    return auth_session.login(
        payload=LoginRequest(email=email, password=_PASSWORD),
        request=make_fake_request({"user-agent": "pytest-agent/1.0"}),
        conn=conn,
    )


def _creds(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _auth(conn, token: str) -> dict:
    return get_current_user(_creds(token), conn)


def _code(conn, token: str) -> str:
    """error_code mà get_current_user ném ra với token này."""
    with pytest.raises(HTTPException) as exc:
        _auth(conn, token)
    return exc.value.detail["error_code"]


def _hash(token: str) -> str:
    return security.hash_session_token(token)


def _raw_session(conn, ss_user_id, **cols):
    """Chèn thẳng một dòng auth_sessions (bỏ qua login) để dựng trạng thái cụ thể. Trả (token, session_id)."""
    token = security.generate_session_token()
    session_id = str(uuid.uuid4())
    db.create_session(
        conn, session_id=session_id, ss_user_id=ss_user_id, token_hash=_hash(token),
        absolute_days=30, idle_days=7,
    )
    if cols:
        sets = ", ".join(f"{k} = {v}" for k, v in cols.items())
        with conn.cursor() as cur:
            cur.execute(f"UPDATE auth_sessions SET {sets} WHERE session_id = %s", (session_id,))
    conn.commit()
    return token, session_id


# ----------------------------------------------------------------------
# 1. Ràng buộc schema
# ----------------------------------------------------------------------
class TestSchemaConstraints:
    def _insert(self, conn, user_id, **over):
        row = {
            "token_hash": "a" * 64, "expires_at": "clock_timestamp() + interval '30 days'",
            "idle_expires_at": "clock_timestamp() + interval '7 days'",
            "revoked_at": "NULL", "revoked_reason": "NULL",
        }
        row.update(over)
        sql = (
            "INSERT INTO auth_sessions (ss_user_id, token_hash, expires_at, idle_expires_at, "
            "revoked_at, revoked_reason) VALUES (%s, %s, "
            f"{row['expires_at']}, {row['idle_expires_at']}, {row['revoked_at']}, "
            + (f"'{row['revoked_reason']}'" if row["revoked_reason"] != "NULL" else "NULL") + ")"
        )
        with conn.cursor() as cur:
            cur.execute(sql, (user_id, row["token_hash"]))

    def test_hop_le_duoc_chap_nhan(self, pg_conn, new_user):
        self._insert(pg_conn, new_user()["id"])
        pg_conn.rollback()

    @pytest.mark.parametrize("bad_hash", [
        "s1." + "x" * 40,   # token thô
        "A" * 64,           # chữ hoa
        "a" * 63,           # thiếu ký tự
        "g" * 64,           # đủ độ dài nhưng không phải hex
    ])
    def test_token_hash_khong_phai_sha256_hex_bi_chan(self, pg_conn, new_user, bad_hash):
        with pytest.raises(psycopg2.errors.CheckViolation):
            self._insert(pg_conn, new_user()["id"], token_hash=bad_hash)
        pg_conn.rollback()

    def test_thu_hoi_phai_co_ca_thoi_diem_va_ly_do(self, pg_conn, new_user):
        uid = new_user()["id"]
        with pytest.raises(psycopg2.errors.CheckViolation):
            self._insert(pg_conn, uid, revoked_at="clock_timestamp()")  # thiếu lý do
        pg_conn.rollback()
        with pytest.raises(psycopg2.errors.CheckViolation):
            self._insert(pg_conn, uid, revoked_reason="logout")  # thiếu thời điểm
        pg_conn.rollback()

    def test_ly_do_thu_hoi_ngoai_danh_sach_bi_chan(self, pg_conn, new_user):
        with pytest.raises(psycopg2.errors.CheckViolation):
            self._insert(pg_conn, new_user()["id"], revoked_at="clock_timestamp()", revoked_reason="tuy_tien")
        pg_conn.rollback()

    def test_han_truot_khong_duoc_dai_hon_han_tuyet_doi(self, pg_conn, new_user):
        with pytest.raises(psycopg2.errors.CheckViolation):
            self._insert(pg_conn, new_user()["id"], idle_expires_at="clock_timestamp() + interval '31 days'")
        pg_conn.rollback()

    def test_token_hash_la_duy_nhat(self, pg_conn, new_user):
        uid = new_user()["id"]
        self._insert(pg_conn, uid)
        with pytest.raises(psycopg2.errors.UniqueViolation):
            self._insert(pg_conn, uid)
        pg_conn.rollback()


# ----------------------------------------------------------------------
# 2. Hàm db.sessions
# ----------------------------------------------------------------------
class TestSessionFunctions:
    def test_tao_phien_dat_dung_han_va_tra_ve_nguoi_dung(self, pg_conn, new_user):
        u = new_user("ss_team")
        token = security.generate_session_token()
        sid = str(uuid.uuid4())
        created = db.create_session(
            pg_conn, session_id=sid, ss_user_id=u["id"], token_hash=_hash(token),
            absolute_days=30, idle_days=7, user_agent="UA", ip_address="1.2.3.4",
        )
        pg_conn.commit()
        assert str(created["session_id"]) == sid
        span_abs = created["expires_at"] - datetime.now(timezone.utc)
        span_idle = created["idle_expires_at"] - datetime.now(timezone.utc)
        assert timedelta(days=29, hours=23) < span_abs <= timedelta(days=30)
        assert timedelta(days=6, hours=23) < span_idle <= timedelta(days=7)

        row = db.get_session_for_auth(pg_conn, _hash(token), touch_interval_seconds=300)
        assert row is not None
        assert (row["email"], row["role"], row["is_active"]) == (u["email"], "ss_team", True)
        assert row["revoked_at"] is None
        assert not row["absolute_expired"] and not row["idle_expired"]
        assert row["needs_touch"] is False  # vừa tạo xong, chưa cần ghi lại
        # Không lộ dữ liệu nhạy cảm qua hàm tra cứu.
        assert "password_hash" not in row and "token_hash" not in row

    def test_han_truot_bi_cat_theo_han_tuyet_doi(self, pg_conn, new_user):
        created = db.create_session(
            pg_conn, session_id=str(uuid.uuid4()), ss_user_id=new_user()["id"],
            token_hash=_hash(security.generate_session_token()), absolute_days=3, idle_days=10,
        )
        pg_conn.commit()
        assert created["idle_expires_at"] == created["expires_at"]

    def test_hai_han_bang_nhau_van_tao_duoc_phien(self, pg_conn, new_user):
        """Hồi quy: clock_timestamp() là hàm volatile, gọi hai lần trong một câu INSERT cho hai giá trị lệch vài
        micro-giây nên hạn trượt vượt hạn tuyệt đối và vi phạm CHECK. Chỉ lộ khi SESSION_IDLE_DAYS ==
        SESSION_ABSOLUTE_DAYS (mặc định 7/30 không nổ), nên phải có test riêng."""
        for _ in range(20):  # lặp để không phụ thuộc may rủi về vi giây
            created = db.create_session(
                pg_conn, session_id=str(uuid.uuid4()), ss_user_id=new_user()["id"],
                token_hash=_hash(security.generate_session_token()), absolute_days=7, idle_days=7,
            )
            assert created["idle_expires_at"] == created["expires_at"]
        pg_conn.commit()

    def test_token_la_khong_co_dong_nao(self, pg_conn):
        assert db.get_session_for_auth(pg_conn, _hash("s1.khong-ton-tai"), touch_interval_seconds=300) is None

    def test_het_han_duoc_tinh_bang_clock_timestamp_khong_phai_now(self, pg_conn, new_user):
        """BẪY có thật: connection từ pool giữ transaction mở giữa các request nên now() đứng yên ở quá khứ.
        Dựng đúng tình huống đó: mở transaction, hạn đặt 1 giây sau, ngủ 1,5 giây TRONG CÙNG transaction.
        Nếu code dùng now() thì phiên vẫn bị coi là còn hạn."""
        token, sid = _raw_session(pg_conn, new_user()["id"])
        with pg_conn.cursor() as cur:
            cur.execute("SELECT 1")  # mở transaction, now() bị đóng băng từ đây
            # Một mốc duy nhất cho cả hai cột (clock_timestamp() là volatile: hai lần gọi cho hai giá trị lệch
            # vài micro-giây, làm hạn trượt vượt hạn tuyệt đối và vi phạm CHECK thất thường).
            cur.execute(
                "UPDATE auth_sessions SET expires_at = t.ts, idle_expires_at = t.ts "
                "FROM (SELECT clock_timestamp() + interval '1 second' AS ts) t WHERE session_id = %s", (sid,))
            cur.execute("SELECT pg_sleep(1.5)")
        row = db.get_session_for_auth(pg_conn, _hash(token), touch_interval_seconds=300)
        assert row["absolute_expired"] is True and row["idle_expired"] is True
        pg_conn.rollback()

    def test_throttle_ghi_last_seen(self, pg_conn, new_user):
        token, sid = _raw_session(pg_conn, new_user()["id"])
        # Vừa tạo: chưa tới ngưỡng, không ghi.
        assert db.touch_session(pg_conn, sid, idle_days=7, min_interval_seconds=300) is False
        # Lùi last_seen 10 phút và hạn trượt còn 1 ngày: tới ngưỡng, phải ghi và đẩy hạn lên.
        with pg_conn.cursor() as cur:
            cur.execute(
                "UPDATE auth_sessions SET last_seen_at = clock_timestamp() - interval '10 minutes', "
                "idle_expires_at = clock_timestamp() + interval '1 day' WHERE session_id = %s", (sid,))
        row = db.get_session_for_auth(pg_conn, _hash(token), touch_interval_seconds=300)
        assert row["needs_touch"] is True
        assert db.touch_session(pg_conn, sid, idle_days=7, min_interval_seconds=300) is True
        pg_conn.commit()
        with pg_conn.cursor() as cur:
            cur.execute("SELECT idle_expires_at - clock_timestamp() FROM auth_sessions WHERE session_id = %s", (sid,))
            remaining = cur.fetchone()[0]
        assert timedelta(days=6, hours=23) < remaining <= timedelta(days=7)
        # Ngay sau đó lại không ghi nữa.
        assert db.touch_session(pg_conn, sid, idle_days=7, min_interval_seconds=300) is False

    def test_touch_khong_bao_gio_vuot_han_tuyet_doi(self, pg_conn, new_user):
        _, sid = _raw_session(
            pg_conn, new_user()["id"],
            expires_at="clock_timestamp() + interval '2 days'",
            idle_expires_at="clock_timestamp() + interval '1 hour'",
            last_seen_at="clock_timestamp() - interval '10 minutes'",
        )
        assert db.touch_session(pg_conn, sid, idle_days=7, min_interval_seconds=300) is True
        pg_conn.commit()
        with pg_conn.cursor() as cur:
            cur.execute("SELECT idle_expires_at = expires_at FROM auth_sessions WHERE session_id = %s", (sid,))
            assert cur.fetchone()[0] is True

    def test_touch_khong_danh_thuc_phien_da_thu_hoi_hoac_het_han(self, pg_conn, new_user):
        uid = new_user()["id"]
        old = "clock_timestamp() - interval '10 minutes'"
        _, revoked = _raw_session(pg_conn, uid, last_seen_at=old)
        db.revoke_session(pg_conn, revoked, db.REVOKE_LOGOUT)
        _, expired = _raw_session(
            pg_conn, uid, last_seen_at=old,
            idle_expires_at="clock_timestamp() - interval '1 minute'")
        pg_conn.commit()
        assert db.touch_session(pg_conn, revoked, idle_days=7, min_interval_seconds=300) is False
        assert db.touch_session(pg_conn, expired, idle_days=7, min_interval_seconds=300) is False

    def test_revoke_session_giu_ly_do_dau_tien(self, pg_conn, new_user):
        _, sid = _raw_session(pg_conn, new_user()["id"])
        assert db.revoke_session(pg_conn, sid, db.REVOKE_REPLACED) is True
        assert db.revoke_session(pg_conn, sid, db.REVOKE_LOGOUT) is False
        pg_conn.commit()
        with pg_conn.cursor() as cur:
            cur.execute("SELECT revoked_reason FROM auth_sessions WHERE session_id = %s", (sid,))
            assert cur.fetchone()[0] == "replaced"

    def test_revoke_all_chi_dung_phien_con_hieu_luc_cua_dung_nguoi(self, pg_conn, new_user):
        a, b = new_user()["id"], new_user()["id"]
        _, valid1 = _raw_session(pg_conn, a)
        _, valid2 = _raw_session(pg_conn, a)
        _, expired = _raw_session(pg_conn, a, idle_expires_at="clock_timestamp() - interval '1 minute'")
        _, other = _raw_session(pg_conn, b)
        assert db.revoke_all_sessions_for_user(pg_conn, a, db.REVOKE_REPLACED) == 2
        pg_conn.commit()
        with pg_conn.cursor() as cur:
            cur.execute("SELECT session_id::text, revoked_reason FROM auth_sessions")
            state = dict(cur.fetchall())
        assert state[valid1] == state[valid2] == "replaced"
        assert state[expired] is None  # phiên hết hạn KHÔNG bị gán nhãn 'replaced'
        assert state[other] is None    # người khác không bị đụng tới
        assert db.count_sessions_for_user(pg_conn, a) == 0
        assert db.count_sessions_for_user(pg_conn, b) == 1

    def test_clear_active_session_id_chi_xoa_khi_khop(self, pg_conn, new_user):
        uid = new_user()["id"]
        sid = str(uuid.uuid4())
        db.set_active_session_id(pg_conn, uid, sid)
        assert db.clear_active_session_id_if(pg_conn, uid, str(uuid.uuid4())) is False
        assert db.get_user_by_id(pg_conn, uid)["active_session_id"] is not None
        assert db.clear_active_session_id_if(pg_conn, uid, sid) is True
        assert db.get_user_by_id(pg_conn, uid)["active_session_id"] is None

    def test_don_dep_giu_dong_thu_hoi_cho_toi_het_han_tuyet_doi(self, pg_conn, new_user):
        uid = new_user()["id"]
        # now() (không phải clock_timestamp()) để expires_at và idle_expires_at nhận ĐÚNG cùng một giá trị.
        past = "now() - interval '%s days'"
        _, ok_valid = _raw_session(pg_conn, uid)
        _, ok_revoked_recent = _raw_session(pg_conn, uid)
        db.revoke_session(pg_conn, ok_revoked_recent, db.REVOKE_REPLACED)
        _, gone_absolute = _raw_session(
            pg_conn, uid, expires_at=past % 3, idle_expires_at=past % 3)
        _, ok_absolute_in_grace = _raw_session(  # hết hạn cách đây 2 giờ, còn trong ngày ân hạn
            pg_conn, uid, expires_at="now() - interval '2 hours'",
            idle_expires_at="now() - interval '2 hours'")
        _, gone_idle = _raw_session(pg_conn, uid, idle_expires_at=past % 2)
        # Phiên đã thu hồi nhưng chưa hết hạn tuyệt đối thì GIỮ, dù hạn trượt đã qua lâu.
        _, ok_revoked_idle_passed = _raw_session(pg_conn, uid, idle_expires_at=past % 2)
        db.revoke_session(pg_conn, ok_revoked_idle_passed, db.REVOKE_LOGOUT)
        pg_conn.commit()

        assert db.delete_stale_sessions(pg_conn) == 2
        pg_conn.commit()
        with pg_conn.cursor() as cur:
            cur.execute("SELECT session_id::text FROM auth_sessions")
            left = {r[0] for r in cur.fetchall()}
        assert left == {ok_valid, ok_revoked_recent, ok_absolute_in_grace, ok_revoked_idle_passed}
        assert gone_absolute not in left and gone_idle not in left


# ----------------------------------------------------------------------
# 3. Luồng đầy đủ qua route + get_current_user
# ----------------------------------------------------------------------
class TestLoginCreatesSession:
    def test_login_tra_ca_session_token_va_cap_jwt_cu(self, pg_conn, new_user):
        u = new_user("ss_team")
        out = _login(pg_conn, u["email"])
        assert out.session_token.startswith(security.SESSION_TOKEN_PREFIX)
        assert out.access_token and out.refresh_token  # client cũ (Flask) không bị phá
        remaining = out.session_expires_at - datetime.now(timezone.utc)
        assert timedelta(days=29, hours=23) < remaining <= timedelta(days=30)
        # Cả hai loại credential cùng cho ra đúng một người dùng và cùng session id.
        by_session = _auth(pg_conn, out.session_token)
        by_jwt = _auth(pg_conn, out.access_token)
        assert by_session["sub"] == by_jwt["sub"] == u["id"]
        assert by_session["sid"] == by_jwt["sid"]
        assert by_session["role"] == "ss_team" and by_session["email"] == u["email"]

    def test_luu_user_agent_va_khong_luu_token_tho_o_bat_ky_dau(self, pg_conn, new_user):
        u = new_user()
        out = _login(pg_conn, u["email"])
        with pg_conn.cursor() as cur:
            cur.execute("SELECT user_agent FROM auth_sessions WHERE ss_user_id = %s", (u["id"],))
            assert cur.fetchone()[0] == "pytest-agent/1.0"
            # Quét MỌI bảng liên quan: token thô không được nằm ở đâu cả, chỉ SHA-256 của nó.
            for table in ("auth_sessions", "auth_refresh_tokens", "app_users"):
                cur.execute(f"SELECT coalesce(string_agg(t::text, ' '), '') FROM {table} t")
                blob = cur.fetchone()[0]
                assert out.session_token not in blob, f"token thô lọt vào bảng {table}"
                assert out.session_token[len(security.SESSION_TOKEN_PREFIX):] not in blob
            cur.execute("SELECT token_hash FROM auth_sessions WHERE ss_user_id = %s", (u["id"],))
            assert cur.fetchone()[0] == _hash(out.session_token)

    def test_dang_nhap_lan_hai_thay_the_phien_cu_ca_session_lan_jwt(self, pg_conn, new_user):
        u = new_user()
        first = _login(pg_conn, u["email"])
        second = _login(pg_conn, u["email"])
        assert _code(pg_conn, first.session_token) == error_codes.SESSION_REPLACED
        assert _code(pg_conn, first.access_token) == error_codes.SESSION_REPLACED  # JWT cũ chết cùng
        assert _auth(pg_conn, second.session_token)["sub"] == u["id"]
        assert db.count_sessions_for_user(pg_conn, u["id"]) == 1  # single-session: chỉ 1 phiên sống

    def test_dang_nhap_cua_nguoi_khac_khong_anh_huong(self, pg_conn, new_user):
        a, b = new_user(), new_user()
        ta = _login(pg_conn, a["email"])
        _login(pg_conn, b["email"])
        assert _auth(pg_conn, ta.session_token)["sub"] == a["id"]

    def test_phien_het_han_truot_hoac_tuyet_doi_bi_tu_choi(self, pg_conn, new_user):
        u = new_user()
        for col in ("idle_expires_at", "expires_at"):
            out = _login(pg_conn, u["email"])
            with pg_conn.cursor() as cur:
                if col == "expires_at":
                    cur.execute("UPDATE auth_sessions SET expires_at = clock_timestamp() - interval '1 minute', "
                                "idle_expires_at = clock_timestamp() - interval '2 minutes' "
                                "WHERE ss_user_id = %s AND revoked_at IS NULL", (u["id"],))
                else:
                    cur.execute("UPDATE auth_sessions SET idle_expires_at = clock_timestamp() - interval '1 minute' "
                                "WHERE ss_user_id = %s AND revoked_at IS NULL", (u["id"],))
            pg_conn.commit()
            assert _code(pg_conn, out.session_token) == error_codes.TOKEN_EXPIRED


class TestCurrentUserFreshness:
    def test_role_moi_co_hieu_luc_ngay_o_request_ke_tiep(self, pg_conn, new_user):
        u = new_user("user")
        out = _login(pg_conn, u["email"])
        assert _auth(pg_conn, out.session_token)["role"] == "user"
        db.update_user_role(pg_conn, u["id"], "admin")
        pg_conn.commit()
        assert _auth(pg_conn, out.session_token)["role"] == "admin"

    def test_khoa_tai_khoan_chan_ngay_va_mo_khoa_thi_phien_cu_khong_song_lai(self, pg_conn, new_user):
        admin = new_user("admin")
        u = new_user()
        out = _login(pg_conn, u["email"])
        auth_users.update_user_active_status(
            ss_user_id=u["id"], payload=UserActiveStatusUpdate(is_active=False),
            admin={"sub": admin["id"], "role": "admin"}, conn=pg_conn)
        # Đúng mã người dùng nhận trước đây khi bị khoá (frontend đã rẽ nhánh theo mã này).
        assert _code(pg_conn, out.session_token) == error_codes.AUTH_ACCOUNT_INACTIVE
        auth_users.update_user_active_status(
            ss_user_id=u["id"], payload=UserActiveStatusUpdate(is_active=True),
            admin={"sub": admin["id"], "role": "admin"}, conn=pg_conn)
        # Mở khoá không làm phiên cũ sống lại.
        assert _code(pg_conn, out.session_token) == error_codes.SESSION_REVOKED

    def test_tai_khoan_bi_khoa_thang_trong_db_van_bi_chan_du_phien_con_hieu_luc(self, pg_conn, new_user):
        u = new_user()
        out = _login(pg_conn, u["email"])
        with pg_conn.cursor() as cur:
            cur.execute("UPDATE app_users SET is_active = false WHERE ss_user_id = %s", (u["id"],))
        pg_conn.commit()
        assert _code(pg_conn, out.session_token) == error_codes.AUTH_ACCOUNT_INACTIVE

    def test_last_seen_duoc_ghi_va_COMMIT_that_su(self, pg_conn, new_user):
        """get_current_user phải commit sau khi touch, nếu không UPDATE bỏ dở trôi sang request sau trên cùng
        connection trong pool. Kiểm bằng một connection KHÁC: nó chỉ thấy dữ liệu đã commit."""
        u = new_user()
        out = _login(pg_conn, u["email"])
        with pg_conn.cursor() as cur:
            cur.execute("UPDATE auth_sessions SET last_seen_at = clock_timestamp() - interval '10 minutes', "
                        "idle_expires_at = clock_timestamp() + interval '1 day' WHERE ss_user_id = %s", (u["id"],))
        pg_conn.commit()
        _auth(pg_conn, out.session_token)
        other = psycopg2.connect(TEST_DATABASE_URL)
        try:
            with other.cursor() as cur:
                cur.execute("SELECT idle_expires_at - clock_timestamp() FROM auth_sessions WHERE ss_user_id = %s",
                            (u["id"],))
                remaining = cur.fetchone()[0]
        finally:
            other.close()
        assert remaining > timedelta(days=6), "touch chưa được commit hoặc chưa đẩy hạn trượt"

    def test_khong_ghi_lai_khi_vua_dung_xong(self, pg_conn, new_user):
        u = new_user()
        out = _login(pg_conn, u["email"])
        with pg_conn.cursor() as cur:
            cur.execute("SELECT last_seen_at FROM auth_sessions WHERE ss_user_id = %s", (u["id"],))
            before = cur.fetchone()[0]
        for _ in range(3):
            _auth(pg_conn, out.session_token)
        with pg_conn.cursor() as cur:
            cur.execute("SELECT last_seen_at FROM auth_sessions WHERE ss_user_id = %s", (u["id"],))
            assert cur.fetchone()[0] == before


class TestLogout:
    def test_logout_bang_session_token_giet_ca_jwt_va_refresh_token_cung_lan_dang_nhap(self, pg_conn, new_user):
        u = new_user()
        out = _login(pg_conn, u["email"])
        auth_session.logout(
            request=make_fake_request(), payload=None, credentials=_creds(out.session_token), conn=pg_conn)
        assert _code(pg_conn, out.session_token) == error_codes.SESSION_REVOKED
        assert _code(pg_conn, out.access_token) == error_codes.SESSION_REVOKED
        # Refresh token phát lúc đăng nhập không được "hồi sinh" phiên sau khi đã đăng xuất.
        with pytest.raises(HTTPException) as exc:
            auth_session.refresh(
                payload=RefreshRequest(refresh_token=out.refresh_token),
                request=make_fake_request(), conn=pg_conn)
        assert exc.value.status_code == 401
        assert db.count_sessions_for_user(pg_conn, u["id"]) == 0

    def test_logout_bang_refresh_token_cu_van_chay_va_giet_ca_session_token(self, pg_conn, new_user):
        out = _login(pg_conn, new_user()["email"])
        auth_session.logout(
            request=make_fake_request(), payload=LogoutRequest(refresh_token=out.refresh_token),
            credentials=None, conn=pg_conn)
        assert _code(pg_conn, out.session_token) == error_codes.SESSION_REVOKED
        assert _code(pg_conn, out.access_token) == error_codes.SESSION_REVOKED

    def test_logout_khong_loi_khi_khong_gui_gi_hoac_token_la(self, pg_conn, new_user):
        out = _login(pg_conn, new_user()["email"])
        auth_session.logout(request=make_fake_request(), payload=None, credentials=None, conn=pg_conn)
        auth_session.logout(
            request=make_fake_request(), payload=None,
            credentials=_creds("s1.token-khong-ton-tai"), conn=pg_conn)
        # JWT trong header Authorization bị bỏ qua, không thu hồi gì.
        auth_session.logout(
            request=make_fake_request(), payload=None, credentials=_creds(out.access_token), conn=pg_conn)
        assert _auth(pg_conn, out.session_token)["role"] == "user"

    def test_logout_bang_phien_cu_da_bi_thay_the_khong_giet_phien_moi(self, pg_conn, new_user):
        u = new_user()
        first = _login(pg_conn, u["email"])
        second = _login(pg_conn, u["email"])
        auth_session.logout(
            request=make_fake_request(), payload=None, credentials=_creds(first.session_token), conn=pg_conn)
        assert _auth(pg_conn, second.session_token)["sub"] == u["id"]
        assert _auth(pg_conn, second.access_token)["sub"] == u["id"]


class TestPasswordChangesRevokeSessions:
    def test_doi_mat_khau_thu_hoi_session_token(self, pg_conn, new_user):
        u = new_user()
        out = _login(pg_conn, u["email"])
        user = _auth(pg_conn, out.session_token)
        auth_session.change_password(
            request=make_fake_request(),
            payload=ChangePasswordRequest(old_password=_PASSWORD, new_password="MatKhau-Moi-456"),
            user=user, conn=pg_conn)
        assert _code(pg_conn, out.session_token) == error_codes.SESSION_REVOKED
        assert _code(pg_conn, out.access_token) == error_codes.SESSION_REVOKED
        # Đăng nhập lại bằng mật khẩu mới tạo được phiên mới.
        again = auth_session.login(
            payload=LoginRequest(email=u["email"], password="MatKhau-Moi-456"),
            request=make_fake_request(), conn=pg_conn)
        assert _auth(pg_conn, again.session_token)["sub"] == u["id"]

    def test_dat_lai_mat_khau_thu_hoi_session_token(self, pg_conn, new_user):
        u = new_user()
        out = _login(pg_conn, u["email"])
        raw_reset = "reset-token-" + uuid.uuid4().hex
        db.set_password_reset_token(
            pg_conn, u["id"], security.hash_verification_token(raw_reset),
            datetime.now(timezone.utc) + timedelta(hours=1))
        pg_conn.commit()
        auth_registration.reset_password(
            payload=ResetPasswordRequest(token=raw_reset, new_password="MatKhau-Reset-789"),
            request=make_fake_request(), conn=pg_conn)
        assert _code(pg_conn, out.session_token) == error_codes.SESSION_REVOKED


class TestLegacyJwtStillWorks:
    def test_refresh_cu_van_cap_access_token_dung_trong_phien_co_session_mo(self, pg_conn, new_user):
        """Pha A không được phá client cũ: Flask/Next.js hiện tại vẫn dùng access + refresh."""
        u = new_user()
        out = _login(pg_conn, u["email"])
        refreshed = auth_session.refresh(
            payload=RefreshRequest(refresh_token=out.refresh_token),
            request=make_fake_request(), conn=pg_conn)
        assert _auth(pg_conn, refreshed.access_token)["sub"] == u["id"]
        # Phiên mờ cùng lần đăng nhập vẫn sống song song.
        assert _auth(pg_conn, out.session_token)["sub"] == u["id"]


class TestConcurrency:
    def test_nhieu_request_song_song_khong_ai_bi_dang_xuat_oan(self, pg_conn, new_user):
        """Mục 5.7 của lộ trình: gọi song song sau khi phiên "nhàn rỗi" lâu phải không ai bị văng. Ép đúng
        điều kiện đua: last_seen cũ 10 phút nên MỌI luồng cùng thấy needs_touch."""
        u = new_user()
        out = _login(pg_conn, u["email"])
        with pg_conn.cursor() as cur:
            cur.execute("UPDATE auth_sessions SET last_seen_at = clock_timestamp() - interval '10 minutes', "
                        "idle_expires_at = clock_timestamp() + interval '1 day' WHERE ss_user_id = %s", (u["id"],))
        pg_conn.commit()

        failures: list[str] = []
        barrier = threading.Barrier(16)

        def worker():
            conn = psycopg2.connect(TEST_DATABASE_URL)
            try:
                barrier.wait(timeout=10)
                for _ in range(5):
                    try:
                        assert _auth(conn, out.session_token)["sub"] == u["id"]
                    except Exception as exc:  # noqa: BLE001 — gom mọi lỗi để báo một lần
                        failures.append(repr(exc))
            finally:
                conn.close()

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert failures == []
        assert _auth(pg_conn, out.session_token)["sub"] == u["id"]
        assert db.count_sessions_for_user(pg_conn, u["id"]) == 1


class TestMigration0045:
    def test_chay_lai_migration_khong_loi_va_khong_mat_du_lieu(self, pg_conn, new_user):
        u = new_user()
        out = _login(pg_conn, u["email"])
        path = os.path.join(os.path.dirname(__file__), "..", "sql", "0045_add_auth_sessions.sql")
        with open(path, encoding="utf-8") as f:
            sql_text = f.read()
        with pg_conn.cursor() as cur:
            cur.execute(sql_text)
            cur.execute(sql_text)
        pg_conn.commit()
        assert _auth(pg_conn, out.session_token)["sub"] == u["id"]


# ----------------------------------------------------------------------
# 4. Qua HTTP thật (TestClient): parse body tuỳ chọn, header Authorization, API key
# ----------------------------------------------------------------------
_API_KEY = "test-api-key-not-a-real-secret"


@pytest.fixture
def client(pg_conn, monkeypatch):
    """TestClient trên app thật, DB = connection test. Không chạy lifespan (không `with`): không cần pool thật,
    không bật scheduler. get_db bị thay để mọi request dùng pg_conn."""
    monkeypatch.setattr("scrapjd.api.auth._API_KEY", _API_KEY)
    app.dependency_overrides[get_db] = lambda: pg_conn
    yield TestClient(app)
    app.dependency_overrides.pop(get_db, None)


def _h(token=None, **extra):
    headers = {"X-API-Key": _API_KEY, **extra}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


class TestHttpContract:
    def _login(self, client, email):
        r = client.post("/auth/login", json={"email": email, "password": _PASSWORD}, headers=_h())
        assert r.status_code == 200, r.text
        return r.json()

    def test_login_tra_session_token_va_me_chay_bang_session_token(self, client, new_user):
        u = new_user("ss_team")
        body = self._login(client, u["email"])
        assert body["session_token"].startswith("s1.") and body["access_token"] and body["refresh_token"]
        assert body["session_expires_at"]
        me = client.get("/auth/me", headers=_h(body["session_token"]))
        assert me.status_code == 200 and me.json()["email"] == u["email"]

    def test_logout_khong_body_khong_content_type_chi_voi_header_bearer(self, client, new_user):
        """Đúng cách frontend mới (Next.js) sẽ gọi: POST không body, chỉ có Authorization."""
        body = self._login(client, new_user()["email"])
        r = client.post("/auth/logout", headers=_h(body["session_token"]))
        assert r.status_code == 204, r.text
        after = client.get("/auth/me", headers=_h(body["session_token"]))
        assert after.status_code == 401
        assert after.json()["detail"]["error_code"] == "session_revoked"

    def test_logout_body_cu_chi_co_refresh_token_van_chay(self, client, new_user):
        """Client cũ (Flask, Next.js hiện tại) gửi {"refresh_token": ...} và không gửi Authorization."""
        body = self._login(client, new_user()["email"])
        r = client.post("/auth/logout", json={"refresh_token": body["refresh_token"]}, headers=_h())
        assert r.status_code == 204, r.text
        assert client.get("/auth/me", headers=_h(body["session_token"])).status_code == 401

    def test_logout_body_rong_hoac_khong_co_gi_deu_204(self, client, new_user):
        self._login(client, new_user()["email"])
        assert client.post("/auth/logout", json={}, headers=_h()).status_code == 204
        assert client.post("/auth/logout", headers=_h()).status_code == 204

    def test_logout_tu_choi_refresh_token_rong_va_field_la(self, client):
        assert client.post("/auth/logout", json={"refresh_token": ""}, headers=_h()).status_code == 422
        assert client.post("/auth/logout", json={"x": 1}, headers=_h()).status_code == 422

    def test_dang_nhap_lan_hai_phien_cu_nhan_session_replaced(self, client, new_user):
        u = new_user()
        first = self._login(client, u["email"])
        second = self._login(client, u["email"])
        old = client.get("/auth/me", headers=_h(first["session_token"]))
        assert old.status_code == 401 and old.json()["detail"]["error_code"] == "session_replaced"
        assert client.get("/auth/me", headers=_h(second["session_token"])).status_code == 200

    def test_session_token_van_can_api_key(self, client, new_user):
        body = self._login(client, new_user()["email"])
        r = client.get("/auth/me", headers={"Authorization": f"Bearer {body['session_token']}"})
        assert r.status_code in (401, 403)

    def test_thieu_hoac_sai_session_token_la_401_token_expired(self, client):
        assert client.get("/auth/me", headers=_h()).status_code == 401
        r = client.get("/auth/me", headers=_h("s1." + "x" * 64))
        assert r.status_code == 401 and r.json()["detail"]["error_code"] == "token_expired"

    def test_route_can_role_dung_role_tu_session(self, client, new_user):
        """require_role() chạy nguyên với session token: học viên bị chặn route staff, admin thì qua."""
        student = self._login(client, new_user("user")["email"])
        admin = self._login(client, new_user("admin")["email"])
        assert client.get("/auth/users", headers=_h(student["session_token"])).status_code == 403
        assert client.get("/auth/users", headers=_h(admin["session_token"])).status_code == 200
