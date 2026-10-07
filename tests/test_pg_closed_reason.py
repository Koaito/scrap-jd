"""
A2 nửa 1/2 trên POSTGRES THẬT: migration 0037 (job_postings.closed_reason / closed_at) và 0038
(audit action REOPEN_JOB). Chưa liên quan tới code Python: nửa này chỉ là schema + backfill.

Chứng minh:
  - backfill từ audit_logs đúng từng ca (nhân viên đóng, nhân viên đóng rồi mở lại, mở lại rồi
    đóng tự động, chưa từng ai sửa, có người sửa nhưng không rõ), job OPEN không bị đụng;
  - backfill KHÔNG đổi updated_at, chạy lại không đổi gì;
  - bất biến closed_reason/closed_at được trigger + CHECK giữ cho mọi đường ghi;
  - REOPEN_JOB dùng được sau migration.

Cách chạy như tests/test_pg_migrations.py: đặt TEST_DATABASE_URL (tên database chứa "test"),
test tự tạo/xoá database tạm; không đặt thì cả file được bỏ qua.
"""
import os
import pathlib
import uuid
from contextlib import contextmanager
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
import pytest
from psycopg2 import sql as pgsql

import db

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SQL_DIR = pathlib.Path(__file__).resolve().parent.parent / "sql"
_SCHEMA = str(_SQL_DIR / "schema.sql")
_BASELINE = str(_SQL_DIR / "baseline" / "0036_schema.sql")
_ONLY_0037 = "0037_add_job_closed_reason.sql"


@contextmanager
def _temp_database():
    parsed = urlparse(TEST_DATABASE_URL)
    base = parsed.path.lstrip("/")
    if "test" not in base.lower():
        pytest.fail(f"Từ chối chạy: database '{base}' không chứa 'test' trong tên.")
    name = f"{base}_cr_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(TEST_DATABASE_URL)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(pgsql.SQL("CREATE DATABASE {}").format(pgsql.Identifier(name)))
        try:
            yield parsed._replace(path=f"/{name}").geturl()
        finally:
            with admin.cursor() as cur:
                cur.execute(pgsql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                            .format(pgsql.Identifier(name)))
    finally:
        admin.close()


@contextmanager
def _connect(url):
    conn = psycopg2.connect(url)
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def _legacy_baselined_db(conn):
    """DB ở trạng thái ngay trước 0037: baseline 0036 + 36 migration cũ đã ghi nhận."""
    db.apply_schema(conn, _BASELINE)
    legacy = [f for f in db.connection._list_migration_files(str(_SQL_DIR))
              if f.startswith("migration_")]
    db.connection._ensure_schema_migrations_table(conn)
    with conn.cursor() as cur:
        for f in legacy:
            cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (f,))
    conn.commit()


def _migrate_only(conn, tmp_path, *names):
    """Chạy đúng các migration đánh số `names` (copy sang thư mục riêng cho khỏi chạy cả 0039+)."""
    for n in names:
        (tmp_path / n).write_text((_SQL_DIR / n).read_text(encoding="utf-8"), encoding="utf-8")
    return db.apply_migrations(conn, str(tmp_path))


def _company(cur):
    cid = str(uuid.uuid4())
    cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)",
                (cid, f"Công ty {cid[:8]}"))
    return cid


def _job(cur, company_id, title, *, status="OPEN", updated_by=None):
    job_id = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO job_postings (job_id, company_id, job_title, job_status, updated_by, "
        "created_at, updated_at) VALUES (%s, %s, %s, %s, %s, '2026-01-01', '2026-02-02 02:02:02')",
        (job_id, company_id, title, status, updated_by))
    return job_id


def _audit(cur, job_id, action, changes=None, at="2026-03-01 10:00:00+00"):
    cur.execute(
        "INSERT INTO audit_logs (action_type, entity_type, entity_id, changes, is_manual_log, created_at) "
        "VALUES (%s, 'JOB', %s, %s, true, %s)",
        (action, job_id, psycopg2.extras.Json(changes) if changes is not None else None, at))


def _state(cur, job_id):
    cur.execute("SELECT job_status::text, closed_reason, closed_at AT TIME ZONE 'UTC', updated_at::text "
                "FROM job_postings WHERE job_id = %s", (job_id,))
    return cur.fetchone()


# ----------------------------------------------------------------------------- backfill
def test_backfill_classifies_closed_jobs_from_audit_logs(tmp_path):
    with _temp_database() as url, _connect(url) as conn:
        _legacy_baselined_db(conn)
        with conn.cursor() as cur:
            c = _company(cur)
            cur.execute("INSERT INTO app_users (ss_user_id, full_name, email) VALUES "
                        "(gen_random_uuid(), 'Nhân viên', 'nv@example.com') RETURNING ss_user_id")
            staff = str(cur.fetchone()[0])

            # 1. nhân viên đóng (DELETE_JOB là sự kiện gần nhất)
            staff_closed = _job(cur, c, "A", status="CLOSED", updated_by=staff)
            _audit(cur, staff_closed, "DELETE_JOB", {"job_status": {"old": "OPEN", "new": "CLOSED"}},
                   at="2026-03-05 08:00:00+00")
            # 2. nhân viên đóng rồi mở lại rồi job bị đóng lại mà không có audit (check_expired)
            reopened_then_closed = _job(cur, c, "B", status="CLOSED", updated_by=staff)
            _audit(cur, reopened_then_closed, "DELETE_JOB", at="2026-03-01 08:00:00+00")
            _audit(cur, reopened_then_closed, "UPDATE_JOB",
                   {"job_status": {"old": "CLOSED", "new": "OPEN"}}, at="2026-03-02 08:00:00+00")
            # 3. nhân viên đóng, mở lại, đóng lại bằng tay: sự kiện gần nhất là DELETE_JOB
            closed_twice = _job(cur, c, "C", status="CLOSED", updated_by=staff)
            _audit(cur, closed_twice, "DELETE_JOB", at="2026-03-01 08:00:00+00")
            _audit(cur, closed_twice, "UPDATE_JOB",
                   {"job_status": {"old": "CLOSED", "new": "OPEN"}}, at="2026-03-02 08:00:00+00")
            _audit(cur, closed_twice, "DELETE_JOB", at="2026-03-09 08:00:00+00")
            # 4. không audit nào, chưa từng ai sửa
            untouched = _job(cur, c, "D", status="CLOSED")
            # 5. có người sửa nhưng không có bản ghi đóng
            edited = _job(cur, c, "E", status="CLOSED", updated_by=staff)
            _audit(cur, edited, "UPDATE_JOB", {"salary_min": {"old": 1, "new": 2}})
            # 6. UPDATE_JOB không đổi trạng thái không được coi là mở lại
            edited_no_status = _job(cur, c, "F", status="CLOSED", updated_by=staff)
            _audit(cur, edited_no_status, "DELETE_JOB", at="2026-03-01 08:00:00+00")
            _audit(cur, edited_no_status, "UPDATE_JOB", {"job_title": {"old": "x", "new": "F"}},
                   at="2026-03-03 08:00:00+00")
            # 7. job OPEN
            open_job = _job(cur, c, "G", status="OPEN", updated_by=staff)
        conn.commit()

        # Trước migration: chưa có cột.
        assert _migrate_only(conn, tmp_path, _ONLY_0037) == [_ONLY_0037]

        with conn.cursor() as cur:
            s, r, at, upd = _state(cur, staff_closed)
            assert (s, r) == ("CLOSED", "staff") and str(at).startswith("2026-03-05 08:00")
            assert _state(cur, reopened_then_closed)[:3] == ("CLOSED", "unknown", None)
            assert _state(cur, closed_twice)[1] == "staff"
            assert str(_state(cur, closed_twice)[2]).startswith("2026-03-09 08:00")
            assert _state(cur, untouched)[:3] == ("CLOSED", "expired_auto", None)
            assert _state(cur, edited)[:3] == ("CLOSED", "unknown", None)
            assert _state(cur, edited_no_status)[1] == "staff"
            assert _state(cur, open_job)[:3] == ("OPEN", None, None)
            # backfill không đụng updated_at của bất kỳ dòng nào
            for j in (staff_closed, reopened_then_closed, closed_twice, untouched, edited,
                      edited_no_status, open_job):
                assert _state(cur, j)[3].startswith("2026-02-02 02:02:02")


def test_backfill_is_idempotent(tmp_path):
    with _temp_database() as url, _connect(url) as conn:
        _legacy_baselined_db(conn)
        with conn.cursor() as cur:
            c = _company(cur)
            job = _job(cur, c, "A", status="CLOSED")
        conn.commit()
        _migrate_only(conn, tmp_path, _ONLY_0037)
        # Chạy lại nguyên file SQL: không lỗi, không đổi gì.
        with conn.cursor() as cur:
            cur.execute("UPDATE job_postings SET closed_reason = 'staff' WHERE job_id = %s", (job,))
            cur.execute((_SQL_DIR / _ONLY_0037).read_text(encoding="utf-8"))
            assert _state(cur, job)[1] == "staff"      # dòng đã có lý do không bị backfill ghi đè


# ----------------------------------------------------------------------------- bất biến
@pytest.fixture()
def fresh():
    """DB dựng từ schema.sql mới nhất (đã gồm 0037/0038)."""
    with _temp_database() as url, _connect(url) as conn:
        db.apply_schema(conn, _SCHEMA)
        conn.commit()
        yield conn


def test_closing_without_reason_defaults_to_unknown(fresh):
    with fresh.cursor() as cur:
        c = _company(cur)
        j = _job(cur, c, "A")
        cur.execute("UPDATE job_postings SET job_status = 'CLOSED' WHERE job_id = %s", (j,))
        s, reason, at, _ = _state(cur, j)
        assert (s, reason) == ("CLOSED", "unknown") and at is not None


def test_closing_keeps_given_reason_and_reopening_clears_both(fresh):
    with fresh.cursor() as cur:
        c = _company(cur)
        j = _job(cur, c, "A")
        cur.execute("UPDATE job_postings SET job_status = 'CLOSED', closed_reason = 'expired_auto' "
                    "WHERE job_id = %s", (j,))
        assert _state(cur, j)[1] == "expired_auto"
        cur.execute("UPDATE job_postings SET job_status = 'OPEN' WHERE job_id = %s", (j,))
        assert _state(cur, j)[:3] == ("OPEN", None, None)


def test_editing_a_closed_job_keeps_reason_and_time(fresh):
    with fresh.cursor() as cur:
        c = _company(cur)
        j = _job(cur, c, "A")
        cur.execute("UPDATE job_postings SET job_status = 'CLOSED', closed_reason = 'staff' "
                    "WHERE job_id = %s", (j,))
        first = _state(cur, j)
        cur.execute("UPDATE job_postings SET ss_team_notes = 'ghi chú', job_status = 'CLOSED' "
                    "WHERE job_id = %s", (j,))
        assert _state(cur, j)[1:3] == first[1:3]


def test_insert_as_closed_gets_unknown_and_open_insert_has_no_reason(fresh):
    with fresh.cursor() as cur:
        c = _company(cur)
        closed = _job(cur, c, "A", status="CLOSED")
        opened = _job(cur, c, "B")
        assert _state(cur, closed)[1] == "unknown"
        assert _state(cur, opened)[1:3] == (None, None)


def test_check_constraints_reject_bad_values(fresh):
    with fresh.cursor() as cur:
        c = _company(cur)
        j = _job(cur, c, "A")
        cur.execute("SAVEPOINT s")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE job_postings SET job_status = 'CLOSED', closed_reason = 'khong_co' "
                        "WHERE job_id = %s", (j,))
        cur.execute("ROLLBACK TO SAVEPOINT s")
        # Trigger đã chuẩn hoá, nên muốn thấy CHECK chặn thì phải bỏ trigger đi.
        cur.execute("ALTER TABLE job_postings DISABLE TRIGGER set_job_closed_state")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE job_postings SET closed_reason = 'staff' WHERE job_id = %s", (j,))
        cur.execute("ROLLBACK TO SAVEPOINT s")
        # ROLLBACK TO SAVEPOINT hoàn tác cả lệnh DISABLE TRIGGER (DDL nằm trong transaction).
        cur.execute("ALTER TABLE job_postings DISABLE TRIGGER set_job_closed_state")
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute("UPDATE job_postings SET job_status = 'CLOSED' WHERE job_id = %s", (j,))


def test_reopen_job_audit_action_exists_after_migration(fresh):
    with fresh.cursor() as cur:
        cur.execute("SELECT 'REOPEN_JOB'::audit_action_enum::text")
        assert cur.fetchone()[0] == "REOPEN_JOB"
