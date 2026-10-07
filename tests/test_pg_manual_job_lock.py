"""
A4 nửa 2/2 trên POSTGRES THẬT: khoá advisory chống trùng ở đường NHẬP TAY (db.create_manual_job và
route POST /jobs), bổ sung cho nửa 1 (pipeline crawl, tests/test_pg_job_dedup_lock.py).

Chứng minh với hai connection thật, hai thread, cuộc đua bị ép xảy ra bằng cách cho câu tra trùng của
mỗi bên chờ bên kia (tối đa 2 giây) trước khi đi tiếp:
  - db.create_manual_job, cùng khoá và cùng level: đúng MỘT job, cả hai bên nhận cùng job_id;
  - cùng khoá nhưng KHÁC level: vẫn tạo được cả hai (khoá chỉ xếp hàng, không từ chối);
  - route POST /jobs: một bên was_existing=False, bên kia True, và chỉ MỘT log CREATE_JOB (khoá phải
    nằm ở route, trước bước tra was_duplicate, chứ không chỉ trong create_manual_job);
  - route trả 503 + error_code job_dedup_lock_timeout khi chờ khoá quá hạn, không ghi gì;
  - nhóm đối chứng (tắt khoá): cùng kịch bản sinh HAI job, nên các test trên qua là có ý nghĩa.

Cách chạy giống tests/test_pg_job_dedup_lock.py: đặt TEST_DATABASE_URL trỏ tới database dùng riêng cho
test (tên chứa "test"); không đặt thì cả file được bỏ qua.
"""
import os
import threading
import uuid
from urllib.parse import urlparse

import psycopg2
import pytest
from fastapi import HTTPException

import db
from api import error_codes
from api.routers.jobs import create_job
from api.schemas import JobCreate

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
def conns():
    opened = [psycopg2.connect(TEST_DATABASE_URL) for _ in range(2)]
    for c in opened:
        c.autocommit = False
    yield opened
    for c in opened:
        c.rollback()
        c.close()


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


def _company(conn):
    cid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)",
                    (cid, f"Công ty Nhập Tay {uuid.uuid4().hex[:8]}"))
    conn.commit()
    return cid


def _user(conn):
    uid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO app_users (ss_user_id, full_name, email) VALUES (%s, %s, %s)",
                    (uid, "Nhân viên", f"{uid}@example.com"))
    conn.commit()
    return uid


def _scalar(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        value = cur.fetchone()[0]
    conn.rollback()
    return value


def _force_race(monkeypatch):
    """Câu tra trùng của mỗi bên chạy xong rồi chờ bên kia tới cùng chỗ (tối đa 2 giây) trước khi đi
    tiếp. Không có khoá thì cả hai đều tra thấy "chưa có". Có khoá thì bên sau còn đang chờ khoá (chưa
    tới câu tra) nên bên trước hết 2 giây chờ rồi đi tiếp, insert và commit."""
    barrier = threading.Barrier(2)
    real = db.find_manual_job_duplicate

    def find_then_wait(conn, **kw):
        result = real(conn, **kw)
        try:
            barrier.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        return result

    # create_manual_job gọi qua biến module db.jobs, route gọi qua gói db.
    monkeypatch.setattr("db.jobs.find_manual_job_duplicate", find_then_wait)
    monkeypatch.setattr(db, "find_manual_job_duplicate", find_then_wait)


def _run_two(fn_a, fn_b):
    workers = [_in_thread(fn_a), _in_thread(fn_b)]
    results = []
    for t, out in workers:
        t.join(30)
        assert not t.is_alive(), "một bên chưa chạy xong"
        assert "error" not in out, out
        results.append(out["value"])
    return results


# ------------------------------------------------------------------ db.create_manual_job
def _create_via_db(company, title, level_code):
    def run():
        conn = psycopg2.connect(TEST_DATABASE_URL)
        conn.autocommit = False
        try:
            job_id = db.create_manual_job(
                conn, job_title=title, company_id=company,
                level_id=db.get_level_id(conn, level_code), province_id=None,
            )
            conn.commit()
            return job_id
        finally:
            conn.rollback()
            conn.close()
    return run


def test_two_manual_creates_with_same_key_and_level_make_one_job(pg_conn, monkeypatch):
    company, title = _company(pg_conn), f"Data Analyst {uuid.uuid4().hex[:6]}"
    _force_race(monkeypatch)
    a, b = _run_two(_create_via_db(company, title, "Junior"), _create_via_db(company, title, "Junior"))

    assert a == b
    assert _scalar(pg_conn, "SELECT count(*) FROM job_postings WHERE job_title = %s", (title,)) == 1


def test_same_key_but_different_level_still_creates_both(pg_conn, monkeypatch):
    company, title = _company(pg_conn), f"Data Analyst {uuid.uuid4().hex[:6]}"
    _force_race(monkeypatch)
    a, b = _run_two(_create_via_db(company, title, "Junior"), _create_via_db(company, title, "Senior"))

    assert a != b
    assert _scalar(pg_conn, "SELECT count(*) FROM job_postings WHERE job_title = %s", (title,)) == 2
    assert _scalar(pg_conn, "SELECT count(DISTINCT dedup_key) FROM job_postings WHERE job_title = %s",
                   (title,)) == 1


def test_control_without_the_lock_the_same_race_creates_two_jobs(pg_conn, monkeypatch):
    """Đối chứng: tắt khoá thì đúng kịch bản đầu tiên sinh hai job. Không bảo vệ điều gì của code; chứng
    minh test dựng đúng cuộc đua."""
    monkeypatch.setattr("db.jobs.lock_job_dedup_key", lambda *a, **k: None)
    company, title = _company(pg_conn), f"Data Analyst {uuid.uuid4().hex[:6]}"
    _force_race(monkeypatch)
    a, b = _run_two(_create_via_db(company, title, "Junior"), _create_via_db(company, title, "Junior"))

    assert a != b
    assert _scalar(pg_conn, "SELECT count(*) FROM job_postings WHERE job_title = %s", (title,)) == 2


# ------------------------------------------------------------------ route POST /jobs
def _post_job(company, user_id, title):
    def run():
        conn = psycopg2.connect(TEST_DATABASE_URL)
        conn.autocommit = False
        try:
            return create_job(
                payload=JobCreate(job_title=title, company_id=company, level_code="Junior"),
                conn=conn, user={"sub": user_id},
            )
        finally:
            conn.rollback()
            conn.close()
    return run


def test_route_two_posts_one_new_one_existing_and_a_single_create_job_log(pg_conn, monkeypatch):
    company, user = _company(pg_conn), _user(pg_conn)
    title = f"Data Analyst {uuid.uuid4().hex[:6]}"
    _force_race(monkeypatch)
    a, b = _run_two(_post_job(company, user, title), _post_job(company, user, title))

    assert a["job_id"] == b["job_id"]
    assert sorted([a["was_existing"], b["was_existing"]]) == [False, True]
    assert _scalar(pg_conn, "SELECT count(*) FROM job_postings WHERE job_title = %s", (title,)) == 1
    assert _scalar(pg_conn, "SELECT count(*) FROM audit_logs WHERE action_type = 'CREATE_JOB' "
                            "AND entity_id = %s", (a["job_id"],)) == 1


def test_route_returns_503_and_writes_nothing_when_the_lock_wait_times_out(pg_conn, conns, monkeypatch):
    company, user = _company(pg_conn), _user(pg_conn)
    title = f"Data Analyst {uuid.uuid4().hex[:6]}"
    holder, caller = conns
    db.lock_job_dedup_key(holder, company_id=company, job_title=title, province_id=None)

    real_lock = db.lock_job_dedup_key
    monkeypatch.setattr(db, "lock_job_dedup_key", lambda conn, **kw: real_lock(conn, timeout_ms=200, **kw))
    with pytest.raises(HTTPException) as exc:
        create_job(payload=JobCreate(job_title=title, company_id=company, level_code="Junior"),
                   conn=caller, user={"sub": user})
    caller.rollback()                                    # như get_db khi route raise

    assert exc.value.status_code == 503
    assert exc.value.detail["error_code"] == error_codes.JOB_DEDUP_LOCK_TIMEOUT
    assert _scalar(pg_conn, "SELECT count(*) FROM job_postings WHERE job_title = %s", (title,)) == 0
    assert _scalar(pg_conn, "SELECT count(*) FROM audit_logs") == 0
