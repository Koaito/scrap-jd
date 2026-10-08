"""
C1 nửa 2/2 trên POSTGRES THẬT: mọi chỗ tạo hoặc cập nhật listing (job_sources_log) ghi các cột trạng thái
mới theo đúng các luật trong db/listing_state.py. Chưa có chỗ đọc nào dùng các cột này (C2, C3), nên test
đọc thẳng bảng.

Cách chạy giống tests/test_pg_repost_link.py: đặt TEST_DATABASE_URL trỏ tới database dùng riêng cho test
(tên chứa "test"); không đặt thì cả file bị bỏ qua.

Các luật được khoá lại:
  1. listing mới: OPEN; job đang CLOSED thì listing sinh ra CLOSED với đúng closed_reason của job;
  2. job chuyển CLOSED: mọi listing chưa CLOSED thành CLOSED cùng lý do; listing đã CLOSED giữ lý do cũ;
  3. nhân viên mở lại job: listing CLOSED vì staff và listing hiện hành về OPEN; lý do khác giữ nguyên;
  4. pipeline mở lại job vì tin đăng lại: listing của URL mới về OPEN kèm hạn mới;
  + hạn do nhân viên sửa/xoá ghi vào listing hiện hành, fetch chi tiết ghi last_seen_at và hạn, gộp job
    giữ nguyên trạng thái listing, và insert_listing không bao giờ để lọt kẽ hở "job CLOSED, listing OPEN".
"""
import dataclasses
import os
import threading
import time
import uuid
from datetime import date
from urllib.parse import urlparse

import psycopg2
import pytest

import db
import duplicate_report as dr
import merge_duplicates as md
from db import listing_state
from db.listing_state import CONFLICT_NONE, initial_listing_state, insert_listing

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")
FUTURE = date(2099, 12, 31)
FUTURE2 = date(2100, 6, 30)
PAST = date(2020, 1, 1)


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


def _company(conn) -> str:
    with conn.cursor() as cur:
        cid = str(uuid.uuid4())
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)",
                    (cid, f"Cty {uuid.uuid4().hex[:8]}"))
    conn.commit()
    return cid


def _url() -> str:
    return f"https://example.com/{uuid.uuid4()}"


def _new_job(conn, *, url=None, deadline=FUTURE, company_id=None, title="Data Analyst",
             detail_fetched=True) -> str:
    job_id = db.insert_job(
        conn, company_id=company_id or _company(conn), job_title=title, matching_industry="Data",
        level_id=None, province_id=None, work_type=None, currency="VNĐ", salary_min=None,
        salary_max=None, salary_type="NEGOTIABLE", source_url=url or _url(), source_name="Fake",
        deadline=deadline, detail_fetched=detail_fetched,
    )
    conn.commit()
    return job_id


def _listing(conn, url):
    """Dòng listing của URL dưới dạng dict."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT job_id::text, listing_status, closed_reason, closed_at, deadline, first_seen_at, "
            "last_seen_at, detail_checked_at FROM job_sources_log WHERE source_url = %s", (url,))
        row = cur.fetchone()
    conn.rollback()
    assert row is not None, f"không có listing {url}"
    keys = ("job_id", "listing_status", "closed_reason", "closed_at", "deadline", "first_seen_at",
            "last_seen_at", "detail_checked_at")
    return dict(zip(keys, row))


def _status(conn, url):
    s = _listing(conn, url)
    return s["listing_status"], s["closed_reason"]


def _job_url(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT source_url FROM job_postings WHERE job_id = %s", (job_id,))
        value = cur.fetchone()[0]
    conn.rollback()
    return value


def _link(conn, job_id, url=None, *, deadline=FUTURE):
    url = url or _url()
    assert db.link_repost_source(conn, job_id, source_name="Fake", source_url=url,
                                 raw_jd_content="JD", deadline=deadline) is True
    conn.commit()
    return url


def _close(conn, job_id, reason=None):
    kw = {"closed_reason": reason} if reason else {}
    assert db.update_job(conn, job_id, job_status="CLOSED", **kw) is True
    conn.commit()


def _force_unknown(conn, url):
    """Dựng listing UNKNOWN như dữ liệu backfill (listing cũ của job OPEN)."""
    with conn.cursor() as cur:
        cur.execute("UPDATE job_sources_log SET listing_status = 'UNKNOWN' WHERE source_url = %s", (url,))
    conn.commit()


# ============================================================ hàm thuần
@pytest.mark.parametrize("job_status,reason,expected", [
    ("OPEN", None, ("OPEN", None)),
    ("OPEN", "staff", ("OPEN", None)),            # job OPEN thì lý do (nếu lọt vào) không có nghĩa
    ("CLOSED", "staff", ("CLOSED", "staff")),
    ("CLOSED", "expired_auto", ("CLOSED", "expired_auto")),
    ("CLOSED", "merged", ("CLOSED", "merged")),
    ("CLOSED", "unknown", ("CLOSED", "unknown")),
    ("CLOSED", None, ("CLOSED", "unknown")),      # không xảy ra với dữ liệu hợp lệ, nhưng không được vỡ CHECK
])
def test_initial_listing_state(job_status, reason, expected):
    assert initial_listing_state(job_status, reason) == expected


# ============================================================ luật 1: listing mới
def test_insert_job_writes_open_listing_with_deadline_and_timestamps(pg_conn):
    url = _url()
    job = _new_job(pg_conn, url=url, deadline=FUTURE)
    s = _listing(pg_conn, url)
    assert s["job_id"] == job
    assert (s["listing_status"], s["closed_reason"], s["closed_at"]) == ("OPEN", None, None)
    assert s["deadline"] == FUTURE
    assert s["first_seen_at"] <= s["last_seen_at"]
    assert s["detail_checked_at"] is not None              # detail_fetched=True


def test_insert_job_without_detail_fetched_leaves_detail_checked_null_but_still_open(pg_conn):
    url = _url()
    _new_job(pg_conn, url=url, detail_fetched=False)
    s = _listing(pg_conn, url)
    assert s["listing_status"] == "OPEN" and s["detail_checked_at"] is None


def test_insert_job_without_deadline_leaves_listing_deadline_null(pg_conn):
    url = _url()
    _new_job(pg_conn, url=url, deadline=None)
    assert _listing(pg_conn, url)["deadline"] is None


def test_manual_job_listing_is_open(pg_conn):
    """Nhập tay (POST /jobs): manual://uuid không kiểm được nên không thể UNKNOWN mãi; đã duyệt là OPEN."""
    job = db.create_manual_job(
        pg_conn, company_id=_company(pg_conn), job_title="Nhập tay", matching_industry="Data",
        level_id=None, province_id=None, work_type=None, currency="VNĐ", salary_min=None, salary_max=None,
        salary_type="NEGOTIABLE", deadline=FUTURE,
    )
    pg_conn.commit()
    url = _job_url(pg_conn, job)
    assert url.startswith("manual://")
    s = _listing(pg_conn, url)
    assert (s["listing_status"], s["deadline"]) == ("OPEN", FUTURE)


def test_link_repost_on_open_job_writes_open_listing_with_its_own_deadline(pg_conn):
    job = _new_job(pg_conn, deadline=FUTURE)
    url = _link(pg_conn, job, deadline=FUTURE2)
    s = _listing(pg_conn, url)
    assert (s["listing_status"], s["closed_reason"], s["deadline"]) == ("OPEN", None, FUTURE2)
    assert s["detail_checked_at"] is not None


@pytest.mark.parametrize("reason", ["staff", "unknown", "merged"])
def test_link_repost_on_job_closed_for_other_reasons_writes_closed_listing(pg_conn, reason):
    job = _new_job(pg_conn)
    _close(pg_conn, job, reason)
    url = _link(pg_conn, job)
    s = _listing(pg_conn, url)
    assert (s["listing_status"], s["closed_reason"]) == ("CLOSED", reason)
    assert s["closed_at"] is not None


def test_link_repost_url_owned_by_another_job_is_not_overwritten(pg_conn):
    a, b = _new_job(pg_conn), _new_job(pg_conn)
    url = _job_url(pg_conn, a)
    assert db.link_repost_source(pg_conn, b, source_name="Fake", source_url=url, deadline=FUTURE2) is False
    pg_conn.rollback()
    s = _listing(pg_conn, url)
    assert s["job_id"] == a and s["deadline"] == FUTURE      # listing của job A không bị đổi


def test_insert_listing_for_missing_job_raises_lookup_error(pg_conn):
    with pytest.raises(LookupError):
        insert_listing(pg_conn, job_id=str(uuid.uuid4()), source_name="Fake", source_url=_url())
    pg_conn.rollback()


def test_insert_listing_rejects_unknown_conflict_mode(pg_conn):
    job = _new_job(pg_conn)
    with pytest.raises(ValueError):
        insert_listing(pg_conn, job_id=job, source_name="Fake", source_url=_url(), on_conflict="drop table")
    pg_conn.rollback()


def test_insert_job_url_of_another_job_still_raises_unique_violation(pg_conn):
    """Giữ nguyên hành vi cũ: URL đã thuộc job khác thì insert_job ồn ào, không để job mồ côi listing."""
    first = _new_job(pg_conn)
    url = _job_url(pg_conn, first)
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _new_job(pg_conn, url=url)
    pg_conn.rollback()


# ============================================================ luật 4: pipeline mở lại job vì tin đăng lại
def test_repost_of_expired_job_reopens_job_and_new_listing_but_not_old_one(pg_conn):
    old_url = _url()
    job = _new_job(pg_conn, url=old_url, deadline=FUTURE)
    _close(pg_conn, job, "expired_auto")
    new_url = _link(pg_conn, job, deadline=FUTURE2)
    assert _status(pg_conn, new_url) == ("CLOSED", "expired_auto")       # sinh ra theo job đang đóng
    assert db.reopen_job_for_repost(pg_conn, job, source_url=new_url, deadline=FUTURE2) is True
    pg_conn.commit()
    s = _listing(pg_conn, new_url)
    assert (s["listing_status"], s["closed_reason"], s["closed_at"], s["deadline"]) == ("OPEN", None, None, FUTURE2)
    assert _status(pg_conn, old_url) == ("CLOSED", "expired_auto")       # URL cũ đã chết, giữ nguyên


def test_repost_with_expired_deadline_does_not_reopen_and_listing_stays_closed(pg_conn):
    job = _new_job(pg_conn, deadline=FUTURE)
    _close(pg_conn, job, "expired_auto")
    new_url = _link(pg_conn, job, deadline=PAST)
    assert db.reopen_job_for_repost(pg_conn, job, source_url=new_url, deadline=PAST) is False
    pg_conn.commit()
    assert _status(pg_conn, new_url) == ("CLOSED", "expired_auto")


def test_reopen_without_a_listing_for_that_url_is_harmless(pg_conn, caplog):
    job = _new_job(pg_conn)
    _close(pg_conn, job, "expired_auto")
    with caplog.at_level("WARNING"):
        assert db.reopen_job_for_repost(pg_conn, job, source_url=_url(), deadline=FUTURE) is True
    pg_conn.commit()
    assert "chưa có listing" in caplog.text


def test_repost_of_open_job_keeps_old_listing_and_adds_open_one(pg_conn):
    old_url = _url()
    job = _new_job(pg_conn, url=old_url, deadline=FUTURE)
    new_url = _link(pg_conn, job, deadline=FUTURE2)
    assert db.extend_job_deadline(pg_conn, job, FUTURE2) is True
    pg_conn.commit()
    assert _status(pg_conn, old_url) == ("OPEN", None) and _status(pg_conn, new_url) == ("OPEN", None)
    # dời hạn của job không ghi đè hạn của listing cũ: mỗi listing giữ hạn của chính nó
    assert _listing(pg_conn, old_url)["deadline"] == FUTURE
    assert _listing(pg_conn, new_url)["deadline"] == FUTURE2


# ============================================================ luật 2: đóng job
def test_staff_close_closes_every_listing_including_unknown_ones(pg_conn):
    job = _new_job(pg_conn)
    cur_url = _job_url(pg_conn, job)
    other = _link(pg_conn, job)
    unknown = _link(pg_conn, job)
    _force_unknown(pg_conn, unknown)
    _close(pg_conn, job)                                   # mặc định staff
    for url in (cur_url, other, unknown):
        s = _listing(pg_conn, url)
        assert (s["listing_status"], s["closed_reason"]) == ("CLOSED", "staff")
        assert s["closed_at"] is not None


def test_auto_close_uses_expired_auto_reason(pg_conn):
    job = _new_job(pg_conn)
    url = _job_url(pg_conn, job)
    _close(pg_conn, job, "expired_auto")
    assert _status(pg_conn, url) == ("CLOSED", "expired_auto")


def test_closing_keeps_reason_and_time_of_listing_already_closed(pg_conn):
    job = _new_job(pg_conn)
    first = _job_url(pg_conn, job)
    second = _link(pg_conn, job)
    with pg_conn.cursor() as cur:        # listing đầu đã đóng từ trước, vì lý do khác
        cur.execute("UPDATE job_sources_log SET listing_status = 'CLOSED', closed_reason = 'expired_auto', "
                    "closed_at = now() - interval '3 days' WHERE source_url = %s", (first,))
    pg_conn.commit()
    before = _listing(pg_conn, first)["closed_at"]
    _close(pg_conn, job)
    assert _status(pg_conn, first) == ("CLOSED", "expired_auto")
    assert _listing(pg_conn, first)["closed_at"] == before
    assert _status(pg_conn, second) == ("CLOSED", "staff")


def test_resending_closed_does_not_rewrite_reason_of_listings(pg_conn):
    """Form gửi lại job_status=CLOSED cho job đã CLOSED vì expired_auto: không biến lý do thành staff."""
    job = _new_job(pg_conn)
    url = _job_url(pg_conn, job)
    _close(pg_conn, job, "expired_auto")
    _close(pg_conn, job)                                   # staff gửi lại CLOSED
    assert _status(pg_conn, url) == ("CLOSED", "expired_auto")


def test_closing_one_job_does_not_touch_listings_of_other_jobs(pg_conn):
    a, b = _new_job(pg_conn), _new_job(pg_conn)
    url_b = _job_url(pg_conn, b)
    _close(pg_conn, a)
    assert _status(pg_conn, url_b) == ("OPEN", None)


def test_update_job_for_missing_job_returns_false_and_writes_nothing(pg_conn):
    assert db.update_job(pg_conn, str(uuid.uuid4()), job_status="CLOSED") is False
    pg_conn.rollback()


# ============================================================ luật 3: nhân viên mở lại
def test_staff_reopen_opens_staff_closed_listings(pg_conn):
    job = _new_job(pg_conn)
    cur_url = _job_url(pg_conn, job)
    other = _link(pg_conn, job)
    _close(pg_conn, job)
    assert db.update_job(pg_conn, job, job_status="OPEN") is True
    pg_conn.commit()
    for url in (cur_url, other):
        s = _listing(pg_conn, url)
        assert (s["listing_status"], s["closed_reason"], s["closed_at"]) == ("OPEN", None, None)


def test_reopen_of_auto_closed_job_opens_current_listing_only(pg_conn):
    """Job đóng vì expired_auto rồi nhân viên mở lại: listing hiện hành về OPEN (không để job OPEN mà 0
    listing OPEN); listing khác đóng vì expired_auto giữ nguyên."""
    job = _new_job(pg_conn)
    cur_url = _job_url(pg_conn, job)
    other = _link(pg_conn, job)
    _close(pg_conn, job, "expired_auto")
    assert db.update_job(pg_conn, job, job_status="OPEN") is True
    pg_conn.commit()
    assert _status(pg_conn, cur_url) == ("OPEN", None)
    assert _status(pg_conn, other) == ("CLOSED", "expired_auto")


def test_reopen_leaves_merged_reason_listing_closed(pg_conn):
    job = _new_job(pg_conn)
    other = _link(pg_conn, job)
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE job_postings SET job_status = 'CLOSED', closed_reason = 'merged' WHERE job_id = %s",
                    (job,))
        cur.execute("UPDATE job_sources_log SET listing_status = 'CLOSED', closed_reason = 'merged' "
                    "WHERE source_url = %s", (other,))
    pg_conn.commit()
    db.update_job(pg_conn, job, job_status="OPEN")
    pg_conn.commit()
    assert _status(pg_conn, other) == ("CLOSED", "merged")


def test_sending_open_for_an_already_open_job_changes_no_listing(pg_conn):
    """Chỉ lần chuyển CLOSED -> OPEN mới mở listing. Form gửi lại OPEN cho job đang OPEN không đụng listing
    nào: không biến UNKNOWN thành OPEN, và không mở listing đóng vì staff (ca có thật: listing của job phụ
    đã đóng vì staff, chuyển sang job giữ đang OPEN khi gộp job)."""
    job = _new_job(pg_conn)
    unknown = _link(pg_conn, job)
    _force_unknown(pg_conn, unknown)
    staff_closed = _link(pg_conn, job)
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE job_sources_log SET listing_status = 'CLOSED', closed_reason = 'staff', "
                    "closed_at = now() WHERE source_url = %s", (staff_closed,))
    pg_conn.commit()
    assert db.update_job(pg_conn, job, job_status="OPEN", job_title="Đổi tên") is True
    pg_conn.commit()
    assert _status(pg_conn, unknown) == ("UNKNOWN", None)
    assert _status(pg_conn, staff_closed) == ("CLOSED", "staff")


def test_reopen_with_new_deadline_in_same_patch_writes_deadline_to_current_listing(pg_conn):
    job = _new_job(pg_conn, deadline=PAST)
    url = _job_url(pg_conn, job)
    _close(pg_conn, job)
    db.update_job(pg_conn, job, job_status="OPEN", deadline=FUTURE2)
    pg_conn.commit()
    s = _listing(pg_conn, url)
    assert (s["listing_status"], s["deadline"]) == ("OPEN", FUTURE2)


# ============================================================ hạn do nhân viên sửa / xoá
def test_staff_deadline_edit_goes_to_current_listing_only(pg_conn):
    job = _new_job(pg_conn, deadline=FUTURE)
    cur_url = _job_url(pg_conn, job)
    other = _link(pg_conn, job, deadline=FUTURE)
    db.update_job(pg_conn, job, deadline=FUTURE2)
    pg_conn.commit()
    assert _listing(pg_conn, cur_url)["deadline"] == FUTURE2
    assert _listing(pg_conn, other)["deadline"] == FUTURE


def test_staff_clearing_deadline_clears_current_listing_deadline(pg_conn):
    job = _new_job(pg_conn, deadline=FUTURE)
    url = _job_url(pg_conn, job)
    db.update_job(pg_conn, job, clear_fields={"deadline"})
    pg_conn.commit()
    assert _listing(pg_conn, url)["deadline"] is None


def test_update_without_deadline_leaves_listing_deadline(pg_conn):
    job = _new_job(pg_conn, deadline=FUTURE)
    url = _job_url(pg_conn, job)
    db.update_job(pg_conn, job, job_title="Tên mới")
    pg_conn.commit()
    assert _listing(pg_conn, url)["deadline"] == FUTURE


# ============================================================ fetch chi tiết, last_seen_at
def test_mark_source_detail_checked_sets_last_seen_and_deadline_but_not_status(pg_conn):
    url = _url()
    job = _new_job(pg_conn, url=url, deadline=None, detail_fetched=False)
    other = _link(pg_conn, job)
    _force_unknown(pg_conn, other)
    with pg_conn.cursor() as cur:        # ép last_seen_at về quá khứ để thấy nó nhích
        cur.execute("UPDATE job_sources_log SET last_seen_at = first_seen_at WHERE source_url = %s", (url,))
        cur.execute("UPDATE job_sources_log SET first_seen_at = now() - interval '2 days', "
                    "last_seen_at = now() - interval '2 days' WHERE source_url = %s", (url,))
    pg_conn.commit()
    before = _listing(pg_conn, url)
    db.mark_source_detail_checked(pg_conn, url, deadline=FUTURE)
    pg_conn.commit()
    after = _listing(pg_conn, url)
    assert after["last_seen_at"] > before["last_seen_at"]
    assert after["detail_checked_at"] is not None
    assert after["deadline"] == FUTURE
    assert after["listing_status"] == "OPEN"                         # không đổi trạng thái
    assert _status(pg_conn, other) == ("UNKNOWN", None)              # listing khác không bị đụng


def test_mark_source_detail_checked_without_deadline_keeps_old_deadline(pg_conn):
    url = _url()
    _new_job(pg_conn, url=url, deadline=FUTURE)
    db.mark_source_detail_checked(pg_conn, url)                       # chữ ký cũ vẫn chạy
    pg_conn.commit()
    assert _listing(pg_conn, url)["deadline"] == FUTURE


def test_mark_source_detail_checked_does_not_touch_job_postings(pg_conn):
    url = _url()
    job = _new_job(pg_conn, url=url, deadline=None)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT updated_at, deadline FROM job_postings WHERE job_id = %s", (job,))
        before = cur.fetchone()
    pg_conn.rollback()
    db.mark_source_detail_checked(pg_conn, url, deadline=FUTURE)
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("SELECT updated_at, deadline FROM job_postings WHERE job_id = %s", (job,))
        assert cur.fetchone() == before
    pg_conn.rollback()


def test_mark_listing_seen_moves_last_seen_forward_only(pg_conn):
    url = _url()
    _new_job(pg_conn, url=url)
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE job_sources_log SET first_seen_at = now() - interval '5 days', "
                    "last_seen_at = now() - interval '5 days' WHERE source_url = %s", (url,))
    pg_conn.commit()
    before = _listing(pg_conn, url)["last_seen_at"]
    assert db.mark_listing_seen(pg_conn, url) is True
    pg_conn.commit()
    after = _listing(pg_conn, url)
    assert after["last_seen_at"] > before and after["listing_status"] == "OPEN"
    # URL không có trong bảng: False, không lỗi
    assert db.mark_listing_seen(pg_conn, _url()) is False
    pg_conn.rollback()
    # last_seen_at đang ở tương lai (đồng hồ lệch giữa hai tiến trình) thì không bị kéo lùi
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE job_sources_log SET last_seen_at = now() + interval '1 hour' WHERE source_url = %s",
                    (url,))
    pg_conn.commit()
    ahead = _listing(pg_conn, url)["last_seen_at"]
    db.mark_listing_seen(pg_conn, url)
    pg_conn.commit()
    assert _listing(pg_conn, url)["last_seen_at"] == ahead


# ============================================================ gộp job
def _merge_pair(conn, keeper, donor):
    groups = dr.build_groups(db.list_duplicate_job_rows(conn))
    only = md.OnlySpec(job_ids={keeper, donor}, keepers={keeper})
    selected, skipped, _, _ = md.select_groups(groups, only)
    assert len(selected) == 1 and skipped == [], skipped
    plans, vanished = md.build_plans(conn, selected)
    assert vanished == [] and len(plans) == 1
    plan = plans[0]
    db.merge_job_group(
        conn, keeper_id=plan.keeper_id, donor_ids=plan.donor_ids, expected=plan.expected,
        changes=plan.changes, child=dataclasses.asdict(plan.child), conflicts=plan.conflicts, notes=plan.notes)
    conn.commit()


def test_merge_revives_closed_keeper_with_donor_listing_and_keeps_every_listing_state(pg_conn):
    company = _company(pg_conn)
    keeper = _new_job(pg_conn, company_id=company, deadline=FUTURE)
    keeper_url = _job_url(pg_conn, keeper)
    _close(pg_conn, keeper)                                          # staff đã đóng job giữ
    donor_url = _url()
    donor = _new_job(pg_conn, company_id=company, url=donor_url, deadline=FUTURE2)
    _merge_pair(pg_conn, keeper, donor)
    # job giữ hồi sinh (OPEN) bằng URL và hạn của job phụ; listing của job phụ chuyển sang, vẫn OPEN
    with pg_conn.cursor() as cur:
        cur.execute("SELECT job_status::text, source_url FROM job_postings WHERE job_id = %s", (keeper,))
        assert cur.fetchone() == ("OPEN", donor_url)
    pg_conn.rollback()
    moved = _listing(pg_conn, donor_url)
    assert (moved["job_id"], moved["listing_status"], moved["deadline"]) == (keeper, "OPEN", FUTURE2)
    assert _status(pg_conn, keeper_url) == ("CLOSED", "staff")      # listing cũ của job giữ vẫn đóng


def test_merge_moves_closed_donor_listing_with_its_reason(pg_conn):
    company = _company(pg_conn)
    keeper = _new_job(pg_conn, company_id=company)
    donor_url = _url()
    donor = _new_job(pg_conn, company_id=company, url=donor_url)
    _close(pg_conn, donor, "expired_auto")
    _merge_pair(pg_conn, keeper, donor)
    s = _listing(pg_conn, donor_url)
    assert (s["job_id"], s["listing_status"], s["closed_reason"]) == (keeper, "CLOSED", "expired_auto")
    assert _status(pg_conn, _job_url(pg_conn, keeper)) == ("OPEN", None)


# ============================================================ kẽ hở đua giữa đóng job và ghi listing mới
def test_new_listing_waits_for_concurrent_close_and_is_born_closed(pg_conn):
    """Tiến trình A đang đóng job (chưa commit) trong lúc tiến trình B ghi listing tin đăng lại: B phải chờ
    rồi thấy job đã CLOSED, không được ghi listing OPEN vào job đã đóng."""
    job = _new_job(pg_conn)
    other_conn = psycopg2.connect(TEST_DATABASE_URL)
    other_conn.autocommit = False
    new_url = _url()
    result = {}

    def writer():
        try:
            result["inserted"] = db.link_repost_source(
                other_conn, job, source_name="Fake", source_url=new_url, deadline=FUTURE)
            other_conn.commit()
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    try:
        assert db.update_job(pg_conn, job, job_status="CLOSED") is True       # A: chưa commit
        t = threading.Thread(target=writer)
        t.start()
        time.sleep(0.8)
        assert t.is_alive(), "B phải đang chờ khoá dòng job của A"
        pg_conn.commit()                                                       # A commit
        t.join(timeout=10)
        assert not t.is_alive()
        assert result == {"inserted": True}
    finally:
        other_conn.close()
    assert _status(pg_conn, new_url) == ("CLOSED", "staff")
    assert _status(pg_conn, _job_url(pg_conn, job)) == ("CLOSED", "staff")


def test_close_waits_for_uncommitted_listing_insert_then_closes_it_too(pg_conn):
    """Chiều ngược lại: B đã ghi listing OPEN (chưa commit), A đóng job sau đó: A chờ B commit rồi đóng cả
    listing mới của B."""
    job = _new_job(pg_conn)
    other_conn = psycopg2.connect(TEST_DATABASE_URL)
    other_conn.autocommit = False
    new_url = _url()
    done = {}

    def closer():
        try:
            done["ok"] = db.update_job(other_conn, job, job_status="CLOSED")
            other_conn.commit()
        except Exception as exc:  # noqa: BLE001
            done["error"] = exc

    try:
        assert db.link_repost_source(pg_conn, job, source_name="Fake", source_url=new_url, deadline=FUTURE)
        t = threading.Thread(target=closer)
        t.start()
        time.sleep(0.8)
        assert t.is_alive(), "A phải đang chờ B"
        pg_conn.commit()
        t.join(timeout=10)
        assert not t.is_alive()
        assert done == {"ok": True}
    finally:
        other_conn.close()
    assert _status(pg_conn, new_url) == ("CLOSED", "staff")


# ============================================================ bất biến toàn cục
def test_every_listing_satisfies_the_invariants_after_a_mixed_history(pg_conn):
    """Chạy một chuỗi thao tác lẫn lộn rồi kiểm các bất biến mà test_pg_listing_state_migration đã khoá bằng
    CHECK, cộng bất biến cấp job: job CLOSED thì không còn listing OPEN/UNKNOWN."""
    company = _company(pg_conn)
    jobs = [_new_job(pg_conn, company_id=company, title=f"Vị trí {i}") for i in range(4)]
    _link(pg_conn, jobs[0])
    _close(pg_conn, jobs[0])
    _link(pg_conn, jobs[0])                                          # tin đăng lại vào job đóng vì staff
    _close(pg_conn, jobs[1], "expired_auto")
    new = _link(pg_conn, jobs[1])
    db.reopen_job_for_repost(pg_conn, jobs[1], source_url=new, deadline=FUTURE2)
    pg_conn.commit()
    _close(pg_conn, jobs[2])
    db.update_job(pg_conn, jobs[2], job_status="OPEN")
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute("""
            SELECT count(*) FROM job_sources_log l JOIN job_postings j USING (job_id)
             WHERE j.job_status = 'CLOSED' AND l.listing_status <> 'CLOSED'""")
        assert cur.fetchone()[0] == 0
        cur.execute("""
            SELECT count(*) FROM job_postings j
             WHERE j.job_status = 'OPEN'
               AND NOT EXISTS (SELECT 1 FROM job_sources_log l
                                WHERE l.job_id = j.job_id AND l.listing_status = 'OPEN')""")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM job_sources_log WHERE last_seen_at < first_seen_at")
        assert cur.fetchone()[0] == 0
    pg_conn.rollback()


def test_module_exports_are_wired_through_db_facade():
    assert db.mark_source_detail_checked.__module__ == "db.jobs"
    assert db.mark_listing_seen is listing_state.mark_listing_seen
    assert CONFLICT_NONE in listing_state._CONFLICT_SQL
