"""
Migration 0048 (đóng listing UNKNOWN di sản) và 0049 (bỏ DEFAULT 'UNKNOWN') trên POSTGRES THẬT.

Dựng DB đúng trạng thái ngay trước 0048 (đường nâng cấp thật, không phải schema.sql), nhét các hình dạng dữ liệu
đã thấy trong DB thật, chạy migration bằng bộ chạy thật rồi kiểm. Chứng minh:
  - job không còn listing OPEN và hạn đã qua: listing UNKNOWN thành CLOSED expired_auto, job thành CLOSED
    expired_auto, hạn / source_url / updated_at của job không đổi;
  - job còn listing OPEN: listing UNKNOWN thành CLOSED 'unknown', job vẫn OPEN, listing OPEN không bị đụng;
  - không đóng nhầm: job không hạn, job còn hạn, job đã CLOSED, job không có UNKNOWN;
  - sau migration, giá trị job khớp 100% giá trị suy ra từ listing (cùng phép kiểm của check-listing-derivation);
  - bẫy hẹn giờ biến mất: listing OPEN của job nhóm A hết hạn thì job đóng được;
  - idempotent; 0049 làm "quên đặt listing_status" thành lỗi NOT NULL nhưng vẫn ghi được khi đặt rõ.
"""

import uuid
from datetime import date, timedelta

import psycopg2
import pytest

from pg_migration_helpers import connect, legacy_db_before, migrate_one, requires_pg, temp_database
from scrapjd import db
from scrapjd.db.job_sync import diff_job_from_derived

pytestmark = requires_pg

M48 = "0048_close_legacy_unknown_listings.sql"
M49 = "0049_job_sources_log_listing_status_no_default.sql"
TODAY = date.today()
PAST = TODAY - timedelta(days=10)
FUTURE = TODAY + timedelta(days=10)


def _company(cur):
    cid = str(uuid.uuid4())
    cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)", (cid, f"Cty {cid[:8]}"))
    return cid


def _job(cur, cid, *, status="OPEN", deadline=None, url=None, reason=None):
    jid = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO job_postings (job_id, company_id, job_title, job_status, deadline, source_url, closed_reason, "
        "updated_at) VALUES (%s, %s, 'Data Analyst', %s, %s, %s, %s, '2026-01-01 00:00+00')",
        (jid, cid, status, deadline, url, reason),
    )
    return jid


def _listing(cur, jid, url, status, *, deadline=None, reason=None, seen="2026-10-01 00:00+00"):
    cur.execute(
        "INSERT INTO job_sources_log (job_id, source_name, source_url, listing_status, deadline, closed_reason, "
        "closed_at, first_seen_at, last_seen_at) VALUES (%s, 'TopCV', %s, %s, %s, %s, "
        "CASE WHEN %s = 'CLOSED' THEN '2026-10-15 00:00+00'::timestamptz END, %s, %s)",
        (jid, url, status, deadline, reason, status, seen, seen),
    )


def _job_row(conn, jid):
    with conn.cursor() as cur:
        cur.execute("SELECT job_status::text, closed_reason, closed_at IS NOT NULL, deadline, source_url, updated_at "
                    "FROM job_postings WHERE job_id = %s", (jid,))
        return cur.fetchone()


def _listings(conn, jid):
    with conn.cursor() as cur:
        cur.execute("SELECT source_url, listing_status, closed_reason FROM job_sources_log WHERE job_id = %s "
                    "ORDER BY source_url", (jid,))
        return cur.fetchall()


def _snapshot(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT log_id::text, listing_status, closed_reason, closed_at FROM job_sources_log ORDER BY 1")
        listings = cur.fetchall()
        cur.execute("SELECT job_id::text, job_status::text, closed_reason, closed_at FROM job_postings ORDER BY 1")
        return listings, cur.fetchall()


@pytest.fixture
def prepared(tmp_path):
    """DB ngay trước 0048 với đủ các hình dạng dữ liệu. Trả (conn, ids)."""
    with temp_database() as url, connect(url) as conn:
        legacy_db_before(conn, tmp_path, 48)
        ids = {}
        with conn.cursor() as cur:
            cid = _company(cur)
            # Nhóm B (22 job trong DB thật): listing hiện hành đã đóng expired_auto, UNKNOWN giữ job ở OPEN.
            b = _job(cur, cid, deadline=PAST, url="https://x/b-unknown")
            _listing(cur, b, "https://x/b-closed", "CLOSED", deadline=PAST, reason="expired_auto",
                     seen="2026-10-02 00:00+00")
            _listing(cur, b, "https://x/b-unknown", "UNKNOWN", seen="2026-10-26 05:00+00")
            _listing(cur, b, "https://x/b-unknown2", "UNKNOWN", seen="2026-10-20 00:00+00")   # 2 UNKNOWN: đóng cả hai
            ids["b"] = b
            # Nhóm A (7 job): còn listing OPEN, UNKNOWN đi kèm.
            a = _job(cur, cid, deadline=FUTURE, url="https://x/a-open")
            _listing(cur, a, "https://x/a-open", "OPEN", deadline=FUTURE)
            _listing(cur, a, "https://x/a-unknown", "UNKNOWN", seen="2026-10-26 05:00+00")
            ids["a"] = a
            # Job quá hạn vài ngày mà lượt quét hạn (C3a) chưa kịp đóng: listing OPEN còn đó (hạn đã qua), kèm UNKNOWN.
            # Phải theo luật 2 (job còn listing OPEN), KHÔNG được luật 1 đóng job: việc đóng listing OPEN là của C3a.
            od = _job(cur, cid, deadline=PAST, url="https://x/od-open")
            _listing(cur, od, "https://x/od-open", "OPEN", deadline=PAST)
            _listing(cur, od, "https://x/od-unknown", "UNKNOWN")
            ids["open_overdue"] = od
            # Không được đụng:
            nodl = _job(cur, cid, deadline=None, url="https://x/nodl")                      # chỉ UNKNOWN, không hạn
            _listing(cur, nodl, "https://x/nodl", "UNKNOWN")
            ids["no_deadline"] = nodl
            fut = _job(cur, cid, deadline=FUTURE, url="https://x/fut")                      # chỉ UNKNOWN, hạn chưa qua
            _listing(cur, fut, "https://x/fut", "UNKNOWN", deadline=FUTURE)
            ids["future"] = fut
            closed = _job(cur, cid, status="CLOSED", deadline=PAST, url="https://x/c", reason="staff")
            _listing(cur, closed, "https://x/c", "CLOSED", deadline=PAST, reason="staff")
            ids["closed"] = closed
            clean = _job(cur, cid, deadline=FUTURE, url="https://x/clean")
            _listing(cur, clean, "https://x/clean", "OPEN", deadline=FUTURE)
            ids["clean"] = clean
        conn.commit()
        ids["tmp"] = tmp_path
        yield conn, ids


# ============================================================ 0048
def test_group_b_listings_and_job_are_closed_as_expired_auto_without_touching_derived_fields(prepared):
    conn, ids = prepared
    before = _job_row(conn, ids["b"])
    assert before[0] == "OPEN"

    assert migrate_one(conn, ids["tmp"], M48) == [M48]

    assert _listings(conn, ids["b"]) == [("https://x/b-closed", "CLOSED", "expired_auto"),
                                         ("https://x/b-unknown", "CLOSED", "expired_auto"),
                                         ("https://x/b-unknown2", "CLOSED", "expired_auto")]
    status, reason, has_closed_at, deadline, url, updated_at = _job_row(conn, ids["b"])
    assert (status, reason, has_closed_at) == ("CLOSED", "expired_auto", True)
    assert deadline == before[3] and url == before[4]            # hạn và source_url suy ra không đổi
    assert updated_at == before[5]                                # máy đóng, không phải sửa tay: updated_at giữ nguyên


def test_group_a_unknown_listing_is_closed_as_unknown_and_job_stays_open(prepared):
    conn, ids = prepared
    before = _job_row(conn, ids["a"])

    migrate_one(conn, ids["tmp"], M48)

    assert _listings(conn, ids["a"]) == [("https://x/a-open", "OPEN", None),
                                         ("https://x/a-unknown", "CLOSED", "unknown")]
    assert _job_row(conn, ids["a"]) == before                     # job không đổi một cột nào


def test_nothing_else_is_touched(prepared):
    conn, ids = prepared
    keep = {k: (_job_row(conn, ids[k]), _listings(conn, ids[k])) for k in ("no_deadline", "future", "closed", "clean")}

    migrate_one(conn, ids["tmp"], M48)

    for k, snap in keep.items():
        assert (_job_row(conn, ids[k]), _listings(conn, ids[k])) == snap, k
    assert _listings(conn, ids["no_deadline"])[0][1] == "UNKNOWN"   # không đủ bằng chứng để đóng
    assert _listings(conn, ids["future"])[0][1] == "UNKNOWN"


def test_job_values_match_derivation_from_listings_everywhere(prepared):
    """Cùng phép kiểm của `check-listing-derivation --strict`: 0 job lệch sau migration."""
    conn, ids = prepared
    migrate_one(conn, ids["tmp"], M48)

    mismatched = {}
    for job in db.list_jobs_with_listings(conn):
        derived = db.derive_job_from_listings(job["listings"])
        if derived is not None:
            changes = diff_job_from_derived(job, derived)
            if changes:
                mismatched[job["job_id"]] = changes
    assert mismatched == {}


def test_time_bomb_is_gone_group_a_job_closes_when_its_open_listing_expires(prepared):
    """Trước 0048: listing OPEN hết hạn, UNKNOWN giữ job OPEN mãi. Sau 0048: job đóng được."""
    conn, ids = prepared
    migrate_one(conn, ids["tmp"], M48)

    assert db.close_expired_listings(conn, ids["a"], FUTURE + timedelta(days=1)) == 1
    db.sync_job_from_listings(conn, ids["a"])
    conn.commit()

    status, reason, *_ = _job_row(conn, ids["a"])
    assert (status, reason) == ("CLOSED", "expired_auto")


def test_closing_with_expired_auto_keeps_the_job_self_healing():
    """Lý do chọn expired_auto cho nhóm B (xem đầu file 0048): đây là lý do được tự mở lại khi tin đăng lại."""
    assert "expired_auto" in db.AUTO_REOPEN_REASONS


def test_0048_is_idempotent(prepared):
    conn, ids = prepared
    migrate_one(conn, ids["tmp"], M48)
    after_first = _snapshot(conn)

    sql_text = (ids["tmp"] / M48).read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql_text)
        cur.execute(sql_text)
    conn.commit()

    assert _snapshot(conn) == after_first


def test_0048_on_empty_database_does_nothing(tmp_path):
    with temp_database() as url, connect(url) as conn:
        legacy_db_before(conn, tmp_path, 48)
        assert migrate_one(conn, tmp_path, M48) == [M48]


# ============================================================ 0049
def test_0049_drops_default_so_forgetting_listing_status_fails_loudly(prepared):
    conn, ids = prepared
    migrate_one(conn, ids["tmp"], M48)

    def insert(status_cols):
        with conn.cursor() as cur:
            cur.execute(f"INSERT INTO job_sources_log (job_id, source_name, source_url{status_cols[0]}) "
                        f"VALUES (%s, 'TopCV', %s{status_cols[1]})", (ids["clean"], f"https://x/{uuid.uuid4()}"))

    with conn.cursor() as cur:                                     # trước 0049 vẫn còn DEFAULT 'UNKNOWN'
        cur.execute("SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'job_sources_log' AND column_name = 'listing_status'")
        assert "UNKNOWN" in cur.fetchone()[0]

    assert migrate_one(conn, ids["tmp"], M49) == [M49]

    with conn.cursor() as cur:
        cur.execute("SELECT column_default FROM information_schema.columns "
                    "WHERE table_name = 'job_sources_log' AND column_name = 'listing_status'")
        assert cur.fetchone()[0] is None
    with pytest.raises(psycopg2.errors.NotNullViolation):
        insert(("", ""))
    conn.rollback()
    insert((", listing_status", ", 'OPEN'"))                       # đặt rõ thì vẫn ghi được
    conn.commit()


def test_0049_is_idempotent(prepared):
    conn, ids = prepared
    assert migrate_one(conn, ids["tmp"], M49) == [M49]
    sql_text = (ids["tmp"] / M49).read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql_text)
        cur.execute(sql_text)
    conn.commit()


def test_overdue_job_with_open_listing_is_not_closed_by_the_migration(prepared):
    """Ca mà luật 1 sai sẽ đóng job khi vẫn còn listing OPEN: việc đóng listing OPEN quá hạn thuộc về C3a."""
    conn, ids = prepared
    before = _job_row(conn, ids["open_overdue"])

    migrate_one(conn, ids["tmp"], M48)

    assert _job_row(conn, ids["open_overdue"]) == before and before[0] == "OPEN"
    assert _listings(conn, ids["open_overdue"]) == [("https://x/od-open", "OPEN", None),
                                                    ("https://x/od-unknown", "CLOSED", "unknown")]
