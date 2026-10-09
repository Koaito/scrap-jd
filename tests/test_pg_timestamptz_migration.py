"""
D3 đợt 2 trên POSTGRES THẬT: migration 0042 đổi 9 cột created_at/updated_at của 5 bảng lõi từ
TIMESTAMP sang TIMESTAMPTZ.

Chứng minh:
  - đổi đủ 9 cột; MỌI thời điểm giữ nguyên (giá trị cũ được hiểu là UTC), kể cả các mốc sát nửa đêm
    UTC và ranh giới tháng, và thứ tự theo thời gian không đổi;
  - kết quả không phụ thuộc TimeZone của session chạy migration (VN, Los Angeles, UTC);
  - view v_duplicate_job_candidates được tạo lại đúng định nghĩa của schema.sql và vẫn trả đúng dữ liệu;
  - DEFAULT now() và trigger updated_at vẫn chạy trên cột mới;
  - chạy lại file không lỗi và không đụng gì (view giữ nguyên oid); DB đã đổi dở dang thì chỉ đổi phần
    còn lại;
  - có view khác phụ thuộc vào các cột thì migration DỪNG với thông báo nêu tên view, không đổi gì,
    không ghi dấu "đã áp dụng";
  - có giao dịch khác giữ khoá thì migration báo lỗi hết thời gian chờ khoá thay vì chờ vô hạn, không
    đổi gì, chạy lại được khi hết khoá;
  - code dùng các cột này (bộ lọc ngày export, _vn_date của dashboard) cho CÙNG kết quả trước và sau
    migration, nên push code trước hay chạy migration trước đều không sai;
  - con trỏ phân trang của GET /jobs đang bay lúc chạy migration (chuỗi giờ KHÔNG có múi giờ) vẫn dùng
    được sau migration và cho đúng trang kế tiếp, giống con trỏ mới (có múi giờ);
  - schema.sql dựng DB mới không còn cột TIMESTAMP (không múi giờ) nào, để migration sau không vô tình
    thêm lại.

Cách chạy như tests/test_pg_migrations.py: đặt TEST_DATABASE_URL (tên database chứa "test"); không đặt
thì cả file được bỏ qua. Mỗi test tự tạo và xoá database tạm.
"""
import uuid
from datetime import date, datetime, timezone

import psycopg2
import psycopg2.errors
import pytest
from psycopg2 import sql as pgsql

from scrapjd import db
from scrapjd.api.routers.jobs import _decode_cursor, _encode_cursor
from scrapjd.api.services.export_query import ExportFilters, query_jobs_for_export
from scrapjd.db.dashboard import _vn_date
from pg_migration_helpers import (
    SCHEMA as _SCHEMA,
    SQL_DIR as _SQL_DIR,
    connect as _connect,
    legacy_db_before,
    migrate_one,
    requires_pg,
    temp_database as _temp_db,
)

pytestmark = requires_pg

_MIGRATION = "0042_timestamptz_core_tables.sql"

# (bảng, cột): đúng 9 cột của D3.
_COLUMNS = [
    ("app_users", "created_at"),
    ("companies", "created_at"), ("companies", "updated_at"),
    ("company_contacts", "created_at"), ("company_contacts", "updated_at"),
    ("job_contact_links", "created_at"), ("job_contact_links", "updated_at"),
    ("job_postings", "created_at"), ("job_postings", "updated_at"),
]
_VIEW = "v_duplicate_job_candidates"

# Mốc giờ UTC (naive, đúng như cột TIMESTAMP cũ lưu): sát nửa đêm UTC, sát ranh giới tháng giờ VN,
# 16:59:59.999999 và 17:00:00 (ranh giới ngày VN), có micro giây.
_T = [
    datetime(2026, 9, 30, 23, 59, 59, 999999),
    datetime(2026, 10, 1, 0, 0, 0),
    datetime(2026, 10, 7, 16, 59, 59, 999999),
    datetime(2026, 10, 7, 17, 0, 0),
    datetime(2026, 10, 7, 12, 34, 56, 123456),
]


# ----------------------------------------------------------------------------- hạ tầng test
def _temp_database():
    return _temp_db("tz")


def _legacy_db_before_0042(conn, tmp_path):
    """DB đúng trạng thái trước D3 đợt 2: baseline 0036 + 36 migration cũ (ghi nhận) + 0037..0041."""
    legacy_db_before(conn, tmp_path, 42)


def _migrate_0042(conn, tmp_path, *, sql_edit=None):
    return migrate_one(conn, tmp_path, _MIGRATION, sql_edit=sql_edit)


def _data_type(cur, table, column):
    cur.execute(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = %s AND column_name = %s",
        (table, column),
    )
    return cur.fetchone()[0]


def _all_types(cur):
    return {(t, c): _data_type(cur, t, c) for t, c in _COLUMNS}


NAIVE = "timestamp without time zone"
AWARE = "timestamp with time zone"


def _seed(conn):
    """Mỗi bảng vài dòng với created_at/updated_at ghi tường minh (naive, UTC). Trả về
    {(bảng, cột, khoá): giá trị naive đã ghi}."""
    written = {}
    with conn.cursor() as cur:
        user_ids, company_ids, contact_ids, job_ids = [], [], [], []

        for i, t in enumerate(_T):
            uid = str(uuid.uuid4())
            cur.execute(
                "INSERT INTO app_users (ss_user_id, full_name, email, created_at) VALUES (%s, %s, %s, %s)",
                (uid, f"User {i}", f"u{i}@example.com", t),
            )
            user_ids.append(uid)
            written[("app_users", "created_at", uid)] = t

        for i, t in enumerate(_T):
            cid = str(uuid.uuid4())
            t2 = t.replace(year=2027)
            cur.execute(
                "INSERT INTO companies (company_id, company_name, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s)",
                (cid, f"Công ty {i}", t, t2),
            )
            company_ids.append(cid)
            written[("companies", "created_at", cid)] = t
            written[("companies", "updated_at", cid)] = t2

        for i, t in enumerate(_T):
            kid = str(uuid.uuid4())
            t2 = t.replace(year=2027)
            cur.execute(
                "INSERT INTO company_contacts (contact_id, company_id, contact_name, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (kid, company_ids[0], f"Liên hệ {i}", t, t2),
            )
            contact_ids.append(kid)
            written[("company_contacts", "created_at", kid)] = t
            written[("company_contacts", "updated_at", kid)] = t2

        for i, t in enumerate(_T):
            jid = str(uuid.uuid4())
            t2 = t.replace(year=2027)
            cur.execute(
                "INSERT INTO job_postings (job_id, company_id, job_title, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (jid, company_ids[0], f"Job {i}", t, t2),
            )
            job_ids.append(jid)
            written[("job_postings", "created_at", jid)] = t
            written[("job_postings", "updated_at", jid)] = t2

        for i, t in enumerate(_T):
            lid = str(uuid.uuid4())
            t2 = t.replace(year=2027)
            cur.execute(
                "INSERT INTO job_contact_links (link_id, job_id, contact_id, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (lid, job_ids[i], contact_ids[i], t, t2),
            )
            written[("job_contact_links", "created_at", lid)] = t
            written[("job_contact_links", "updated_at", lid)] = t2
    conn.commit()
    return written


_KEY_COLUMN = {
    "app_users": "ss_user_id", "companies": "company_id", "company_contacts": "contact_id",
    "job_contact_links": "link_id", "job_postings": "job_id",
}


def _read_back(conn, written):
    """{(bảng, cột, khoá): (naive UTC, thời điểm có múi giờ)} đọc lại sau migration."""
    out = {}
    with conn.cursor() as cur:
        for (table, column, key) in written:
            cur.execute(
                pgsql.SQL("SELECT {c} AT TIME ZONE 'UTC', {c} FROM {t} WHERE {k} = %s").format(
                    c=pgsql.Identifier(column), t=pgsql.Identifier(table),
                    k=pgsql.Identifier(_KEY_COLUMN[table])),
                (key,),
            )
            out[(table, column, key)] = cur.fetchone()
    return out


def _viewdef(cur):
    cur.execute("SELECT pg_get_viewdef(%s::regclass, true)", (_VIEW,))
    return cur.fetchone()[0]


# ----------------------------------------------------------------------------- migration 0042
@pytest.mark.parametrize("session_tz", ["UTC", "Asia/Ho_Chi_Minh", "America/Los_Angeles"])
def test_migration_converts_all_nine_columns_and_keeps_every_instant(tmp_path, session_tz):
    with _temp_database() as url, _connect(url) as conn:
        _legacy_db_before_0042(conn, tmp_path)
        written = _seed(conn)
        with conn.cursor() as cur:
            assert set(_all_types(cur).values()) == {NAIVE}

        # Session chạy migration có múi giờ KHÁC UTC cũng không được làm lệch giá trị.
        with conn.cursor() as cur:
            cur.execute("SET TIME ZONE %s", (session_tz,))
        conn.commit()

        assert _migrate_0042(conn, tmp_path) == [_MIGRATION]

        with conn.cursor() as cur:
            assert _all_types(cur) == {col: AWARE for col in _COLUMNS}

        back = _read_back(conn, written)
        assert len(back) == len(written) == 5 * 9      # 5 dòng cho mỗi một trong 9 cột
        for key, naive_utc in written.items():
            as_naive, as_aware = back[key]
            assert as_naive == naive_utc, key                       # cùng giờ UTC, từng micro giây
            assert as_aware == naive_utc.replace(tzinfo=timezone.utc), key
            assert as_aware.tzinfo is not None


def test_migration_keeps_ordering_in_time(tmp_path):
    with _temp_database() as url, _connect(url) as conn:
        _legacy_db_before_0042(conn, tmp_path)
        written = _seed(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT job_id FROM job_postings ORDER BY created_at, job_id")
            before = [r[0] for r in cur.fetchall()]
        _migrate_0042(conn, tmp_path)
        with conn.cursor() as cur:
            cur.execute("SELECT job_id FROM job_postings ORDER BY created_at, job_id")
            after = [r[0] for r in cur.fetchall()]
        assert before == after and len(before) == len(_T)
        assert written  # đã seed thật


def test_view_is_recreated_with_the_schema_sql_definition_and_still_works(tmp_path):
    with _temp_database() as url_new, _connect(url_new) as conn_new:
        db.apply_schema(conn_new, _SCHEMA)
        with conn_new.cursor() as cur:
            expected_def = _viewdef(cur)

        with _temp_database() as url, _connect(url) as conn:
            _legacy_db_before_0042(conn, tmp_path)
            with conn.cursor() as cur:
                # hai job cùng công ty + tiêu đề + tỉnh (cùng dedup_key), created_at khác nhau
                cid = str(uuid.uuid4())
                cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, 'ACME')", (cid,))
                a, b = str(uuid.uuid4()), str(uuid.uuid4())
                cur.execute("INSERT INTO job_postings (job_id, company_id, job_title, created_at) "
                            "VALUES (%s, %s, 'Data Analyst', %s)", (b, cid, _T[3]))
                cur.execute("INSERT INTO job_postings (job_id, company_id, job_title, created_at) "
                            "VALUES (%s, %s, 'Data Analyst', %s)", (a, cid, _T[0]))
            conn.commit()

            _migrate_0042(conn, tmp_path)

            with conn.cursor() as cur:
                assert _viewdef(cur) == expected_def
                cur.execute(f"SELECT num_duplicates, job_ids::text[] FROM {_VIEW}")
                rows = cur.fetchall()
                assert len(rows) == 1
                assert rows[0][0] == 2
                assert rows[0][1] == [a, b]      # xếp theo created_at tăng dần: a (_T[0]) trước b (_T[3])


def test_defaults_and_updated_at_trigger_still_work_on_the_new_columns(tmp_path):
    with _temp_database() as url, _connect(url) as conn:
        _legacy_db_before_0042(conn, tmp_path)
        _migrate_0042(conn, tmp_path)
        with conn.cursor() as cur:
            cur.execute("SELECT column_default FROM information_schema.columns WHERE table_name = 'job_postings' "
                        "AND column_name IN ('created_at', 'updated_at')")
            assert [r[0] for r in cur.fetchall()] == ["now()", "now()"]

            cid = str(uuid.uuid4())
            cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, 'ACME') "
                        "RETURNING created_at, updated_at", (cid,))
            created, updated = cur.fetchone()
            assert created.tzinfo is not None and updated.tzinfo is not None

            cur.execute("UPDATE companies SET company_name = 'ACME 2' WHERE company_id = %s", (cid,))
            cur.execute("SELECT updated_at, now() FROM companies WHERE company_id = %s", (cid,))
            updated_after, now = cur.fetchone()
            assert updated_after == now          # trigger trg_set_updated_at đặt = now() của transaction


def test_rerunning_the_migration_file_is_a_noop_and_leaves_the_view_alone(tmp_path):
    with _temp_database() as url, _connect(url) as conn:
        _legacy_db_before_0042(conn, tmp_path)
        _migrate_0042(conn, tmp_path)
        sql_text = (_SQL_DIR / _MIGRATION).read_text(encoding="utf-8")
        with conn.cursor() as cur:
            cur.execute("SELECT %s::regclass::oid", (_VIEW,))
            oid_before = cur.fetchone()[0]
            cur.execute(sql_text)
            cur.execute(sql_text)
            cur.execute("SELECT %s::regclass::oid", (_VIEW,))
            assert cur.fetchone()[0] == oid_before          # không DROP/CREATE lại view khi không có gì đổi
            assert set(_all_types(cur).values()) == {AWARE}
        conn.commit()


def test_partially_converted_database_only_converts_the_rest(tmp_path):
    with _temp_database() as url, _connect(url) as conn:
        _legacy_db_before_0042(conn, tmp_path)
        written = _seed(conn)
        with conn.cursor() as cur:
            # ai đó đã đổi tay một cột (view phải gỡ trước)
            cur.execute(f"DROP VIEW {_VIEW}")
            cur.execute("ALTER TABLE companies ALTER COLUMN created_at TYPE timestamptz "
                        "USING created_at AT TIME ZONE 'UTC'")
            cur.execute(f"CREATE VIEW {_VIEW} AS SELECT dedup_key, array_agg(job_id ORDER BY created_at) AS job_ids, "
                        "array_agg(job_title ORDER BY created_at) AS job_titles, count(*) AS num_duplicates "
                        "FROM job_postings GROUP BY dedup_key HAVING count(*) > 1")
        conn.commit()

        assert _migrate_0042(conn, tmp_path) == [_MIGRATION]

        with conn.cursor() as cur:
            assert _all_types(cur) == {col: AWARE for col in _COLUMNS}
        back = _read_back(conn, written)
        for key, naive_utc in written.items():
            assert back[key][0] == naive_utc, key          # cột đã đổi tay không bị đổi lần hai (không lệch)


def test_another_view_on_these_columns_stops_the_migration_without_changing_anything(tmp_path):
    with _temp_database() as url, _connect(url) as conn:
        _legacy_db_before_0042(conn, tmp_path)
        with conn.cursor() as cur:
            cur.execute("CREATE VIEW v_hand_made AS SELECT company_id, created_at FROM companies")
        conn.commit()

        with pytest.raises(psycopg2.Error) as exc:
            _migrate_0042(conn, tmp_path)
        assert "v_hand_made" in str(exc.value)
        conn.rollback()

        with conn.cursor() as cur:
            assert set(_all_types(cur).values()) == {NAIVE}                 # không đổi gì
            cur.execute("SELECT to_regclass(%s)", (_VIEW,))
            assert cur.fetchone()[0] is not None                            # view cũ còn nguyên
            cur.execute("SELECT count(*) FROM schema_migrations WHERE filename = %s", (_MIGRATION,))
            assert cur.fetchone()[0] == 0                                   # không ghi dấu đã áp dụng

        # Gỡ view lạ rồi chạy lại thì qua.
        with conn.cursor() as cur:
            cur.execute("DROP VIEW v_hand_made")
        conn.commit()
        assert _migrate_0042(conn, tmp_path) == [_MIGRATION]


def test_lock_wait_times_out_instead_of_blocking_forever(tmp_path):
    with _temp_database() as url, _connect(url) as conn, _connect(url) as other:
        _legacy_db_before_0042(conn, tmp_path)
        text = (_SQL_DIR / _MIGRATION).read_text(encoding="utf-8")
        assert "lock_timeout = '10s'" in text            # giá trị thật của migration

        # Giao dịch khác (vd crawl đang chạy) đang đọc job_postings và chưa kết thúc.
        with other.cursor() as cur:
            cur.execute("SELECT count(*) FROM job_postings")

        # Bản thử rút ngắn 10 giây xuống 300ms để test nhanh; cơ chế y hệt.
        with pytest.raises(psycopg2.errors.LockNotAvailable):
            _migrate_0042(conn, tmp_path, sql_edit=lambda t: t.replace("'10s'", "'300ms'"))
        conn.rollback()

        with conn.cursor() as cur:
            assert set(_all_types(cur).values()) == {NAIVE}
            cur.execute("SELECT to_regclass(%s)", (_VIEW,))
            assert cur.fetchone()[0] is not None
            cur.execute("SELECT count(*) FROM schema_migrations WHERE filename = %s", (_MIGRATION,))
            assert cur.fetchone()[0] == 0

        # Hết khoá thì chạy lại được.
        other.rollback()
        assert _migrate_0042(conn, tmp_path) == [_MIGRATION]


def _vn_day_results(conn):
    """Kết quả của hai chỗ đọc đã sửa ở D3, trên dữ liệu _seed: (a) job lọc theo từng ngày VN qua export,
    (b) ngày VN của created_at theo _vn_date()."""
    out = {}
    for label, (frm, to) in {
        "01/10": (date(2026, 10, 1), date(2026, 10, 1)),
        "07/10": (date(2026, 10, 7), date(2026, 10, 7)),
        "08/10": (date(2026, 10, 8), date(2026, 10, 8)),
    }.items():
        rows = query_jobs_for_export(conn, ExportFilters(from_date=frm, to_date=to))
        out[label] = sorted(r["job_title"] for r in rows)
    with conn.cursor() as cur:
        cur.execute(f"SELECT job_title, {_vn_date('created_at')} FROM job_postings ORDER BY job_title")
        out["vn_date"] = [(t, d.isoformat()) for t, d in cur.fetchall()]
    conn.rollback()
    return out


def test_export_filter_and_vn_date_give_the_same_answer_before_and_after_the_migration(tmp_path):
    """Thứ tự triển khai không quan trọng: code mới chạy đúng cả khi cột còn là TIMESTAMP (lưu UTC, session
    UTC như Supabase) lẫn khi đã là TIMESTAMPTZ."""
    with _temp_database() as url, _connect(url) as conn:
        _legacy_db_before_0042(conn, tmp_path)
        _seed(conn)
        before = _vn_day_results(conn)

        # Đáp án đúng, tính tay: _T = 30/09 23:59:59.999999, 01/10 00:00, 07/10 16:59:59.999999,
        # 07/10 17:00, 07/10 12:34:56 (UTC) = 01/10 06:59:59, 01/10 07:00, 07/10 23:59:59, 08/10 00:00,
        # 07/10 19:34:56 (giờ VN).
        assert before["01/10"] == ["Job 0", "Job 1"]
        assert before["07/10"] == ["Job 2", "Job 4"]
        assert before["08/10"] == ["Job 3"]
        assert before["vn_date"] == [("Job 0", "2026-10-01"), ("Job 1", "2026-10-01"), ("Job 2", "2026-10-07"),
                                     ("Job 3", "2026-10-08"), ("Job 4", "2026-10-07")]

        _migrate_0042(conn, tmp_path)
        assert _vn_day_results(conn) == before


def test_pagination_cursor_issued_before_the_migration_still_works_after_it(tmp_path):
    """Client đang cuộn danh sách job lúc migration chạy giữ con trỏ cũ (isoformat không múi giờ). Sau
    migration con trỏ đó phải cho đúng trang kế tiếp, y hệt con trỏ mới (isoformat có +00:00)."""
    with _temp_database() as url, _connect(url) as conn:
        _legacy_db_before_0042(conn, tmp_path)
        _seed(conn)

        rows, _, cursor = db.list_jobs(conn, limit=2)
        conn.rollback()
        old_cursor_str = _encode_cursor(cursor[0], str(cursor[1]))
        assert "+" not in _decode_cursor_text(old_cursor_str)             # đúng là dạng không múi giờ
        expected_page_2 = [str(r["job_id"]) for r in
                           db.list_jobs(conn, limit=2, cursor=_decode_cursor(old_cursor_str))[0]]
        conn.rollback()
        assert len(expected_page_2) == 2

        _migrate_0042(conn, tmp_path)

        # (1) con trỏ cũ, đã phát trước migration
        got_old = [str(r["job_id"]) for r in db.list_jobs(conn, limit=2, cursor=_decode_cursor(old_cursor_str))[0]]
        conn.rollback()
        assert got_old == expected_page_2

        # (2) con trỏ mới, phát sau migration cho cùng vị trí
        _, _, new_cursor = db.list_jobs(conn, limit=2)
        conn.rollback()
        new_cursor_str = _encode_cursor(new_cursor[0], str(new_cursor[1]))
        assert "+00:00" in _decode_cursor_text(new_cursor_str)
        got_new = [str(r["job_id"]) for r in db.list_jobs(conn, limit=2, cursor=_decode_cursor(new_cursor_str))[0]]
        conn.rollback()
        assert got_new == expected_page_2


def _decode_cursor_text(cursor: str) -> str:
    import base64
    return base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")


# ----------------------------------------------------------------------------- schema.sql
def test_schema_sql_has_no_timestamp_without_time_zone_column_anywhere():
    """Chốt lâu dài: mọi cột thời điểm của hệ thống là TIMESTAMPTZ. Migration/bảng mới dùng TIMESTAMP
    (không múi giờ) làm test này đỏ, để khỏi lặp lại chuyện mỗi chỗ đọc tự đoán múi giờ."""
    with _temp_database() as url, _connect(url) as conn:
        db.apply_schema(conn, _SCHEMA)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name || '.' || column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND data_type = 'timestamp without time zone' "
                "ORDER BY 1"
            )
            naive = [r[0] for r in cur.fetchall()]
            assert naive == [], f"Cột TIMESTAMP không múi giờ: {naive}. Dùng TIMESTAMPTZ."
            assert set(_all_types(cur).values()) == {AWARE}
