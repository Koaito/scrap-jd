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
import hashlib
import os
import pathlib
import re
from unittest.mock import MagicMock

import pytest

from scrapjd import db


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


class TestBaselineMigrations:
    def test_marks_pending_without_running_their_sql(self, migrations_dir):
        conn = _FakeConn(applied_filenames=[])
        marked = db.baseline_migrations(conn, migrations_dir)
        assert marked == ["migration_aaa_first.sql", "migration_zzz_second.sql"]
        executed_sql = [sql for sql, _ in conn._cursor.executed]
        # Chỉ có CREATE TABLE schema_migrations + INSERT ghi log; SQL của file
        # migration (ALTER TABLE ...) KHÔNG được chạy.
        assert not any("aaa_col" in sql or "zzz_col" in sql for sql in executed_sql)
        inserted = {
            params[0] for sql, params in conn._cursor.executed
            if "INSERT INTO schema_migrations" in sql
        }
        assert inserted == set(marked)

    def test_skips_files_already_applied(self, migrations_dir):
        conn = _FakeConn(applied_filenames=["migration_aaa_first.sql"])
        assert db.baseline_migrations(conn, migrations_dir) == ["migration_zzz_second.sql"]

    def test_except_files_stay_pending(self, migrations_dir):
        conn = _FakeConn(applied_filenames=[])
        marked = db.baseline_migrations(
            conn, migrations_dir, except_files=["migration_zzz_second.sql"]
        )
        assert marked == ["migration_aaa_first.sql"]
        inserted = {
            params[0] for sql, params in conn._cursor.executed
            if "INSERT INTO schema_migrations" in sql
        }
        assert "migration_zzz_second.sql" not in inserted

    def test_nothing_pending_returns_empty_and_commits_nothing_new(self, migrations_dir):
        conn = _FakeConn(applied_filenames=[
            "migration_aaa_first.sql", "migration_zzz_second.sql",
        ])
        assert db.baseline_migrations(conn, migrations_dir) == []
        insert_calls = [
            sql for sql, _ in conn._cursor.executed
            if "INSERT INTO schema_migrations" in sql
        ]
        assert insert_calls == []


class TestSchemaCoversMigrations:
    """schema.sql là nguồn dựng DB mới và `init-db` coi mọi migration hiện có
    là đã áp dụng. Nếu một migration tạo bảng mà schema.sql không có thì DB
    mới thiếu bảng đó mà không ai biết."""

    _SQL_DIR = os.path.join(os.path.dirname(__file__), "..", "sql")

    def _read(self, name):
        with open(os.path.join(self._SQL_DIR, name), encoding="utf-8") as f:
            return f.read()

    def test_every_table_created_by_a_migration_is_in_schema(self):
        schema = self._read("schema.sql")
        pattern = re.compile(r"CREATE TABLE IF NOT EXISTS\s+([a-z_]+)", re.IGNORECASE)
        schema_tables = {m.group(1).lower() for m in pattern.finditer(schema)}
        missing = {}
        for name in sorted(os.listdir(self._SQL_DIR)):
            if not (name.startswith("migration_") and name.endswith(".sql")):
                continue
            for m in pattern.finditer(self._read(name)):
                table = m.group(1).lower()
                if table not in schema_tables:
                    missing.setdefault(name, []).append(table)
        assert missing == {}

    def test_retired_drop_products_services_migration_is_a_no_op(self):
        """Cột companies.products_services đang được code ghi vào; migration
        này không được xoá nó."""
        statements = "\n".join(
            line for line in self._read("migration_drop_products_services.sql").splitlines()
            if not line.strip().startswith("--")
        )
        assert "DROP" not in statements.upper()
        assert "products_services TEXT" in self._read("schema.sql")


# ======================================================================
# Migration đánh số (NNNN_*.sql) — D1
# ======================================================================
_SQL_DIR = pathlib.Path(__file__).resolve().parent.parent / "sql"

# 36 migration_*.sql cũ = baseline, ĐÓNG BĂNG. Thay đổi schema mới đi theo kiểu NNNN_*.sql.
_LEGACY_MIGRATIONS = frozenset({
    "migration_add_application_audit_log.sql",
    "migration_add_applications_saved_jobs.sql",
    "migration_add_audit_columns.sql",
    "migration_add_audit_logs.sql",
    "migration_add_auth.sql",
    "migration_add_chat_messages.sql",
    "migration_add_company_soft_delete.sql",
    "migration_add_crawl_batches.sql",
    "migration_add_crawl_progress_logs.sql",
    "migration_add_crawl_runs.sql",
    "migration_add_crawl_snapshots.sql",
    "migration_add_cv_url.sql",
    "migration_add_email_templates.sql",
    "migration_add_email_verification.sql",
    "migration_add_import_export.sql",
    "migration_add_job_level_signals.sql",
    "migration_add_job_level_source.sql",
    "migration_add_maintenance_runs.sql",
    "migration_add_merge_job_audit_action.sql",
    "migration_add_partnership_potential.sql",
    "migration_add_password_reset.sql",
    "migration_add_phone_track.sql",
    "migration_add_role_hierarchy.sql",
    "migration_add_salary_period.sql",
    "migration_add_single_session.sql",
    "migration_add_skip_updated_at_flag.sql",
    "migration_add_source_detail_checked_at.sql",
    "migration_add_source_profile_url.sql",
    "migration_add_tax_id.sql",
    "migration_add_work_type_deadline.sql",
    "migration_add_work_type_flexible.sql",
    "migration_drop_products_services.sql",
    "migration_remove_expired_job_status.sql",
    "migration_rename_needs_manual_check_stat_key.sql",
    "migration_rename_ss_team_members.sql",
    "migration_update_provinces_2025.sql",
})

# sha256 của sql/baseline/0036_schema.sql = bản sao NGUYÊN VĂN của sql/schema.sql ở thời điểm
# baseline (36 migration cũ). Test PG so schema.sql với (file này + các migration đánh số), nên
# file này mà bị sửa thì phép so mất ý nghĩa.
_BASELINE_SHA256 = "22d6aa475f68812d1c5ea2ef7a32305df109826cbea6a27d28ebd1d38ff1c97b"


class TestNumberedMigrationListing:
    def test_numbered_files_listed_by_number_after_all_legacy(self, tmp_path):
        for name in (
            "0038_second.sql", "0037_first.sql", "migration_zzz_legacy.sql",
            "migration_aaa_legacy.sql", "schema.sql", "README.md",
        ):
            (tmp_path / name).write_text("SELECT 1;")
        files = db.connection._list_migration_files(str(tmp_path))
        # Sort theo tên thuần sẽ cho 0037 và 0038 đứng TRƯỚC migration_* ('0' < 'm').
        assert files == [
            "migration_aaa_legacy.sql", "migration_zzz_legacy.sql",
            "0037_first.sql", "0038_second.sql",
        ]

    def test_numbers_compared_as_integers(self, tmp_path):
        for name in ("0100_c.sql", "0037_a.sql", "0099_b.sql"):
            (tmp_path / name).write_text("SELECT 1;")
        assert db.connection._list_migration_files(str(tmp_path)) == [
            "0037_a.sql", "0099_b.sql", "0100_c.sql",
        ]

    def test_duplicate_number_raises(self, tmp_path):
        (tmp_path / "0037_add_a.sql").write_text("SELECT 1;")
        (tmp_path / "0037_add_b.sql").write_text("SELECT 1;")
        with pytest.raises(ValueError, match="0037"):
            db.connection._list_migration_files(str(tmp_path))

    @pytest.mark.parametrize("bad", [
        "37_short.sql", "0037-dash.sql", "0037_Upper.sql", "0037_.sql", "00370_five.sql",
        "0037_trailing_.sql", "0037_x.SQL", "0037_x.sql.bak",
    ])
    def test_misnamed_files_are_not_migrations(self, tmp_path, bad):
        (tmp_path / bad).write_text("SELECT 1;")
        assert db.connection._list_migration_files(str(tmp_path)) == []

    def test_pending_includes_numbered_and_skips_applied_ones(self, tmp_path):
        (tmp_path / "migration_old.sql").write_text("SELECT 1;")
        (tmp_path / "0037_new.sql").write_text("SELECT 1;")
        (tmp_path / "0038_newer.sql").write_text("SELECT 1;")
        conn = _FakeConn(applied_filenames=["migration_old.sql", "0037_new.sql"])
        assert db.list_pending_migrations(conn, str(tmp_path)) == ["0038_newer.sql"]


class TestRepoMigrationFiles:
    """Kiểm tra thư mục sql/ THẬT của repo (không cần DB)."""

    def test_legacy_migrations_are_frozen(self):
        found = {p.name for p in _SQL_DIR.glob("migration_*.sql")}
        assert found == _LEGACY_MIGRATIONS, (
            "Không thêm/sửa tên/xoá file migration_*.sql: 36 file cũ là baseline đã đóng băng. "
            "Thay đổi schema mới phải là file NNNN_<mô_tả>.sql (bắt đầu từ 0037), xem "
            "sql/README_MIGRATIONS.md. Lệch: "
            f"thừa {sorted(found - _LEGACY_MIGRATIONS)}, thiếu {sorted(_LEGACY_MIGRATIONS - found)}"
        )

    def test_every_sql_file_has_a_known_role(self):
        strange = [
            p.name for p in _SQL_DIR.glob("*.sql")
            if p.name != "schema.sql"
            and p.name not in _LEGACY_MIGRATIONS
            and not db.connection._NUMBERED_MIGRATION_RE.match(p.name)
        ]
        assert strange == [], (
            f"File .sql trong sql/ không phải schema.sql, migration cũ, hay NNNN_<mô_tả>.sql "
            f"(chữ thường/số/gạch dưới): {strange}. File sai tên sẽ KHÔNG bao giờ được chạy."
        )

    def test_numbered_migrations_unique_and_contiguous_from_0037(self):
        numbers = sorted(
            int(db.connection._NUMBERED_MIGRATION_RE.match(p.name).group(1))
            for p in _SQL_DIR.glob("*.sql")
            if db.connection._NUMBERED_MIGRATION_RE.match(p.name)
        )
        assert numbers == list(range(37, 37 + len(numbers))), (
            f"Số migration phải liên tục từ 0037, không trùng, không hở. Hiện có: {numbers}"
        )

    def test_baseline_schema_is_untouched(self):
        data = (_SQL_DIR / "baseline" / "0036_schema.sql").read_bytes()
        assert hashlib.sha256(data).hexdigest() == _BASELINE_SHA256, (
            "sql/baseline/0036_schema.sql đã bị sửa. File này là schema.sql NGUYÊN VĂN tại baseline "
            "(sau 36 migration cũ) và không được đổi; muốn đổi schema hãy thêm migration NNNN_*.sql "
            "và cập nhật sql/schema.sql."
        )
