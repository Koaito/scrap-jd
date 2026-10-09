"""
C1 nửa 2/2, đường pipeline THẬT trên Postgres thật: chạy pipeline.run_pipeline() với adapter giả rồi kiểm
trạng thái listing (job_sources_log) sau từng tình huống crawl. Phần luật ghi từng hàm nằm ở
tests/test_pg_listing_state_writes.py; file này chứng minh pipeline nối các hàm đó đúng thứ tự (tin đăng lại
được ghi trước khi job được mở lại, v.v.).

Cách chạy giống tests/test_pg_pipeline_repost_reopen.py: đặt TEST_DATABASE_URL (tên chứa "test"); không đặt
thì bỏ qua.
"""
import os
import uuid
from datetime import date
from urllib.parse import urlparse

import psycopg2
import pytest

import db
import pipeline
from adapters.base import BaseAdapter
from scrapjd.models import RawJobRecord

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")
FUTURE_TEXT, FUTURE = "31/12/2099", date(2099, 12, 31)
PAST_TEXT = "01/01/2020"


class FakeAdapter(BaseAdapter):
    source_name = "Fake"

    def __init__(self, urls, *, company, deadline=FUTURE_TEXT):
        super().__init__()
        self.urls, self.company, self.deadline = urls, company, deadline

    def fetch_jobs(self, category_key, max_pages):
        for url in self.urls:
            yield RawJobRecord(
                job_title="Data Analyst", company_name=self.company, source_url=url, source_name="Fake",
                salary_text="", province_text="Hà Nội", experience_text="2 năm",
            )

    def fetch_job_full_detail(self, source_url):
        return {
            "work_type": "Toàn thời gian", "deadline_text": self.deadline,
            "job_description": "mô tả công việc", "requirements": "yêu cầu", "perks": "",
            "required_skills": ["SQL"],
        }


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
def company():
    return f"Công ty Listing {uuid.uuid4().hex[:8]}"


def _crawl(conn, adapter):
    stats = pipeline.run_pipeline(adapter, conn, "data-analyst", 1)
    conn.rollback()
    return stats


def _listing(conn, url):
    with conn.cursor() as cur:
        cur.execute("SELECT listing_status, closed_reason, deadline, detail_checked_at IS NOT NULL, "
                    "last_seen_at >= first_seen_at FROM job_sources_log WHERE source_url = %s", (url,))
        row = cur.fetchone()
    conn.rollback()
    assert row is not None, url
    return row


def _job(conn, company):
    with conn.cursor() as cur:
        cur.execute("SELECT j.job_id::text, j.job_status::text FROM job_postings j "
                    "JOIN companies c USING (company_id) WHERE c.company_name = %s", (company,))
        rows = cur.fetchall()
    conn.rollback()
    assert len(rows) == 1, rows
    return rows[0]


def _seed(conn, company, url="https://x/old"):
    assert _crawl(conn, FakeAdapter([url], company=company))["inserted"] == 1
    return _job(conn, company)[0]


def test_new_job_gets_an_open_listing_with_deadline_and_detail_stamp(pg_conn, company):
    _seed(pg_conn, company)
    assert _listing(pg_conn, "https://x/old") == ("OPEN", None, FUTURE, True, True)


def test_repost_of_open_job_adds_open_listing_with_own_deadline(pg_conn, company):
    _seed(pg_conn, company)
    stats = _crawl(pg_conn, FakeAdapter(["https://x/new"], company=company, deadline="30/06/2100"))
    assert stats["skipped_duplicate_repost"] == 1
    assert _listing(pg_conn, "https://x/old")[:3] == ("OPEN", None, FUTURE)
    assert _listing(pg_conn, "https://x/new")[:3] == ("OPEN", None, date(2100, 6, 30))


@pytest.mark.parametrize("reason", ["staff", "unknown", "merged"])
def test_repost_of_job_closed_for_other_reasons_adds_closed_listing(pg_conn, company, reason):
    job_id = _seed(pg_conn, company)
    assert db.update_job(pg_conn, job_id, job_status="CLOSED", closed_reason=reason)
    pg_conn.commit()
    stats = _crawl(pg_conn, FakeAdapter(["https://x/new"], company=company))
    assert stats["repost_kept_closed"] == 1
    assert _job(pg_conn, company)[1] == "CLOSED"
    assert _listing(pg_conn, "https://x/old")[:2] == ("CLOSED", reason)
    assert _listing(pg_conn, "https://x/new")[:2] == ("CLOSED", reason)


def test_repost_of_expired_job_reopens_job_and_only_the_new_listing(pg_conn, company):
    job_id = _seed(pg_conn, company)
    assert db.update_job(pg_conn, job_id, job_status="CLOSED", closed_reason="expired_auto")
    pg_conn.commit()
    stats = _crawl(pg_conn, FakeAdapter(["https://x/new"], company=company))
    assert stats["repost_reopened"] == 1
    assert _job(pg_conn, company)[1] == "OPEN"
    assert _listing(pg_conn, "https://x/old")[:2] == ("CLOSED", "expired_auto")
    assert _listing(pg_conn, "https://x/new")[:3] == ("OPEN", None, FUTURE)


def test_repost_with_past_deadline_keeps_job_and_new_listing_closed(pg_conn, company):
    job_id = _seed(pg_conn, company)
    assert db.update_job(pg_conn, job_id, job_status="CLOSED", closed_reason="expired_auto")
    pg_conn.commit()
    stats = _crawl(pg_conn, FakeAdapter(["https://x/new"], company=company, deadline=PAST_TEXT))
    assert stats["repost_kept_closed"] == 1 and "repost_reopened" not in stats
    assert _job(pg_conn, company)[1] == "CLOSED"
    assert _listing(pg_conn, "https://x/new")[:3] == ("CLOSED", "expired_auto", date(2020, 1, 1))


def test_refetched_existing_job_writes_deadline_and_last_seen_to_its_listing(pg_conn, company):
    """Job crawl từ trước khi có hạn: lượt sau vá hạn cho job và cho cả listing của đúng URL đó."""
    url = "https://x/legacy"
    cid = db.get_or_create_company_by_profile(pg_conn, company, None)
    db.insert_job(
        pg_conn, company_id=cid, job_title="Data Analyst", matching_industry="Data", level_id=None,
        province_id=None, work_type=None, currency="VNĐ", salary_min=None, salary_max=None,
        salary_type="NEGOTIABLE", source_url=url, source_name="Fake", deadline=None,
    )
    pg_conn.commit()
    assert _listing(pg_conn, url) == ("OPEN", None, None, False, True)

    stats = _crawl(pg_conn, FakeAdapter([url], company=company))

    assert stats["updated_existing"] == 1
    assert _listing(pg_conn, url) == ("OPEN", None, FUTURE, True, True)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT deadline FROM job_postings j JOIN companies c USING (company_id) "
                    "WHERE c.company_name = %s", (company,))
        assert cur.fetchone()[0] == FUTURE
    pg_conn.rollback()
