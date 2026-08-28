"""
Test cho cơ chế tracking migration (schema_migrations table) thêm 08/2026
— xem docstring db/connection.py::apply_migrations() để biết bối cảnh
(trước đây 29 file migration_*.sql rời rạc không có cách nào biết DB
nào đã chạy file nào).

Dùng conn/cursor mock TỰ VIẾT (không dùng fixture mock_conn chung ở
conftest.py — MagicMock mặc định không kiểm soát được cur.fetchall(),
cần giả lập tình huống DB "đã áp dụng 1 số migration, còn thiếu số
khác") + file migration THẬT trong thư mục tạm (tmp_path) để test đúng
hành vi đọc file, không mock open()/os.listdir() (dễ test sai logic
thật).
"""
from unittest.mock import MagicMock

import pytest

import db


class _FakeCursor:
    """Cursor giả lập tối thiểu: ghi lại mọi execute() để assert sau,
    fetchall() trả về đúng thứ đã set qua `applied_filenames`."""

    def __init__(self, applied_filenames):
        self.applied_filenames = applied_filenames
        self.executed = []  # list[(sql, params)]

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return [(f,) for f in self.applied_filenames]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeConn:
    """conn giả lập — mỗi lần gọi conn.cursor() trả về CÙNG 1 _FakeCursor
    (để executed accumulate xuyên suốt nhiều `with conn.cursor() as cur`),
    applied_filenames là danh sách filename đã có sẵn trong
    schema_migrations TRƯỚC khi gọi hàm đang test."""

    def __init__(self, applied_filenames=()):
        self._cursor = _FakeCursor(list(applied_filenames))
        self.commit = MagicMock()

    def cursor(self):
        return self._cursor


@pytest.fixture
def migrations_dir(tmp_path):
    """2 migration THẬT (idempotent, giống quy ước cả repo) trong thư
    mục tạm — tên đặt CỐ Ý không theo thứ tự bảng chữ cái tự nhiên để
    test luôn assertion về sort theo tên (_list_migration_files)."""
    (tmp_path / "migration_zzz_second.sql").write_text(
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS zzz_col TEXT;"
    )
    (tmp_path / "migration_aaa_first.sql").write_text(
        "ALTER TABLE companies ADD COLUMN IF NOT EXISTS aaa_col TEXT;"
    )
    # File KHÔNG đúng quy ước tên (không bắt đầu "migration_" hoặc
    # không phải .sql) phải bị BỎ QUA — không phải migration.
    (tmp_path / "README.md").write_text("not a migration")
    (tmp_path / "helper_not_a_migration.sql").write_text("SELECT 1;")
    return str(tmp_path)


class TestListPendingMigrations:
    def test_returns_all_when_none_applied(self, migrations_dir):
        conn = _FakeConn(applied_filenames=[])
        pending = db.list_pending_migrations(conn, migrations_dir)
        # Sort theo TÊN, không phải thứ tự tạo file trong fixture.
        assert pending == ["migration_aaa_first.sql", "migration_zzz_second.sql"]

    def test_ignores_non_migration_files(self, migrations_dir):
        conn = _FakeConn(applied_filenames=[])
        pending = db.list_pending_migrations(conn, migrations_dir)
        assert "README.md" not in pending
        assert "helper_not_a_migration.sql" not in pending

    def test_excludes_already_applied(self, migrations_dir):
        conn = _FakeConn(applied_filenames=["migration_aaa_first.sql"])
        pending = db.list_pending_migrations(conn, migrations_dir)
        assert pending == ["migration_zzz_second.sql"]

    def test_empty_when_all_applied(self, migrations_dir):
        conn = _FakeConn(applied_filenames=[
            "migration_aaa_first.sql", "migration_zzz_second.sql",
        ])
        pending = db.list_pending_migrations(conn, migrations_dir)
        assert pending == []

    def test_creates_tracking_table_if_missing(self, migrations_dir):
        """list_pending_migrations() PHẢI tự tạo bảng schema_migrations
        nếu chưa có (DB lần đầu adopt tính năng này) — không được raise
        lỗi 'relation does not exist'."""
        conn = _FakeConn(applied_filenames=[])
        db.list_pending_migrations(conn, migrations_dir)
        create_table_calls = [
            sql for sql, _ in conn._cursor.executed
            if "CREATE TABLE IF NOT EXISTS schema_migrations" in sql
        ]
        assert len(create_table_calls) == 1


class TestApplyMigrations:
    def test_applies_only_pending_in_name_order(self, migrations_dir):
        conn = _FakeConn(applied_filenames=["migration_aaa_first.sql"])
        applied = db.apply_migrations(conn, migrations_dir)
        assert applied == ["migration_zzz_second.sql"]

    def test_returns_empty_list_when_nothing_pending(self, migrations_dir):
        conn = _FakeConn(applied_filenames=[
            "migration_aaa_first.sql", "migration_zzz_second.sql",
        ])
        applied = db.apply_migrations(conn, migrations_dir)
        assert applied == []
        # Không migration nào chạy -> không có INSERT nào vào schema_migrations.
        insert_calls = [
            sql for sql, _ in conn._cursor.executed
            if "INSERT INTO schema_migrations" in sql
        ]
        assert insert_calls == []

    def test_executes_actual_sql_content_of_each_pending_file(self, migrations_dir):
        conn = _FakeConn(applied_filenames=[])
        db.apply_migrations(conn, migrations_dir)
        executed_sql = [sql for sql, _ in conn._cursor.executed]
        assert any("aaa_col" in sql for sql in executed_sql)
        assert any("zzz_col" in sql for sql in executed_sql)

    def test_records_each_applied_migration_in_tracking_table(self, migrations_dir):
        conn = _FakeConn(applied_filenames=[])
        db.apply_migrations(conn, migrations_dir)
        insert_calls = [
            params for sql, params in conn._cursor.executed
            if "INSERT INTO schema_migrations" in sql
        ]
        recorded_filenames = {params[0] for params in insert_calls}
        assert recorded_filenames == {
            "migration_aaa_first.sql", "migration_zzz_second.sql",
        }

    def test_commits_after_each_migration_not_only_at_end(self, migrations_dir):
        """Mỗi migration commit RIÊNG (không gộp 1 transaction lớn) —
        để migration lỗi ở giữa KHÔNG rollback các migration trước đã
        chạy + ghi log thành công (xem docstring apply_migrations())."""
        conn = _FakeConn(applied_filenames=[])
        db.apply_migrations(conn, migrations_dir)
        # 2 migration pending -> commit() gọi ít nhất 2 lần (có thể thêm
        # 1 lần nữa từ _ensure_schema_migrations_table, không sao).
        assert conn.commit.call_count >= 2
