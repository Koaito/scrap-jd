"""
C3a trên POSTGRES THẬT: nhánh hạn của check_expired_source_jobs chạy trên listing.

  - list_checkable_listings: chỉ listing OPEN/UNKNOWN của job OPEN;
  - close_expired_listings: đóng 'expired_auto' đúng listing quá hạn, kiểm lại hạn trong chính câu UPDATE,
    không đụng listing không hạn / hạn chưa tới / đã CLOSED;
  - script.run(): job chỉ CLOSED khi hết listing sống; job còn listing khác vẫn OPEN và URL/hạn suy ra từ
    listing còn lại; giá trị job luôn khớp db.derive_job_from_listings sau lượt chạy.

Cách chạy giống tests/test_pg_job_sync.py: đặt TEST_DATABASE_URL (tên chứa "test"); không đặt thì bỏ qua.
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
TODAY = date.today()
PAST, PAST2 = TODAY - timedelta(days=3), TODAY - timedelta(days=1)
FUTURE, FUTURE2 = TODAY + timedelta(days=30), TODAY + timedelta(days=60)


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


class _KeepOpen:
    """Bọc connection để run() gọi close() không đóng connection của test."""

    def __init__(self, conn):
        self._conn = conn

    def close(self):
        pass

    def __getattr__(self, name):
        return getattr(self._conn, name)


class _Checker:
    def __init__(self, codes=None):
        self.codes, self.calls = codes or {}, []

    def check(self, url):
        self.calls.append(url)
        return self.codes.get(url, 200)


@pytest.fixture
def run_script(pg_conn, monkeypatch):
    checker = _Checker()
    monkeypatch.setattr(db, "get_connection", lambda: _KeepOpen(pg_conn))
    monkeypatch.setattr(cej, "_Throttled404Checker", lambda: checker)

    def go(**kw):
        kw.setdefault("skip_cv_cleanup", True)
        kw.setdefault("check_deadline_only", True)
        return cej.run(**kw)

    go.checker = checker
    return go


def _url():
    return f"https://example.com/{uuid.uuid4()}"


def _job(conn, *, deadline=FUTURE, url=None):
    cid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)", (cid, f"Cty {cid[:8]}"))
    job_id = db.insert_job(
        conn, company_id=cid, job_title="Data Analyst", matching_industry="Data", level_id=None,
        province_id=None, work_type=None, currency="VNĐ", salary_min=None, salary_max=None,
        salary_type="NEGOTIABLE", source_url=url or _url(), source_name="Fake", deadline=deadline,
        detail_fetched=True)
    conn.commit()
    return job_id


def _add_listing(conn, job_id, *, deadline=FUTURE, status="OPEN"):
    url = _url()
    assert db.link_repost_source(conn, job_id, source_name="Fake", source_url=url, deadline=deadline)
    if status != "OPEN":
        with conn.cursor() as cur:
            cur.execute("UPDATE job_sources_log SET listing_status = %s WHERE source_url = %s", (status, url))
    conn.commit()
    return url


def _set_listing(conn, url, *, deadline=None, status=None):
    with conn.cursor() as cur:
        if deadline is not None:
            cur.execute("UPDATE job_sources_log SET deadline = %s WHERE source_url = %s", (deadline, url))
        if status == "CLOSED":
            cur.execute("UPDATE job_sources_log SET listing_status = 'CLOSED', closed_reason = 'staff', "
                        "closed_at = now() WHERE source_url = %s", (url,))
        elif status is not None:
            cur.execute("UPDATE job_sources_log SET listing_status = %s WHERE source_url = %s", (status, url))
    conn.commit()


def _listing(conn, url):
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT listing_status, closed_reason, closed_at IS NOT NULL, deadline "
                    "FROM job_sources_log WHERE source_url = %s", (url,))
        return cur.fetchone()


def _job_row(conn, job_id):
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT job_status::text, closed_reason, deadline, source_url FROM job_postings "
                    "WHERE job_id = %s", (job_id,))
        return cur.fetchone()


def _first_listing_url(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT source_url FROM job_sources_log WHERE job_id = %s ORDER BY first_seen_at, source_url",
                    (job_id,))
        return cur.fetchone()[0]


def _assert_job_matches_derivation(conn, job_id):
    conn.commit()
    job = next(j for j in db.list_jobs_with_listings(conn) if j["job_id"] == str(job_id))
    derived = db.derive_job_from_listings(job["listings"])
    assert (job["job_status"], job["closed_reason"], job["deadline"], job["source_url"]) == (
        derived.job_status, derived.closed_reason, derived.deadline, derived.source_url)


# ============================================================ list_checkable_listings
def test_list_checkable_returns_only_live_listings_of_open_jobs(pg_conn):
    open_job = _job(pg_conn)
    first = _first_listing_url(pg_conn, open_job)
    unknown = _add_listing(pg_conn, open_job, status="UNKNOWN")
    closed = _add_listing(pg_conn, open_job)
    _set_listing(pg_conn, closed, status="CLOSED")
    closed_job = _job(pg_conn)
    db.update_job(pg_conn, closed_job, job_status="CLOSED")
    pg_conn.commit()

    rows = db.list_checkable_listings(pg_conn)

    assert sorted(r[2] for r in rows) == sorted([first, unknown])
    assert {str(r[0]) for r in rows} == {str(open_job)}
    assert all(len(r) == 4 for r in rows)


def test_list_checkable_groups_listings_of_a_job_together(pg_conn):
    a, b = _job(pg_conn), _job(pg_conn)
    _add_listing(pg_conn, a)
    _add_listing(pg_conn, b)
    _add_listing(pg_conn, a)
    ids = [str(r[0]) for r in db.list_checkable_listings(pg_conn)]
    # mỗi job một khối liền nhau (không xen kẽ)
    assert [i for n, i in enumerate(ids) if n == 0 or ids[n - 1] != i] == list(dict.fromkeys(ids))


def test_list_checkable_includes_manual_jobs(pg_conn):
    job = _job(pg_conn, url="manual://" + str(uuid.uuid4()))
    rows = db.list_checkable_listings(pg_conn)
    assert [str(r[0]) for r in rows] == [str(job)]
    assert rows[0][2].startswith("manual://")


# ============================================================ close_expired_listings
def test_close_expired_listings_closes_only_past_deadline_live_listings(pg_conn):
    job = _job(pg_conn, deadline=PAST)
    expired_open = _first_listing_url(pg_conn, job)
    expired_unknown = _add_listing(pg_conn, job, deadline=PAST2, status="UNKNOWN")
    future = _add_listing(pg_conn, job, deadline=FUTURE)
    no_deadline = _add_listing(pg_conn, job, deadline=None)
    already_closed = _add_listing(pg_conn, job, deadline=PAST)
    _set_listing(pg_conn, already_closed, status="CLOSED")
    before_closed = _listing(pg_conn, already_closed)

    assert db.close_expired_listings(pg_conn, job, TODAY) == 2
    pg_conn.commit()

    assert _listing(pg_conn, expired_open)[:3] == ("CLOSED", "expired_auto", True)
    assert _listing(pg_conn, expired_unknown)[:3] == ("CLOSED", "expired_auto", True)
    assert _listing(pg_conn, future)[0] == "OPEN"
    assert _listing(pg_conn, no_deadline)[0] == "OPEN"
    assert _listing(pg_conn, already_closed) == before_closed      # giữ lý do 'staff' và giờ đóng của nó


def test_close_expired_listings_deadline_of_today_is_not_expired(pg_conn):
    job = _job(pg_conn, deadline=TODAY)
    assert db.close_expired_listings(pg_conn, job, TODAY) == 0
    pg_conn.commit()
    assert _listing(pg_conn, _first_listing_url(pg_conn, job))[0] == "OPEN"


def test_close_expired_listings_rechecks_deadline_in_the_update(pg_conn):
    """Danh sách đọc từ trước có thể cũ: hạn vừa được dời thì listing không bị đóng."""
    job = _job(pg_conn, deadline=PAST)
    url = _first_listing_url(pg_conn, job)
    _set_listing(pg_conn, url, deadline=FUTURE2)                    # hạn dời giữa lúc đọc và lúc ghi
    assert db.close_expired_listings(pg_conn, job, TODAY) == 0
    pg_conn.commit()
    assert _listing(pg_conn, url)[0] == "OPEN"


def test_close_expired_listings_missing_job_is_a_noop(pg_conn):
    assert db.close_expired_listings(pg_conn, str(uuid.uuid4()), TODAY) == 0
    pg_conn.rollback()


def test_close_expired_listings_is_idempotent(pg_conn):
    job = _job(pg_conn, deadline=PAST)
    assert db.close_expired_listings(pg_conn, job, TODAY) == 1
    assert db.close_expired_listings(pg_conn, job, TODAY) == 0
    pg_conn.commit()


# ============================================================ script.run (nhánh hạn)
def test_run_closes_job_when_its_only_listing_expired(pg_conn, run_script):
    job = _job(pg_conn, deadline=PAST)
    url = _first_listing_url(pg_conn, job)

    stats = run_script()

    assert stats["expired_by_deadline"] == 1 and stats["listings_expired_by_deadline"] == 1
    assert _listing(pg_conn, url)[:3] == ("CLOSED", "expired_auto", True)
    assert _job_row(pg_conn, job)[:2] == ("CLOSED", "expired_auto")
    _assert_job_matches_derivation(pg_conn, job)


def test_run_keeps_job_open_while_another_listing_is_alive(pg_conn, run_script):
    job = _job(pg_conn, deadline=PAST)
    old = _first_listing_url(pg_conn, job)
    alive = _add_listing(pg_conn, job, deadline=FUTURE)
    _set_listing(pg_conn, old, deadline=PAST)
    db.sync_job_from_listings(pg_conn, str(job))
    pg_conn.commit()

    stats = run_script()

    assert stats["expired_by_deadline"] == 0 and stats["listings_expired_by_deadline"] == 1
    assert _listing(pg_conn, old)[:2] == ("CLOSED", "expired_auto")
    assert _listing(pg_conn, alive)[0] == "OPEN"
    status, reason, deadline, source_url = _job_row(pg_conn, job)
    assert (status, reason, deadline, source_url) == ("OPEN", None, FUTURE, alive)
    _assert_job_matches_derivation(pg_conn, job)


def test_run_keeps_job_open_while_a_listing_has_no_deadline(pg_conn, run_script):
    job = _job(pg_conn, deadline=PAST)
    _add_listing(pg_conn, job, deadline=None)
    run_script()
    assert _job_row(pg_conn, job)[0] == "OPEN"
    _assert_job_matches_derivation(pg_conn, job)


def test_run_closes_job_when_all_live_listings_expired_even_if_one_is_unknown(pg_conn, run_script):
    job = _job(pg_conn, deadline=PAST)
    _add_listing(pg_conn, job, deadline=PAST2, status="UNKNOWN")
    _set_listing(pg_conn, _first_listing_url(pg_conn, job), deadline=PAST)

    stats = run_script()

    assert stats["expired_by_deadline"] == 1 and stats["listings_expired_by_deadline"] == 2
    assert _job_row(pg_conn, job)[:2] == ("CLOSED", "expired_auto")
    _assert_job_matches_derivation(pg_conn, job)


def test_run_does_not_close_unknown_listing_without_deadline(pg_conn, run_script):
    job = _job(pg_conn, deadline=PAST)
    unknown = _add_listing(pg_conn, job, deadline=None, status="UNKNOWN")
    run_script()
    assert _listing(pg_conn, unknown)[0] == "UNKNOWN"
    assert _job_row(pg_conn, job)[0] == "OPEN"


def test_run_closes_manual_job_with_past_deadline(pg_conn, run_script):
    job = _job(pg_conn, deadline=PAST, url="manual://" + str(uuid.uuid4()))
    run_script()
    assert _job_row(pg_conn, job)[:2] == ("CLOSED", "expired_auto")


def test_run_leaves_closed_jobs_alone(pg_conn, run_script):
    job = _job(pg_conn, deadline=PAST)
    db.update_job(pg_conn, job, job_status="CLOSED")                # staff
    pg_conn.commit()
    stats = run_script()
    assert stats["checked"] == 0 and stats["expired_by_deadline"] == 0
    assert _job_row(pg_conn, job)[:2] == ("CLOSED", "staff")


def test_run_dry_run_writes_nothing(pg_conn, run_script):
    job = _job(pg_conn, deadline=PAST)
    url = _first_listing_url(pg_conn, job)

    stats = run_script(dry_run=True)

    assert stats["expired_by_deadline"] == 1 and stats["listings_expired_by_deadline"] == 1
    assert _listing(pg_conn, url)[0] == "OPEN"
    assert _job_row(pg_conn, job)[0] == "OPEN"


def test_run_twice_changes_nothing_the_second_time(pg_conn, run_script):
    _job(pg_conn, deadline=PAST)
    run_script()
    stats = run_script()
    assert stats["checked"] == 0 and stats["expired_by_deadline"] == 0


def test_run_does_not_bump_updated_at_beyond_the_close(pg_conn, run_script):
    job = _job(pg_conn, deadline=PAST)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT updated_at FROM job_postings WHERE job_id = %s", (job,))
        before = cur.fetchone()[0]
    pg_conn.commit()
    run_script()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT updated_at FROM job_postings WHERE job_id = %s", (job,))
        after = cur.fetchone()[0]
    pg_conn.commit()
    assert after == before             # đồng bộ theo listing không phải người sửa job


def test_run_network_branch_skips_job_closed_by_deadline(pg_conn, run_script):
    expired = _job(pg_conn, deadline=PAST)
    alive_url = _url()
    alive = _job(pg_conn, deadline=FUTURE, url=alive_url)

    stats = run_script(check_deadline_only=False)

    assert run_script.checker.calls == [alive_url]
    assert stats["expired_by_deadline"] == 1 and stats["still_alive"] == 1
    assert _job_row(pg_conn, expired)[0] == "CLOSED" and _job_row(pg_conn, alive)[0] == "OPEN"
