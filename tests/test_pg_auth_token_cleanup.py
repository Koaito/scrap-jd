"""
Dọn token xác thực hết hạn (db.auth_cleanup + services/token_cleanup) trên POSTGRES THẬT.

Chứng minh:
  - refresh token chỉ bị xoá khi hết hạn QUÁ khoảng chờ; token còn hạn, mới hết hạn, hoặc đang chờ bên trong
    khoảng chờ được giữ nguyên (kể cả token đã revoke: nó còn phục vụ phát hiện đánh cắp);
  - khoá ngoại replaced_by_token_id không làm lệnh xoá lỗi: chuỗi cả hai đầu đủ tuổi thì xoá hết, token đủ tuổi mà
    còn bị một dòng chưa đủ tuổi trỏ tới thì được giữ lại cho lượt sau;
  - token xác minh email và đặt lại mật khẩu hết hạn quá khoảng chờ bị gỡ (cả cột hạn), hai loại độc lập nhau,
    không đụng cột nào khác của user; token còn hạn, token không có hạn thì giữ;
  - chạy lại không làm gì; job dọn của scheduler commit đúng và không sập khi DB lỗi.

Cần TEST_DATABASE_URL trỏ tới DB RIÊNG có chữ "test"; không đặt thì cả file bị bỏ qua.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from urllib.parse import urlparse

import psycopg2
import pytest

from scrapjd import db
from scrapjd.api.services import token_cleanup

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")
NOW = lambda: datetime.now(timezone.utc)  # noqa: E731
DAY = timedelta(days=1)


@pytest.fixture(scope="module")
def pg_conn():
    dbname = urlparse(TEST_DATABASE_URL).path.lstrip("/")
    if "test" not in dbname.lower():
        pytest.fail(f"Từ chối chạy: database '{dbname}' không chứa 'test' trong tên.")
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
def clean(pg_conn):
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE auth_refresh_tokens, app_users CASCADE")
    pg_conn.commit()
    yield
    pg_conn.rollback()


def _user(conn, **cols):
    uid = str(uuid.uuid4())
    names = ["ss_user_id", "full_name", "email", *cols]
    vals = [uid, "Test", f"{uid}@example.com", *cols.values()]
    with conn.cursor() as cur:
        cur.execute(f"INSERT INTO app_users ({', '.join(names)}) VALUES ({', '.join(['%s'] * len(names))})", vals)
    conn.commit()
    return uid


def _token(conn, user_id, expires_ago_days, *, revoked=False, replaced_by=None):
    """Refresh token hết hạn cách đây expires_ago_days ngày (số âm = còn hạn)."""
    tid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO auth_refresh_tokens (refresh_token_id, ss_user_id, token_hash, expires_at, revoked_at, "
            "replaced_by_token_id) VALUES (%s, %s, %s, %s, %s, %s)",
            (tid, user_id, uuid.uuid4().hex + uuid.uuid4().hex, NOW() - expires_ago_days * DAY,
             NOW() - DAY if revoked else None, replaced_by),
        )
    conn.commit()
    return tid


def _ids(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT refresh_token_id::text FROM auth_refresh_tokens")
        return {r[0] for r in cur.fetchall()}


# ============================================================ refresh token
def test_only_tokens_expired_beyond_grace_are_deleted(pg_conn):
    u = _user(pg_conn)
    old = _token(pg_conn, u, 30)
    old_revoked = _token(pg_conn, u, 8, revoked=True)
    just_expired = _token(pg_conn, u, 1)           # mới hết hạn: còn trong khoảng chờ
    in_grace = _token(pg_conn, u, 6, revoked=True)  # revoked nhưng còn trong khoảng chờ: giữ (phát hiện đánh cắp)
    alive = _token(pg_conn, u, -10)

    assert db.delete_stale_refresh_tokens(pg_conn) == 2
    pg_conn.commit()

    assert _ids(pg_conn) == {just_expired, in_grace, alive}
    assert old not in _ids(pg_conn) and old_revoked not in _ids(pg_conn)


def test_grace_days_is_configurable(pg_conn):
    u = _user(pg_conn)
    t = _token(pg_conn, u, 3)
    assert db.delete_stale_refresh_tokens(pg_conn, grace_days=7) == 0
    assert db.delete_stale_refresh_tokens(pg_conn, grace_days=2) == 1
    assert t not in _ids(pg_conn)


def test_rotation_chain_with_both_ends_old_is_deleted_in_one_statement(pg_conn):
    """A (đã xoay vòng) trỏ sang B, cả hai đủ tuổi: xoá chung, khoá ngoại không vướng."""
    u = _user(pg_conn)
    b = _token(pg_conn, u, 20, revoked=True)
    a = _token(pg_conn, u, 25, revoked=True, replaced_by=b)

    assert db.delete_stale_refresh_tokens(pg_conn) == 2
    assert a not in _ids(pg_conn) and b not in _ids(pg_conn)


def test_token_still_referenced_by_a_younger_row_is_kept_for_next_run(pg_conn):
    """B đủ tuổi nhưng A (chưa đủ tuổi) còn trỏ tới B: xoá B sẽ vi phạm khoá ngoại, nên giữ B."""
    u = _user(pg_conn)
    b = _token(pg_conn, u, 10, revoked=True)
    a = _token(pg_conn, u, 2, revoked=True, replaced_by=b)

    assert db.delete_stale_refresh_tokens(pg_conn) == 0       # không lỗi, không xoá gì
    assert _ids(pg_conn) == {a, b}

    with pg_conn.cursor() as cur:                              # A cũng đủ tuổi (thời gian trôi) -> lượt sau xoá hết
        cur.execute("UPDATE auth_refresh_tokens SET expires_at = now() - interval '9 days' WHERE refresh_token_id = %s",
                    (a,))
    pg_conn.commit()
    assert db.delete_stale_refresh_tokens(pg_conn) == 2
    assert _ids(pg_conn) == set()


# ============================================================ token xác minh / đặt lại mật khẩu
def _user_tokens(conn, uid):
    with conn.cursor() as cur:
        cur.execute("SELECT email_verify_token, email_verify_expires, password_reset_token, password_reset_expires, "
                    "full_name, email_verified FROM app_users WHERE ss_user_id = %s", (uid,))
        return cur.fetchone()


def test_expired_verify_and_reset_tokens_are_cleared_independently(pg_conn):
    both = _user(pg_conn, email_verify_token="v" * 64, email_verify_expires=NOW() - 30 * DAY,
                 password_reset_token="r" * 64, password_reset_expires=NOW() - 30 * DAY)
    only_reset_old = _user(pg_conn, email_verify_token="w" * 64, email_verify_expires=NOW() + DAY,
                           password_reset_token="s" * 64, password_reset_expires=NOW() - 8 * DAY)

    assert db.clear_expired_user_tokens(pg_conn) == 3
    pg_conn.commit()

    assert _user_tokens(pg_conn, both)[:4] == (None, None, None, None)
    v, ve, r, re_, name, _ = _user_tokens(pg_conn, only_reset_old)
    assert v == "w" * 64 and ve is not None            # token xác minh còn hạn: giữ
    assert r is None and re_ is None                   # token đặt lại hết hạn quá lâu: gỡ
    assert name == "Test"                              # không đụng cột nào khác


def test_tokens_within_grace_alive_or_without_expiry_are_kept(pg_conn):
    within = _user(pg_conn, email_verify_token="a" * 64, email_verify_expires=NOW() - 2 * DAY)
    alive = _user(pg_conn, password_reset_token="b" * 64, password_reset_expires=NOW() + DAY)
    no_expiry = _user(pg_conn, email_verify_token="c" * 64)          # bất thường: không có hạn -> không đụng

    assert db.clear_expired_user_tokens(pg_conn) == 0
    assert _user_tokens(pg_conn, within)[0] == "a" * 64
    assert _user_tokens(pg_conn, alive)[2] == "b" * 64
    assert _user_tokens(pg_conn, no_expiry)[0] == "c" * 64


def test_legacy_raw_token_shape_from_real_data_is_cleared_once_expired(pg_conn):
    """Dạng thật trong DB: token xác minh thô 43 ký tự (lưu trước khi vá sang hash) đã hết hạn từ lâu."""
    u = _user(pg_conn, email_verify_token="x" * 43, email_verify_expires=NOW() - 92 * DAY)
    assert db.clear_expired_user_tokens(pg_conn) == 1
    assert _user_tokens(pg_conn, u)[:2] == (None, None)


def test_second_run_does_nothing(pg_conn):
    u = _user(pg_conn, email_verify_token="v" * 64, email_verify_expires=NOW() - 30 * DAY)
    _token(pg_conn, u, 30)
    assert db.delete_stale_refresh_tokens(pg_conn) == 1 and db.clear_expired_user_tokens(pg_conn) == 1
    pg_conn.commit()
    assert db.delete_stale_refresh_tokens(pg_conn) == 0 and db.clear_expired_user_tokens(pg_conn) == 0


# ============================================================ job của scheduler
def test_scheduler_job_commits_both_cleanups(pg_conn, monkeypatch):
    u = _user(pg_conn, password_reset_token="r" * 64, password_reset_expires=NOW() - 30 * DAY)
    _token(pg_conn, u, 30)
    monkeypatch.setattr(db, "get_pooled_connection", lambda: pg_conn)
    monkeypatch.setattr(db, "release_connection", lambda c: None)

    token_cleanup.run_token_cleanup_once()

    pg_conn.rollback()                                    # chỉ thấy được nếu job đã commit
    assert _ids(pg_conn) == set()
    assert _user_tokens(pg_conn, u)[2] is None


def test_scheduler_job_swallows_errors_and_rolls_back(monkeypatch):
    conn = MagicMock()
    fake = MagicMock()
    fake.get_pooled_connection.return_value = conn
    fake.delete_stale_refresh_tokens.side_effect = RuntimeError("DB lỗi")
    monkeypatch.setattr(token_cleanup, "db_module", fake)

    token_cleanup.run_token_cleanup_once()                # không được raise ra scheduler

    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()
    fake.release_connection.assert_called_once_with(conn)
