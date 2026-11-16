"""
Tiến độ script vá dữ liệu trong DB (migration 0046, scrapjd/db/backfill_progress.py,
scripts/backfill.progress_marker) trên POSTGRES THẬT.

Chứng minh:
  - migration 0046 chạy lại được (idempotent) và schema.sql có cùng bảng;
  - đánh dấu/đọc/xoá tiến độ đúng, mỗi script một không gian riêng, đánh dấu lại không đổi done_at;
  - CHECK chặn script_name/item_id rỗng;
  - progress_marker commit NGAY (máy/connection khác thấy liền) và rollback sạch khi ghi lỗi.

Cách chạy như tests/test_pg_migrations.py: đặt TEST_DATABASE_URL (tên database chứa "test"), test tự tạo/xoá
database tạm; không đặt thì cả file được bỏ qua.
"""
import os
import pathlib
import uuid
from urllib.parse import urlparse

import psycopg2
import pytest
from psycopg2 import sql as pgsql

from scrapjd import db
from scripts.backfill import progress_marker

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SQL_DIR = pathlib.Path(__file__).resolve().parent.parent / "sql"
_MIGRATION = _SQL_DIR / "0046_add_backfill_progress.sql"
A, B = "backfill_a", "backfill_b"


@pytest.fixture(scope="module")
def database_url():
    parsed = urlparse(TEST_DATABASE_URL)
    base = parsed.path.lstrip("/")
    if "test" not in base.lower():
        pytest.fail(f"Từ chối chạy: database '{base}' không chứa 'test' trong tên.")
    name = f"{base}_bp_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(TEST_DATABASE_URL)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(pgsql.SQL("CREATE DATABASE {}").format(pgsql.Identifier(name)))
        url = parsed._replace(path=f"/{name}").geturl()
        setup = psycopg2.connect(url)
        try:
            db.apply_schema(setup, str(_SQL_DIR / "schema.sql"))
        finally:
            setup.close()
        yield url
    finally:
        with admin.cursor() as cur:
            cur.execute(pgsql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(pgsql.Identifier(name)))
        admin.close()


@pytest.fixture
def conn(database_url):
    c = psycopg2.connect(database_url)
    c.autocommit = False
    with c.cursor() as cur:
        cur.execute("DELETE FROM backfill_progress")
    c.commit()
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def test_migration_0046_is_idempotent(conn):
    sql_text = _MIGRATION.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql_text)
        cur.execute(sql_text)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass('backfill_progress') IS NOT NULL")
        assert cur.fetchone()[0] is True


def test_load_empty_and_load_closes_the_read_transaction(conn):
    assert db.load_backfill_done(conn, A) == set()
    assert conn.get_transaction_status() == psycopg2.extensions.TRANSACTION_STATUS_IDLE


def test_mark_then_load_is_scoped_per_script(conn):
    db.mark_backfill_done(conn, A, "j1")
    db.mark_backfill_done(conn, A, "j2")
    db.mark_backfill_done(conn, B, "j9")
    conn.commit()
    assert db.load_backfill_done(conn, A) == {"j1", "j2"}
    assert db.load_backfill_done(conn, B) == {"j9"}


def test_mark_again_keeps_first_done_at_and_single_row(conn):
    db.mark_backfill_done(conn, A, "j1")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT done_at FROM backfill_progress WHERE script_name=%s AND item_id=%s", (A, "j1"))
        first = cur.fetchone()[0]
    db.mark_backfill_done(conn, A, "j1")
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT count(*), min(done_at) FROM backfill_progress WHERE script_name=%s", (A,))
        count, done_at = cur.fetchone()
    assert count == 1 and done_at == first


def test_mark_many_counts_only_new_rows_and_accepts_uuid_like_values(conn):
    u = uuid.uuid4()
    assert db.mark_backfill_done_many(conn, A, ["x", "y", "x", u]) == 3
    assert db.mark_backfill_done_many(conn, A, ["x", "z"]) == 1
    assert db.mark_backfill_done_many(conn, A, []) == 0
    conn.commit()
    assert db.load_backfill_done(conn, A) == {"x", "y", "z", str(u)}


def test_reset_only_removes_that_script(conn):
    db.mark_backfill_done_many(conn, A, ["1", "2"])
    db.mark_backfill_done_many(conn, B, ["1"])
    conn.commit()
    assert db.reset_backfill_progress(conn, A) == 2
    conn.commit()
    assert db.load_backfill_done(conn, A) == set()
    assert db.load_backfill_done(conn, B) == {"1"}


@pytest.mark.parametrize("script, item", [("", "j1"), (A, "")])
def test_check_rejects_blank_names(conn, script, item):
    with pytest.raises(psycopg2.errors.CheckViolation):
        db.mark_backfill_done(conn, script, item)
    conn.rollback()


def test_progress_marker_commits_immediately_for_other_connections(conn, database_url):
    on_done = progress_marker(conn, A)
    on_done("job-1")
    other = psycopg2.connect(database_url)
    try:
        assert db.load_backfill_done(other, A) == {"job-1"}
    finally:
        other.close()


def test_progress_marker_rolls_back_and_reraises_on_failure(conn):
    on_done = progress_marker(conn, "")  # script_name rỗng vi phạm CHECK
    with pytest.raises(psycopg2.errors.CheckViolation):
        on_done("job-1")
    # connection dùng lại được ngay (không kẹt trong transaction lỗi) và không để lại dòng nào
    assert conn.get_transaction_status() == psycopg2.extensions.TRANSACTION_STATUS_IDLE
    progress_marker(conn, A)("job-2")
    assert db.load_backfill_done(conn, A) == {"job-2"}
