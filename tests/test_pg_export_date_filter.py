"""
D3 đợt 2 trên POSTGRES THẬT: bộ lọc from_date/to_date của export (preview lẫn file tải) tính theo NGÀY
VIỆT NAM, gồm cả hai đầu. Xem tests/test_export_date_range.py cho phần không cần DB.

Chứng minh:
  - các mốc sát ranh giới ngày VN (23:59:59, 00:00:00, 23:59:59.999999) rơi đúng ngày;
  - kịch bản đã tái hiện lỗi cũ: job tạo lúc 07:00, 17:00 và 01:00 hôm sau (giờ VN), chọn đến 07/10 ra 2
    dòng (cũ: 1), chọn từ 08/10 ra 1 dòng (cũ: 0);
  - mỗi dòng thuộc đúng MỘT ngày: lọc từng ngày liên tiếp thì hợp lại đủ mọi dòng, không dòng nào hiện hai
    lần hay mất;
  - ngày staff lọc khớp ngày hiện trong file (cột created_at xuất ra, giờ VN);
  - preview (count_rows_for_export) và file tải (query_*_for_export) luôn cùng số dòng;
  - kết quả không phụ thuộc TimeZone của session DB;
  - áp cho cả job, company, contact, cho date_field = created_at và updated_at, và kết hợp được với limit.

Cách chạy như tests/test_pg_integration.py: đặt TEST_DATABASE_URL (tên database chứa "test"); không đặt
thì cả file được bỏ qua.
"""
import os
import uuid
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import psycopg2
import pytest

from scrapjd import db
from scrapjd.api.services.export_query import (
    ExportFilters,
    count_rows_for_export,
    query_companies_for_export,
    query_contacts_for_export,
    query_jobs_for_export,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")
_VN = timezone(timedelta(hours=7))


def _vn(day, hour=0, minute=0, second=0, micro=0):
    return datetime(2026, 10, day, hour, minute, second, micro, tzinfo=_VN)


# Nhãn -> thời điểm tạo theo GIỜ VN. Sát ranh giới ngày 07/10 và 08/10.
_MOMENTS = {
    "A_06_2359": _vn(6, 23, 59, 59),
    "B_07_0000": _vn(7, 0, 0, 0),
    "C_07_0700": _vn(7, 7, 0, 0),
    "D_07_1700": _vn(7, 17, 0, 0),
    "E_07_last": _vn(7, 23, 59, 59, 999999),
    "F_08_0000": _vn(8, 0, 0, 0),
    "G_08_0100": _vn(8, 1, 0, 0),
}
_LABEL_BY_TITLE = {f"Job {label}": label for label in _MOMENTS}
_UPDATED_SHIFT = timedelta(days=3)       # updated_at = created_at + 3 ngày, để phân biệt hai date_field


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
    with conn.cursor() as cur:
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, 'Công ty gốc')",
                    (str(uuid.uuid4()),))
    conn.commit()
    _seed(conn)
    yield conn
    conn.rollback()
    conn.close()


def _seed(conn):
    """Mỗi mốc: 1 job, 1 company, 1 contact cùng created_at (aware, ghi tường minh +07:00)."""
    with conn.cursor() as cur:
        cur.execute("TRUNCATE job_postings, company_contacts, companies CASCADE")
        for label, created in _MOMENTS.items():
            updated = created + _UPDATED_SHIFT
            cid, jid, kid = (str(uuid.uuid4()) for _ in range(3))
            cur.execute(
                "INSERT INTO companies (company_id, company_name, created_at, updated_at) VALUES (%s, %s, %s, %s)",
                (cid, f"Co {label}", created.isoformat(), updated.isoformat()))
            cur.execute(
                "INSERT INTO job_postings (job_id, company_id, job_title, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (jid, cid, f"Job {label}", created.isoformat(), updated.isoformat()))
            cur.execute(
                "INSERT INTO company_contacts (contact_id, company_id, contact_name, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (kid, cid, f"Contact {label}", created.isoformat(), updated.isoformat()))
    conn.commit()


@pytest.fixture(autouse=True)
def _rollback(pg_conn):
    pg_conn.rollback()
    yield
    pg_conn.rollback()


def _job_labels(conn, **kwargs):
    rows = query_jobs_for_export(conn, ExportFilters(**kwargs))
    return {_LABEL_BY_TITLE[r["job_title"]] for r in rows}


def _day(d):
    return date(2026, 10, d)


# ----------------------------------------------------------------------------- ranh giới ngày VN
def test_single_vn_day_includes_first_and_last_instant_and_nothing_else(pg_conn):
    got = _job_labels(pg_conn, from_date=_day(7), to_date=_day(7))
    assert got == {"B_07_0000", "C_07_0700", "D_07_1700", "E_07_last"}


def test_to_date_includes_the_whole_last_day(pg_conn):
    # Lỗi cũ: `<= to_date` chỉ giữ dòng tạo đúng 00:00:00 của ngày cuối.
    assert _job_labels(pg_conn, to_date=_day(7)) == {
        "A_06_2359", "B_07_0000", "C_07_0700", "D_07_1700", "E_07_last"}


def test_from_date_uses_the_vn_day_not_the_utc_day(pg_conn):
    # F (00:00 08/10 VN) và G (01:00 08/10 VN) đều là ngày 08/10 giờ VN dù tính theo UTC vẫn là 07/10.
    assert _job_labels(pg_conn, from_date=_day(8)) == {"F_08_0000", "G_08_0100"}


def test_the_scenario_that_exposed_the_old_bug(pg_conn):
    # Job tạo 07:00, 17:00 và 01:00 hôm sau (giờ VN): C, D, G.
    only = {"C_07_0700", "D_07_1700", "G_08_0100"}
    kept = _job_labels(pg_conn)
    assert only <= kept
    up_to_07 = _job_labels(pg_conn, to_date=_day(7)) & only
    from_08 = _job_labels(pg_conn, from_date=_day(8)) & only
    assert up_to_07 == {"C_07_0700", "D_07_1700"}          # cũ: chỉ 1 dòng
    assert from_08 == {"G_08_0100"}                        # cũ: 0 dòng


def test_each_row_belongs_to_exactly_one_day(pg_conn):
    seen = []
    for d in (5, 6, 7, 8, 9):
        seen.extend(sorted(_job_labels(pg_conn, from_date=_day(d), to_date=_day(d))))
    assert sorted(seen) == sorted(_MOMENTS)       # đủ 7 dòng, mỗi dòng đúng một lần


def test_the_day_you_filter_is_the_day_shown_in_the_file(pg_conn):
    for d in (6, 7, 8):
        rows = query_jobs_for_export(pg_conn, ExportFilters(from_date=_day(d), to_date=_day(d)))
        assert rows, d
        assert {r["created_at"][:10] for r in rows} == {_day(d).isoformat()}


# ----------------------------------------------------------------------------- không phụ thuộc session
@pytest.mark.parametrize("session_tz", ["UTC", "Asia/Ho_Chi_Minh", "America/Los_Angeles", "Pacific/Auckland"])
def test_result_does_not_depend_on_the_session_time_zone(pg_conn, session_tz):
    with pg_conn.cursor() as cur:
        cur.execute("SET TIME ZONE %s", (session_tz,))
    try:
        assert _job_labels(pg_conn, from_date=_day(7), to_date=_day(7)) == {
            "B_07_0000", "C_07_0700", "D_07_1700", "E_07_last"}
        assert _job_labels(pg_conn, from_date=_day(8)) == {"F_08_0000", "G_08_0100"}
        # giờ trong file vẫn theo giờ VN, không theo session
        rows = query_jobs_for_export(pg_conn, ExportFilters(from_date=_day(8), to_date=_day(8)))
        assert {r["created_at"] for r in rows} == {"2026-10-08 00:00:00", "2026-10-08 01:00:00"}
    finally:
        pg_conn.rollback()
        with pg_conn.cursor() as cur:
            cur.execute("SET TIME ZONE 'UTC'")
        pg_conn.commit()


# ----------------------------------------------------------------------------- preview khớp file
@pytest.mark.parametrize("entity,fn", [
    ("job", query_jobs_for_export),
    ("company", query_companies_for_export),
    ("contact", query_contacts_for_export),
])
@pytest.mark.parametrize("date_field", ["created_at", "updated_at"])
def test_preview_count_matches_downloaded_rows_for_every_entity_and_date_field(pg_conn, entity, fn, date_field):
    for frm, to in [(_day(7), _day(7)), (None, _day(7)), (_day(8), None), (_day(6), _day(9)), (_day(20), _day(21))]:
        filters = ExportFilters(date_field=date_field, from_date=frm, to_date=to)
        assert count_rows_for_export(pg_conn, entity, filters) == len(fn(pg_conn, filters)), (frm, to)


@pytest.mark.parametrize("entity,fn", [
    ("job", query_jobs_for_export),
    ("company", query_companies_for_export),
    ("contact", query_contacts_for_export),
])
def test_company_and_contact_filters_use_the_same_vn_day_rule(pg_conn, entity, fn):
    filters = ExportFilters(from_date=_day(8), to_date=_day(8))
    assert count_rows_for_export(pg_conn, entity, filters) == 2          # F và G
    rows = fn(pg_conn, filters)
    assert {r["created_at"] for r in rows} == {"2026-10-08 00:00:00", "2026-10-08 01:00:00"}


def test_updated_at_is_filtered_by_its_own_vn_day(pg_conn):
    # updated_at = created_at + 3 ngày: B,C,D,E -> 10/10; A -> 09/10; F,G -> 11/10.
    got = _job_labels(pg_conn, date_field="updated_at", from_date=_day(10), to_date=_day(10))
    assert got == {"B_07_0000", "C_07_0700", "D_07_1700", "E_07_last"}
    assert _job_labels(pg_conn, date_field="updated_at", from_date=_day(9), to_date=_day(9)) == {"A_06_2359"}


def test_limit_applies_after_the_date_filter(pg_conn):
    rows = query_jobs_for_export(pg_conn, ExportFilters(from_date=_day(7), to_date=_day(7), limit=2))
    assert len(rows) == 2
    # 2 dòng MỚI NHẤT của ngày 07/10: E rồi D
    assert [_LABEL_BY_TITLE[r["job_title"]] for r in rows] == ["E_07_last", "D_07_1700"]
    # tổng khớp filter vẫn là 4 (limit không ảnh hưởng số đếm của preview)
    assert count_rows_for_export(pg_conn, "job", ExportFilters(from_date=_day(7), to_date=_day(7), limit=2)) == 4


def test_extreme_dates_do_not_error_on_the_database(pg_conn):
    assert len(_job_labels(pg_conn, from_date=date.min, to_date=date.max)) == len(_MOMENTS)
    assert _job_labels(pg_conn, to_date=date.min) == set()
