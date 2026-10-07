"""
A3 nửa 1/2, kiểm tra TĨNH (không cần Postgres) rằng sql/0039_add_job_dedup_key.sql và sql/schema.sql
đang nói cùng một điều. Test thật so hai bên là tests/test_pg_migrations.py (cần TEST_DATABASE_URL);
test này chỉ để máy không có Postgres cũng bắt được lệch phổ biến nhất: sửa hàm/trigger ở một nơi
mà quên nơi kia, hoặc làm khoá khác công thức chuẩn hoá của content_hash.
"""
import pathlib
import re

_SQL_DIR = pathlib.Path(__file__).resolve().parent.parent / "sql"
_MIGRATION = (_SQL_DIR / "0039_add_job_dedup_key.sql").read_text(encoding="utf-8")
# Chỉ phần lệnh, bỏ dòng chú thích (chú thích có nhắc tên lệnh nên làm lệch phép tìm vị trí).
_MIGRATION_CODE = "\n".join(l for l in _MIGRATION.splitlines() if not l.lstrip().startswith("--"))
_SCHEMA = (_SQL_DIR / "schema.sql").read_text(encoding="utf-8")

# Cùng biểu thức chuẩn hoá tiêu đề với generate_job_hash(): content_hash và dedup_key không được lệch.
_NORM_TITLE = "lower(regexp_replace(trim(p_job_title), '\\s+', ' ', 'g'))"


def _block(text: str) -> str:
    start = text.index("CREATE OR REPLACE FUNCTION job_dedup_key_version()")
    end_marker = "FOR EACH ROW EXECUTE FUNCTION trg_set_job_dedup_key();"
    return text[start:text.index(end_marker, start) + len(end_marker)]


def test_function_and_trigger_block_is_identical_in_migration_and_schema():
    # pg_get_functiondef (dùng trong test_pg_migrations) so thân hàm từng ký tự.
    assert _block(_MIGRATION) == _block(_SCHEMA)


def test_key_uses_the_same_title_normalisation_as_content_hash():
    hash_fn = _SCHEMA[_SCHEMA.index("CREATE OR REPLACE FUNCTION generate_job_hash("):]
    hash_fn = hash_fn[:hash_fn.index("$$ LANGUAGE plpgsql IMMUTABLE;")]
    assert _NORM_TITLE in hash_fn
    assert _NORM_TITLE in _block(_SCHEMA)


def test_key_does_not_take_level_as_input():
    signature = re.search(r"FUNCTION job_dedup_key\((.*?)\)\s*RETURNS", _SCHEMA, re.S).group(1)
    assert "level" not in signature.lower()
    assert [p.split()[0] for p in signature.split(",")] == [
        "p_company_id", "p_job_title", "p_province_id"]


def test_schema_declares_both_columns_not_null_and_the_index():
    assert re.search(r"dedup_key\s+VARCHAR\(64\) NOT NULL,", _SCHEMA)
    assert re.search(r"dedup_key_version SMALLINT NOT NULL,", _SCHEMA)
    assert "idx_job_postings_dedup_key" in _SCHEMA and "ON job_postings(dedup_key);" in _SCHEMA
    assert "COMMENT ON COLUMN job_postings.dedup_key IS" in _SCHEMA
    assert "COMMENT ON COLUMN job_postings.dedup_key_version IS" in _SCHEMA


def test_migration_is_written_to_be_rerunnable_and_does_not_touch_updated_at():
    assert "ADD COLUMN IF NOT EXISTS dedup_key VARCHAR(64)" in _MIGRATION
    assert "ADD COLUMN IF NOT EXISTS dedup_key_version SMALLINT" in _MIGRATION
    assert "DROP TRIGGER IF EXISTS set_job_dedup_key ON job_postings;" in _MIGRATION
    assert "CREATE INDEX IF NOT EXISTS idx_job_postings_dedup_key" in _MIGRATION
    # cờ giữ updated_at phải được bật TRƯỚC câu backfill
    code = _MIGRATION_CODE
    assert code.index("app.skip_updated_at") < code.index("UPDATE job_postings")
    # NOT NULL chỉ đặt SAU backfill (đặt trước thì bảng đang có dữ liệu sẽ lỗi)
    assert code.index("UPDATE job_postings") < code.index("SET NOT NULL")


def test_migration_does_not_touch_content_hash_or_the_existing_view():
    code = _MIGRATION_CODE
    assert "v_duplicate_job_candidates" not in code
    assert "generate_job_hash" not in code and "set_job_hash" not in code
