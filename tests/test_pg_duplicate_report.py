"""
Báo cáo job nghi trùng (Phần 3a) trên POSTGRES THẬT: truy vấn db.list_duplicate_job_rows và
lệnh duplicate_report.run. Cách chạy giống tests/test_pg_recompute_levels.py: đặt
TEST_DATABASE_URL trỏ tới database dùng riêng cho test (tên chứa "test"); không đặt thì cả
file được bỏ qua.

Chứng minh trên SQL thật:
  - chỉ trả job nằm trong nhóm >= 2 job cùng công ty + cùng tiêu đề chuẩn hoá (khác hoa/thường,
    khoảng trắng thừa vẫn cùng nhóm; công ty khác hoặc tiêu đề khác thì không);
  - gom được cả cặp khác level / khác tỉnh (khác content_hash) mà view v_duplicate_job_candidates
    không thấy;
  - đếm đúng dữ liệu con (đơn ứng tuyển, lượt lưu), cờ người sửa/ghi chú, URL nguồn trong log;
  - số nhóm cùng hash tính ở Python khớp số dòng của view;
  - CHỈ ĐỌC: chạy xong không đổi dữ liệu và không đổi updated_at.
"""
import os
import uuid
from urllib.parse import urlparse

import psycopg2
import pytest

import db
import duplicate_report as dr

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
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
    db.baseline_migrations(conn)  # như `init-db`: schema.sql đã chứa kết quả mọi migration
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture(autouse=True)
def clean_jobs(pg_conn):
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE job_postings CASCADE")
    pg_conn.commit()
    yield
    pg_conn.rollback()


# ------------------------------------------------------------------ helpers
def _one(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    conn.rollback()
    return row


def _company(conn, name=None):
    cid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)",
                    (cid, name or f"Công ty Trùng {uuid.uuid4().hex[:8]}"))
    conn.commit()
    return cid


def _user(conn):
    uid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO app_users (ss_user_id, full_name, email) VALUES (%s, %s, %s)",
                    (uid, "Người dùng", f"{uid}@example.com"))
    conn.commit()
    return uid


def _province_ids(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT province_id FROM provinces ORDER BY province_id LIMIT 2")
        ids = [r[0] for r in cur.fetchall()]
    conn.rollback()
    assert len(ids) == 2, "schema.sql phải seed ít nhất 2 tỉnh"
    return ids


def _job(conn, company_id, title, *, level="Junior", province_id=None, url=None, updated_by=None,
         notes=None):
    job_id = db.insert_job(
        conn, company_id=company_id, job_title=title, matching_industry="Data",
        level_id=db.get_level_id(conn, level) if level else None, province_id=province_id,
        work_type=None, currency="VNĐ", salary_min=None, salary_max=None, salary_type="NEGOTIABLE",
        source_url=url or f"https://www.example.com/{uuid.uuid4()}", source_name="Fake",
    )
    with conn.cursor() as cur:
        # Đặt updated_at về quá khứ để test "không đổi updated_at" có ý nghĩa.
        cur.execute("SELECT set_config('app.skip_updated_at', 'on', true)")
        cur.execute("UPDATE job_postings SET updated_at = %s, updated_by = %s, ss_team_notes = %s "
                    "WHERE job_id = %s", (PAST, updated_by, notes, job_id))
    conn.commit()
    return job_id


def _by_id(rows):
    return {r["job_id"]: r for r in rows}


# ------------------------------------------------------------------ truy vấn
def test_only_jobs_in_groups_of_two_or_more_are_returned(pg_conn):
    c1, c2 = _company(pg_conn), _company(pg_conn)
    a1 = _job(pg_conn, c1, "Data Analyst")
    a2 = _job(pg_conn, c1, "Data Analyst")
    _job(pg_conn, c1, "Data Engineer")        # cùng công ty, tiêu đề khác
    _job(pg_conn, c2, "Data Analyst")         # công ty khác
    rows = db.list_duplicate_job_rows(pg_conn)
    assert {r["job_id"] for r in rows} == {a1, a2}


def test_title_normalization_matches_hash_formula(pg_conn):
    # Hoa/thường (ký tự ASCII), khoảng trắng thừa không làm tách nhóm (cùng công thức generate_job_hash).
    # Chỉ đổi hoa/thường ở chữ ASCII để test không phụ thuộc locale của database test.
    c = _company(pg_conn)
    j1 = _job(pg_conn, c, "Kỹ Sư  Dữ Liệu")
    j2 = _job(pg_conn, c, "  kỹ sư dữ liệu ")
    rows = _by_id(db.list_duplicate_job_rows(pg_conn))
    assert set(rows) == {j1, j2}
    assert rows[j1]["norm_title"] == rows[j2]["norm_title"]
    # Hai job này cũng cùng content_hash (cùng khoá với trigger), nên chính là một nhóm của view.
    assert rows[j1]["content_hash"] == rows[j2]["content_hash"]


def test_groups_split_by_level_or_province_are_found_but_not_in_the_view(pg_conn):
    p1, p2 = _province_ids(pg_conn)
    c = _company(pg_conn)
    l1 = _job(pg_conn, c, "Business Analyst", level="Junior", province_id=p1)
    l2 = _job(pg_conn, c, "Business Analyst", level="Senior", province_id=p1)   # khác level
    c2 = _company(pg_conn)
    v1 = _job(pg_conn, c2, "Kế toán", level="Junior", province_id=p1)
    v2 = _job(pg_conn, c2, "Kế toán", level="Junior", province_id=p2)           # khác tỉnh
    rows = db.list_duplicate_job_rows(pg_conn)
    assert {r["job_id"] for r in rows} == {l1, l2, v1, v2}
    assert db.count_duplicate_job_groups(pg_conn) == 0  # view cũ không thấy (hash khác nhau)

    by_title = {g.title: g for g in dr.build_groups(rows)}
    assert by_title["Business Analyst"].tier == dr.TIER_LEVEL
    assert by_title["Kế toán"].tier == dr.TIER_PROVINCE and by_title["Kế toán"].keeper_id is None


def test_child_counts_flags_and_log_urls(pg_conn):
    c = _company(pg_conn)
    user_a, user_b, editor = _user(pg_conn), _user(pg_conn), _user(pg_conn)
    keep = _job(pg_conn, c, "Product Owner", url="https://www.topcv.vn/po-1", updated_by=editor,
                notes="khách hàng quan tâm")
    other = _job(pg_conn, c, "Product Owner", url="https://careerviet.vn/po-2")
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO job_applications (ss_user_id, job_id) VALUES (%s, %s), (%s, %s)",
                    (user_a, keep, user_b, keep))
        cur.execute("INSERT INTO saved_jobs (ss_user_id, job_id) VALUES (%s, %s)", (user_a, keep))
    pg_conn.commit()

    rows = _by_id(db.list_duplicate_job_rows(pg_conn))
    k, o = rows[keep], rows[other]
    assert (k["n_applications"], k["n_saved"], k["n_contact_links"]) == (2, 1, 0)
    assert k["has_editor"] is True and k["has_notes"] is True
    assert (o["n_applications"], o["n_saved"], o["has_editor"], o["has_notes"]) == (0, 0, False, False)
    # insert_job ghi một dòng job_sources_log cho mỗi job.
    assert k["log_urls"] == ["https://www.topcv.vn/po-1"]
    assert o["log_urls"] == ["https://careerviet.vn/po-2"]

    group = dr.build_groups(list(rows.values()))[0]
    assert group.keeper_id == keep and "bảo vệ" in group.keeper_why
    assert group.cross_source is True and group.protected_count == 1


def test_blank_notes_are_not_counted_as_notes(pg_conn):
    c = _company(pg_conn)
    a = _job(pg_conn, c, "Tester", notes="   ")
    _job(pg_conn, c, "Tester")
    assert _by_id(db.list_duplicate_job_rows(pg_conn))[a]["has_notes"] is False


def test_statuses_deadlines_and_company_fields_are_returned(pg_conn):
    c = _company(pg_conn, "Công ty Ánh Dương")
    j1 = _job(pg_conn, c, "Data Engineer")
    j2 = _job(pg_conn, c, "Data Engineer")
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE job_postings SET job_status = 'CLOSED', deadline = '2026-09-01' WHERE job_id = %s", (j1,))
    pg_conn.commit()
    rows = _by_id(db.list_duplicate_job_rows(pg_conn))
    assert rows[j1]["job_status"] == "CLOSED" and str(rows[j1]["deadline"]) == "2026-09-01"
    assert rows[j2]["job_status"] == "OPEN"
    assert rows[j1]["company_name"] == "Công ty Ánh Dương" and rows[j1]["company_active"] is True
    assert rows[j1]["company_id"] == c  # trả dạng str, không phải UUID


# ------------------------------------------------------------------ đối chiếu view + chỉ đọc
def test_hash_group_count_matches_view_row_count(pg_conn):
    c = _company(pg_conn)
    for _ in range(3):
        _job(pg_conn, c, "Sales Executive")           # nhóm 3 job cùng hash
    c2 = _company(pg_conn)
    _job(pg_conn, c2, "Marketing")
    _job(pg_conn, c2, "Marketing")                    # nhóm 2 job cùng hash
    _job(pg_conn, c2, "Marketing", level="Senior")    # cùng nhóm lỏng, khác hash
    rows = db.list_duplicate_job_rows(pg_conn)
    assert dr.count_hash_groups(rows) == db.count_duplicate_job_groups(pg_conn) == 2


def test_run_is_read_only_and_reports(pg_conn, capsys, tmp_path):
    c = _company(pg_conn)
    j1 = _job(pg_conn, c, "Data Analyst")
    _job(pg_conn, c, "Data Analyst")
    before = _one(pg_conn, "SELECT count(*), max(updated_at)::text, min(updated_at)::text FROM job_postings")
    hash_before = _one(pg_conn, "SELECT content_hash FROM job_postings WHERE job_id = %s", (j1,))[0]

    path = tmp_path / "trung.csv"
    assert dr.run(pg_conn, show=3, csv_path=str(path)) == 0
    out = capsys.readouterr().out
    assert "BÁO CÁO JOB NGHI TRÙNG" in out and "khớp" in out and "Tổng job trong DB: 2" in out
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")

    after = _one(pg_conn, "SELECT count(*), max(updated_at)::text, min(updated_at)::text FROM job_postings")
    assert after == before and before[1] == before[2] == PAST  # không đổi updated_at của job nào
    assert _one(pg_conn, "SELECT content_hash FROM job_postings WHERE job_id = %s", (j1,))[0] == hash_before


def test_run_on_empty_db(pg_conn, capsys):
    assert dr.run(pg_conn) == 0
    out = capsys.readouterr().out
    assert "Tổng job trong DB: 0" in out and "Nhóm nghi trùng (cùng công ty + tiêu đề chuẩn hoá): 0" in out
