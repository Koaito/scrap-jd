"""
Gộp job trùng (Phần 3b nửa 1/2) trên POSTGRES THẬT: truy vấn db.list_merge_job_details và lệnh
merge_duplicates.run (chạy thử). Cách chạy giống tests/test_pg_duplicate_report.py: đặt
TEST_DATABASE_URL trỏ tới database dùng riêng cho test (tên chứa "test"); không đặt thì cả file
được bỏ qua.

Chứng minh trên SQL thật:
  - list_merge_job_details trả đủ khối cần hợp nhất (lương, hạn, trạng thái, level + dấu, ghi
    chú) và dữ liệu con (log URL, lượt lưu, đơn ứng tuyển kèm cờ CV, liên kết liên hệ kèm số lượt
    trao đổi), id dạng str, job không tồn tại thì vắng mặt;
  - luồng đọc -> chọn nhóm -> lập kế hoạch chạy được trên dữ liệu thật, kể cả nhóm hồi sinh,
    nhóm có dữ liệu con trùng khoá, và chế độ --only;
  - CHỈ ĐỌC: chạy xong không đổi số job, không đổi dữ liệu con, không đổi updated_at.
"""
import os
import uuid
from urllib.parse import urlparse

import psycopg2
import pytest

import db
import merge_duplicates as md

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
    db.baseline_migrations(conn)
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture(autouse=True)
def clean_jobs(pg_conn):
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE job_postings, company_contacts CASCADE")
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


def _company(conn):
    cid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)",
                    (cid, f"Công ty Gộp {uuid.uuid4().hex[:8]}"))
    conn.commit()
    return cid


def _user(conn):
    uid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO app_users (ss_user_id, full_name, email) VALUES (%s, %s, %s)",
                    (uid, "Người dùng", f"{uid}@example.com"))
    conn.commit()
    return uid


def _contact(conn, company_id):
    cid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO company_contacts (contact_id, company_id, contact_name) VALUES (%s, %s, %s)",
                    (cid, company_id, "HR"))
    conn.commit()
    return cid


def _job(conn, company_id, title, *, level="Junior", url=None, status="OPEN", deadline=None,
         salary=(None, None), notes=None, updated_by=None):
    job_id = db.insert_job(
        conn, company_id=company_id, job_title=title, matching_industry="Data",
        level_id=db.get_level_id(conn, level), province_id=None, work_type=None, currency="VNĐ",
        salary_min=salary[0], salary_max=salary[1], salary_type="RANGE" if salary[0] else "NEGOTIABLE",
        source_url=url or f"https://www.example.com/{uuid.uuid4()}", source_name="Fake",
    )
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.skip_updated_at', 'on', true)")
        cur.execute("UPDATE job_postings SET updated_at = %s, job_status = %s, deadline = %s, "
                    "ss_team_notes = %s, updated_by = %s WHERE job_id = %s",
                    (PAST, status, deadline, notes, updated_by, job_id))
        # C3c: listing của job theo kịp trạng thái/hạn vừa đặt (như dữ liệu thật sau backfill C1); nếu không,
        # luật suy ra sẽ thấy listing OPEN không hạn dưới một job "CLOSED" và gộp ra kết quả khác ý test.
        cur.execute("UPDATE job_sources_log SET listing_status = %s, deadline = %s, "
                    "closed_reason = CASE WHEN %s = 'CLOSED' THEN 'unknown' END, "
                    "closed_at = CASE WHEN %s = 'CLOSED' THEN now() END WHERE job_id = %s",
                    (status, deadline, status, status, job_id))
    conn.commit()
    return job_id


def _count(conn, table):
    return _one(conn, f"SELECT count(*) FROM {table}")[0]


# ------------------------------------------------------------------ list_merge_job_details
def test_details_return_blocks_and_children(pg_conn):
    c = _company(pg_conn)
    u1, u2 = _user(pg_conn), _user(pg_conn)
    contact = _contact(pg_conn, c)
    j = _job(pg_conn, c, "Data Analyst", level="Senior", salary=(10_000_000, 20_000_000), notes="gọi lại",
             deadline="2026-12-01", url="https://www.topcv.vn/da-1")
    other = _job(pg_conn, c, "Data Analyst")
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO saved_jobs (ss_user_id, job_id) VALUES (%s, %s)", (u1, j))
        cur.execute("INSERT INTO job_applications (ss_user_id, job_id, cv_url) VALUES (%s, %s, %s), (%s, %s, NULL)",
                    (u1, j, "cv-files/x.pdf", u2, j))
        cur.execute("INSERT INTO job_contact_links (job_id, contact_id) VALUES (%s, %s) RETURNING link_id",
                    (j, contact))
        link_id = cur.fetchone()[0]
        cur.execute("INSERT INTO job_contact_interactions (link_id, interaction_type) VALUES (%s, 'EMAIL'), (%s, 'CALL')",
                    (link_id, link_id))
    pg_conn.commit()

    got = db.list_merge_job_details(pg_conn, [j, other, str(uuid.uuid4())])
    assert set(got) == {j, other}                    # id không tồn tại vắng mặt
    row = got[j]
    assert row["level_code"] == "Senior" and row["salary_min"] == 10_000_000 and row["salary_max"] == 20_000_000
    assert str(row["deadline"]) == "2026-12-01" and row["job_status"] == "OPEN"
    assert row["ss_team_notes"] == "gọi lại" and row["has_notes"] is True and row["has_editor"] is False
    assert row["company_id"] == c and isinstance(row["job_id"], str)
    assert row["salary_period"] == "MONTH" and row["source_url"] == "https://www.topcv.vn/da-1"
    assert [x["source_url"] for x in row["logs"]] == ["https://www.topcv.vn/da-1"]
    assert [x["ss_user_id"] for x in row["saved"]] == [u1]
    assert sorted((x["ss_user_id"], x["has_cv"]) for x in row["applications"]) == sorted([(u1, True), (u2, False)])
    assert row["links"] == [{"link_id": str(link_id), "contact_id": contact, "n_interactions": 2}]
    assert (row["n_applications"], row["n_saved"], row["n_contact_links"]) == (2, 1, 1)
    assert got[other]["saved"] == [] and got[other]["links"] == [] and got[other]["has_notes"] is False


def test_details_empty_input_and_editor_flag(pg_conn):
    assert db.list_merge_job_details(pg_conn, []) == {}
    c = _company(pg_conn)
    editor = _user(pg_conn)
    j = _job(pg_conn, c, "Tester", updated_by=editor)
    assert db.list_merge_job_details(pg_conn, [j])[j]["has_editor"] is True


# ------------------------------------------------------------------ luồng chạy thử trên DB thật
def _snapshot(conn):
    return (_count(conn, "job_postings"), _count(conn, "job_sources_log"), _count(conn, "saved_jobs"),
            _count(conn, "job_applications"), _count(conn, "job_contact_links"),
            _one(conn, "SELECT min(updated_at)::text, max(updated_at)::text FROM job_postings"),
            _count(conn, "audit_logs"))


def test_run_default_plans_high_confidence_groups_and_changes_nothing(pg_conn, capsys, tmp_path):
    c = _company(pg_conn)
    u = _user(pg_conn)
    # Nhóm 1 (cao): bản cũ CLOSED có lương + bản mới OPEN không lương, lượt lưu chỉ ở bản cũ -> bản cũ được
    # bảo vệ, là job giữ, được hồi sinh và có dữ liệu con.
    old = _job(pg_conn, c, "Data Engineer", status="CLOSED", deadline="2026-09-01", salary=(20_000_000, 30_000_000),
               url="https://www.topcv.vn/old")
    _job(pg_conn, c, "Data Engineer", status="OPEN", deadline="2026-11-01", url="https://www.topcv.vn/new")
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO saved_jobs (ss_user_id, job_id) VALUES (%s, %s)", (u, old))
    pg_conn.commit()
    # Nhóm 2 (cần xem: hai tin cùng OPEN): không tự gộp.
    c2 = _company(pg_conn)
    _job(pg_conn, c2, "Kế toán")
    _job(pg_conn, c2, "Kế toán")
    before = _snapshot(pg_conn)

    path = tmp_path / "kehoach.csv"
    assert md.run(pg_conn, show=5, csv_path=str(path)) == 0
    out = capsys.readouterr().out
    assert "Nhóm nghi trùng trong DB: 2" in out and "Nhóm SẼ gộp:   1" in out
    assert md.SKIP_REVIEW in out and "hồi sinh" in out
    assert "dữ liệu con" in out and "bỏ vì trùng" in out
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")

    assert _snapshot(pg_conn) == before            # không xoá, không chuyển, không đổi updated_at, không audit


def test_build_plans_on_real_rows_revives_and_drops_duplicate_children(pg_conn):
    c = _company(pg_conn)
    u = _user(pg_conn)
    old = _job(pg_conn, c, "Data Engineer", status="CLOSED", deadline="2026-09-01", url="https://www.topcv.vn/old",
               salary=(20_000_000, 30_000_000))
    new = _job(pg_conn, c, "Data Engineer", status="OPEN", deadline="2026-11-01", url="https://www.topcv.vn/new")
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO saved_jobs (ss_user_id, job_id) VALUES (%s, %s), (%s, %s)", (u, old, u, new))
    pg_conn.commit()

    import duplicate_report as dr
    groups = dr.build_groups(db.list_duplicate_job_rows(pg_conn))
    # Cả hai job đều có dữ liệu cần bảo vệ -> mặc định KHÔNG tự gộp, phải duyệt tay và đánh dấu job giữ.
    selected, skipped, _, _ = md.select_groups(groups)
    assert selected == [] and [r for _, r in skipped] == [md.SKIP_MANUAL_PICK]
    only = md.OnlySpec(job_ids={old, new}, keepers={old})
    selected, skipped, _, _ = md.select_groups(groups, only)
    assert len(selected) == 1 and skipped == []
    plans, vanished = md.build_plans(pg_conn, selected)
    assert vanished == [] and len(plans) == 1
    p = plans[0]
    assert p.keeper_id == old and p.donor_ids == [new] and p.revives
    assert p.derived_changes["deadline"]["new"].isoformat() == "2026-11-01"
    assert p.derived_changes["source_url"]["new"] == "https://www.topcv.vn/new"
    assert p.derived_changes["job_status"] == {"old": "CLOSED", "new": "OPEN"} and p.listing_actions == {}
    assert len(p.child.saved_drop) == 1 and p.child.saved_move == []     # cùng người dùng lưu cả hai
    assert len(p.child.logs_move) == 1 and p.child.logs_drop == []       # log URL mới sẽ chuyển sang job giữ


def test_run_with_only_file_merges_review_group_and_reports_unknown_ids(pg_conn, capsys, tmp_path):
    c = _company(pg_conn)
    a = _job(pg_conn, c, "Kế toán")
    b = _job(pg_conn, c, "Kế toán")
    ghost = str(uuid.uuid4())
    f = tmp_path / "duyet.txt"
    f.write_text(f"# đã duyệt\n{a}\n{b}\n{ghost}\n", encoding="utf-8")
    before = _snapshot(pg_conn)
    assert md.run(pg_conn, only=md.parse_only(f.read_text(encoding="utf-8"))) == 0
    out = capsys.readouterr().out
    assert "theo file --only" in out and "Nhóm SẼ gộp:   1" in out
    assert ghost in out and "không thuộc nhóm nghi trùng" in out
    assert _snapshot(pg_conn) == before


def test_run_on_empty_db(pg_conn, capsys):
    assert md.run(pg_conn) == 0
    out = capsys.readouterr().out
    assert "Nhóm nghi trùng trong DB: 0" in out and "Nhóm SẼ gộp:   0" in out
