"""
C2 nửa 2/2 trên POSTGRES THẬT: db.job_sync.sync_job_from_listings đưa job_status, closed_reason, deadline,
source_url của job cho bằng giá trị suy ra từ listing, và các đường ghi gọi nó đúng chỗ.

Cách chạy giống tests/test_pg_listing_state_writes.py: đặt TEST_DATABASE_URL (tên chứa "test"); không đặt thì
bỏ qua. Các test đường ghi cụ thể (đóng, mở lại, sửa hạn...) nằm ở test_pg_listing_state_writes.py; file này
tập trung vào chính hàm đồng bộ: no-op khi khớp, chỉ ghi cột lệch, không làm nhảy updated_at (và không làm rò
cờ skip_updated_at), sửa các bất đồng trạng thái, hoãn đúng chỗ khi link tin đăng lại, và chạy song song.
"""
import logging
import os
import threading
import uuid
from datetime import date
from urllib.parse import urlparse

import psycopg2
import pytest

import db
from db.job_recrawl import AUTO_REOPEN_REASONS
from db.job_sync import SKIP_UPDATED_AT_SETTING, sync_job_from_listings

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")
FUTURE, FUTURE2 = date(2099, 12, 31), date(2100, 6, 30)


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
    cid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)", (cid, f"Cty {cid[:8]}"))
    job_id = db.insert_job(
        conn, company_id=cid, job_title="Data Analyst", matching_industry="Data", level_id=None,
        province_id=None, work_type=None, currency="VNĐ", salary_min=None, salary_max=None,
        salary_type="NEGOTIABLE", source_url=_url(), source_name="Fake", deadline=deadline, detail_fetched=True)
    conn.commit()
    return job_id


def _link(conn, job_id, *, deadline=FUTURE, **kw):
    url = _url()
    assert db.link_repost_source(conn, job_id, source_name="Fake", source_url=url, deadline=deadline, **kw)
    conn.commit()
    return url


def _job_row(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT job_status::text, closed_reason, deadline, source_url, updated_at, closed_at "
                    "FROM job_postings WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
    conn.rollback()
    return row


def _raw(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
    conn.commit()


# ============================================================ no-op và phạm vi
def test_sync_is_a_noop_when_job_already_matches(pg_conn):
    job = _job(pg_conn)
    before = _job_row(pg_conn, job)
    assert sync_job_from_listings(pg_conn, job) == {}
    pg_conn.commit()
    assert _job_row(pg_conn, job) == before


def test_sync_twice_changes_nothing_the_second_time(pg_conn):
    job = _job(pg_conn)
    _raw(pg_conn, "UPDATE job_postings SET deadline = NULL WHERE job_id = %s", (job,))
    assert set(sync_job_from_listings(pg_conn, job)) == {"deadline"}
    assert sync_job_from_listings(pg_conn, job) == {}
    pg_conn.commit()


def test_job_without_listings_is_left_alone(pg_conn):
    job = _job(pg_conn)
    _raw(pg_conn, "DELETE FROM job_sources_log WHERE job_id = %s", (job,))
    _raw(pg_conn, "UPDATE job_postings SET deadline = %s WHERE job_id = %s", (FUTURE2, job))
    assert sync_job_from_listings(pg_conn, job) == {}
    pg_conn.commit()
    assert _job_row(pg_conn, job)[2] == FUTURE2


def test_missing_job_returns_empty(pg_conn):
    assert sync_job_from_listings(pg_conn, str(uuid.uuid4())) == {}
    pg_conn.rollback()


def test_only_drifted_columns_are_reported_and_written(pg_conn):
    job = _job(pg_conn)
    url = _job_row(pg_conn, job)[3]
    _raw(pg_conn, "UPDATE job_postings SET deadline = %s, job_title = 'Đã sửa tay' WHERE job_id = %s", (FUTURE2, job))
    changes = sync_job_from_listings(pg_conn, job)
    pg_conn.commit()
    assert changes == {"deadline": (FUTURE2, FUTURE)}
    row = _job_row(pg_conn, job)
    assert row[2] == FUTURE and row[3] == url


# ============================================================ updated_at và cờ phiên
def test_sync_does_not_bump_updated_at(pg_conn):
    job = _job(pg_conn)
    _raw(pg_conn, "UPDATE job_postings SET deadline = NULL WHERE job_id = %s", (job,))
    before = _job_row(pg_conn, job)[4]
    assert sync_job_from_listings(pg_conn, job)
    pg_conn.commit()
    assert _job_row(pg_conn, job)[4] == before


def test_skip_flag_does_not_leak_into_the_next_statement(pg_conn):
    """Sau đồng bộ, một câu UPDATE thường trong cùng transaction vẫn phải làm nhảy updated_at."""
    job = _job(pg_conn)
    _raw(pg_conn, "UPDATE job_postings SET deadline = NULL WHERE job_id = %s", (job,))
    before = _job_row(pg_conn, job)[4]
    assert sync_job_from_listings(pg_conn, job)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT current_setting(%s, true)", (SKIP_UPDATED_AT_SETTING,))
        assert cur.fetchone()[0] != "on"
        cur.execute("UPDATE job_postings SET job_title = 'Sửa sau đồng bộ' WHERE job_id = %s", (job,))
    pg_conn.commit()
    assert _job_row(pg_conn, job)[4] > before


def test_skip_flag_already_on_stays_on_after_sync(pg_conn):
    """Transaction của merge-duplicates / recompute đã bật cờ: đồng bộ không được tắt nó."""
    job = _job(pg_conn)
    _raw(pg_conn, "UPDATE job_postings SET deadline = NULL WHERE job_id = %s", (job,))
    with pg_conn.cursor() as cur:
        cur.execute("SELECT set_config(%s, 'on', true)", (SKIP_UPDATED_AT_SETTING,))
    assert sync_job_from_listings(pg_conn, job)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT current_setting(%s, true)", (SKIP_UPDATED_AT_SETTING,))
        assert cur.fetchone()[0] == "on"
    pg_conn.rollback()


# ============================================================ sửa bất đồng trạng thái
def test_job_closed_but_listing_open_is_reopened_and_closed_state_cleared(pg_conn):
    job = _job(pg_conn)
    _raw(pg_conn, "UPDATE job_postings SET job_status = 'CLOSED', closed_reason = 'staff' WHERE job_id = %s", (job,))
    changes = sync_job_from_listings(pg_conn, job)
    pg_conn.commit()
    assert changes == {"job_status": ("CLOSED", "OPEN")}
    row = _job_row(pg_conn, job)
    assert (row[0], row[1], row[5]) == ("OPEN", None, None)


def test_job_open_but_every_listing_closed_is_closed_with_derived_reason(pg_conn):
    job = _job(pg_conn)
    _raw(pg_conn, "UPDATE job_sources_log SET listing_status = 'CLOSED', closed_reason = 'staff', "
                  "closed_at = now() WHERE job_id = %s", (job,))
    changes = sync_job_from_listings(pg_conn, job)
    pg_conn.commit()
    assert changes["job_status"] == ("OPEN", "CLOSED") and changes["closed_reason"] == (None, "staff")
    row = _job_row(pg_conn, job)
    assert (row[0], row[1]) == ("CLOSED", "staff") and row[5] is not None


def test_closed_job_with_different_reason_than_listings_gets_derived_reason(pg_conn):
    job = _job(pg_conn)
    _raw(pg_conn, "UPDATE job_sources_log SET listing_status = 'CLOSED', closed_reason = 'staff', "
                  "closed_at = now() WHERE job_id = %s", (job,))
    _raw(pg_conn, "UPDATE job_postings SET job_status = 'CLOSED', closed_reason = 'expired_auto' WHERE job_id = %s",
         (job,))
    changes = sync_job_from_listings(pg_conn, job)
    pg_conn.commit()
    assert changes == {"closed_reason": ("expired_auto", "staff")}


def test_job_with_only_unknown_listings_stays_open(pg_conn):
    job = _job(pg_conn)
    _raw(pg_conn, "UPDATE job_sources_log SET listing_status = 'UNKNOWN' WHERE job_id = %s", (job,))
    assert sync_job_from_listings(pg_conn, job) == {}
    pg_conn.commit()
    assert _job_row(pg_conn, job)[0] == "OPEN"


def test_status_change_is_logged_at_info(pg_conn, caplog):
    job = _job(pg_conn)
    _raw(pg_conn, "UPDATE job_postings SET job_status = 'CLOSED', closed_reason = 'staff' WHERE job_id = %s", (job,))
    with caplog.at_level(logging.INFO, logger="db.job_sync"):
        sync_job_from_listings(pg_conn, job)
    pg_conn.commit()
    assert job in caplog.text and "CLOSED -> OPEN" in caplog.text


# ============================================================ link_repost_source (hoãn có chủ đích)
def test_link_on_open_job_moves_source_url_but_leaves_deadline_to_extend(pg_conn):
    job = _job(pg_conn, deadline=FUTURE)
    new = _link(pg_conn, job, deadline=FUTURE2)
    row = _job_row(pg_conn, job)
    assert row[3] == new and row[2] == FUTURE                       # URL theo listing, hạn chờ extend_job_deadline
    assert db.extend_job_deadline(pg_conn, job, FUTURE2) is True    # nên extend vẫn dời được và đếm được
    pg_conn.commit()
    assert _job_row(pg_conn, job)[2] == FUTURE2


def test_link_does_not_pull_deadline_of_a_hand_edited_open_job(pg_conn):
    """Job nhân viên đã sửa tay: pipeline không gọi extend cho nó, nên tin đăng lại không kéo hạn của họ."""
    job = _job(pg_conn, deadline=FUTURE)
    db.update_job(pg_conn, job, deadline=date(2095, 1, 1))
    pg_conn.commit()
    _link(pg_conn, job, deadline=FUTURE2)
    assert _job_row(pg_conn, job)[2] == date(2095, 1, 1)


def test_link_on_auto_closed_job_leaves_job_untouched_for_reopen_to_read(pg_conn):
    job = _job(pg_conn)
    old_url = _job_row(pg_conn, job)[3]
    db.update_job(pg_conn, job, job_status="CLOSED", closed_reason="expired_auto")
    pg_conn.commit()
    before = _job_row(pg_conn, job)
    new = _link(pg_conn, job, deadline=FUTURE2)
    assert _job_row(pg_conn, job) == before and before[3] == old_url   # reopen_job_for_repost còn đọc giá trị cũ
    assert db.reopen_job_for_repost(pg_conn, job, source_url=new, deadline=FUTURE2) is True
    pg_conn.commit()
    row = _job_row(pg_conn, job)
    assert (row[0], row[1], row[2], row[3]) == ("OPEN", None, FUTURE2, new)


def test_link_on_staff_closed_job_follows_listings_fully(pg_conn):
    job = _job(pg_conn, deadline=FUTURE)
    db.update_job(pg_conn, job, job_status="CLOSED")
    pg_conn.commit()
    new = _link(pg_conn, job, deadline=FUTURE2)
    row = _job_row(pg_conn, job)
    assert (row[0], row[1], row[2], row[3]) == ("CLOSED", "staff", FUTURE2, new)


def test_link_of_existing_url_changes_nothing(pg_conn):
    job = _job(pg_conn)
    url = _job_row(pg_conn, job)[3]
    before = _job_row(pg_conn, job)
    assert db.link_repost_source(pg_conn, job, source_name="Fake", source_url=url, deadline=FUTURE2) is False
    pg_conn.commit()
    assert _job_row(pg_conn, job) == before


def test_audit_of_reopen_keeps_the_real_old_values(pg_conn):
    """reopen_job_for_repost ghi audit giá trị cũ/mới: nhờ hoãn đồng bộ ở link, giá trị cũ là giá trị thật."""
    job = _job(pg_conn, deadline=FUTURE)
    old_url = _job_row(pg_conn, job)[3]
    db.update_job(pg_conn, job, job_status="CLOSED", closed_reason="expired_auto")
    pg_conn.commit()
    new = _link(pg_conn, job, deadline=FUTURE2)
    assert db.reopen_job_for_repost(pg_conn, job, source_url=new, deadline=FUTURE2)
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT changes FROM audit_logs WHERE action_type = 'REOPEN_JOB' AND entity_id = %s", (job,))
        (changes,) = cur.fetchone()
    pg_conn.rollback()
    assert changes["source_url"] == {"old": old_url, "new": new}
    assert changes["deadline"]["old"] == FUTURE.isoformat()


# ============================================================ mark_*: listing đổi thì job theo kịp
def test_mark_listing_seen_resyncs_url_of_unknown_only_job(pg_conn):
    """Job chỉ còn listing UNKNOWN: source_url suy ra theo last_seen_at, nên một lần thấy lại đổi URL job."""
    job = _job(pg_conn)
    first = _job_row(pg_conn, job)[3]
    second = _link(pg_conn, job)
    _raw(pg_conn, "UPDATE job_sources_log SET listing_status = 'UNKNOWN' WHERE job_id = %s", (job,))
    _raw(pg_conn, "UPDATE job_sources_log SET last_seen_at = now() + interval '1 day' WHERE source_url = %s",
         (first,))
    db.mark_listing_seen(pg_conn, first)
    pg_conn.commit()
    assert _job_row(pg_conn, job)[3] == first and second != first


def test_detail_checked_with_deadline_pulls_job_deadline_of_single_listing_job(pg_conn):
    job = _job(pg_conn, deadline=None)
    url = _job_row(pg_conn, job)[3]
    db.mark_source_detail_checked(pg_conn, url, deadline=FUTURE2)
    pg_conn.commit()
    assert _job_row(pg_conn, job)[2] == FUTURE2


# ============================================================ update_job
def test_update_job_reopen_when_no_listing_matches_job_url_still_opens_one(pg_conn):
    """Dữ liệu cũ: job.source_url không trùng listing nào. Nhân viên mở lại job thì phải có listing sống,
    nếu không đồng bộ sẽ đóng lại ngay."""
    job = _job(pg_conn)
    db.update_job(pg_conn, job, job_status="CLOSED")
    pg_conn.commit()
    _raw(pg_conn, "UPDATE job_postings SET source_url = 'https://khong-co-listing' WHERE job_id = %s", (job,))
    db.update_job(pg_conn, job, job_status="OPEN")
    pg_conn.commit()
    assert _job_row(pg_conn, job)[0] == "OPEN"
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM job_sources_log WHERE job_id = %s AND listing_status = 'OPEN'", (job,))
        assert cur.fetchone()[0] == 1
    pg_conn.rollback()


def test_update_job_without_status_or_deadline_runs_no_sync(pg_conn, monkeypatch):
    job = _job(pg_conn)
    calls = []
    import db.listing_state as ls
    monkeypatch.setattr(ls, "sync_job_from_listings", lambda *a, **k: calls.append(a))
    db.update_job(pg_conn, job, job_title="Chỉ đổi tên")
    pg_conn.commit()
    assert calls == []


def test_update_job_reconciles_hand_written_status_with_listings(pg_conn):
    job = _job(pg_conn)
    assert db.update_job(pg_conn, job, job_status="CLOSED", closed_reason="unknown")
    pg_conn.commit()
    row = _job_row(pg_conn, job)
    assert (row[0], row[1]) == ("CLOSED", "unknown")


# ============================================================ song song
def test_two_crawlers_linking_to_the_same_job_do_not_deadlock(pg_conn):
    job = _job(pg_conn)
    urls = [_url(), _url()]
    errors = []

    def worker(url):
        conn = psycopg2.connect(TEST_DATABASE_URL)
        try:
            conn.autocommit = False
            db.link_repost_source(conn, job, source_name="Fake", source_url=url, deadline=FUTURE2)
            conn.commit()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(u,)) for u in urls]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert errors == []
    row = _job_row(pg_conn, job)
    assert row[3] in urls                                           # job trỏ tới listing OPEN mới nhất trong hai


def test_auto_reopen_reasons_constant_is_what_link_defers_on():
    assert AUTO_REOPEN_REASONS == frozenset({"expired_auto"})
