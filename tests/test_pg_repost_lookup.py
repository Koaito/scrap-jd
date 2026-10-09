"""
Phần 3c nửa 1/2 trên POSTGRES THẬT: hai hàm tầng DB mới của việc chặn nguồn sinh job trùng,
db.find_repost_candidate (tra job để coi tin vừa crawl là đăng lại). Việc mở lại job CLOSED từng nằm ở
db.reopen_job_for_repost, từ C4 phần 1/3 do db.link_repost_source làm (tests/test_pg_job_sync.py).
Cách chạy như tests/test_pg_merge_duplicates.py: đặt TEST_DATABASE_URL trỏ tới database dùng
riêng cho test (tên chứa "test"); không đặt thì cả file được bỏ qua.

Chứng minh:
  - khoá tra: cùng công ty + tiêu đề (không phân biệt hoa/thường, gộp khoảng trắng bên trong) +
    tỉnh; KHÔNG xét level; xét cả job CLOSED; tỉnh/công ty khác thì không khớp;
  - thứ tự chọn khi nhiều job khớp: OPEN > cùng level > tạo gần nhất;
  - closed_reason đọc thẳng từ cột job_postings.closed_reason (A2), không còn đọc audit_logs;
  - find_manual_job_duplicate (dùng cho POST /jobs) giữ nguyên hành vi cũ.
"""
import os
import uuid
from datetime import date
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
import pytest

import db

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")
PAST = "2020-01-01 00:00:00"


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


# ------------------------------------------------------------------ helpers
def _company(conn):
    cid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)",
                    (cid, f"Công ty Đăng Lại {uuid.uuid4().hex[:8]}"))
    conn.commit()
    return cid


def _province(conn, name):
    pid = db.get_or_create_province(conn, name)
    conn.commit()
    return pid


def _job(conn, company_id, title, *, level="Junior", province_id=None, status="OPEN",
         deadline=None, url=None, created="2026-09-01 10:00:00"):
    job_id = db.insert_job(
        conn, company_id=company_id, job_title=title, matching_industry="Data",
        level_id=db.get_level_id(conn, level), province_id=province_id, work_type=None,
        currency="VNĐ", salary_min=None, salary_max=None, salary_type="NEGOTIABLE",
        source_url=url or f"https://www.example.com/{uuid.uuid4()}", source_name="Fake",
    )
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.skip_updated_at', 'on', true)")
        cur.execute("UPDATE job_postings SET job_status = %s, deadline = %s, created_at = %s, "
                    "updated_at = %s WHERE job_id = %s", (status, deadline, created, PAST, job_id))
    conn.commit()
    return job_id


def _audit(conn, job_id, action, changes=None, at="2026-09-10 10:00:00"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO audit_logs (action_type, entity_type, entity_id, changes, is_manual_log, created_at) "
            "VALUES (%s, 'JOB', %s, %s, true, %s)",
            (action, job_id, psycopg2.extras.Json(changes) if changes is not None else None, at))
    conn.commit()


def _find(conn, company, title, province=None, level=None):
    res = db.find_repost_candidate(conn, company_id=company, job_title=title, province_id=province,
                                   level_id=db.get_level_id(conn, level) if level else None)
    conn.rollback()
    return res


def _row(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT job_status::text, deadline, source_url, updated_at::text, content_hash "
                    "FROM job_postings WHERE job_id = %s", (job_id,))
        row = cur.fetchone()
    conn.rollback()
    return row


# ------------------------------------------------------------------ khoá tra
def test_matches_open_job_ignoring_level_case_and_inner_whitespace(pg_conn):
    c = _company(pg_conn)
    job = _job(pg_conn, c, "Data Engineer", level="Junior")
    # level khác, hoa/thường khác, hai dấu cách bên trong, khoảng trắng hai đầu: vẫn khớp
    for title in ("Data Engineer", "DATA ENGINEER", "  data   engineer  ", "Data\tEngineer"):
        res = _find(pg_conn, c, title, level="Senior")
        assert res is not None and res["job_id"] == job, title
    assert res["job_status"] == "OPEN" and res["closed_reason"] is None


def test_matches_closed_job_too(pg_conn):
    c = _company(pg_conn)
    job = _job(pg_conn, c, "Backend Dev", status="CLOSED", deadline="2026-09-05")
    res = _find(pg_conn, c, "Backend Dev")
    assert res["job_id"] == job and res["job_status"] == "CLOSED" and res["deadline"] == date(2026, 9, 5)


@pytest.mark.parametrize("title", ["Data Engineer Senior", "Data", "Engineer Data"])
def test_different_title_does_not_match(pg_conn, title):
    c = _company(pg_conn)
    _job(pg_conn, c, "Data Engineer")
    assert _find(pg_conn, c, title) is None


def test_other_company_does_not_match(pg_conn):
    _job(pg_conn, _company(pg_conn), "Data Engineer")
    assert _find(pg_conn, _company(pg_conn), "Data Engineer") is None


def test_province_is_part_of_the_key_including_null(pg_conn):
    c = _company(pg_conn)
    hn, hcm = _province(pg_conn, "Hà Nội"), _province(pg_conn, "Hồ Chí Minh")
    in_hn = _job(pg_conn, c, "Kế toán", province_id=hn)
    no_prov = _job(pg_conn, c, "Kế toán", province_id=None)
    assert _find(pg_conn, c, "Kế toán", province=hn)["job_id"] == in_hn
    assert _find(pg_conn, c, "Kế toán", province=None)["job_id"] == no_prov
    assert _find(pg_conn, c, "Kế toán", province=hcm) is None


def test_no_candidate_returns_none_on_empty_db(pg_conn):
    assert _find(pg_conn, _company(pg_conn), "Gì đó") is None


# ------------------------------------------------------------------ thứ tự chọn khi nhiều job khớp
def test_prefers_open_over_closed_even_when_closed_has_same_level(pg_conn):
    c = _company(pg_conn)
    _job(pg_conn, c, "QA", level="Senior", status="CLOSED", created="2026-09-20 00:00:00")
    open_job = _job(pg_conn, c, "QA", level="Junior", status="OPEN", created="2026-09-01 00:00:00")
    assert _find(pg_conn, c, "QA", level="Senior")["job_id"] == open_job


def test_prefers_same_level_then_most_recent_within_same_status(pg_conn):
    c = _company(pg_conn)
    _job(pg_conn, c, "QA", level="Junior", created="2026-09-20 00:00:00")
    same_level_old = _job(pg_conn, c, "QA", level="Senior", created="2026-09-01 00:00:00")
    same_level_new = _job(pg_conn, c, "QA", level="Senior", created="2026-09-10 00:00:00")
    assert _find(pg_conn, c, "QA", level="Senior")["job_id"] == same_level_new   # cùng level + mới nhất
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM job_sources_log WHERE job_id = %s", (same_level_new,))
        cur.execute("DELETE FROM job_postings WHERE job_id = %s", (same_level_new,))
    pg_conn.commit()
    assert _find(pg_conn, c, "QA", level="Senior")["job_id"] == same_level_old
    # không truyền level: chỉ còn xếp theo mới nhất
    assert _find(pg_conn, c, "QA")["level_id"] == db.get_level_id(pg_conn, "Junior")


# ------------------------------------------------------------------ closed_reason
def _close(conn, job_id, reason=None):
    with conn.cursor() as cur:
        if reason is None:
            cur.execute("UPDATE job_postings SET job_status = 'CLOSED' WHERE job_id = %s", (job_id,))
        else:
            cur.execute("UPDATE job_postings SET job_status = 'CLOSED', closed_reason = %s "
                        "WHERE job_id = %s", (reason, job_id))
    conn.commit()


@pytest.mark.parametrize("reason", ["staff", "expired_auto", "merged", "unknown"])
def test_closed_reason_is_read_from_the_column(pg_conn, reason):
    c = _company(pg_conn)
    job = _job(pg_conn, c, "DevOps")
    _close(pg_conn, job, reason)
    assert _find(pg_conn, c, "DevOps")["closed_reason"] == reason


def test_closed_without_reason_reads_as_unknown_and_open_as_none(pg_conn):
    c = _company(pg_conn)
    job = _job(pg_conn, c, "SRE")
    assert _find(pg_conn, c, "SRE")["closed_reason"] is None
    _close(pg_conn, job)                                    # code quên truyền lý do: trigger điền unknown
    assert _find(pg_conn, c, "SRE")["closed_reason"] == "unknown"


def test_audit_logs_are_no_longer_read(pg_conn):
    c = _company(pg_conn)
    job = _job(pg_conn, c, "SRE")
    _close(pg_conn, job, "expired_auto")
    _audit(pg_conn, job, "DELETE_JOB", {"job_status": {"old": "OPEN", "new": "CLOSED"}})
    assert _find(pg_conn, c, "SRE")["closed_reason"] == "expired_auto"


def test_find_repost_candidate_leaves_the_transaction_alone(pg_conn):
    c = _company(pg_conn)
    _job(pg_conn, c, "Data Engineer")
    with pg_conn.cursor() as cur:
        cur.execute("SELECT set_config('app.skip_updated_at', 'on', true)")
    db.find_repost_candidate(pg_conn, company_id=c, job_title="Data Engineer", province_id=None)
    # hàm chỉ đọc và không tự rollback/commit: cờ của transaction hiện tại vẫn còn
    with pg_conn.cursor() as cur:
        cur.execute("SELECT current_setting('app.skip_updated_at', true)")
        assert cur.fetchone()[0] == "on"
    pg_conn.rollback()


# Việc mở lại job vì tin đăng lại (trước đây db.reopen_job_for_repost) nay do db.link_repost_source làm, test ở
# tests/test_pg_job_sync.py (C4 phần 1/3).


# ------------------------------------------------------------------ hành vi cũ của POST /jobs không đổi
def test_find_manual_job_duplicate_still_ignores_closed_and_uses_level(pg_conn):
    c = _company(pg_conn)
    _job(pg_conn, c, "Data Engineer", level="Junior", status="CLOSED")
    jr = db.get_level_id(pg_conn, "Junior")
    assert db.find_manual_job_duplicate(pg_conn, company_id=c, job_title="Data Engineer",
                                        level_id=jr, province_id=None) is None
    open_job = _job(pg_conn, c, "Data Engineer", level="Junior", status="OPEN")
    assert db.find_manual_job_duplicate(pg_conn, company_id=c, job_title="Data Engineer",
                                        level_id=jr, province_id=None) == open_job
    assert db.find_manual_job_duplicate(pg_conn, company_id=c, job_title="Data Engineer",
                                        level_id=db.get_level_id(pg_conn, "Senior"), province_id=None) is None
    pg_conn.rollback()
