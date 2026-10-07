"""
Báo cáo đo tỷ lệ gộp nhầm (A5) trên POSTGRES THẬT: truy vấn db.list_multi_source_job_logs,
db.list_merge_log_origins và lệnh repost_report.run. Cách chạy giống tests/test_pg_duplicate_report.py:
đặt TEST_DATABASE_URL trỏ tới database dùng riêng cho test (tên chứa "test"); không đặt thì cả file
được bỏ qua.

Chứng minh trên SQL thật:
  - chỉ lấy job có >= 2 dòng job_sources_log, đủ cột, thứ tự xác định (tin cũ nhất trước);
  - nhận ra log do merge-duplicates chuyển sang bằng một lần gộp THẬT (db.merge_job_group), đúng cấu
    trúc snapshot (moved = danh sách log_id, dropped = nguyên dòng), đếm đúng số log bị bỏ;
  - DB chưa có dấu vết gộp thì origins rỗng; action khác MERGE_JOB không bị nhầm;
  - run(): phân loại đúng tin đăng lại giống / tin khác hẳn (gộp nhầm), xuất CSV, và CHỈ ĐỌC (không
    đổi dữ liệu, không đổi updated_at, không để transaction mở).
"""
import dataclasses
import os
import uuid
from urllib.parse import urlparse

import psycopg2
import pytest

import db
import duplicate_report as dr
import merge_duplicates as md
import repost_report as rr

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")
PAST = "2020-01-01 00:00:00"

BODY_A = ("quản lý chiến dịch quảng cáo trên facebook và google phân tích dữ liệu hiệu quả "
          "lập báo cáo hằng tuần cho trưởng phòng marketing phối hợp với đội thiết kế nội dung")
BODY_B = ("xây dựng và vận hành hệ thống backend bằng python và postgresql viết api tối ưu truy vấn "
          "triển khai lên máy chủ đám mây theo dõi lỗi và hiệu năng của dịch vụ")


def _raw(body):
    return "=== Mô tả công việc ===\n" + body + "\n\n=== Quyền lợi ứng viên ===\nbảo hiểm đầy đủ thưởng tháng mười ba"


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
def _scalar(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        value = cur.fetchone()[0]
    conn.rollback()
    return value


def _company(conn):
    cid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)",
                    (cid, f"Công ty Đăng Lại {uuid.uuid4().hex[:8]}"))
    conn.commit()
    return cid


def _job(conn, company, title, *, level="Junior", url=None, body=BODY_A):
    url = url or f"https://www.topcv.vn/{uuid.uuid4()}"
    job_id = db.insert_job(
        conn, company_id=company, job_title=title, matching_industry="Data",
        level_id=db.get_level_id(conn, level), province_id=None, work_type=None, currency="VNĐ",
        salary_min=None, salary_max=None, salary_type="NEGOTIABLE",
        source_url=url, source_name="TopCV", raw_jd_content=_raw(body) if body else None,
    )
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.skip_updated_at', 'on', true)")
        cur.execute("UPDATE job_postings SET updated_at = %s WHERE job_id = %s", (PAST, job_id))
    conn.commit()
    return job_id


def _repost(conn, job_id, *, body=BODY_A, url=None, site="www.topcv.vn"):
    url = url or f"https://{site}/{uuid.uuid4()}"
    db.link_repost_source(conn, job_id, source_name="TopCV", source_url=url,
                          raw_jd_content=_raw(body) if body else "")
    conn.commit()
    return url


def _merge(conn, ids, keeper):
    groups = dr.build_groups(db.list_duplicate_job_rows(conn))
    selected, skipped, _, _ = md.select_groups(groups, md.OnlySpec(job_ids=set(ids), keepers={keeper}))
    assert len(selected) == 1 and skipped == [], skipped
    plans, vanished = md.build_plans(conn, selected)
    assert vanished == [] and len(plans) == 1
    plan = plans[0]
    res = db.merge_job_group(
        conn, keeper_id=plan.keeper_id, donor_ids=plan.donor_ids, expected=plan.expected,
        changes=plan.changes, child=dataclasses.asdict(plan.child), conflicts=plan.conflicts, notes=plan.notes)
    conn.commit()
    return res


def _snapshot(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT job_id::text, updated_at::text, job_status::text FROM job_postings ORDER BY job_id")
        jobs = cur.fetchall()
        cur.execute("SELECT log_id::text, job_id::text, raw_jd_content FROM job_sources_log ORDER BY log_id")
        logs = cur.fetchall()
        cur.execute("SELECT count(*) FROM audit_logs")
        n_audit = cur.fetchone()[0]
    conn.rollback()
    return jobs, logs, n_audit


# ------------------------------------------------------------------ list_multi_source_job_logs
def test_only_jobs_with_two_or_more_logs_are_listed_oldest_log_first(pg_conn):
    c = _company(pg_conn)
    single = _job(pg_conn, c, "Chỉ một tin")
    multi = _job(pg_conn, c, "Có tin đăng lại")
    first_url = _scalar(pg_conn, "SELECT source_url FROM job_sources_log WHERE job_id = %s", (multi,))
    _repost(pg_conn, multi, body=BODY_B)
    with pg_conn.cursor() as cur:   # đảo ngày để chắc thứ tự theo collected_date chứ không theo thứ tự insert
        cur.execute("UPDATE job_sources_log SET collected_date = '2026-01-01' WHERE job_id = %s AND source_url = %s",
                    (multi, first_url))
    pg_conn.commit()

    rows = db.list_multi_source_job_logs(pg_conn)

    assert {r["job_id"] for r in rows} == {multi} and single not in {r["job_id"] for r in rows}
    assert len(rows) == 2 and rows[0]["source_url"] == first_url
    assert str(rows[0]["collected_date"]) == "2026-01-01"
    row = rows[0]
    assert set(row) == set(rr_columns()) and row["level_code"] == "Junior" and row["job_status"] == "OPEN"
    assert BODY_A in row["raw_jd_content"]


def rr_columns():
    from db.job_reposts import _LOG_COLUMNS
    return _LOG_COLUMNS


def test_empty_database_gives_empty_lists(pg_conn):
    assert db.list_multi_source_job_logs(pg_conn) == []
    assert db.list_merge_log_origins(pg_conn) == {"moved": {}, "dropped": 0}


# ------------------------------------------------------------------ list_merge_log_origins (gộp thật)
def test_origins_come_from_a_real_merge_and_count_dropped_logs(pg_conn):
    c = _company(pg_conn)
    shared = "https://www.topcv.vn/cung-url"
    keeper = _job(pg_conn, c, "Data Analyst", url=shared, body=BODY_A)
    donor = _job(pg_conn, c, "Data Analyst", level="Senior", url=shared + "-khac", body=BODY_B)
    _repost(pg_conn, donor, url=shared, body=BODY_B)        # trùng (job, url) với log của job giữ => bị bỏ
    moved_log = _scalar(pg_conn, "SELECT log_id::text FROM job_sources_log WHERE job_id = %s AND source_url = %s",
                        (donor, shared + "-khac"))

    res = _merge(pg_conn, [keeper, donor], keeper)
    assert res["children"]["job_sources_log"] == (1, 1)

    origins = db.list_merge_log_origins(pg_conn)
    assert origins["dropped"] == 1
    assert set(origins["moved"]) == {moved_log}
    info = origins["moved"][moved_log]
    assert info["donor_job_id"] == donor and info["keeper_job_id"] == keeper and info["merged_at"] is not None


def test_non_merge_audit_rows_are_not_mistaken_for_merges(pg_conn):
    c = _company(pg_conn)
    j = _job(pg_conn, c, "Data Analyst")
    db.log_action(pg_conn, actor_id=None, action_type="CREATE_JOB", entity_type="JOB", entity_id=j,
                  entity_label="x", company_id=c, changes={"merged_into": "x", "snapshot": {}})
    pg_conn.commit()
    assert db.list_merge_log_origins(pg_conn) == {"moved": {}, "dropped": 0}


# ------------------------------------------------------------------ run()
def test_run_flags_the_wrong_merge_and_passes_the_real_repost(pg_conn, tmp_path, capsys):
    c = _company(pg_conn)
    good = _job(pg_conn, c, "Marketing Executive", body=BODY_A)
    _repost(pg_conn, good, body=BODY_A)                                     # tin đăng lại đúng nghĩa
    bad = _job(pg_conn, c, "Backend Developer", level="Junior", body=BODY_A)
    _repost(pg_conn, bad, body=BODY_B, site="vietnamworks.com")             # vị trí khác bị nối nhầm
    path = tmp_path / "gop.csv"
    before = _snapshot(pg_conn)

    assert rr.run(pg_conn, show=5, csv_path=str(path)) == 0
    out = capsys.readouterr().out

    assert "Job có >= 2 tin nguồn (job_sources_log): 2" in out
    assert "Tỷ lệ nghi gộp nhầm: 1 / 2 = 50.0%" in out
    assert "Backend Developer" in out and "KHÁC trang" in out
    assert _snapshot(pg_conn) == before                                      # chỉ đọc: không đổi gì
    assert pg_conn.info.transaction_status == psycopg2.extensions.TRANSACTION_STATUS_IDLE
    lines = path.read_text(encoding="utf-8-sig").strip().splitlines()
    assert len(lines) == 3 and ",nghi gộp nhầm," in lines[1] and ",giống," in lines[2]
    assert _scalar(pg_conn, "SELECT count(*) FROM job_postings WHERE updated_at <> %s", (PAST,)) == 0


def test_run_after_a_real_merge_reports_the_merged_log(pg_conn, capsys):
    c = _company(pg_conn)
    keeper = _job(pg_conn, c, "Data Analyst", body=BODY_A)
    donor = _job(pg_conn, c, "Data Analyst", level="Senior", body=BODY_B)   # hai vị trí khác nội dung bị gộp
    _merge(pg_conn, [keeper, donor], keeper)

    assert rr.run(pg_conn, show=5) == 0
    out = capsys.readouterr().out

    assert "Tin do merge-duplicates chuyển sang job giữ: 1" in out
    assert "có tin do merge-duplicates" in out and "1 /" in out
    assert "merge-duplicates (từ job" in out


def test_run_with_no_multi_source_jobs(pg_conn, capsys):
    c = _company(pg_conn)
    _job(pg_conn, c, "Chỉ một tin")
    assert rr.run(pg_conn) == 0
    out = capsys.readouterr().out
    assert "Job có >= 2 tin nguồn (job_sources_log): 0" in out and "0 / 0 = —" in out
