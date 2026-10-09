"""
C2 nửa 1/2 trên POSTGRES THẬT: câu SQL đọc của db.list_jobs_with_listings và báo cáo so lệch chạy trên dữ
liệu do chính các đường ghi của C1 tạo ra. Mục tiêu là chứng minh trước khi viết nửa 2/2:
  - đường ghi nào đã cho job khớp với giá trị suy ra từ listing, đường nào còn lệch và lệch kiểu gì;
  - báo cáo đọc được dữ liệu thật, không làm bẩn transaction, và bắt được một lệch cố ý dựng ra.

Cách chạy giống tests/test_pg_listing_state_writes.py: đặt TEST_DATABASE_URL (tên chứa "test"); không đặt
thì bỏ qua.
"""
import os
import uuid
from datetime import date
from urllib.parse import urlparse

import psycopg2
import pytest

import check_listing_derivation as cld
from scrapjd import db

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


def _company(conn) -> str:
    cid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)", (cid, f"Cty {cid[:8]}"))
    conn.commit()
    return cid


def _url():
    return f"https://example.com/{uuid.uuid4()}"


def _job(conn, *, url=None, deadline=FUTURE):
    job_id = db.insert_job(
        conn, company_id=_company(conn), job_title="Data Analyst", matching_industry="Data", level_id=None,
        province_id=None, work_type=None, currency="VNĐ", salary_min=None, salary_max=None,
        salary_type="NEGOTIABLE", source_url=url or _url(), source_name="Fake", deadline=deadline,
        detail_fetched=True)
    conn.commit()
    return job_id


def _link(conn, job_id, *, deadline=FUTURE):
    url = _url()
    assert db.link_repost_source(conn, job_id, source_name="Fake", source_url=url, deadline=deadline)
    conn.commit()
    return url


def _report(conn):
    return cld.build_report(db.list_jobs_with_listings(conn))


def _fields(rep, job_id):
    return {m.field: m for m in rep.mismatches if m.job_id == job_id}


def test_fresh_job_matches_derivation(pg_conn):
    job = _job(pg_conn)
    rep = _report(pg_conn)
    assert (rep.jobs_compared, rep.jobs_matching, rep.mismatches) == (1, 1, [])
    assert job


def test_staff_closed_job_matches_derivation(pg_conn):
    job = _job(pg_conn)
    db.update_job(pg_conn, job, job_status="CLOSED")
    pg_conn.commit()
    assert _report(pg_conn).mismatches == []


def test_auto_closed_and_reopened_by_staff_matches_derivation(pg_conn):
    job = _job(pg_conn)
    db.update_job(pg_conn, job, job_status="CLOSED", closed_reason="expired_auto")
    db.update_job(pg_conn, job, job_status="OPEN")
    pg_conn.commit()
    assert _report(pg_conn).mismatches == []


def test_job_reopened_by_repost_matches_derivation_on_all_four_fields(pg_conn):
    job = _job(pg_conn)
    db.update_job(pg_conn, job, job_status="CLOSED", closed_reason="expired_auto")
    _link(pg_conn, job, deadline=FUTURE2)            # C4: tin đăng lại mở lại job (listing OPEN, job suy ra OPEN)
    assert _report(pg_conn).mismatches == []


def test_repost_on_open_job_matches_derivation_on_all_four_fields(pg_conn):
    """Ca khó nhất, từng lệch ở C2 nửa 1/2: job OPEN nhận tin đăng lại. Từ nửa 2/2 link_repost_source đồng bộ
    source_url và hạn của job sang listing mới nhất (C4 phần 2/3: không còn extend_job_deadline)."""
    job = _job(pg_conn)
    new = _link(pg_conn, job, deadline=FUTURE2)
    assert _report(pg_conn).mismatches == []
    with pg_conn.cursor() as cur:
        cur.execute("SELECT source_url FROM job_postings WHERE job_id = %s", (job,))
        assert cur.fetchone()[0] == new
    pg_conn.rollback()


def test_staff_deadline_edit_matches_derivation_with_single_listing(pg_conn):
    job = _job(pg_conn)
    db.update_job(pg_conn, job, deadline=FUTURE2)
    pg_conn.commit()
    assert _report(pg_conn).mismatches == []


def test_deliberate_deadline_drift_is_caught_with_its_kind(pg_conn):
    job = _job(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE job_postings SET deadline = %s WHERE job_id = %s", (FUTURE2, job))
    pg_conn.commit()
    diffs = _fields(_report(pg_conn), job)
    assert set(diffs) == {"deadline"} and diffs["deadline"].kind == "cả hai có hạn, job muộn hơn"


def test_job_without_listing_is_reported_separately(pg_conn):
    job = _job(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM job_sources_log WHERE job_id = %s", (job,))
    pg_conn.commit()
    rep = _report(pg_conn)
    assert (rep.total_jobs, rep.jobs_without_listings, rep.jobs_compared, rep.mismatches) == (1, 1, 0, [])


def test_list_jobs_with_listings_shape_and_clean_transaction(pg_conn):
    job = _job(pg_conn)
    second = _link(pg_conn, job)
    rows = db.list_jobs_with_listings(pg_conn)
    assert pg_conn.status == psycopg2.extensions.STATUS_READY      # đã đóng transaction đọc
    (row,) = rows
    assert row["job_id"] == job and isinstance(row["job_id"], str)
    assert [l["listing_status"] for l in row["listings"]] == ["OPEN", "OPEN"]
    assert second in {l["source_url"] for l in row["listings"]}
    assert set(row["listings"][0]) == {"job_id", "source_url", "listing_status", "deadline", "first_seen_at",
                                       "last_seen_at", "closed_reason", "closed_at"}
    assert "raw_jd_content" not in row["listings"][0]


def test_run_prints_report_and_strict_flags_drift(pg_conn, capsys, tmp_path):
    job = _job(pg_conn)
    assert cld.run(pg_conn, strict=True) == 0
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE job_postings SET deadline = NULL WHERE job_id = %s", (job,))
    pg_conn.commit()
    csv_path = tmp_path / "lech.csv"
    assert cld.run(pg_conn, strict=True, csv_path=str(csv_path)) == 2
    out = capsys.readouterr().out
    assert "job không có hạn, listing có" in out
    assert job in csv_path.read_text(encoding="utf-8-sig")
