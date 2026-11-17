"""
E1 trên POSTGRES THẬT: thứ tự khoá của lớp db luôn là JOB trước, LISTING sau.

Các đường ghi nhận job_id (insert_listing, close_expired_listings, close_listing_dead, update_job, gộp job...)
đều khoá dòng job rồi mới ghi listing. Hai đường nhận URL thay vì job_id (mark_listing_seen,
mark_listing_detail_checked) trước đây UPDATE listing trước rồi mới khoá job (qua sync_job_from_listings): đi
ngược listing -> job, nên gặp một đường đi xuôi là hai transaction giữ khoá của nhau (deadlock). Test ở đây
dựng đúng tình huống đó, và kiểm hàm lock_job_of_listing mà hai đường kia dùng để khoá job trước.

Cách chạy giống tests/test_pg_job_sync.py: đặt TEST_DATABASE_URL (tên database chứa "test"); không đặt thì
cả file bị bỏ qua.
"""
import os
import threading
import time
import uuid
from datetime import date
from urllib.parse import urlparse

import psycopg2
import pytest
from psycopg2 import errors

from scrapjd import db
from scrapjd.db import listing_state
from scrapjd.db.listing_state import lock_job_of_listing

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")
FUTURE, FUTURE2 = date(2099, 12, 31), date(2100, 6, 30)
AFTER_ALL = date(2101, 1, 1)           # `today` khiến mọi listing hạn FUTURE/FUTURE2 thành đã hết hạn


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


def _url():
    return f"https://example.com/{uuid.uuid4()}"


def _job(conn, *, deadline=FUTURE):
    """Job mới (một listing: chính source_url của nó); trả (job_id, url của listing đó)."""
    cid = str(uuid.uuid4())
    url = _url()
    with conn.cursor() as cur:
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)", (cid, f"Cty {cid[:8]}"))
    job_id = db.insert_job(
        conn, company_id=cid, job_title="Data Analyst", matching_industry="Data", level_id=None,
        province_id=None, work_type=None, currency="VNĐ", salary_min=None, salary_max=None,
        salary_type="NEGOTIABLE", source_url=url, source_name="Fake", deadline=deadline, detail_fetched=True)
    conn.commit()
    return job_id, url


def _job_row_is_locked(job_id):
    """Dòng job đang bị transaction khác giữ khoá? Thử khoá từ kết nối riêng bằng NOWAIT."""
    conn = psycopg2.connect(TEST_DATABASE_URL)
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("SELECT 1 FROM job_postings WHERE job_id = %s FOR NO KEY UPDATE NOWAIT", (job_id,))
            except errors.LockNotAvailable:
                return True
            return False
    finally:
        conn.rollback()
        conn.close()


# ============================================================ lock_job_of_listing
def test_lock_job_of_listing_returns_the_job_and_holds_its_lock(pg_conn):
    job, url = _job(pg_conn)
    assert lock_job_of_listing(pg_conn, url) == job
    assert _job_row_is_locked(job)
    pg_conn.commit()
    assert not _job_row_is_locked(job)                 # khoá thả khi transaction kết thúc


def test_lock_job_of_listing_returns_none_for_unknown_url(pg_conn):
    assert lock_job_of_listing(pg_conn, _url()) is None
    pg_conn.rollback()


def test_lock_job_of_listing_follows_a_listing_moved_to_another_job_while_waiting(pg_conn):
    """Gộp job chuyển listing sang job khác đúng lúc ta chờ khoá job cũ: sau khi khoá được phải tra lại và
    khoá job mới, không được trả job cũ (rồi ghi listing mà không giữ khoá job đang sở hữu nó)."""
    old_job, url = _job(pg_conn)
    new_job, _ = _job(pg_conn)
    mover = psycopg2.connect(TEST_DATABASE_URL)
    result, errors_seen = [], []
    try:
        with mover.cursor() as cur:
            cur.execute("SELECT 1 FROM job_postings WHERE job_id = %s FOR UPDATE", (old_job,))
            cur.execute("UPDATE job_sources_log SET job_id = %s WHERE source_url = %s", (new_job, url))

        def waiter():
            conn = psycopg2.connect(TEST_DATABASE_URL)
            try:
                result.append(lock_job_of_listing(conn, url))
                result.append(_job_row_is_locked(new_job))
                conn.commit()
            except Exception as exc:  # noqa: BLE001
                errors_seen.append(exc)
            finally:
                conn.close()

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.5)                                # waiter đã đọc job cũ và đang chờ khoá nó
        mover.commit()
        thread.join(timeout=20)
    finally:
        mover.close()
    assert errors_seen == []
    assert result == [new_job, True]


# ============================================================ không deadlock
def _run_against_close_expired(pg_conn, call_by_url):
    """Chạy `call_by_url(conn, url)` (đường nhận URL) song song với close_expired_listings (đường nhận job_id,
    đi job -> listing) trên CÙNG một job và một listing. Đường nhận URL bị làm chậm đúng giữa UPDATE listing và
    đồng bộ job, để đường kia chắc chắn chen vào đó. Trả danh sách lỗi."""
    job, url = _job(pg_conn)
    real_sync = listing_state.sync_job_from_listings
    after_write = threading.Event()
    other_started = threading.Event()
    errors_seen = []

    def slow_sync(conn, job_id):
        after_write.set()
        other_started.wait(timeout=5)
        time.sleep(0.5)                                # đủ để đường kia giành khoá trước nếu không bị chặn
        return real_sync(conn, job_id)

    def by_url():
        conn = psycopg2.connect(TEST_DATABASE_URL)
        try:
            call_by_url(conn, url)
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            errors_seen.append(exc)
        finally:
            conn.close()

    def by_job():
        conn = psycopg2.connect(TEST_DATABASE_URL)
        try:
            after_write.wait(timeout=5)
            other_started.set()
            listing_state.close_expired_listings(conn, job, AFTER_ALL)
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            errors_seen.append(exc)
        finally:
            conn.close()

    listing_state.sync_job_from_listings = slow_sync
    try:
        threads = [threading.Thread(target=by_url), threading.Thread(target=by_job)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        listing_state.sync_job_from_listings = real_sync
    return errors_seen


def test_mark_listing_seen_does_not_deadlock_with_close_expired_listings(pg_conn):
    assert _run_against_close_expired(pg_conn, db.mark_listing_seen) == []


def test_mark_source_detail_checked_does_not_deadlock_with_close_expired_listings(pg_conn):
    errors_seen = _run_against_close_expired(
        pg_conn, lambda conn, url: db.mark_source_detail_checked(conn, url, deadline=FUTURE2))
    assert errors_seen == []


def test_lock_order_failure_would_be_reported_as_deadlock_not_hang(pg_conn):
    """Chốt rằng tình huống dựng ở trên thật sự là deadlock khi đi ngược thứ tự: UPDATE listing trước rồi khoá
    job (cách cũ) cho DeadlockDetected, để hai test trên đỏ đúng lý do nếu ai đó bỏ lock_job_of_listing."""
    def listing_first(conn, url):
        with conn.cursor() as cur:
            cur.execute("UPDATE job_sources_log SET last_seen_at = GREATEST(last_seen_at, now()) "
                        "WHERE source_url = %s RETURNING job_id", (url,))
            job_id = str(cur.fetchone()[0])
        listing_state.sync_job_from_listings(conn, job_id)

    errors_seen = _run_against_close_expired(pg_conn, listing_first)
    assert len(errors_seen) == 1 and isinstance(errors_seen[0], errors.DeadlockDetected)
