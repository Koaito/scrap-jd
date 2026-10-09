"""
Cùng MÃ JOB nhưng URL đổi (VietnamWorks: nhà tuyển dụng sửa tiêu đề), chạy trên
POSTGRES THẬT (10/2026).

Cách chạy giống tests/test_pg_integration.py: đặt TEST_DATABASE_URL trỏ tới một
database dùng riêng cho test (tên phải chứa "test", fixture DROP SCHEMA public).
Không đặt biến này -> cả file được bỏ qua.

Chứng minh trên SQL thật: regex theo mã job không khớp nhầm job khác; câu UPDATE
chỉ ghi khi job còn OPEN và chưa ai sửa tay; pipeline thật cập nhật job cũ thay vì
tạo job trùng, không fetch lại URL đã ghi nhận, và không bao giờ rút ngắn hạn nộp.
"""
import os
import uuid
from datetime import date
from urllib.parse import urlparse

import psycopg2
import pytest

from scrapjd import db
from scrapjd import pipeline
from scrapjd.adapters.base import BaseAdapter
from scrapjd.adapters.vietnamworks import VietnamWorksAdapter
from scrapjd.models import RawJobRecord

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")
SOURCE = "VietnamWorks"


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


def _detail(jd="mô tả v1", deadline="05/09/2026", work_type="Toàn thời gian"):
    return {
        "work_type": work_type, "deadline_text": deadline,
        "job_description": jd, "requirements": "yêu cầu " + jd, "perks": "",
        "required_skills": ["SQL"],
    }


class VnwLikeAdapter(BaseAdapter):
    """Giống VietnamWorks: mã job ở cuối URL (dùng đúng hook của adapter thật)."""

    source_name = SOURCE
    job_code_url_regex = VietnamWorksAdapter.job_code_url_regex

    def __init__(self, company, code, slug, title, salary="15 - 25 triệu", experience="1 năm",
                 detail=None):
        super().__init__()
        self.url = f"https://www.vietnamworks.com/{slug}-{code}-jv"
        self.record = RawJobRecord(
            job_title=title, company_name=company, source_url=self.url, source_name=SOURCE,
            salary_text=salary, province_text="Hà Nội", experience_text=experience,
        )
        self.detail = detail or _detail()
        self.detail_calls = []

    def fetch_jobs(self, category_key, max_pages):
        yield self.record

    def fetch_job_full_detail(self, source_url):
        self.detail_calls.append(source_url)
        return dict(self.detail)


def _crawl(conn, adapter):
    return pipeline.run_pipeline(adapter, conn, "data-analyst", 1)


def _one(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _jobs_of(conn, company):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT jp.job_id, jp.job_title FROM job_postings jp JOIN companies c "
            "USING (company_id) WHERE c.company_name = %s ORDER BY jp.created_at", (company,))
        return cur.fetchall()


def _new_company():
    return f"Công ty Mã Job {uuid.uuid4().hex[:8]}"


def _code():
    return str(uuid.uuid4().int)[:8]


def _make_job(conn, url, title, source=SOURCE, company=None):
    """Tạo thẳng 1 job (không qua pipeline) với 1 nguồn."""
    with conn.cursor() as cur:
        cid = str(uuid.uuid4())
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)",
                    (cid, company or _new_company()))
    job_id = db.insert_job(
        conn, company_id=cid, job_title=title, matching_industry="Data",
        level_id=None, province_id=None, work_type=None, currency="VNĐ",
        salary_min=None, salary_max=None, salary_type="NEGOTIABLE",
        source_url=url, source_name=source, raw_jd_content="gốc",
    )
    conn.commit()
    return job_id


def _make_user(conn):
    user_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO app_users (ss_user_id, full_name, email) VALUES (%s, %s, %s)",
                    (user_id, "Người sửa tay", f"{user_id}@example.com"))
    conn.commit()
    return user_id


# ------------------------------------------------------------ hàm db: tra cứu

def test_find_by_regex_matches_exact_code_and_source_only(pg_conn):
    code = _code()
    mine = _make_job(pg_conn, f"https://www.vietnamworks.com/ten-cu-{code}-jv", "A")
    # Cùng mã nhưng nguồn khác, mã dài hơn chứa mã này, mã khác, URL không đúng đuôi:
    _make_job(pg_conn, f"https://topcv.vn/x-{code}-jv", "B", source="TopCV")
    _make_job(pg_conn, f"https://www.vietnamworks.com/x-9{code}-jv", "C")
    _make_job(pg_conn, f"https://www.vietnamworks.com/x-{code}1-jv", "D")
    _make_job(pg_conn, f"https://www.vietnamworks.com/x-{code}-jvx", "E")

    regex = VietnamWorksAdapter().job_code_url_regex(
        f"https://www.vietnamworks.com/ten-moi-{code}-jv?source=searchResults")
    rows = db.find_jobs_by_source_url_regex(pg_conn, source_name=SOURCE, url_regex=regex)
    pg_conn.rollback()

    assert [str(r[0]) for r in rows] == [mine]
    job_id, title, status, updated_by, created_at = rows[0]
    assert (title, status, updated_by) == ("A", "OPEN", None) and created_at is not None


def test_find_by_regex_also_finds_jobs_through_a_secondary_source_url(pg_conn):
    code = _code()
    job_id = _make_job(pg_conn, "https://www.vietnamworks.com/goc-1-jv", "Gốc")
    db.link_repost_source(pg_conn, job_id, source_name=SOURCE,
                          source_url=f"https://www.vietnamworks.com/phu-{code}-jv")
    pg_conn.commit()

    regex = VietnamWorksAdapter().job_code_url_regex(f"https://www.vietnamworks.com/moi-{code}-jv")
    rows = db.find_jobs_by_source_url_regex(pg_conn, source_name=SOURCE, url_regex=regex)
    pg_conn.rollback()

    assert [str(r[0]) for r in rows] == [job_id]


# ------------------------------------------------------------ hàm db: ghi

def _salary(lo, hi, kind="RANGE"):
    return {"currency": "VNĐ", "salary_min": lo, "salary_max": hi,
            "salary_type": kind, "salary_period": "MONTH"}


def test_update_job_from_recrawl_writes_only_what_it_is_given(pg_conn):
    job_id = _make_job(pg_conn, f"https://www.vietnamworks.com/a-{_code()}-jv", "Tên cũ")
    senior = db.get_level_id(pg_conn, "Senior")
    pg_conn.rollback()

    ok = db.update_job_from_recrawl(
        pg_conn, job_id, job_title="Tên mới", level_id=senior, work_type="FULL_TIME",
        parsed_content={"job_description": "mới"}, salary=_salary(30_000_000, 40_000_000),
    )
    pg_conn.commit()

    assert ok is True
    row = _one(pg_conn,
               "SELECT job_title, level_id, work_type, parsed_content->>'job_description', "
               "salary_min, salary_max, salary_type FROM job_postings WHERE job_id = %s", (job_id,))
    assert row == ("Tên mới", senior, "FULL_TIME", "mới", 30_000_000, 40_000_000, "RANGE")

    # Không truyền level/work_type/parsed_content/salary -> giữ nguyên, chỉ đổi tiêu đề.
    assert db.update_job_from_recrawl(pg_conn, job_id, job_title="Tên mới 2") is True
    pg_conn.commit()
    row = _one(pg_conn,
               "SELECT job_title, level_id, work_type, parsed_content->>'job_description', "
               "salary_min, salary_max FROM job_postings WHERE job_id = %s", (job_id,))
    assert row == ("Tên mới 2", senior, "FULL_TIME", "mới", 30_000_000, 40_000_000)


def test_update_job_from_recrawl_can_write_null_salary_bounds(pg_conn):
    """Lương dạng "Tới X" có salary_min = NULL thật: phải ghi được NULL, không bỏ qua."""
    job_id = _make_job(pg_conn, f"https://www.vietnamworks.com/a-{_code()}-jv", "T")
    db.update_job_from_recrawl(pg_conn, job_id, job_title="T", salary=_salary(1_000, 2_000))
    db.update_job_from_recrawl(pg_conn, job_id, job_title="T", salary=_salary(None, 3_000, "UPTO"))
    pg_conn.commit()

    assert _one(pg_conn, "SELECT salary_min, salary_max, salary_type FROM job_postings "
                         "WHERE job_id = %s", (job_id,)) == (None, 3_000, "UPTO")


def test_update_job_from_recrawl_refuses_hand_edited_and_closed_jobs(pg_conn):
    user_id = _make_user(pg_conn)
    edited = _make_job(pg_conn, f"https://www.vietnamworks.com/a-{_code()}-jv", "Sửa tay")
    closed = _make_job(pg_conn, f"https://www.vietnamworks.com/a-{_code()}-jv", "Đã đóng")
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE job_postings SET updated_by = %s WHERE job_id = %s", (user_id, edited))
        cur.execute("UPDATE job_postings SET job_status = 'CLOSED' WHERE job_id = %s", (closed,))
    pg_conn.commit()

    assert db.update_job_from_recrawl(pg_conn, edited, job_title="Ghi đè?") is False
    assert db.update_job_from_recrawl(pg_conn, closed, job_title="Ghi đè?") is False
    pg_conn.commit()

    assert _one(pg_conn, "SELECT job_title FROM job_postings WHERE job_id = %s", (edited,)) == ("Sửa tay",)
    assert _one(pg_conn, "SELECT job_title FROM job_postings WHERE job_id = %s", (closed,)) == ("Đã đóng",)


# ------------------------------------------------------------ pipeline thật

def test_title_edit_updates_the_job_through_the_real_pipeline(pg_conn):
    company, code = _new_company(), _code()

    # Lượt 1: job được insert theo tiêu đề cũ, level Junior (1 năm), hạn 05/09/2026.
    a1 = VnwLikeAdapter(company, code, "data-engineer", "Data Engineer")
    assert _crawl(pg_conn, a1)["inserted"] == 1
    (job_id, _), = _jobs_of(pg_conn, company)
    province_before = _one(pg_conn, "SELECT province_id, company_id FROM job_postings "
                                    "WHERE job_id = %s", (job_id,))

    # Lượt 2: nhà tuyển dụng sửa tiêu đề + lương + JD + hạn -> URL mới, cùng mã.
    a2 = VnwLikeAdapter(
        company, code, "data-engineer-senior-ha-noi", "Data Engineer (Senior)",
        salary="30 - 40 triệu", experience="5 năm",
        detail=_detail(jd="mô tả v2", deadline="10/10/2026", work_type="Bán thời gian"),
    )
    s2 = _crawl(pg_conn, a2)

    assert s2["inserted"] == 0 and s2["updated_by_job_code"] == 1
    assert "linked_by_job_code_only" not in s2 and "job_code_title_mismatch" not in s2
    assert [r[0] for r in _jobs_of(pg_conn, company)] == [job_id], "không được tạo job trùng"
    row = _one(pg_conn,
               "SELECT jp.job_title, l.level_code, jp.work_type, jp.salary_min, jp.salary_max, "
               "jp.parsed_content->>'job_description', jp.deadline, jp.updated_by, jp.source_url, "
               "jp.job_status FROM job_postings jp LEFT JOIN levels l USING (level_id) "
               "WHERE jp.job_id = %s", (job_id,))
    assert row == ("Data Engineer (Senior)", "Senior", "PART_TIME", 30_000_000, 40_000_000,
                   "mô tả v2", date(2026, 10, 10), None, a2.url, "OPEN")
    # Công ty và tỉnh không bị đụng; URL mới được ghi làm listing mới, và từ C2 source_url của job là URL đó
    # (listing OPEN mới nhất); URL gốc vẫn còn trong job_sources_log.
    assert _one(pg_conn, "SELECT province_id, company_id FROM job_postings WHERE job_id = %s",
                (job_id,)) == province_before
    assert _one(pg_conn, "SELECT count(*) FROM job_sources_log WHERE job_id = %s", (job_id,)) == (2,)
    assert _one(pg_conn, "SELECT raw_jd_content IS NOT NULL, detail_checked_at IS NOT NULL "
                         "FROM job_sources_log WHERE source_url = %s", (a2.url,)) == (True, True)

    # Lượt 3: crawl lại đúng URL mới -> đã biết và đủ field, KHÔNG fetch lại, không đổi gì.
    a3 = VnwLikeAdapter(company, code, "data-engineer-senior-ha-noi", "Data Engineer (Senior)",
                        detail=_detail(jd="không được ghi"))
    s3 = _crawl(pg_conn, a3)
    assert a3.detail_calls == [] and s3["skipped_duplicate"] == 1
    assert "updated_by_job_code" not in s3
    assert _one(pg_conn, "SELECT parsed_content->>'job_description' FROM job_postings "
                         "WHERE job_id = %s", (job_id,)) == ("mô tả v2",)

    # Lượt 4: sửa tiêu đề lần nữa nhưng hạn nộp sớm hơn hạn đang có -> không rút ngắn.
    a4 = VnwLikeAdapter(company, code, "ten-lan-ba", "Data Engineer (Senior) Remote",
                        salary="30 - 40 triệu", experience="5 năm",
                        detail=_detail(jd="mô tả v3", deadline="01/01/2026"))
    s4 = _crawl(pg_conn, a4)
    assert s4["updated_by_job_code"] == 1 and len(_jobs_of(pg_conn, company)) == 1
    assert _one(pg_conn, "SELECT job_title, deadline FROM job_postings WHERE job_id = %s",
                (job_id,)) == ("Data Engineer (Senior) Remote", date(2026, 10, 10))


def test_job_turned_into_another_position_creates_a_new_job(pg_conn):
    company, code = _new_company(), _code()
    _crawl(pg_conn, VnwLikeAdapter(company, code, "account-manager", "Account Manager"))

    s2 = _crawl(pg_conn, VnwLikeAdapter(company, code, "sales-assistant", "Sales Assistant"))

    assert s2["inserted"] == 1 and s2["job_code_title_mismatch"] == 1
    assert "updated_by_job_code" not in s2
    assert [t for _, t in _jobs_of(pg_conn, company)] == ["Account Manager", "Sales Assistant"], \
        "job cũ giữ nguyên, tin mới thành job mới"


def test_hand_edited_job_only_gets_the_new_url_linked(pg_conn):
    company, code = _new_company(), _code()
    _crawl(pg_conn, VnwLikeAdapter(company, code, "data-engineer", "Data Engineer"))
    (job_id, _), = _jobs_of(pg_conn, company)
    user_id = _make_user(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE job_postings SET updated_by = %s, ss_team_notes = 'giữ' "
                    "WHERE job_id = %s", (user_id, job_id))
    pg_conn.commit()

    a2 = VnwLikeAdapter(company, code, "data-engineer-senior", "Data Engineer (Senior)",
                        salary="30 - 40 triệu", detail=_detail(jd="mô tả v2", deadline="10/10/2026"))
    s2 = _crawl(pg_conn, a2)

    assert s2["linked_by_job_code_only"] == 1 and s2["inserted"] == 0
    assert "updated_by_job_code" not in s2
    assert _one(pg_conn, "SELECT job_title, salary_min, deadline, "
                         "parsed_content->>'job_description' FROM job_postings WHERE job_id = %s",
                (job_id,)) == ("Data Engineer", 15_000_000, date(2026, 10, 10), "mô tả v1")
    # Tiêu đề, lương, nội dung giữ nguyên (nhân viên đã sửa), riêng HẠN theo listing mới (C4 phần 2/3, bạn chọn
    # phương án a 09/10): hạn job là hạn muộn nhất trong các listing OPEN, trước đây job sửa tay không bị kéo hạn.
    assert _one(pg_conn, "SELECT count(*) FROM job_sources_log WHERE job_id = %s", (job_id,)) == (2,)

    # Đã ghi nhận URL mới: lượt sau không fetch lại.
    a3 = VnwLikeAdapter(company, code, "data-engineer-senior", "Data Engineer (Senior)")
    _crawl(pg_conn, a3)
    assert a3.detail_calls == []


def test_closed_job_is_not_revived_by_a_new_url_with_the_same_code(pg_conn):
    company, code = _new_company(), _code()
    _crawl(pg_conn, VnwLikeAdapter(company, code, "data-engineer", "Data Engineer"))
    (job_id, _), = _jobs_of(pg_conn, company)
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE job_postings SET job_status = 'CLOSED' WHERE job_id = %s", (job_id,))
    pg_conn.commit()

    s2 = _crawl(pg_conn, VnwLikeAdapter(company, code, "data-engineer-senior", "Data Engineer (Senior)"))

    assert s2["inserted"] == 1 and "updated_by_job_code" not in s2
    assert "job_code_title_mismatch" not in s2
    assert _one(pg_conn, "SELECT job_title, job_status FROM job_postings WHERE job_id = %s",
                (job_id,)) == ("Data Engineer", "CLOSED")


def test_with_several_rows_of_the_same_code_the_most_similar_open_one_is_updated(pg_conn):
    """Dữ liệu cũ có sẵn các cặp trùng mã (37 cặp lúc đo): chọn dòng giống tiêu đề nhất."""
    company, code = _new_company(), _code()
    less = _make_job(pg_conn, f"https://www.vietnamworks.com/a-{code}-jv",
                     "Data Analyst Engineer", company=company)
    most = _make_job(pg_conn, f"https://www.vietnamworks.com/b-{code}-jv",
                     "Data Engineer", company=company)

    s = _crawl(pg_conn, VnwLikeAdapter(company, code, "moi", "Data Engineer Python Developer"))

    assert s["updated_by_job_code"] == 1 and s["inserted"] == 0
    titles = dict(_jobs_of(pg_conn, company))
    assert titles[most] == "Data Engineer Python Developer"
    assert titles[less] == "Data Analyst Engineer"
