"""
A2 nửa 2/2 trên POSTGRES THẬT: các nơi đóng/mở job ghi đúng closed_reason.

  - db.update_job (PATCH /jobs, import): đóng thì mặc định 'staff', truyền được lý do khác, đã đóng
    rồi thì giữ lý do cũ, mở lại thì xoá;
  - check_expired_source_jobs.run: đóng do quá hạn và do nguồn chết đều ghi 'expired_auto';
  - dòng job_postings sau khi đóng luôn có closed_at.
Cách chạy như các test_pg_*: cần TEST_DATABASE_URL (database tên chứa "test").
"""
import os
import uuid
from datetime import date, timedelta
from urllib.parse import urlparse

import psycopg2
import pytest

import check_expired_source_jobs as cej
import db

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")


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
    db.baseline_migrations(conn)
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture(autouse=True)
def clean(pg_conn):
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE job_postings, audit_logs CASCADE")
    pg_conn.commit()
    yield
    pg_conn.rollback()


def _job(conn, *, deadline=None, url=None):
    with conn.cursor() as cur:
        cid = str(uuid.uuid4())
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)", (cid, f"C {cid[:8]}"))
    conn.commit()
    return db.insert_job(
        conn, company_id=cid, job_title="Data Analyst", matching_industry="Data", level_id=None,
        province_id=None, work_type=None, currency="VNĐ", salary_min=None, salary_max=None,
        salary_type="NEGOTIABLE", source_url=url or f"https://x/{uuid.uuid4()}", source_name="Fake",
        deadline=deadline,
    )


def _state(conn, job_id):
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT job_status::text, closed_reason, closed_at IS NOT NULL "
                    "FROM job_postings WHERE job_id = %s", (job_id,))
        return cur.fetchone()


def test_update_job_close_defaults_to_staff(pg_conn):
    job = _job(pg_conn)
    assert db.update_job(pg_conn, job, job_status="CLOSED")
    assert _state(pg_conn, job) == ("CLOSED", "staff", True)


def test_update_job_close_with_explicit_reason(pg_conn):
    job = _job(pg_conn)
    db.update_job(pg_conn, job, job_status="CLOSED", closed_reason="expired_auto")
    assert _state(pg_conn, job) == ("CLOSED", "expired_auto", True)


def test_resending_closed_does_not_change_the_reason(pg_conn):
    job = _job(pg_conn)
    db.update_job(pg_conn, job, job_status="CLOSED", closed_reason="expired_auto")
    db.update_job(pg_conn, job, job_status="CLOSED", ss_team_notes="form gửi lại trạng thái cũ")
    assert _state(pg_conn, job) == ("CLOSED", "expired_auto", True)


def test_update_job_reopen_clears_reason_and_time(pg_conn):
    job = _job(pg_conn)
    db.update_job(pg_conn, job, job_status="CLOSED")
    db.update_job(pg_conn, job, job_status="OPEN")
    assert _state(pg_conn, job) == ("OPEN", None, False)


def test_update_job_rejects_bad_closed_reason(pg_conn):
    job = _job(pg_conn)
    with pytest.raises(ValueError):
        db.update_job(pg_conn, job, job_status="CLOSED", closed_reason="khong_co")
    with pytest.raises(ValueError):
        db.update_job(pg_conn, job, closed_reason="staff")           # không đi kèm job_status=CLOSED
    with pytest.raises(ValueError):
        db.update_job(pg_conn, job, job_status="OPEN", closed_reason="staff")


class _KeepOpen:
    """Bọc connection để check_expired_source_jobs.run() gọi close() không đóng connection của test."""

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _Checker:
    def __init__(self, codes):
        self._codes = codes

    def check(self, url):
        return self._codes[url]


def test_check_expired_closes_with_expired_auto(pg_conn, monkeypatch):
    past = date.today() - timedelta(days=3)
    by_deadline = _job(pg_conn, deadline=past, url="https://x/by-deadline")
    dead = _job(pg_conn, url="https://x/dead")
    alive = _job(pg_conn, url="https://x/alive")
    monkeypatch.setattr(db, "get_connection", lambda: _KeepOpen(pg_conn))
    monkeypatch.setattr(cej, "_Throttled404Checker",
                        lambda: _Checker({"https://x/dead": 404, "https://x/alive": 200}))

    stats = cej.run(skip_cv_cleanup=True)

    assert stats["expired_by_deadline"] == 1 and stats["expired_by_source_dead"] == 1
    assert _state(pg_conn, by_deadline) == ("CLOSED", "expired_auto", True)
    assert _state(pg_conn, dead) == ("CLOSED", "expired_auto", True)
    assert _state(pg_conn, alive) == ("OPEN", None, False)
