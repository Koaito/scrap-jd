"""
C1 nửa 1/2 trên POSTGRES THẬT: migration 0043 thêm trạng thái của từng listing (URL tin đăng) vào
job_sources_log: listing_status, deadline, first_seen_at, last_seen_at, closed_reason, closed_at.

Chứng minh:
  - backfill đúng quy tắc đã chốt: job CLOSED thì mọi listing CLOSED (chép lý do và giờ đóng của job); job
    OPEN thì listing hiện hành (source_url trùng job_postings.source_url) là OPEN còn listing khác là UNKNOWN;
    deadline chỉ chép cho listing hiện hành; first_seen_at = 00:00 giờ VN của collected_date; last_seen_at =
    detail_checked_at nếu có và không sớm hơn first_seen_at, ngược lại = first_seen_at;
  - job_postings KHÔNG bị đụng (kể cả updated_at);
  - kết quả không phụ thuộc TimeZone của session chạy migration;
  - các CHECK chặn dữ liệu sai: trạng thái lạ, lý do lạ, CLOSED thiếu lý do, không CLOSED mà có lý do,
    last_seen_at sớm hơn first_seen_at;
  - code cũ (INSERT không biết các cột mới) vẫn chạy được sau migration và ra dòng UNKNOWN, nên migration
    chạy TRƯỚC khi push code là an toàn;
  - chạy lại file không ghi đè gì (kể cả giá trị đã bị sửa tay sau lần chạy đầu);
  - có giao dịch khác giữ khoá thì migration báo hết thời gian chờ khoá, không đổi gì, chạy lại được sau đó.

Cách chạy như tests/test_pg_migrations.py: đặt TEST_DATABASE_URL (tên database chứa "test"); không đặt thì
cả file được bỏ qua.
"""
import uuid
from datetime import date, datetime, timezone

import psycopg2
import psycopg2.errors
import pytest

from pg_migration_helpers import (
    SQL_DIR,
    connect,
    legacy_db_before,
    migrate_one,
    requires_pg,
    temp_database,
)

pytestmark = requires_pg

_MIGRATION = "0043_add_listing_state_job_sources_log.sql"
_NEW_COLUMNS = ("listing_status", "deadline", "first_seen_at", "last_seen_at", "closed_reason", "closed_at")


def _utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# 00:00 giờ VN của 07/10/2026 = 17:00 UTC ngày 06/10.
_FIRST_SEEN_07_10 = _utc(2026, 10, 6, 17, 0, 0)
_FIRST_SEEN_01_09 = _utc(2026, 8, 31, 17, 0, 0)
_FIRST_SEEN_01_10 = _utc(2026, 9, 30, 17, 0, 0)
_DETAIL_J1 = _utc(2026, 10, 8, 3, 0, 0)
_DETAIL_J2B = _utc(2026, 10, 2, 5, 30, 0)
_CLOSED_AT_J3 = _utc(2026, 9, 20, 5, 0, 0)
_CLOSED_AT_J4 = _utc(2026, 9, 25, 9, 15, 0)


# ----------------------------------------------------------------------------- dữ liệu mẫu (trước 0043)
def _job(cur, title, *, status="OPEN", deadline=None, source_url=None, reason=None, closed_at=None):
    company_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)", (company_id, f"Cty {title}"))
    cur.execute(
        "INSERT INTO job_postings (job_id, company_id, job_title, job_status, deadline, source_url, "
        "closed_reason, closed_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (job_id, company_id, title, status, deadline, source_url, reason, closed_at),
    )
    return job_id


def _listing(cur, job_id, url, collected, detail_checked_at=None):
    log_id = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO job_sources_log (log_id, job_id, source_name, source_url, collected_date, "
        "detail_checked_at) VALUES (%s, %s, 'topcv', %s, %s, %s)",
        (log_id, job_id, url, collected, detail_checked_at),
    )
    return log_id


def _seed(conn):
    """Các ca: J1 OPEN một listing; J2 OPEN hai listing; J3 CLOSED expired_auto; J4 CLOSED staff hai listing;
    J5 CLOSED không rõ giờ đóng; J6 OPEN không có source_url; J7 detail_checked_at sớm hơn first_seen_at;
    J8 listing không có URL. Trả {tên: log_id hoặc job_id}."""
    ids = {}
    with conn.cursor() as cur:
        j1 = _job(cur, "J1", deadline=date(2026, 11, 30), source_url="https://x/j1")
        ids["L1"] = _listing(cur, j1, "https://x/j1", date(2026, 10, 7), _DETAIL_J1)

        j2 = _job(cur, "J2", deadline=date(2026, 12, 1), source_url="https://x/j2a")
        ids["L2a"] = _listing(cur, j2, "https://x/j2a", date(2026, 9, 1))
        ids["L2b"] = _listing(cur, j2, "https://x/j2b", date(2026, 10, 1), _DETAIL_J2B)

        j3 = _job(cur, "J3", status="CLOSED", deadline=date(2026, 9, 15), source_url="https://x/j3",
                  reason="expired_auto", closed_at=_CLOSED_AT_J3)
        ids["L3"] = _listing(cur, j3, "https://x/j3", date(2026, 9, 1))

        j4 = _job(cur, "J4", status="CLOSED", deadline=date(2026, 9, 30), source_url="https://x/j4a",
                  reason="staff", closed_at=_CLOSED_AT_J4)
        ids["L4a"] = _listing(cur, j4, "https://x/j4a", date(2026, 9, 1))
        ids["L4b"] = _listing(cur, j4, "https://x/j4b", date(2026, 9, 10))

        j5 = _job(cur, "J5", status="CLOSED", source_url="https://x/j5", reason="unknown",
                  closed_at=_utc(2026, 1, 1))
        ids["L5"] = _listing(cur, j5, "https://x/j5", date(2026, 1, 1))
        # Job đóng từ trước khi có closed_at (NULL = không biết giờ). Tắt trigger để dựng đúng trạng thái đó.
        cur.execute("ALTER TABLE job_postings DISABLE TRIGGER set_job_closed_state")
        cur.execute("UPDATE job_postings SET closed_at = NULL WHERE job_id = %s", (j5,))
        cur.execute("ALTER TABLE job_postings ENABLE TRIGGER set_job_closed_state")

        j6 = _job(cur, "J6", deadline=date(2026, 12, 31), source_url=None)
        ids["L6"] = _listing(cur, j6, "https://x/j6", date(2026, 10, 7))

        j7 = _job(cur, "J7", source_url="https://x/j7")
        ids["L7"] = _listing(cur, j7, "https://x/j7", date(2026, 10, 7), _utc(2026, 10, 1))

        j8 = _job(cur, "J8", source_url=None)
        ids["L8"] = _listing(cur, j8, None, date(2026, 10, 7))
    conn.commit()
    return ids


def _listing_state(conn, log_id):
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_NEW_COLUMNS)} FROM job_sources_log WHERE log_id = %s", (log_id,))
        return dict(zip(_NEW_COLUMNS, cur.fetchone()))


def _jobs_fingerprint(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT md5(string_agg(jp::text, '|' ORDER BY job_id)) FROM job_postings jp")
        return cur.fetchone()[0]


def _state(listing_status, deadline, first, last, reason=None, closed_at=None):
    return {"listing_status": listing_status, "deadline": deadline, "first_seen_at": first,
            "last_seen_at": last, "closed_reason": reason, "closed_at": closed_at}


@pytest.fixture
def migrated(tmp_path):
    """DB đã seed ở trạng thái trước 0043, rồi đã chạy 0043. Trả (conn, ids, dấu vân tay job trước migration)."""
    with temp_database("lst") as url, connect(url) as conn:
        legacy_db_before(conn, tmp_path, 43)
        ids = _seed(conn)
        fingerprint = _jobs_fingerprint(conn)
        assert migrate_one(conn, tmp_path, _MIGRATION) == [_MIGRATION]
        yield conn, ids, fingerprint


# ----------------------------------------------------------------------------- backfill
def test_open_job_with_single_listing_copies_deadline_and_is_open(migrated):
    conn, ids, _ = migrated
    assert _listing_state(conn, ids["L1"]) == _state(
        "OPEN", date(2026, 11, 30), _FIRST_SEEN_07_10, _DETAIL_J1)


def test_open_job_current_listing_is_open_and_other_listing_is_unknown_without_deadline(migrated):
    conn, ids, _ = migrated
    assert _listing_state(conn, ids["L2a"]) == _state(
        "OPEN", date(2026, 12, 1), _FIRST_SEEN_01_09, _FIRST_SEEN_01_09)       # detail_checked_at NULL
    assert _listing_state(conn, ids["L2b"]) == _state(
        "UNKNOWN", None, _FIRST_SEEN_01_10, _DETAIL_J2B)


def test_closed_job_closes_every_listing_and_copies_reason_and_time(migrated):
    conn, ids, _ = migrated
    first_01_09 = _FIRST_SEEN_01_09
    assert _listing_state(conn, ids["L3"]) == _state(
        "CLOSED", date(2026, 9, 15), first_01_09, first_01_09, "expired_auto", _CLOSED_AT_J3)
    # Job đóng bởi nhân viên: cả listing hiện hành lẫn listing phụ đều CLOSED/staff; deadline chỉ ở listing hiện hành.
    assert _listing_state(conn, ids["L4a"]) == _state(
        "CLOSED", date(2026, 9, 30), first_01_09, first_01_09, "staff", _CLOSED_AT_J4)
    assert _listing_state(conn, ids["L4b"]) == _state(
        "CLOSED", None, _utc(2026, 9, 9, 17, 0, 0), _utc(2026, 9, 9, 17, 0, 0), "staff", _CLOSED_AT_J4)


def test_closed_job_with_unknown_close_time_keeps_it_unknown(migrated):
    conn, ids, _ = migrated
    state = _listing_state(conn, ids["L5"])
    assert state["listing_status"] == "CLOSED"
    assert state["closed_reason"] == "unknown"
    assert state["closed_at"] is None            # NULL = không biết giờ, không bịa giờ


def test_listing_without_a_current_url_match_is_unknown_and_gets_no_deadline(migrated):
    conn, ids, _ = migrated
    # J6: job không có source_url nên không listing nào là "hiện hành"; J8: listing không có URL.
    # Hạn của job (31/12) không được chép sang vì không biết nó là hạn của URL nào.
    assert _listing_state(conn, ids["L6"]) == _state("UNKNOWN", None, _FIRST_SEEN_07_10, _FIRST_SEEN_07_10)
    assert _listing_state(conn, ids["L8"]) == _state("UNKNOWN", None, _FIRST_SEEN_07_10, _FIRST_SEEN_07_10)


def test_last_seen_is_never_earlier_than_first_seen(migrated):
    conn, ids, _ = migrated
    # J7: detail_checked_at (01/10) sớm hơn 00:00 VN của collected_date (07/10): lấy first_seen_at.
    assert _listing_state(conn, ids["L7"]) == _state("OPEN", None, _FIRST_SEEN_07_10, _FIRST_SEEN_07_10)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM job_sources_log WHERE last_seen_at < first_seen_at")
        assert cur.fetchone()[0] == 0


def test_backfill_does_not_touch_job_postings(migrated):
    conn, _, fingerprint_before = migrated
    assert _jobs_fingerprint(conn) == fingerprint_before          # kể cả updated_at


def test_every_listing_row_is_backfilled_and_row_count_is_unchanged(migrated):
    conn, ids, _ = migrated
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM job_sources_log")
        assert cur.fetchone()[0] == len(ids)
        cur.execute("SELECT count(*) FROM job_sources_log WHERE listing_status IS NULL "
                    "OR first_seen_at IS NULL OR last_seen_at IS NULL")
        assert cur.fetchone()[0] == 0


@pytest.mark.parametrize("session_tz", ["UTC", "Asia/Ho_Chi_Minh", "America/Los_Angeles"])
def test_backfill_does_not_depend_on_the_session_time_zone(tmp_path, session_tz):
    with temp_database("lst") as url, connect(url) as conn:
        legacy_db_before(conn, tmp_path, 43)
        ids = _seed(conn)
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE %s", (session_tz,))
        conn.commit()
        migrate_one(conn, tmp_path, _MIGRATION)
        assert _listing_state(conn, ids["L1"])["first_seen_at"] == _FIRST_SEEN_07_10
        assert _listing_state(conn, ids["L4b"])["first_seen_at"] == _utc(2026, 9, 9, 17, 0, 0)


# ----------------------------------------------------------------------------- ràng buộc
def _fresh_listing(conn, **overrides):
    """Chèn một listing mới (qua job mới) với các cột mới tuỳ ý; trả log_id."""
    with conn.cursor() as cur:
        job_id = _job(cur, f"T{uuid.uuid4().hex[:6]}", source_url=f"https://x/{uuid.uuid4().hex}")
        cols = {"job_id": job_id, "source_name": "topcv", "source_url": f"https://x/{uuid.uuid4().hex}"}
        cols.update(overrides)
        names = ", ".join(cols)
        marks = ", ".join(["%s"] * len(cols))
        cur.execute(f"INSERT INTO job_sources_log ({names}) VALUES ({marks}) RETURNING log_id",
                    tuple(cols.values()))
        return cur.fetchone()[0]


@pytest.mark.parametrize("overrides,constraint", [
    ({"listing_status": "DEAD"}, "chk_job_sources_log_listing_status"),
    ({"listing_status": "CLOSED", "closed_reason": "bogus"}, "chk_job_sources_log_closed_reason"),
    ({"listing_status": "CLOSED"}, "chk_job_sources_log_closed_state"),               # CLOSED thiếu lý do
    ({"listing_status": "OPEN", "closed_reason": "staff"}, "chk_job_sources_log_closed_state"),
    ({"listing_status": "UNKNOWN", "closed_at": _utc(2026, 1, 1)}, "chk_job_sources_log_closed_state"),
    ({"first_seen_at": _utc(2026, 10, 2), "last_seen_at": _utc(2026, 10, 1)}, "chk_job_sources_log_seen_order"),
])
def test_constraints_reject_bad_data(migrated, overrides, constraint):
    conn, _, _ = migrated
    with pytest.raises(psycopg2.errors.CheckViolation) as exc:
        _fresh_listing(conn, **overrides)
    assert constraint in str(exc.value)
    conn.rollback()


def test_constraints_accept_every_valid_combination(migrated):
    conn, _, _ = migrated
    _fresh_listing(conn, listing_status="OPEN")
    _fresh_listing(conn, listing_status="UNKNOWN")
    _fresh_listing(conn, listing_status="CLOSED", closed_reason="expired_auto", closed_at=_utc(2026, 1, 1))
    _fresh_listing(conn, listing_status="CLOSED", closed_reason="unknown")        # không biết giờ đóng
    for reason in ("staff", "expired_auto", "merged", "unknown"):
        _fresh_listing(conn, listing_status="CLOSED", closed_reason=reason)
    conn.commit()


# ----------------------------------------------------------------------------- tương thích code cũ
def test_old_code_insert_without_the_new_columns_still_works_and_yields_unknown(migrated):
    conn, _, _ = migrated
    log_id = _fresh_listing(conn)                       # chỉ job_id, source_name, source_url, như code hiện tại
    conn.commit()
    state = _listing_state(conn, log_id)
    assert state["listing_status"] == "UNKNOWN"          # không tuyên bố "còn sống" khi chưa kiểm
    assert state["deadline"] is None
    assert state["closed_reason"] is None and state["closed_at"] is None
    assert state["first_seen_at"] is not None and state["last_seen_at"] is not None
    assert state["last_seen_at"] >= state["first_seen_at"]


def test_existing_writers_in_the_codebase_work_on_the_migrated_schema(migrated):
    """link_repost_source và đường cập nhật detail_checked_at của code hiện tại chạy được sau migration."""
    import db
    conn, ids, _ = migrated
    with conn.cursor() as cur:
        cur.execute("SELECT job_id FROM job_sources_log WHERE log_id = %s", (ids["L1"],))
        job_id = str(cur.fetchone()[0])
    assert db.link_repost_source(conn, job_id, source_name="topcv", source_url="https://x/repost-new",
                                 raw_jd_content="JD", salary_raw_text="") is True
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT log_id FROM job_sources_log WHERE source_url = 'https://x/repost-new'")
        state = _listing_state(conn, cur.fetchone()[0])
    assert state["listing_status"] == "UNKNOWN"


# ----------------------------------------------------------------------------- idempotent
def test_rerunning_the_file_changes_nothing_and_keeps_manual_edits(migrated):
    conn, ids, _ = migrated
    text = (SQL_DIR / _MIGRATION).read_text(encoding="utf-8")
    with conn.cursor() as cur:
        # sửa tay một listing sau lần chạy đầu
        cur.execute(
            "UPDATE job_sources_log SET listing_status = 'CLOSED', closed_reason = 'expired_auto', "
            "closed_at = %s, deadline = %s WHERE log_id = %s",
            (_utc(2026, 10, 9), date(2026, 10, 1), ids["L1"]))
        cur.execute("SELECT md5(string_agg(s::text, '|' ORDER BY log_id)) FROM job_sources_log s")
        before = cur.fetchone()[0]
        cur.execute(text)
        cur.execute(text)
        cur.execute("SELECT md5(string_agg(s::text, '|' ORDER BY log_id)) FROM job_sources_log s")
        assert cur.fetchone()[0] == before
    conn.commit()
    assert _listing_state(conn, ids["L1"])["listing_status"] == "CLOSED"


# ----------------------------------------------------------------------------- khoá bảng
def test_lock_wait_times_out_instead_of_blocking_forever(tmp_path):
    with temp_database("lst") as url, connect(url) as conn, connect(url) as other:
        legacy_db_before(conn, tmp_path, 43)
        ids = _seed(conn)
        text = (SQL_DIR / _MIGRATION).read_text(encoding="utf-8")
        assert "lock_timeout = '10s'" in text            # giá trị thật của migration

        with other.cursor() as cur:                       # giao dịch khác (vd crawl) đang đọc, chưa kết thúc
            cur.execute("SELECT count(*) FROM job_sources_log")

        with pytest.raises(psycopg2.errors.LockNotAvailable):
            migrate_one(conn, tmp_path, _MIGRATION, sql_edit=lambda t: t.replace("'10s'", "'300ms'"))
        conn.rollback()

        with conn.cursor() as cur:
            cur.execute("SELECT column_name FROM information_schema.columns "
                        "WHERE table_name = 'job_sources_log' AND column_name = ANY(%s)",
                        (list(_NEW_COLUMNS),))
            assert cur.fetchall() == []                   # không đổi gì
            cur.execute("SELECT count(*) FROM schema_migrations WHERE filename = %s", (_MIGRATION,))
            assert cur.fetchone()[0] == 0

        other.rollback()                                  # hết khoá thì chạy lại được
        assert migrate_one(conn, tmp_path, _MIGRATION) == [_MIGRATION]
        assert _listing_state(conn, ids["L1"])["listing_status"] == "OPEN"
