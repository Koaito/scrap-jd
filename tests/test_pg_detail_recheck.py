"""
Mục 2 (10/2026) trên POSTGRES THẬT: dấu job_sources_log.detail_checked_at và
việc pipeline thật không fetch lại job thiếu field liên tục.

Cách chạy giống tests/test_pg_integration.py: TEST_DATABASE_URL trỏ tới
database dùng riêng cho test (tên chứa "test", fixture DROP SCHEMA public).
Không đặt biến này -> cả file được bỏ qua.
"""
import os
import uuid
from datetime import date
from urllib.parse import urlparse

import psycopg2
import pytest

from scrapjd import db
import pipeline
from scrapjd.adapters.base import BaseAdapter
from scrapjd.models import RawJobRecord

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SQL_DIR = os.path.join(os.path.dirname(__file__), "..", "sql")
_SCHEMA_PATH = os.path.join(_SQL_DIR, "schema.sql")
_MIGRATION_PATH = os.path.join(_SQL_DIR, "migration_add_source_detail_checked_at.sql")


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


class NoDeadlineAdapter(BaseAdapter):
    """Nguồn KHÔNG ghi hạn nộp (deadline_text rỗng) — đúng ca gây fetch lại
    mãi mãi trước đây. Đếm số lần phải fetch chi tiết."""

    source_name = "Fake"

    def __init__(self, url, company, deadline_text=""):
        super().__init__()
        self.url, self.company, self.deadline_text = url, company, deadline_text
        self.detail_calls = 0

    def fetch_jobs(self, category_key, max_pages):
        yield RawJobRecord(
            job_title="Data Analyst", company_name=self.company, source_url=self.url,
            source_name="Fake", province_text="Hà Nội", experience_text="2 năm",
        )

    def fetch_job_full_detail(self, source_url):
        self.detail_calls += 1
        return {
            "work_type": "Toàn thời gian", "deadline_text": self.deadline_text,
            "job_description": "mô tả", "requirements": "yêu cầu", "perks": "",
            "required_skills": ["SQL"],
        }


def _one(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
        return row[0] if row else None


def _run(conn, adapter):
    return pipeline.run_pipeline(adapter, conn, "data-analyst", 1)


def _age(conn, url, days):
    """Giả lập lần fetch gần nhất đã cách đây `days` ngày (None = chưa ghi nhận)."""
    with conn.cursor() as cur:
        if days is None:
            cur.execute("UPDATE job_sources_log SET detail_checked_at = NULL WHERE source_url = %s", (url,))
        else:
            cur.execute("UPDATE job_sources_log SET detail_checked_at = now() - make_interval(days => %s) "
                        "WHERE source_url = %s", (days, url))
    conn.commit()


def _make_job(conn, url, **kw):
    cid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)",
                    (cid, f"Công ty {uuid.uuid4().hex[:6]}"))
    job_id = db.insert_job(
        conn, company_id=cid, job_title="Data Analyst", matching_industry="Data",
        level_id=None, province_id=None, work_type=None, currency="VND",
        salary_min=None, salary_max=None, salary_type="NEGOTIABLE",
        source_url=url, source_name="TopCV", raw_jd_content="gốc", **kw,
    )
    conn.commit()
    return job_id


# ------------------------------------------------------------ schema/migration

def test_schema_defines_detail_checked_at(pg_conn):
    assert _one(pg_conn, "SELECT data_type FROM information_schema.columns "
                         "WHERE table_name = 'job_sources_log' AND column_name = 'detail_checked_at'"
                ) == "timestamp with time zone"


def test_migration_adds_column_to_legacy_table_and_is_idempotent(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("ALTER TABLE job_sources_log DROP COLUMN detail_checked_at")
    pg_conn.commit()
    sql = open(_MIGRATION_PATH, encoding="utf-8").read()
    for _ in range(2):  # chạy 2 lần không lỗi
        with pg_conn.cursor() as cur:
            cur.execute(sql)
        pg_conn.commit()
    assert _one(pg_conn, "SELECT count(*) FROM information_schema.columns "
                         "WHERE table_name = 'job_sources_log' AND column_name = 'detail_checked_at'") == 1


# ----------------------------------------------------------------- db functions

def test_insert_job_stamps_only_when_detail_was_fetched(pg_conn):
    fetched = _make_job(pg_conn, f"https://t/{uuid.uuid4().hex}", detail_fetched=True)
    manual = _make_job(pg_conn, f"https://t/{uuid.uuid4().hex}")
    sql = "SELECT detail_checked_at FROM job_sources_log WHERE job_id = %s"
    assert _one(pg_conn, sql, (fetched,)) is not None
    assert _one(pg_conn, sql, (manual,)) is None


def test_mark_source_detail_checked_does_not_touch_job_updated_at(pg_conn):
    url = f"https://t/{uuid.uuid4().hex}"
    job_id = _make_job(pg_conn, url)
    before = _one(pg_conn, "SELECT updated_at FROM job_postings WHERE job_id = %s", (job_id,))

    db.mark_source_detail_checked(pg_conn, url)
    pg_conn.commit()

    assert _one(pg_conn, "SELECT detail_checked_at FROM job_sources_log WHERE source_url = %s", (url,)) is not None
    assert _one(pg_conn, "SELECT updated_at FROM job_postings WHERE job_id = %s", (job_id,)) == before


def test_link_repost_source_stamps_detail_checked(pg_conn):
    job_id = _make_job(pg_conn, f"https://t/{uuid.uuid4().hex}")
    alias = f"https://t/{uuid.uuid4().hex}"
    db.link_repost_source(pg_conn, job_id, source_name="TopCV", source_url=alias)
    pg_conn.commit()
    assert _one(pg_conn, "SELECT detail_checked_at FROM job_sources_log WHERE source_url = %s", (alias,)) is not None


def test_probe_returns_detail_checked_at(pg_conn):
    url = f"https://t/{uuid.uuid4().hex}"
    _make_job(pg_conn, url, detail_fetched=True)
    probe = db.get_job_probe_by_source_url(pg_conn, url)
    assert len(probe) == 5 and probe[4] is not None


# ------------------------------------------------------------- pipeline thật

def test_job_without_source_deadline_is_not_refetched_every_crawl(pg_conn):
    company = f"Công ty Không Hạn {uuid.uuid4().hex[:6]}"
    url = f"https://topcv/{uuid.uuid4().hex}"

    # Lượt 1: insert (fetch chi tiết 1 lần), deadline NULL vì nguồn không ghi.
    a1 = NoDeadlineAdapter(url, company)
    assert _run(pg_conn, a1)["inserted"] == 1
    assert a1.detail_calls == 1
    assert _one(pg_conn, "SELECT deadline FROM job_postings WHERE job_id = "
                         "(SELECT job_id FROM job_sources_log WHERE source_url = %s)", (url,)) is None

    # Lượt 2, 3: job thiếu deadline nhưng vừa mới fetch xong -> KHÔNG fetch lại
    # (trước đây: fetch lại ở mọi lượt, mãi mãi).
    for _ in range(2):
        a = NoDeadlineAdapter(url, company)
        s = _run(pg_conn, a)
        assert a.detail_calls == 0
        assert s["skipped_duplicate"] == 1

    # Quá hạn kiểm tra lại (8 ngày): fetch đúng 1 lần rồi lại được ghi dấu.
    _age(pg_conn, url, 8)
    a4 = NoDeadlineAdapter(url, company)
    _run(pg_conn, a4)
    assert a4.detail_calls == 1
    a5 = NoDeadlineAdapter(url, company)
    _run(pg_conn, a5)
    assert a5.detail_calls == 0


def test_legacy_job_with_null_stamp_is_still_patched_immediately(pg_conn):
    company = f"Công ty Cũ {uuid.uuid4().hex[:6]}"
    url = f"https://topcv/{uuid.uuid4().hex}"
    _run(pg_conn, NoDeadlineAdapter(url, company))
    _age(pg_conn, url, None)  # như dòng crawl từ trước khi có cột

    a = NoDeadlineAdapter(url, company)
    _run(pg_conn, a)

    assert a.detail_calls == 1
    assert _one(pg_conn, "SELECT detail_checked_at FROM job_sources_log WHERE source_url = %s", (url,)) is not None


def test_job_self_heals_when_source_starts_providing_the_field(pg_conn):
    company = f"Công ty Tự Chữa {uuid.uuid4().hex[:6]}"
    url = f"https://topcv/{uuid.uuid4().hex}"
    _run(pg_conn, NoDeadlineAdapter(url, company))  # selector hỏng/nguồn thiếu -> deadline NULL

    _age(pg_conn, url, 8)  # qua kỳ kiểm tra lại, lúc này selector đã được sửa
    fixed = NoDeadlineAdapter(url, company, deadline_text="05/09/2026")
    stats = _run(pg_conn, fixed)

    assert fixed.detail_calls == 1 and stats["updated_existing"] == 1
    job_deadline = _one(pg_conn, "SELECT deadline FROM job_postings WHERE job_id = "
                                 "(SELECT job_id FROM job_sources_log WHERE source_url = %s)", (url,))
    assert job_deadline == date(2026, 9, 5)

    # Đủ field rồi thì không bao giờ cần fetch lại nữa, dù dấu đã rất cũ.
    _age(pg_conn, url, 400)
    again = NoDeadlineAdapter(url, company)
    _run(pg_conn, again)
    assert again.detail_calls == 0
