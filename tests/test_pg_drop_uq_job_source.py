"""
C2 nửa 2/2 trên POSTGRES THẬT: migration 0044 gỡ uq_job_source UNIQUE (job_id, source_url) của job_sources_log.

Chứng minh:
  - ràng buộc cũ mất, ràng buộc thay thế UNIQUE (source_url) còn nguyên và vẫn chặn một URL nằm ở hai job;
  - dữ liệu không đổi;
  - chạy lại file không làm gì (idempotent);
  - thiếu UNIQUE (source_url) thì DỪNG với thông báo và không gỡ gì (không để bảng trống ràng buộc chống trùng);
  - có giao dịch khác giữ khoá thì báo hết thời gian chờ khoá, không đổi gì, chạy lại được sau đó;
  - các đường ghi của code hiện tại (tạo job, link tin đăng lại, gộp job) chạy được sau khi ràng buộc đã gỡ,
    tức là code không còn phụ thuộc vào nó; còn trước migration thì chúng cũng chạy (an toàn cho cả hai thứ tự
    với code từ C2 nửa 1/2 trở lên);
  - schema.sql cho cùng kết quả (test_pg_migrations đã so toàn bộ schema; ở đây khoá thêm tên ràng buộc).

Cách chạy như tests/test_pg_migrations.py: đặt TEST_DATABASE_URL (tên database chứa "test"); không đặt thì bỏ qua.
"""
import uuid
from datetime import date

import psycopg2
import psycopg2.errors
import pytest

from scrapjd import db
from pg_migration_helpers import (
    SQL_DIR,
    connect,
    legacy_db_before,
    migrate_one,
    requires_pg,
    temp_database,
)

pytestmark = requires_pg

_MIGRATION = "0044_drop_uq_job_source.sql"


def _constraints(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT conname FROM pg_constraint WHERE conrelid = 'job_sources_log'::regclass "
                    "AND contype = 'u' ORDER BY conname")
        out = [r[0] for r in cur.fetchall()]
    conn.rollback()
    return out


def _company(conn):
    cid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)", (cid, f"Cty {cid[:8]}"))
    conn.commit()
    return cid


def _insert_job(conn, url=None):
    job_id = db.insert_job(
        conn, company_id=_company(conn), job_title="Data Analyst", matching_industry="Data", level_id=None,
        province_id=None, work_type=None, currency="VNĐ", salary_min=None, salary_max=None,
        salary_type="NEGOTIABLE", source_url=url or f"https://example.com/{uuid.uuid4()}", source_name="Fake",
        deadline=date(2099, 12, 31), detail_fetched=True)
    conn.commit()
    return job_id


def test_before_migration_both_unique_constraints_exist(tmp_path):
    with temp_database() as url, connect(url) as conn:
        legacy_db_before(conn, tmp_path, 44)
        assert _constraints(conn) == ["uq_job_source", "uq_job_sources_log_source_url"]


def test_migration_drops_only_the_redundant_constraint_and_keeps_data(tmp_path):
    with temp_database() as url, connect(url) as conn:
        legacy_db_before(conn, tmp_path, 44)
        job = _insert_job(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM job_sources_log ORDER BY source_url")
            before = cur.fetchall()
        conn.rollback()
        assert migrate_one(conn, tmp_path, _MIGRATION) == [_MIGRATION]
        assert _constraints(conn) == ["uq_job_sources_log_source_url"]
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM job_sources_log ORDER BY source_url")
            assert cur.fetchall() == before and len(before) == 1
        conn.rollback()
        assert job


def test_url_still_cannot_belong_to_two_jobs_after_migration(tmp_path):
    with temp_database() as url, connect(url) as conn:
        legacy_db_before(conn, tmp_path, 44)
        migrate_one(conn, tmp_path, _MIGRATION)
        first = _insert_job(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT source_url FROM job_postings WHERE job_id = %s", (first,))
            taken = cur.fetchone()[0]
        conn.rollback()
        with pytest.raises(psycopg2.errors.UniqueViolation):
            _insert_job(conn, url=taken)
        conn.rollback()


def test_migration_is_idempotent(tmp_path):
    with temp_database() as url, connect(url) as conn:
        legacy_db_before(conn, tmp_path, 44)
        migrate_one(conn, tmp_path, _MIGRATION)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM schema_migrations WHERE filename = %s", (_MIGRATION,))
        conn.commit()
        assert migrate_one(conn, tmp_path, _MIGRATION) == [_MIGRATION]      # chạy lại: không lỗi, không đổi gì
        assert _constraints(conn) == ["uq_job_sources_log_source_url"]


def test_migration_refuses_when_the_replacement_unique_is_missing(tmp_path):
    with temp_database() as url, connect(url) as conn:
        legacy_db_before(conn, tmp_path, 44)
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE job_sources_log DROP CONSTRAINT uq_job_sources_log_source_url")
        conn.commit()
        with pytest.raises(psycopg2.Error) as exc:
            migrate_one(conn, tmp_path, _MIGRATION)
        assert "Thiếu UNIQUE (source_url)" in str(exc.value)
        conn.rollback()
        assert _constraints(conn) == ["uq_job_source"]                       # không gỡ gì


def test_migration_times_out_instead_of_waiting_when_crawl_holds_a_lock(tmp_path):
    with temp_database() as url, connect(url) as conn, connect(url) as other:
        legacy_db_before(conn, tmp_path, 44)
        with other.cursor() as cur:
            cur.execute("SELECT count(*) FROM job_sources_log")               # giao dịch khác đang đọc, chưa kết thúc
        with pytest.raises(psycopg2.errors.LockNotAvailable):
            migrate_one(conn, tmp_path, _MIGRATION, sql_edit=lambda t: t.replace("'10s'", "'300ms'"))
        conn.rollback()
        assert _constraints(conn) == ["uq_job_source", "uq_job_sources_log_source_url"]
        other.rollback()
        assert migrate_one(conn, tmp_path, _MIGRATION) == [_MIGRATION]


@pytest.mark.parametrize("migrated", [False, True])
def test_current_write_paths_work_before_and_after_the_migration(tmp_path, migrated):
    """Code hiện tại không còn ON CONFLICT (job_id, source_url): tạo job, link tin đăng lại và đóng/mở job chạy
    đủ ở cả hai trạng thái ràng buộc, nên thứ tự push và migrate không quan trọng với code từ C2 nửa 1/2."""
    with temp_database() as url, connect(url) as conn:
        legacy_db_before(conn, tmp_path, 44)
        if migrated:
            migrate_one(conn, tmp_path, _MIGRATION)
        job = _insert_job(conn)
        new = f"https://example.com/{uuid.uuid4()}"
        assert db.link_repost_source(conn, job, source_name="Fake", source_url=new, deadline=date(2100, 1, 1))
        assert db.update_job(conn, job, job_status="CLOSED")
        assert db.update_job(conn, job, job_status="OPEN")
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*), count(*) FILTER (WHERE listing_status = 'OPEN') "
                        "FROM job_sources_log WHERE job_id = %s", (job,))
            assert cur.fetchone() == (2, 2)      # đóng rồi mở lại bằng tay: cả hai listing đóng vì staff đều mở lại
        conn.rollback()


def test_schema_sql_has_no_uq_job_source(tmp_path):
    text = (SQL_DIR / "schema.sql").read_text(encoding="utf-8")
    assert "CONSTRAINT uq_job_source " not in text
    assert "CONSTRAINT uq_job_sources_log_source_url UNIQUE (source_url)" in text
