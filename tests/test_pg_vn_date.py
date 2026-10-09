"""
D3 đợt 1 trên POSTGRES THẬT: db.dashboard._vn_date() đúng với CẢ HAI kiểu cột.

Các cột created_at/updated_at của app_users, companies, company_contacts,
job_contact_links, job_postings hiện là TIMESTAMP (naive, lưu UTC) và sẽ đổi sang
TIMESTAMPTZ ở D3 đợt 2. Hàm _vn_date() phải cho cùng một ngày VN trước và sau khi
đổi, để code đi trước migration. Dạng cũ `(col AT TIME ZONE 'UTC') AT TIME ZONE
'Asia/Ho_Chi_Minh'` đúng trên cột naive nhưng sai ngày ở 14/24 khung giờ trên cột
TIMESTAMPTZ.

  - 48 mốc giờ liên tiếp (qua nửa đêm UTC và qua ranh giới tháng) trên bảng tạm
    TIMESTAMP và TIMESTAMPTZ: ngày trả về phải bằng (giờ UTC + 7h).date();
  - trên TIMESTAMPTZ kết quả không đổi khi TimeZone của session đổi;
  - dạng cũ trên TIMESTAMPTZ sai đúng 14/24 khung giờ (lý do không dùng lại);
  - dữ liệu thật qua get_monthly_recap_counts: job tạo sát ranh giới tháng giờ VN
    được tính đúng tháng.
Cách chạy như các test_pg_*: cần TEST_DATABASE_URL (database tên chứa "test").
"""
import os
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import psycopg2
import pytest

from scrapjd import db
from scrapjd.db.dashboard import _vn_date, get_monthly_recap_counts

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")
_VN = timedelta(hours=7)


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
        cur.execute("TRUNCATE job_postings, companies CASCADE")
    pg_conn.commit()
    yield
    pg_conn.rollback()


# 48 mốc, mỗi giờ một mốc, từ 30/09 00:30 UTC: phủ cả 24 giờ UTC, qua nửa đêm UTC
# và qua ranh giới tháng (giờ VN của mốc cuối đã sang tháng 10).
_START = datetime(2026, 9, 30, 0, 30)
_INSTANTS = [_START + timedelta(hours=h) for h in range(48)]


def _vn_dates(conn, column_type, *, expr=None):
    """{mốc UTC: ngày VN do SQL tính} cho một bảng tạm có cột kiểu column_type."""
    with conn.cursor() as cur:
        cur.execute(f"CREATE TEMP TABLE t_vn (ts {column_type}) ON COMMIT DROP")
        # Ghi từ chuỗi UTC rõ ràng để không phụ thuộc TimeZone của session lúc ghi.
        for i in _INSTANTS:
            if column_type == "timestamptz":
                cur.execute("INSERT INTO t_vn VALUES (%s::timestamp AT TIME ZONE 'UTC')", (i.isoformat(sep=" "),))
            else:
                cur.execute("INSERT INTO t_vn VALUES (%s::timestamp)", (i.isoformat(sep=" "),))
        cur.execute(f"SELECT ts, {expr or _vn_date('ts')} FROM t_vn")
        rows = cur.fetchall()
    conn.rollback()
    out = {}
    for ts, d in rows:
        key = ts if ts.tzinfo is None else ts.astimezone(timezone.utc).replace(tzinfo=None)
        out[key] = d
    return out


def _expected():
    return {i: (i + _VN).date() for i in _INSTANTS}


def test_vn_date_is_right_on_a_naive_timestamp_column(pg_conn):
    got = _vn_dates(pg_conn, "timestamp")
    assert got == _expected()


def test_vn_date_is_right_on_a_timestamptz_column(pg_conn):
    got = _vn_dates(pg_conn, "timestamptz")
    assert got == _expected()


@pytest.mark.parametrize("session_tz", ["UTC", "America/New_York", "Asia/Tokyo"])
def test_vn_date_on_timestamptz_does_not_depend_on_session_timezone(pg_conn, session_tz):
    with pg_conn.cursor() as cur:
        cur.execute(f"SET LOCAL TIME ZONE '{session_tz}'")
        cur.execute("CREATE TEMP TABLE t_vn (ts timestamptz) ON COMMIT DROP")
        for i in _INSTANTS:
            cur.execute("INSERT INTO t_vn VALUES (%s::timestamp AT TIME ZONE 'UTC')", (i.isoformat(sep=" "),))
        cur.execute(f"SELECT (ts AT TIME ZONE 'UTC'), {_vn_date('ts')} FROM t_vn")
        got = {utc: d for utc, d in cur.fetchall()}
    pg_conn.rollback()
    assert got == _expected()


def test_old_expression_is_wrong_on_timestamptz_in_14_of_24_hours(pg_conn):
    """Chốt lý do _vn_date() không dùng lại dạng 'AT TIME ZONE UTC' hai lần."""
    def old(c):
        return f"(({c} AT TIME ZONE 'UTC') AT TIME ZONE 'Asia/Ho_Chi_Minh')::date"

    got = _vn_dates(pg_conn, "timestamptz", expr=old("ts"))
    day = _INSTANTS[:24]
    wrong = [i for i in day if got[i] != (i + _VN).date()]
    assert len(wrong) == 14
    # Cùng dạng cũ trên cột naive thì đúng (đó là lý do nó từng được dùng).
    assert _vn_dates(pg_conn, "timestamp", expr=old("ts")) == _expected()


def _vn_today(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT (now() AT TIME ZONE 'Asia/Ho_Chi_Minh')::date")
        d = cur.fetchone()[0]
    conn.rollback()
    return d


def _job_created_at(conn, utc_naive: datetime):
    """Tạo 1 job rồi đặt created_at = mốc UTC (chuỗi, đúng với cả hai kiểu cột khi session là UTC)."""
    with conn.cursor() as cur:
        cid = str(uuid.uuid4())
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)", (cid, f"C {cid[:8]}"))
    conn.commit()
    job_id = db.insert_job(
        conn, company_id=cid, job_title="Data Analyst", matching_industry="Data", level_id=None,
        province_id=None, work_type=None, currency="VNĐ", salary_min=None, salary_max=None,
        salary_type="NEGOTIABLE", source_url=f"https://x/{uuid.uuid4()}", source_name="Fake", deadline=None,
    )
    with conn.cursor() as cur:
        cur.execute("UPDATE job_postings SET created_at = %s WHERE job_id = %s",
                    (utc_naive.isoformat(sep=" "), job_id))
    conn.commit()
    return job_id


def test_monthly_recap_puts_jobs_near_the_month_boundary_in_the_right_vn_month(pg_conn):
    today = _vn_today(pg_conn)
    this_start = today.replace(day=1)
    # 00:30 ngày 1 giờ VN = 17:30 UTC ngày trước: đã thuộc THÁNG NÀY theo giờ VN.
    first_minute_this = datetime.combine(this_start, datetime.min.time()) + timedelta(minutes=30) - _VN
    # 23:30 ngày cuối tháng trước giờ VN = 16:30 UTC cùng ngày: còn THÁNG TRƯỚC.
    last_minute_prev = datetime.combine(this_start, datetime.min.time()) - timedelta(minutes=30) - _VN
    _job_created_at(pg_conn, first_minute_this)
    _job_created_at(pg_conn, last_minute_prev)

    recap = get_monthly_recap_counts(pg_conn)
    pg_conn.rollback()
    assert recap["jobs_this"] == 1
    assert recap["jobs_last"] == 1
