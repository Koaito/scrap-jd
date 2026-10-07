"""
Test trên POSTGRES THẬT cho cơ chế migration (D1).

Vì sao cần: sql/schema.sql (dựng DB mới bằng `init-db`) và các migration (nâng cấp DB đang chạy)
là HAI nơi mô tả cùng một schema. Quên cập nhật một trong hai thì DB dựng mới và DB nâng cấp
lệch nhau, và chỉ lộ ra khi chạy trên prod. Test dưới đây chặn lỗi đó:

    DB A = sql/schema.sql dựng từ trống
    DB B = sql/baseline/0036_schema.sql (schema.sql tại thời điểm baseline) + MỌI migration
           đánh số NNNN_*.sql, chạy bằng chính db.apply_migrations()
    Yêu cầu: schema của A và B giống hệt.

Hiện chưa có migration đánh số nào nên B = baseline và A = schema.sql: phép so vẫn có nghĩa, vì
nó bắt được việc sửa schema.sql mà quên viết migration (baseline đóng băng, xem
tests/test_migrations.py). Từ migration 0037 trở đi nó bắt cả chiều ngược lại.

So sánh bằng CATALOG của Postgres (cột, ràng buộc, index, enum, hàm, trigger, view, sequence,
extension, comment), KHÔNG so văn bản `pg_dump`. Lý do: cột thêm bằng ALTER TABLE nằm CUỐI bảng,
còn trong schema.sql nằm đúng vị trí trong CREATE TABLE, nên pg_dump của hai DB khác thứ tự cột
dù schema tương đương, gây báo lệch giả. So theo tên cột không bị vậy, và không cần pg_dump trong
máy chạy test. Chưa so DỮ LIỆU (bảng tham chiếu như levels, provinces).

Cách chạy: đặt TEST_DATABASE_URL trỏ tới một database dành riêng cho test, ví dụ
    TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/scrapjd_test pytest tests/test_pg_migrations.py
Test tự tạo/xoá các database tạm tên <db>_mig_<id> (cần quyền CREATEDB, PostgreSQL >= 13). Không đặt
biến này thì cả file được bỏ qua. Từ chối chạy nếu tên database không chứa "test".
"""
import os
import pathlib
import uuid
from contextlib import contextmanager
from urllib.parse import urlparse

import psycopg2
import pytest
from psycopg2 import sql as pgsql

import db

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SQL_DIR = pathlib.Path(__file__).resolve().parent.parent / "sql"
_SCHEMA = str(_SQL_DIR / "schema.sql")
_BASELINE = str(_SQL_DIR / "baseline" / "0036_schema.sql")

# Bảng ghi dấu migration là cơ chế theo dõi, không thuộc schema nghiệp vụ.
_IGNORED_TABLES = ("schema_migrations",)


# ----------------------------------------------------------------------
# Database tạm
# ----------------------------------------------------------------------
@contextmanager
def _temp_database():
    """Tạo một database trống riêng, trả URL của nó, xoá khi xong."""
    parsed = urlparse(TEST_DATABASE_URL)
    base = parsed.path.lstrip("/")
    if "test" not in base.lower():
        pytest.fail(f"Từ chối chạy: database '{base}' không chứa 'test' trong tên.")
    name = f"{base}_mig_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(TEST_DATABASE_URL)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(pgsql.SQL("CREATE DATABASE {}").format(pgsql.Identifier(name)))
        try:
            yield parsed._replace(path=f"/{name}").geturl()
        finally:
            with admin.cursor() as cur:
                cur.execute(
                    pgsql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(pgsql.Identifier(name))
                )
    finally:
        admin.close()


@contextmanager
def _connect(url):
    conn = psycopg2.connect(url)
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


# ----------------------------------------------------------------------
# Chụp schema từ catalog
# ----------------------------------------------------------------------
_QUERIES = {
    # Khoá theo (bảng, tên cột), KHÔNG theo vị trí cột.
    "columns": """
        SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
               pg_get_expr(d.adbin, d.adrelid), a.attidentity::text, a.attgenerated::text,
               col_description(c.oid, a.attnum)
          FROM pg_attribute a
          JOIN pg_class c ON c.oid = a.attrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
          LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
         WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm')
           AND a.attnum > 0 AND NOT a.attisdropped AND c.relname <> ALL(%(ignored)s)
    """,
    "tables": """
        SELECT c.relname, c.relkind::text, obj_description(c.oid, 'pg_class')
          FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm')
           AND c.relname <> ALL(%(ignored)s)
    """,
    "constraints": """
        SELECT c.relname, k.conname, pg_get_constraintdef(k.oid)
          FROM pg_constraint k
          JOIN pg_class c ON c.oid = k.conrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relname <> ALL(%(ignored)s)
    """,
    "indexes": """
        SELECT tablename, indexname, indexdef FROM pg_indexes
         WHERE schemaname = 'public' AND tablename <> ALL(%(ignored)s)
    """,
    # Nhãn enum so như TẬP (thứ tự thêm bằng ADD VALUE có thể khác thứ tự trong schema.sql).
    "enums": """
        SELECT t.typname, e.enumlabel
          FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
          JOIN pg_namespace n ON n.oid = t.typnamespace
         WHERE n.nspname = 'public'
    """,
    # Bỏ hàm do extension (pg_trgm, pgcrypto) tạo.
    "functions": """
        SELECT p.oid::regprocedure::text, pg_get_functiondef(p.oid)
          FROM pg_proc p
         WHERE p.pronamespace = 'public'::regnamespace AND p.prokind IN ('f', 'p')
           AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.objid = p.oid AND d.deptype = 'e')
    """,
    "triggers": """
        SELECT c.relname, t.tgname, pg_get_triggerdef(t.oid)
          FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE NOT t.tgisinternal AND n.nspname = 'public' AND c.relname <> ALL(%(ignored)s)
    """,
    "views": "SELECT viewname, definition FROM pg_views WHERE schemaname = 'public'",
    "sequences": """
        SELECT sequencename, data_type::text, start_value::text, increment_by::text
          FROM pg_sequences WHERE schemaname = 'public'
    """,
    "extensions": "SELECT extname FROM pg_extension",
}


def snapshot_schema(conn) -> dict:
    """{loại đối tượng: set các tuple mô tả}. So hai snapshot bằng diff_snapshots()."""
    out = {}
    with conn.cursor() as cur:
        for kind, query in _QUERIES.items():
            cur.execute(query, {"ignored": list(_IGNORED_TABLES)})
            out[kind] = {tuple(None if v is None else str(v) for v in row) for row in cur.fetchall()}
    conn.rollback()
    return out


def diff_snapshots(a: dict, b: dict, a_name: str, b_name: str) -> list:
    """Danh sách dòng mô tả khác biệt; rỗng nếu hai schema giống nhau."""
    lines = []
    for kind in _QUERIES:
        for row in sorted(a[kind] - b[kind]):
            lines.append(f"[{kind}] chỉ có ở {a_name}: {row}")
        for row in sorted(b[kind] - a[kind]):
            lines.append(f"[{kind}] chỉ có ở {b_name}: {row}")
    return lines


def _schema_sql_snapshot() -> dict:
    with _temp_database() as url, _connect(url) as conn:
        db.apply_schema(conn, _SCHEMA)
        return snapshot_schema(conn)


def _baseline_plus_migrations_snapshot(migrations_dir: str) -> tuple:
    """(snapshot, danh sách migration đã áp dụng): baseline đóng băng + apply_migrations()."""
    with _temp_database() as url, _connect(url) as conn:
        db.apply_schema(conn, _BASELINE)
        # DB baseline coi như đã có 36 migration cũ (đúng với DB thật đã `init-db`/`--baseline`).
        legacy = [f for f in db.connection._list_migration_files(str(_SQL_DIR)) if f.startswith("migration_")]
        db.connection._ensure_schema_migrations_table(conn)
        with conn.cursor() as cur:
            for f in legacy:
                cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (f,))
        conn.commit()
        applied = db.apply_migrations(conn, migrations_dir)
        return snapshot_schema(conn), applied


# ----------------------------------------------------------------------
# Test
# ----------------------------------------------------------------------
def test_schema_sql_matches_baseline_plus_numbered_migrations():
    """Nếu test này đỏ: schema.sql và migration đang mô tả hai schema khác nhau."""
    from_schema = _schema_sql_snapshot()
    from_migrations, _applied = _baseline_plus_migrations_snapshot(str(_SQL_DIR))
    diff = diff_snapshots(from_schema, from_migrations, "schema.sql", "baseline+migration")
    assert diff == [], (
        "schema.sql và (baseline 0036 + migration đánh số) KHÁC NHAU. Thay đổi schema phải đi đủ "
        "hai chỗ: file sql/NNNN_*.sql VÀ sql/schema.sql. Khác biệt:\n  " + "\n  ".join(diff)
    )


def test_detector_catches_migration_missing_from_schema_sql(tmp_path):
    """Đối chứng: migration thêm cột mà schema.sql không có -> phải bị phát hiện."""
    (tmp_path / "0037_add_probe_column.sql").write_text(
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS drift_probe TEXT;"
    )
    from_schema = _schema_sql_snapshot()
    from_migrations, applied = _baseline_plus_migrations_snapshot(str(tmp_path))
    assert applied == ["0037_add_probe_column.sql"]
    diff = diff_snapshots(from_schema, from_migrations, "schema.sql", "baseline+migration")
    assert any("drift_probe" in line and "baseline+migration" in line for line in diff), diff


def test_detector_catches_schema_sql_change_without_migration():
    """Đối chứng chiều ngược: schema.sql có thứ mà baseline + migration không tạo ra."""
    with _temp_database() as url, _connect(url) as conn:
        db.apply_schema(conn, _BASELINE)
        base = snapshot_schema(conn)
        with conn.cursor() as cur:
            cur.execute("CREATE INDEX drift_probe_idx ON companies (company_name)")
        conn.commit()
        changed = snapshot_schema(conn)
    diff = diff_snapshots(changed, base, "schema.sql", "baseline+migration")
    assert any("drift_probe_idx" in line for line in diff), diff


def test_column_order_does_not_cause_false_drift():
    """Cột thêm bằng ALTER nằm cuối bảng, trong schema.sql nằm giữa CREATE TABLE: không là lệch."""
    with _temp_database() as u1, _connect(u1) as c1, _temp_database() as u2, _connect(u2) as c2:
        with c1.cursor() as cur:
            cur.execute("CREATE TABLE t (a INT, b INT, c INT)")
        with c2.cursor() as cur:
            cur.execute("CREATE TABLE t (a INT, c INT); ALTER TABLE t ADD COLUMN b INT;")
        c1.commit()
        c2.commit()
        assert diff_snapshots(snapshot_schema(c1), snapshot_schema(c2), "1", "2") == []


def test_schema_sql_is_idempotent():
    """schema.sql chạy lại nhiều lần không lỗi và không đổi schema (init-db trên DB đã có)."""
    with _temp_database() as url, _connect(url) as conn:
        db.apply_schema(conn, _SCHEMA)
        first = snapshot_schema(conn)
        db.apply_schema(conn, _SCHEMA)
        assert diff_snapshots(first, snapshot_schema(conn), "lần 1", "lần 2") == []


def test_init_db_flow_leaves_no_pending_migration():
    """init-db = apply_schema + baseline_migrations: sau đó `migrate --check` phải sạch, kể cả
    migration đánh số (schema.sql đã chứa kết quả của chúng)."""
    with _temp_database() as url, _connect(url) as conn:
        db.apply_schema(conn, _SCHEMA)
        db.baseline_migrations(conn, str(_SQL_DIR))
        assert db.list_pending_migrations(conn, str(_SQL_DIR)) == []


def test_numbered_migrations_run_in_number_order_and_are_recorded(tmp_path):
    (tmp_path / "0038_second.sql").write_text(
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS mig_second TEXT DEFAULT 'x';"
        "UPDATE companies SET mig_second = mig_first WHERE mig_first IS NOT NULL;"
    )
    (tmp_path / "0037_first.sql").write_text(
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS mig_first TEXT;"
    )
    with _temp_database() as url, _connect(url) as conn:
        db.apply_schema(conn, _SCHEMA)
        # 0038 dùng cột do 0037 tạo: chỉ đúng nếu chạy theo SỐ, không theo thứ tự tạo file/tên.
        applied = db.apply_migrations(conn, str(tmp_path))
        assert applied == ["0037_first.sql", "0038_second.sql"]
        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM schema_migrations ORDER BY applied_at, filename")
            assert [r[0] for r in cur.fetchall()] == ["0037_first.sql", "0038_second.sql"]
        # Chạy lại: không còn gì.
        assert db.apply_migrations(conn, str(tmp_path)) == []


def test_failing_numbered_migration_stops_and_is_not_recorded(tmp_path):
    (tmp_path / "0037_ok.sql").write_text("ALTER TABLE companies ADD COLUMN IF NOT EXISTS mig_ok TEXT;")
    (tmp_path / "0038_broken.sql").write_text("SELECT * FROM bang_khong_ton_tai;")
    (tmp_path / "0039_after.sql").write_text("ALTER TABLE companies ADD COLUMN IF NOT EXISTS mig_after TEXT;")
    with _temp_database() as url, _connect(url) as conn:
        db.apply_schema(conn, _SCHEMA)
        with pytest.raises(psycopg2.errors.UndefinedTable):
            db.apply_migrations(conn, str(tmp_path))
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT filename FROM schema_migrations")
            recorded = {r[0] for r in cur.fetchall()}
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'companies' AND column_name LIKE 'mig_%'"
            )
            cols = {r[0] for r in cur.fetchall()}
        assert recorded == {"0037_ok.sql"}           # file hỏng không được ghi dấu
        assert cols == {"mig_ok"}                      # file sau file hỏng không chạy
