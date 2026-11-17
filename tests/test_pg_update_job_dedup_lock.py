"""
E4 trên POSTGRES THẬT: db.update_job giành khoá chống trùng (advisory) của khoá MỚI trước khi đổi tiêu đề hoặc
tỉnh của job.

Trigger set_job_dedup_key tính lại dedup_key ở mọi UPDATE, nên sửa job_title/province_id là đổi khoá chống
trùng. Crawl và nhập tay giành khoá advisory theo khoá đó rồi mới "tra trùng, rồi insert"; đường sửa trước đây
không giành nên chen vào giữa được. Test ở đây kiểm:
  - khoá mới được giữ tới commit khi đổi tiêu đề / tỉnh / xoá tỉnh, và KHÔNG khoá khi khoá không đổi;
  - nhập tay cùng khoá phải chờ bên sửa rồi nhận lại job đã sửa (đúng MỘT job); nhóm đối chứng tắt khoá thì
    ra HAI job, nên test trên qua là có ý nghĩa;
  - chờ quá hạn thì JobDedupLockTimeout, và route PATCH trả 503 + job_dedup_lock_timeout, không ghi gì;
  - job bị đổi tỉnh giữa lúc đọc và lúc có khoá thì giành thêm đúng khoá, dòng job đã bị giữ.

Cách chạy giống tests/test_pg_lock_order.py: đặt TEST_DATABASE_URL (tên database chứa "test"); không đặt thì
cả file bị bỏ qua.
"""
import functools
import os
import threading
import time
import uuid
from urllib.parse import urlparse

import psycopg2
import pytest
from fastapi import HTTPException

from scrapjd import db
from scrapjd.api import error_codes
from scrapjd.api.routers.jobs import patch_job
from scrapjd.api.schemas import JobUpdate
from scrapjd.db import job_dedup_lock

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


@pytest.fixture
def editor():
    """Kết nối thứ hai (không autocommit) đóng vai nhân viên đang sửa job."""
    conn = psycopg2.connect(TEST_DATABASE_URL)
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


def _company(conn):
    cid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)",
                    (cid, f"Cty {cid[:8]}"))
    conn.commit()
    return cid


def _user(conn):
    uid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO app_users (ss_user_id, full_name, email) VALUES (%s, %s, %s)",
                    (uid, "Nhân viên", f"{uid}@example.com"))
    conn.commit()
    return uid


def _province(conn, name):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO provinces (province_name) VALUES (%s) RETURNING province_id",
                    (f"{name} {uuid.uuid4().hex[:6]}",))
        pid = cur.fetchone()[0]
    conn.commit()
    return pid


def _job(conn, company, title, *, province_id=None):
    job_id = db.insert_job(
        conn, company_id=company, job_title=title, matching_industry="Data", level_id=None,
        province_id=province_id, work_type=None, currency="VNĐ", salary_min=None, salary_max=None,
        salary_type="NEGOTIABLE", source_url=f"https://example.com/{uuid.uuid4()}", source_name="Fake",
        detail_fetched=True)
    conn.commit()
    return job_id


def _scalar(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        value = cur.fetchone()[0]
    conn.rollback()
    return value


def _key_is_locked(company, title, province_id=None):
    """Khoá advisory của khoá chống trùng này đang bị transaction khác giữ? Thử giành từ kết nối riêng."""
    conn = psycopg2.connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended("
                "'job_dedup:' || job_dedup_key(%s::uuid, %s::text, %s::int), 0))",
                (company, title, province_id))
            return cur.fetchone()[0] is False
    finally:
        conn.rollback()
        conn.close()


def _job_row_is_locked(job_id):
    conn = psycopg2.connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT 1 FROM job_postings WHERE job_id = %s FOR NO KEY UPDATE NOWAIT", (job_id,))
            except psycopg2.errors.LockNotAvailable:
                return True
            return False
    finally:
        conn.rollback()
        conn.close()


def _in_thread(fn):
    out = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - gom lại để test tự khẳng định
            out["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


def _wait_until_blocked(pid_holder, timeout=10):
    """Chờ tới khi backend của thread đang đứng chờ một khoá (pg_stat_activity), không dùng sleep cố định."""
    deadline = time.time() + timeout
    conn = psycopg2.connect(TEST_DATABASE_URL)
    conn.autocommit = True
    try:
        while time.time() < deadline:
            if pid_holder:
                with conn.cursor() as cur:
                    cur.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", (pid_holder[0],))
                    row = cur.fetchone()
                if row and row[0] == "Lock":
                    return True
            time.sleep(0.05)
        return False
    finally:
        conn.close()


# ============================================================ khi nào khoá
def test_changing_the_title_holds_the_lock_of_the_new_key_until_commit(pg_conn, editor):
    company = _company(pg_conn)
    job = _job(pg_conn, company, "Data Analyst")
    assert db.update_job(editor, job, job_title="Data Engineer") is True
    assert _key_is_locked(company, "Data Engineer")
    assert not _key_is_locked(company, "Data Analyst")           # khoá cũ không bị giữ
    editor.commit()
    assert not _key_is_locked(company, "Data Engineer")          # nhả khi transaction kết thúc


def test_changing_the_province_holds_the_lock_of_the_new_key(pg_conn, editor):
    company = _company(pg_conn)
    hn = _province(pg_conn, "Hà Nội E4")
    job = _job(pg_conn, company, "Data Analyst")
    assert db.update_job(editor, job, province_id=hn) is True
    assert _key_is_locked(company, "Data Analyst", hn)
    editor.commit()


def test_clearing_the_province_holds_the_lock_of_the_key_without_province(pg_conn, editor):
    company = _company(pg_conn)
    hn = _province(pg_conn, "Hà Nội E4")
    job = _job(pg_conn, company, "Data Analyst", province_id=hn)
    assert db.update_job(editor, job, clear_fields={"province_id"}) is True
    assert _key_is_locked(company, "Data Analyst", None)
    editor.commit()


def test_title_and_province_together_lock_the_combined_key(pg_conn, editor):
    company = _company(pg_conn)
    hn = _province(pg_conn, "Hà Nội E4")
    job = _job(pg_conn, company, "Data Analyst")
    db.update_job(editor, job, job_title="BI Analyst", province_id=hn)
    assert _key_is_locked(company, "BI Analyst", hn)
    assert not _key_is_locked(company, "BI Analyst", None)       # không phải khoá "chỉ đổi tiêu đề"
    editor.commit()


def test_resending_the_same_title_or_editing_other_fields_takes_no_lock(pg_conn, editor):
    company = _company(pg_conn)
    job = _job(pg_conn, company, "Data Analyst")
    # Form gửi lại đúng tiêu đề đang có (kể cả lệch hoa/thường, vì khoá chuẩn hoá) và sửa field khác.
    assert db.update_job(editor, job, job_title="  data ANALYST ", ss_team_notes="ghi chú") is True
    assert not _key_is_locked(company, "Data Analyst")
    assert db.update_job(editor, job, ss_team_notes="đổi tiếp") is True
    assert not _key_is_locked(company, "Data Analyst")
    editor.commit()


def test_unknown_job_takes_no_lock_and_returns_false(pg_conn, editor):
    assert db.update_job(editor, str(uuid.uuid4()), job_title="Gì đó") is False
    editor.rollback()


# ============================================================ cuộc đua với nhập tay
def _manual_create(company, title, pid_holder):
    def run():
        conn = psycopg2.connect(TEST_DATABASE_URL)
        conn.autocommit = False
        try:
            pid_holder.append(conn.info.backend_pid)
            job_id = db.create_manual_job(conn, job_title=title, company_id=company, level_id=None,
                                          province_id=None)
            conn.commit()
            return job_id
        finally:
            conn.rollback()
            conn.close()
    return run


def test_manual_create_waits_for_an_edit_then_reuses_the_edited_job(pg_conn, editor):
    """Nhân viên đổi job A sang khoá X (chưa commit). Nhập tay đúng khoá X phải chờ, rồi sau commit thấy A và
    trả lại A, không tạo job thứ hai."""
    company = _company(pg_conn)
    job_a = _job(pg_conn, company, "Cũ")
    db.update_job(editor, job_a, job_title="Data Analyst")

    pid = []
    t, out = _in_thread(_manual_create(company, "Data Analyst", pid))
    assert _wait_until_blocked(pid), "nhập tay phải đang chờ khoá của bên sửa"
    editor.commit()
    t.join(30)

    assert "error" not in out, out
    assert out["value"] == job_a
    assert _scalar(pg_conn, "SELECT count(*) FROM job_postings WHERE job_title = 'Data Analyst'") == 1


def test_control_without_the_lock_the_same_race_makes_two_jobs(pg_conn, editor, monkeypatch):
    """Đối chứng: tắt khoá của update_job thì nhập tay không phải chờ, tra thấy "chưa có" rồi tạo job thứ hai,
    nên cuối cùng có HAI job cùng khoá. Chứng tỏ test trên qua là nhờ khoá."""
    monkeypatch.setattr("scrapjd.db.jobs.lock_dedup_key_for_job_edit", lambda *a, **k: None)
    company = _company(pg_conn)
    job_a = _job(pg_conn, company, "Cũ")
    db.update_job(editor, job_a, job_title="Data Analyst")       # chưa commit

    pid = []
    t, out = _in_thread(_manual_create(company, "Data Analyst", pid))
    t.join(30)
    editor.commit()

    assert "error" not in out, out
    assert out["value"] != job_a
    assert _scalar(pg_conn, "SELECT count(*) FROM job_postings WHERE job_title = 'Data Analyst'") == 2


# ============================================================ hết thời gian chờ
def test_lock_times_out_when_another_transaction_holds_the_key(pg_conn, editor):
    company = _company(pg_conn)
    job = _job(pg_conn, company, "Cũ")
    db.lock_job_dedup_key(editor, company_id=company, job_title="Data Analyst", province_id=None)

    with pytest.raises(db.JobDedupLockTimeout):
        db.lock_dedup_key_for_job_edit(pg_conn, job, job_title="Data Analyst", timeout_ms=200)
    pg_conn.rollback()
    editor.rollback()


def test_route_patch_returns_503_on_lock_timeout_and_writes_nothing(pg_conn, editor, monkeypatch):
    company, user = _company(pg_conn), _user(pg_conn)
    job = _job(pg_conn, company, "Cũ")
    db.lock_job_dedup_key(editor, company_id=company, job_title="Data Analyst", province_id=None)
    monkeypatch.setattr("scrapjd.db.jobs.lock_dedup_key_for_job_edit",
                        functools.partial(job_dedup_lock.lock_dedup_key_for_job_edit, timeout_ms=200))

    caller = psycopg2.connect(TEST_DATABASE_URL)
    caller.autocommit = False
    try:
        with pytest.raises(HTTPException) as exc:
            patch_job(job, JobUpdate(job_title="Data Analyst"), conn=caller, user={"sub": user})
    finally:
        caller.rollback()
        caller.close()
    editor.rollback()

    assert exc.value.status_code == 503
    assert exc.value.detail["error_code"] == error_codes.JOB_DEDUP_LOCK_TIMEOUT
    assert _scalar(pg_conn, "SELECT job_title FROM job_postings WHERE job_id = %s", (job,)) == "Cũ"


# ============================================================ job đổi giữa lúc đọc và lúc có khoá
def test_key_is_relocked_when_the_job_changes_province_while_waiting(pg_conn, editor, monkeypatch):
    """Lần đọc đầu tính khoá (công ty, "Data Engineer", không tỉnh). Đúng lúc giành khoá, bên khác đổi tỉnh của
    job rồi commit. Sau khi có khoá phải đọc lại, thấy khoá đã khác và giành lại khoá đúng (có tỉnh), không
    được dừng ở khoá cũ."""
    company = _company(pg_conn)
    hn = _province(pg_conn, "Hà Nội E4")
    job = _job(pg_conn, company, "Data Analyst")
    real_lock, calls = job_dedup_lock.lock_job_dedup_key, []

    def lock_then_change_province_once(conn, **kw):
        real_lock(conn, **kw)
        calls.append(kw["province_id"])
        if len(calls) == 1:
            other = psycopg2.connect(TEST_DATABASE_URL)
            try:
                with other.cursor() as cur:
                    cur.execute("UPDATE job_postings SET province_id = %s WHERE job_id = %s", (hn, job))
                other.commit()
            finally:
                other.close()

    monkeypatch.setattr(job_dedup_lock, "lock_job_dedup_key", lock_then_change_province_once)
    db.lock_dedup_key_for_job_edit(editor, job, job_title="Data Engineer")

    assert calls == [None, hn]
    assert _key_is_locked(company, "Data Engineer", hn)
    assert _job_row_is_locked(job)                     # dòng job đã khoá nên không ai đổi được nữa
    editor.rollback()
