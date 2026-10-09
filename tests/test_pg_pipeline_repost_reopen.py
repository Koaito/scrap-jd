"""
Phần 3c nửa 2/2 trên POSTGRES THẬT: chạy pipeline.run_pipeline() thật với adapter giả để chứng
minh tin đăng lại KHÔNG còn sinh job trùng. Cách chạy giống tests/test_pg_repost_link.py: đặt
TEST_DATABASE_URL trỏ tới database dùng riêng cho test (tên chứa "test"); không đặt thì bỏ qua.

Các tình huống (đều đúng với dữ liệu job trùng thật đã đo):
  - job cũ đã CLOSED, tin đăng lại URL mới: job được MỞ LẠI (OPEN, hạn mới, source_url mới),
    không có job mới; lượt crawl sau gặp lại URL mới thì nhận ra, không fetch lại, không trùng;
  - level suy ra khác với level job cũ: vẫn nhận ra là đăng lại (level không còn nằm trong khoá);
  - tiêu đề khác nhau ở khoảng trắng bên trong và hoa/thường: vẫn nhận ra;
  - job do nhân viên chủ động đóng (audit DELETE_JOB): giữ CLOSED, chỉ ghi URL mới làm nguồn phụ;
  - hạn của tin mới đã qua: không mở lại;
  - job cũ OPEN: dời hạn như trước; tỉnh khác: vẫn là job mới (chi nhánh khác).
"""
import os
import uuid
from datetime import date
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
import pytest

from scrapjd import db
from scrapjd import pipeline
from scrapjd.adapters.base import BaseAdapter
from scrapjd.models import RawJobRecord

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")
FUTURE = "31/12/2099"


def _detail(deadline_text):
    return {
        "work_type": "Toàn thời gian", "deadline_text": deadline_text,
        "job_description": "mô tả công việc", "requirements": "yêu cầu", "perks": "",
        "required_skills": ["SQL"],
    }


class FakeAdapter(BaseAdapter):
    source_name = "Fake"

    def __init__(self, urls, *, company, title="Data Analyst", province="Hà Nội",
                 experience="2 năm", deadline=FUTURE):
        super().__init__()
        self.urls, self.company, self.title = urls, company, title
        self.province, self.experience, self.deadline = province, experience, deadline
        self.detail_calls = []

    def fetch_jobs(self, category_key, max_pages):
        for url in self.urls:
            yield RawJobRecord(
                job_title=self.title, company_name=self.company, source_url=url,
                source_name="Fake", salary_text="", province_text=self.province,
                experience_text=self.experience,
            )

    def fetch_job_full_detail(self, source_url):
        self.detail_calls.append(source_url)
        return _detail(self.deadline)


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
        cur.execute("TRUNCATE job_postings, audit_logs CASCADE")   # URL cố định giữa các test không được sót lại
    pg_conn.commit()
    yield
    pg_conn.rollback()


@pytest.fixture
def company():
    return f"Công ty Reopen {uuid.uuid4().hex[:8]}"


def _crawl(conn, adapter):
    stats = pipeline.run_pipeline(adapter, conn, "data-analyst", 1)
    conn.rollback()
    return stats


def _scalar(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        value = cur.fetchone()[0]
    conn.rollback()
    return value


def _jobs_of(conn, company):
    return _scalar(conn, "SELECT count(*) FROM job_postings j JOIN companies c USING (company_id) "
                         "WHERE c.company_name = %s", (company,))


def _only_job(conn, company):
    with conn.cursor() as cur:
        cur.execute("SELECT j.job_id::text, j.job_status::text, j.deadline, j.source_url, j.level_id "
                    "FROM job_postings j JOIN companies c USING (company_id) WHERE c.company_name = %s", (company,))
        rows = cur.fetchall()
    conn.rollback()
    assert len(rows) == 1, rows
    return rows[0]


def _seed_closed_job(conn, company, url="https://x/old", reason="expired_auto", **adapter_kw):
    """Crawl thật một job rồi đóng nó như check_expired_source_jobs (closed_reason, không audit)."""
    stats = _crawl(conn, FakeAdapter([url], company=company, **adapter_kw))
    assert stats["inserted"] == 1
    job_id = _only_job(conn, company)[0]
    # Đóng bằng db.update_job (như nhân viên / check_expired_source_jobs): từ C1, đóng job đóng luôn listing.
    # Đóng bằng SQL thô sẽ để listing OPEN, và từ C2 job suy ra OPEN từ listing đó nên bị mở lại ngầm.
    assert db.update_job(conn, job_id, job_status="CLOSED", closed_reason=reason)
    with conn.cursor() as cur:
        cur.execute("UPDATE job_postings SET deadline = '2020-01-01' WHERE job_id = %s", (job_id,))
        cur.execute("UPDATE job_sources_log SET deadline = '2020-01-01' WHERE job_id = %s", (job_id,))
    conn.commit()
    return job_id


def test_closed_job_is_reopened_by_a_repost_and_no_duplicate_is_created(pg_conn, company):
    job_id = _seed_closed_job(pg_conn, company)
    adapter = FakeAdapter(["https://x/new"], company=company)
    stats = _crawl(pg_conn, adapter)

    assert stats["inserted"] == 0 and stats["skipped_duplicate_repost"] == 1 and stats["repost_reopened"] == 1
    assert "repost_kept_closed" not in stats
    assert _jobs_of(pg_conn, company) == 1
    jid, status, deadline, source_url, _ = _only_job(pg_conn, company)
    assert (jid, status, deadline, source_url) == (job_id, "OPEN", date(2099, 12, 31), "https://x/new")
    # URL cũ vẫn tra ra job này (job_sources_log), URL mới cũng vậy
    for url in ("https://x/old", "https://x/new"):
        assert str(db.get_job_probe_by_source_url(pg_conn, url)[0]) == job_id
    pg_conn.rollback()

    # Lượt crawl sau gặp lại đúng URL mới: nhận ra qua job_sources_log, không sinh trùng
    again = FakeAdapter(["https://x/new"], company=company)
    stats2 = _crawl(pg_conn, again)
    assert stats2["inserted"] == 0 and stats2["skipped_duplicate_repost"] == 0
    assert _jobs_of(pg_conn, company) == 1


def test_repost_with_different_level_and_spacing_is_still_recognised(pg_conn, company):
    job_id = _seed_closed_job(pg_conn, company, title="Data Analyst", experience="2 năm")
    senior = db.get_level_id(pg_conn, "Senior")
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE job_postings SET level_id = %s WHERE job_id = %s", (senior, job_id))
    pg_conn.commit()

    # Lần đăng lại: tiêu đề hoa/thường khác + hai dấu cách, kinh nghiệm khác => level suy ra khác Senior
    stats = _crawl(pg_conn, FakeAdapter(["https://x/new"], company=company, title="  DATA   analyst ",
                                        experience="1 năm"))
    assert stats["inserted"] == 0 and stats["repost_reopened"] == 1
    jid, status, _, _, level_id = _only_job(pg_conn, company)
    assert jid == job_id and status == "OPEN"
    assert level_id == senior                      # job giữ level cũ, không bị tin đăng lại đổi


@pytest.mark.parametrize("reason", ["staff", "unknown"])
def test_job_not_closed_by_expiry_stays_closed_and_only_gets_the_new_url(pg_conn, company, reason):
    job_id = _seed_closed_job(pg_conn, company, reason=reason)

    stats = _crawl(pg_conn, FakeAdapter(["https://x/new"], company=company))
    assert stats["inserted"] == 0 and stats["skipped_duplicate_repost"] == 1
    assert stats["repost_kept_closed"] == 1 and "repost_reopened" not in stats
    jid, status, deadline, source_url, _ = _only_job(pg_conn, company)
    # Job vẫn CLOSED, không audit mở lại. Từ C2 job là giá trị tổng hợp từ listing: không còn listing OPEN nên
    # source_url là listing thấy gần nhất (URL mới) và hạn là hạn muộn nhất trong các listing (tin mới: 2099).
    assert (jid, status, deadline, source_url) == (job_id, "CLOSED", date(2099, 12, 31), "https://x/new")
    assert str(db.get_job_probe_by_source_url(pg_conn, "https://x/new")[0]) == job_id   # URL mới đã được ghi nhận
    assert _scalar(pg_conn, "SELECT count(*) FROM audit_logs WHERE action_type = 'REOPEN_JOB'") == 0
    pg_conn.rollback()

    # Nhân viên mở lại bằng tay rồi job hết hạn tự đóng: lần này tự mở lại được.
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE job_postings SET job_status = 'OPEN' WHERE job_id = %s", (job_id,))
        cur.execute("UPDATE job_postings SET job_status = 'CLOSED', closed_reason = 'expired_auto' "
                    "WHERE job_id = %s", (job_id,))
    pg_conn.commit()
    stats = _crawl(pg_conn, FakeAdapter(["https://x/newer"], company=company))
    assert stats["repost_reopened"] == 1 and _only_job(pg_conn, company)[1] == "OPEN"


def test_reopen_clears_closed_state_and_writes_reopen_job_audit(pg_conn, company):
    job_id = _seed_closed_job(pg_conn, company)
    _crawl(pg_conn, FakeAdapter(["https://x/new"], company=company))
    with pg_conn.cursor() as cur:
        cur.execute("SELECT closed_reason, closed_at FROM job_postings WHERE job_id = %s", (job_id,))
        assert cur.fetchone() == (None, None)
        cur.execute("SELECT actor_id, entity_type, entity_label, is_manual_log, changes "
                    "FROM audit_logs WHERE action_type = 'REOPEN_JOB' AND entity_id = %s", (job_id,))
        rows = cur.fetchall()
    pg_conn.rollback()
    assert len(rows) == 1
    actor, entity_type, label, is_manual, changes = rows[0]
    assert (actor, entity_type, is_manual) == (None, "JOB", False) and label
    assert changes["job_status"] == {"old": "CLOSED", "new": "OPEN"}
    assert changes["closed_reason"] == {"old": "expired_auto", "new": None}
    assert changes["source_url"] == {"old": "https://x/old", "new": "https://x/new"}
    assert changes["deadline"]["old"] == "2020-01-01" and changes["deadline"]["new"] == "2099-12-31"


def test_repost_whose_deadline_already_passed_does_not_reopen(pg_conn, company):
    job_id = _seed_closed_job(pg_conn, company)
    stats = _crawl(pg_conn, FakeAdapter(["https://x/new"], company=company, deadline="01/01/2020"))
    assert stats["inserted"] == 0 and stats["skipped_duplicate_repost"] == 1
    assert stats["repost_kept_closed"] == 1 and "repost_reopened" not in stats
    jid, status, _, source_url, _ = _only_job(pg_conn, company)
    # Không mở lại được (hạn tin mới đã qua), job vẫn CLOSED; source_url suy ra từ listing thấy gần nhất (C2).
    assert (jid, status, source_url) == (job_id, "CLOSED", "https://x/new")


def test_open_job_repost_extends_deadline_as_before(pg_conn, company):
    job_id = _crawl_one_open(pg_conn, company, deadline="01/12/2099")
    stats = _crawl(pg_conn, FakeAdapter(["https://x/new"], company=company, deadline="31/12/2099"))
    assert stats["inserted"] == 0 and stats["skipped_duplicate_repost"] == 1
    assert stats["repost_deadline_extended"] == 1 and "repost_reopened" not in stats
    jid, status, deadline, source_url, _ = _only_job(pg_conn, company)
    # C2: job OPEN nhận tin đăng lại có source_url = URL listing OPEN mới nhất.
    assert (jid, status, deadline, source_url) == (job_id, "OPEN", date(2099, 12, 31), "https://x/new")


def _crawl_one_open(conn, company, deadline):
    stats = _crawl(conn, FakeAdapter(["https://x/old"], company=company, deadline=deadline))
    assert stats["inserted"] == 1
    return _only_job(conn, company)[0]


def test_same_title_in_another_province_is_still_a_new_job(pg_conn, company):
    _seed_closed_job(pg_conn, company, province="Hà Nội")
    stats = _crawl(pg_conn, FakeAdapter(["https://x/hcm"], company=company, province="Hồ Chí Minh"))
    assert stats["inserted"] == 1 and stats["skipped_duplicate_repost"] == 0
    assert _jobs_of(pg_conn, company) == 2
