"""
Tin đăng lại dưới URL khác, chạy trên POSTGRES THẬT (10/2026).

Cách chạy giống tests/test_pg_integration.py: đặt TEST_DATABASE_URL trỏ tới một
database dùng riêng cho test (tên phải chứa "test", fixture DROP SCHEMA public).
Không đặt biến này -> cả file được bỏ qua.

Quan trọng nhất là test_repost_url_is_not_refetched_on_later_crawls: chạy
pipeline THẬT 3 lượt liên tiếp và đếm số lần adapter phải fetch chi tiết.
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
from models import RawJobRecord

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")

DETAIL = {
    "work_type": "Toàn thời gian", "deadline_text": "05/09/2026",
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
    yield conn
    conn.rollback()
    conn.close()


class CountingAdapter(BaseAdapter):
    """Giống TopCV/VietnamWorks: không có hook bỏ qua sớm, mỗi record đều qua
    pipeline. Đếm số lần phải fetch chi tiết theo URL."""

    source_name = "Fake"

    def __init__(self, urls, company):
        super().__init__()
        self.urls = urls
        self.company = company
        self.detail_calls = []

    def fetch_jobs(self, category_key, max_pages):
        for url in self.urls:
            yield RawJobRecord(
                job_title="Data Analyst", company_name=self.company, source_url=url,
                source_name="Fake", salary_text="", province_text="Hà Nội",
                experience_text="2 năm",
            )

    def fetch_job_full_detail(self, source_url):
        self.detail_calls.append(source_url)
        return dict(DETAIL)


def _count(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def _make_job(conn, url, name="Công ty Link"):
    with conn.cursor() as cur:
        cid = str(uuid.uuid4())
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)", (cid, name))
    job_id = db.insert_job(
        conn, company_id=cid, job_title="Data Analyst", matching_industry="Data",
        level_id=None, province_id=None, work_type=None, currency="VND",
        salary_min=None, salary_max=None, salary_type="NEGOTIABLE",
        source_url=url, source_name="TopCV", raw_jd_content="gốc",
    )
    conn.commit()
    return job_id


# ---------------------------------------------------------------- db function

def test_link_repost_source_adds_row_once_without_new_job(pg_conn):
    job_id = _make_job(pg_conn, "https://topcv/a")
    jobs_before = _count(pg_conn, "SELECT count(*) FROM job_postings")

    first = db.link_repost_source(pg_conn, job_id, source_name="TopCV",
                                  source_url="https://topcv/a-repost", raw_jd_content="đăng lại")
    second = db.link_repost_source(pg_conn, job_id, source_name="TopCV",
                                   source_url="https://topcv/a-repost")
    pg_conn.commit()

    assert (first, second) == (True, False)
    assert _count(pg_conn, "SELECT count(*) FROM job_postings") == jobs_before
    assert _count(pg_conn, "SELECT count(*) FROM job_sources_log WHERE job_id = %s", (job_id,)) == 2
    with pg_conn.cursor() as cur:
        cur.execute("SELECT source_url FROM job_postings WHERE job_id = %s", (job_id,))
        assert cur.fetchone()[0] == "https://topcv/a"  # nguồn gốc của job không đổi
        cur.execute("SELECT raw_jd_content FROM job_sources_log WHERE source_url = %s",
                    ("https://topcv/a-repost",))
        assert cur.fetchone()[0] == "đăng lại"


def test_probe_recognises_linked_url(pg_conn):
    job_id = _make_job(pg_conn, "https://topcv/b")
    db.link_repost_source(pg_conn, job_id, source_name="TopCV", source_url="https://topcv/b-repost")
    pg_conn.commit()

    probe = db.get_job_probe_by_source_url(pg_conn, "https://topcv/b-repost")

    assert probe is not None and str(probe[0]) == job_id


def test_stats_by_source_counts_jobs_not_urls(pg_conn):
    before = {r["source_name"]: r["n"] for r in db.get_stats_summary(pg_conn)["by_source"]}
    job_id = _make_job(pg_conn, "https://topcv/c")
    db.link_repost_source(pg_conn, job_id, source_name="TopCV", source_url="https://topcv/c-r1")
    db.link_repost_source(pg_conn, job_id, source_name="TopCV", source_url="https://topcv/c-r2")
    pg_conn.commit()

    after = {r["source_name"]: r["n"] for r in db.get_stats_summary(pg_conn)["by_source"]}

    assert after["TopCV"] == before.get("TopCV", 0) + 1  # 1 job, không phải 3 URL


# ------------------------------------------------------------ pipeline thật

def test_repost_url_is_not_refetched_on_later_crawls(pg_conn):
    company = f"Công ty Repost {uuid.uuid4().hex[:6]}"
    original, repost = "https://topcv/orig", "https://topcv/orig-repost"

    # Lượt 1: job gốc được insert.
    a1 = CountingAdapter([original], company)
    s1 = pipeline.run_pipeline(a1, pg_conn, "data-analyst", 1)
    assert s1["inserted"] == 1

    # Lượt 2: nhà tuyển dụng "làm mới" tin -> URL mới, cùng nội dung. Lần đầu
    # thấy nên phải fetch chi tiết 1 lần, rồi được nhận ra là đăng lại.
    a2 = CountingAdapter([repost], company)
    s2 = pipeline.run_pipeline(a2, pg_conn, "data-analyst", 1)
    assert s2["inserted"] == 0 and s2["skipped_duplicate_repost"] == 1
    assert a2.detail_calls == [repost]

    # Lượt 3: URL đăng lại đã được ghi nhận -> là job "đã có và đủ field",
    # KHÔNG fetch chi tiết nữa (trước đây: fetch lại + bỏ lại ở mọi lượt).
    a3 = CountingAdapter([repost], company)
    s3 = pipeline.run_pipeline(a3, pg_conn, "data-analyst", 1)
    assert a3.detail_calls == []
    assert s3["skipped_duplicate"] == 1
    assert s3["skipped_duplicate_repost"] == 0
    assert s3["inserted"] == 0

    assert _count(pg_conn, "SELECT count(*) FROM job_postings jp JOIN companies c "
                           "USING (company_id) WHERE c.company_name = %s", (company,)) == 1
    assert _count(pg_conn, "SELECT count(*) FROM job_sources_log jsl JOIN job_postings jp "
                           "USING (job_id) JOIN companies c USING (company_id) "
                           "WHERE c.company_name = %s", (company,)) == 2


# ------------------------------------------------------- dời deadline (1c)

def _deadline(conn, job_id):
    with conn.cursor() as cur:
        cur.execute("SELECT deadline FROM job_postings WHERE job_id = %s", (job_id,))
        return cur.fetchone()[0]


def _set_deadline(conn, job_id, d):
    with conn.cursor() as cur:
        cur.execute("UPDATE job_postings SET deadline = %s WHERE job_id = %s", (d, job_id))
    conn.commit()


def test_extend_job_deadline_only_moves_forward(pg_conn):
    job_id = _make_job(pg_conn, "https://topcv/d1")
    _set_deadline(pg_conn, job_id, date(2026, 8, 1))

    assert db.extend_job_deadline(pg_conn, job_id, date(2026, 10, 30)) is True
    assert _deadline(pg_conn, job_id) == date(2026, 10, 30)

    # Hạn sớm hơn / bằng hạn hiện tại: không rút ngắn, không báo là đã đổi.
    assert db.extend_job_deadline(pg_conn, job_id, date(2026, 9, 1)) is False
    assert db.extend_job_deadline(pg_conn, job_id, date(2026, 10, 30)) is False
    assert _deadline(pg_conn, job_id) == date(2026, 10, 30)


def test_extend_job_deadline_fills_null_and_ignores_none(pg_conn):
    job_id = _make_job(pg_conn, "https://topcv/d2")
    assert _deadline(pg_conn, job_id) is None

    assert db.extend_job_deadline(pg_conn, job_id, None) is False
    assert db.extend_job_deadline(pg_conn, job_id, date(2026, 10, 30)) is True
    assert _deadline(pg_conn, job_id) == date(2026, 10, 30)


def test_extend_job_deadline_does_not_touch_closed_job(pg_conn):
    job_id = _make_job(pg_conn, "https://topcv/d3")
    _set_deadline(pg_conn, job_id, date(2026, 8, 1))
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE job_postings SET job_status = 'CLOSED' WHERE job_id = %s", (job_id,))
    pg_conn.commit()

    assert db.extend_job_deadline(pg_conn, job_id, date(2026, 10, 30)) is False
    assert _deadline(pg_conn, job_id) == date(2026, 8, 1)


def test_repost_revives_expired_job_through_real_pipeline(pg_conn):
    company = f"Công ty Hết Hạn {uuid.uuid4().hex[:6]}"
    original, repost = "https://topcv/exp", "https://topcv/exp-repost"

    a1 = CountingAdapter([original], company)
    assert pipeline.run_pipeline(a1, pg_conn, "data-analyst", 1)["inserted"] == 1
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT jp.job_id FROM job_postings jp JOIN companies c USING (company_id) "
            "WHERE c.company_name = %s", (company,))
        job_id = cur.fetchone()[0]
    _set_deadline(pg_conn, job_id, date(2020, 1, 1))  # job cũ đã quá hạn từ lâu

    # Tin đăng lại với hạn mới (DETAIL của CountingAdapter: 05/09/2026).
    a2 = CountingAdapter([repost], company)
    s2 = pipeline.run_pipeline(a2, pg_conn, "data-analyst", 1)

    assert s2["skipped_duplicate_repost"] == 1
    assert s2["repost_deadline_extended"] == 1
    assert _deadline(pg_conn, job_id) == date(2026, 9, 5)

    # Crawl lại đúng tin đó: hạn không đổi nữa, không đếm thêm.
    s3 = pipeline.run_pipeline(CountingAdapter([repost], company), pg_conn, "data-analyst", 1)
    assert s3["repost_deadline_extended"] == 0
