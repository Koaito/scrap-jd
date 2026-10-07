"""
A3 nửa 2/2 trên POSTGRES THẬT: các hàm tra trùng dùng chung khoá dedup_key (công ty + tiêu đề chuẩn
hoá + tỉnh) và view v_duplicate_job_candidates sau migration 0040.

Chứng minh:
  - find_manual_job_duplicate (POST /jobs): cùng khoá VÀ cùng level thì trả job cũ; khác level thì
    không; job CLOSED không tính; tiêu đề lệch hoa/thường và khoảng trắng bên trong vẫn khớp (trước A3
    chỉ trim hai đầu);
  - find_similar_open_jobs: liệt kê job đang mở cùng khoá ở level khác, bỏ job được loại trừ, job
    đóng, job khác tỉnh hay khác công ty;
  - create_manual_job: cùng level trả lại job cũ không tạo thêm; khác level tạo job mới;
  - find_repost_candidate (crawler) vẫn khớp khi tiêu đề có hai dấu cách bên trong và khi tỉnh NULL;
  - view v_duplicate_job_candidates gom theo dedup_key: cặp chỉ khác level là một nhóm.

Cách chạy giống tests/test_pg_repost_lookup.py: đặt TEST_DATABASE_URL trỏ tới database dùng riêng cho
test (tên chứa "test"); không đặt thì cả file được bỏ qua.
"""
import os
import uuid
from urllib.parse import urlparse

import psycopg2
import pytest

import db

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")


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
                    (cid, f"Công ty Nhập Tay {uuid.uuid4().hex[:8]}"))
    conn.commit()
    return cid


def _province(conn, name):
    pid = db.get_or_create_province(conn, name)
    conn.commit()
    return pid


def _level(conn, code):
    return db.get_level_id(conn, code) if code else None


def _job(conn, company_id, title, *, level="Junior", province_id=None, status="OPEN",
         created="2026-09-01 10:00:00"):
    job_id = db.insert_job(
        conn, company_id=company_id, job_title=title, matching_industry="Data",
        level_id=_level(conn, level), province_id=province_id, work_type=None,
        currency="VNĐ", salary_min=None, salary_max=None, salary_type="NEGOTIABLE",
        source_url=f"https://www.example.com/{uuid.uuid4()}", source_name="Fake",
    )
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.skip_updated_at', 'on', true)")
        cur.execute("UPDATE job_postings SET job_status = %s, created_at = %s WHERE job_id = %s",
                    (status, created, job_id))
    conn.commit()
    return job_id


def _dup(conn, company, title, *, level="Junior", province_id=None):
    res = db.find_manual_job_duplicate(conn, company_id=company, job_title=title,
                                       level_id=_level(conn, level), province_id=province_id)
    conn.rollback()
    return res


def _count(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM job_postings")
        n = cur.fetchone()[0]
    conn.rollback()
    return n


# ------------------------------------------------------------------ find_manual_job_duplicate
def test_same_key_and_same_level_returns_the_oldest_open_job(pg_conn):
    c, hn = _company(pg_conn), _province(pg_conn, "Hà Nội")
    older = _job(pg_conn, c, "Data Analyst", province_id=hn, created="2026-09-01 10:00:00")
    _job(pg_conn, c, "Data Analyst", province_id=hn, created="2026-09-02 10:00:00")
    assert _dup(pg_conn, c, "Data Analyst", province_id=hn) == older


def test_different_level_is_not_a_manual_duplicate(pg_conn):
    c, hn = _company(pg_conn), _province(pg_conn, "Hà Nội")
    _job(pg_conn, c, "Data Analyst", level="Junior", province_id=hn)
    assert _dup(pg_conn, c, "Data Analyst", level="Senior", province_id=hn) is None
    assert _dup(pg_conn, c, "Data Analyst", level=None, province_id=hn) is None


def test_missing_level_matches_missing_level(pg_conn):
    c = _company(pg_conn)
    j = _job(pg_conn, c, "Tester", level=None)
    assert _dup(pg_conn, c, "Tester", level=None) == j


def test_title_case_and_inner_whitespace_do_not_hide_a_duplicate(pg_conn):
    c = _company(pg_conn)
    j = _job(pg_conn, c, "Data   Analyst")
    assert _dup(pg_conn, c, "  data analyst ") == j


def test_closed_other_province_and_other_company_do_not_count(pg_conn):
    c, c2 = _company(pg_conn), _company(pg_conn)
    hn, hcm = _province(pg_conn, "Hà Nội"), _province(pg_conn, "Hồ Chí Minh")
    _job(pg_conn, c, "Data Analyst", province_id=hn, status="CLOSED")
    _job(pg_conn, c, "Data Analyst", province_id=hcm)
    _job(pg_conn, c2, "Data Analyst", province_id=hn)
    assert _dup(pg_conn, c, "Data Analyst", province_id=hn) is None


# ------------------------------------------------------------------ find_similar_open_jobs
def test_similar_open_jobs_lists_other_levels_oldest_first_and_honours_exclusion(pg_conn):
    c, hn = _company(pg_conn), _province(pg_conn, "Hà Nội")
    senior = _job(pg_conn, c, "Data Analyst", level="Senior", province_id=hn,
                  created="2026-09-01 10:00:00")
    junior = _job(pg_conn, c, "data  analyst", level="Junior", province_id=hn,
                  created="2026-09-02 10:00:00")
    _job(pg_conn, c, "Data Analyst", level="Middle", province_id=hn, status="CLOSED")
    _job(pg_conn, c, "Data Analyst", level="Middle", province_id=_province(pg_conn, "Đà Nẵng"))
    _job(pg_conn, _company(pg_conn), "Data Analyst", level="Middle", province_id=hn)

    found = db.find_similar_open_jobs(pg_conn, company_id=c, job_title="Data Analyst",
                                      province_id=hn)
    pg_conn.rollback()
    assert [(j["job_id"], j["level_code"], j["job_status"]) for j in found] == [
        (senior, "Senior", "OPEN"), (junior, "Junior", "OPEN")]

    found = db.find_similar_open_jobs(pg_conn, company_id=c, job_title="Data Analyst",
                                      province_id=hn, exclude_job_id=senior)
    pg_conn.rollback()
    assert [j["job_id"] for j in found] == [junior]


def test_similar_open_jobs_is_empty_when_nothing_matches(pg_conn):
    c = _company(pg_conn)
    assert db.find_similar_open_jobs(pg_conn, company_id=c, job_title="Không có",
                                     province_id=None) == []
    pg_conn.rollback()


# ------------------------------------------------------------------ create_manual_job
def _create(conn, company, title, *, level, province_id):
    job_id = db.create_manual_job(conn, job_title=title, company_id=company,
                                  level_id=_level(conn, level), province_id=province_id)
    conn.commit()
    return job_id


def test_create_manual_job_returns_existing_for_same_level_and_creates_for_another(pg_conn):
    c, hn = _company(pg_conn), _province(pg_conn, "Hà Nội")
    first = _create(pg_conn, c, "Product Owner", level="Junior", province_id=hn)
    again = _create(pg_conn, c, "  product   owner ", level="Junior", province_id=hn)
    assert again == first and _count(pg_conn) == 1

    other_level = _create(pg_conn, c, "Product Owner", level="Senior", province_id=hn)
    assert other_level != first and _count(pg_conn) == 2

    similar = db.find_similar_open_jobs(pg_conn, company_id=c, job_title="Product Owner",
                                        province_id=hn, exclude_job_id=other_level)
    pg_conn.rollback()
    assert [j["job_id"] for j in similar] == [first]


# ------------------------------------------------------------------ crawler vẫn khớp như trước
def test_repost_candidate_still_matches_inner_whitespace_and_null_province(pg_conn):
    c, hn = _company(pg_conn), _province(pg_conn, "Hà Nội")
    no_province = _job(pg_conn, c, "Backend  Developer", province_id=None)
    in_hanoi = _job(pg_conn, c, "Backend Developer", province_id=hn, level="Senior")

    def find(province):
        res = db.find_repost_candidate(pg_conn, company_id=c, job_title=" backend developer ",
                                       province_id=province, level_id=_level(pg_conn, "Junior"))
        pg_conn.rollback()
        return res and res["job_id"]

    assert find(None) == no_province          # level khác nhau không cản (khoá không gồm level)
    assert find(hn) == in_hanoi
    assert find(_province(pg_conn, "Đà Nẵng")) is None


# ------------------------------------------------------------------ view
def test_view_groups_by_dedup_key_so_a_level_only_difference_is_one_group(pg_conn):
    c, hn = _company(pg_conn), _province(pg_conn, "Hà Nội")
    a = _job(pg_conn, c, "Business Analyst", level="Junior", province_id=hn)
    b = _job(pg_conn, c, "Business Analyst", level="Senior", province_id=hn)
    _job(pg_conn, c, "Business Analyst", level="Junior", province_id=_province(pg_conn, "Đà Nẵng"))
    with pg_conn.cursor() as cur:
        cur.execute("SELECT dedup_key, job_ids, num_duplicates FROM v_duplicate_job_candidates")
        rows = cur.fetchall()
    pg_conn.rollback()
    assert len(rows) == 1 and rows[0][2] == 2
    assert {str(j) for j in rows[0][1]} == {a, b}
    assert db.count_duplicate_job_groups(pg_conn) == 1
