"""
E3 trên POSTGRES THẬT: migration 0047 (job_sources_log.source_url NOT NULL + CHECK source_url <> '').

Chứng minh:
  - dữ liệu sạch: migration thêm NOT NULL và CHECK, NULL và '' bị DB từ chối, URL thật và manual://<uuid> vẫn ghi
    được, dữ liệu không đổi;
  - có dòng NULL và/hoặc rỗng: migration DỪNG với thông báo có số lượng, ví dụ và câu liệt kê; không xoá/sửa gì,
    không tạo ràng buộc, không ghi dấu "đã áp dụng", và chạy lại được sau khi dữ liệu được xử lý;
  - chạy lại file không làm gì (idempotent);
  - có giao dịch khác giữ khoá thì báo hết thời gian chờ khoá, không đổi gì, chạy lại được sau đó;
  - các đường ghi của code (tạo job, link tin đăng lại, nhập tay) chạy được cả trước lẫn sau migration, nên thứ
    tự push và migrate không quan trọng;
  - schema.sql cho cùng kết quả (test_pg_migrations đã so toàn bộ schema; ở đây khoá thêm tên ràng buộc và cờ
    NOT NULL của cột).

Cách chạy như tests/test_pg_migrations.py: đặt TEST_DATABASE_URL (tên database chứa "test"); không đặt thì bỏ qua.
"""
import uuid
from datetime import date

import psycopg2
import psycopg2.errors
import pytest

from scrapjd import db
from pg_migration_helpers import (
    SCHEMA,
    connect,
    legacy_db_before,
    migrate_one,
    requires_pg,
    temp_database,
)

pytestmark = requires_pg

_MIGRATION = "0047_job_sources_log_source_url_not_null.sql"
_CHECK = "chk_job_sources_log_source_url_not_blank"


def _company(cur):
    cid = str(uuid.uuid4())
    cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)", (cid, f"Cty {cid[:8]}"))
    return cid


def _raw_job(cur, company_id, title="Job"):
    job_id = str(uuid.uuid4())
    cur.execute("INSERT INTO job_postings (job_id, company_id, job_title) VALUES (%s, %s, %s)",
                (job_id, company_id, title))
    return job_id


def _raw_log(cur, job_id, url):
    cur.execute("INSERT INTO job_sources_log (job_id, source_name, source_url) VALUES (%s, 'TopCV', %s)",
                (job_id, url))


def _column_is_not_null(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'job_sources_log' AND column_name = 'source_url'")
        return cur.fetchone()[0] == "NO"


def _has_check(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM pg_constraint WHERE conname = %s AND conrelid = 'job_sources_log'::regclass",
                    (_CHECK,))
        return cur.fetchone()[0] == 1


def _insert_job(conn, url):
    cid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)", (cid, f"Cty {cid[:8]}"))
    return db.insert_job(
        conn, company_id=cid, job_title="Data Analyst", matching_industry="Data", level_id=None, province_id=None,
        work_type=None, currency="VNĐ", salary_min=None, salary_max=None, salary_type="NEGOTIABLE",
        source_url=url, source_name="Fake", deadline=date(2100, 1, 1), detail_fetched=True)


# ============================================================ dữ liệu sạch
def test_migration_adds_not_null_and_check_and_rejects_null_and_blank(tmp_path):
    with temp_database() as url, connect(url) as conn:
        legacy_db_before(conn, tmp_path, 47)
        with conn.cursor() as cur:
            job = _raw_job(cur, _company(cur))
            _raw_log(cur, job, "https://topcv.vn/a")
            _raw_log(cur, job, f"manual://{uuid.uuid4()}")
        conn.commit()
        assert not _column_is_not_null(conn) and not _has_check(conn)

        assert migrate_one(conn, tmp_path, _MIGRATION) == [_MIGRATION]
        assert _column_is_not_null(conn) and _has_check(conn)

        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.NotNullViolation):
                _raw_log(cur, job, None)
        conn.rollback()
        with conn.cursor() as cur:
            with pytest.raises(psycopg2.errors.CheckViolation):
                _raw_log(cur, job, "")
        conn.rollback()
        with conn.cursor() as cur:
            _raw_log(cur, job, "https://topcv.vn/b")                      # URL thật vẫn ghi được
            cur.execute("SELECT count(*) FROM job_sources_log")
            assert cur.fetchone()[0] == 3


def test_migration_is_idempotent(tmp_path):
    with temp_database() as url, connect(url) as conn:
        legacy_db_before(conn, tmp_path, 47)
        assert migrate_one(conn, tmp_path, _MIGRATION) == [_MIGRATION]
        sql_text = (tmp_path / _MIGRATION).read_text(encoding="utf-8")
        with conn.cursor() as cur:
            cur.execute(sql_text)                                          # chạy lại thẳng file, không qua bộ chạy
            cur.execute(sql_text)
        conn.commit()
        assert _column_is_not_null(conn) and _has_check(conn)


# ============================================================ dữ liệu vi phạm
def test_migration_stops_with_a_clear_message_when_rows_have_null_or_blank_url(tmp_path):
    """DB đã có dòng vi phạm: migration không được tự xoá hay sửa, mà phải dừng và nói rõ có bao nhiêu dòng."""
    with temp_database() as url, connect(url) as conn:
        legacy_db_before(conn, tmp_path, 47)
        with conn.cursor() as cur:
            job = _raw_job(cur, _company(cur))
            _raw_log(cur, job, "https://topcv.vn/ok")
            _raw_log(cur, job, None)
            _raw_log(cur, job, None)                                       # NULL được nhiều dòng (UNIQUE không chặn)
            _raw_log(cur, job, "")                                         # '' chỉ được một dòng
        conn.commit()

        with pytest.raises(psycopg2.Error) as exc:
            migrate_one(conn, tmp_path, _MIGRATION)
        message = str(exc.value)
        assert "2 dòng source_url NULL" in message and "1 dòng source_url rỗng" in message
        assert "ví dụ log_id" in message and "WHERE source_url IS NULL OR source_url = ''" in message
        conn.rollback()

        assert not _column_is_not_null(conn) and not _has_check(conn)     # không tạo gì
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM job_sources_log")
            assert cur.fetchone()[0] == 4                                  # không xoá dòng nào
            cur.execute("SELECT count(*) FROM schema_migrations WHERE filename = %s", (_MIGRATION,))
            assert cur.fetchone()[0] == 0                                  # chưa ghi dấu đã áp dụng

        # Xử lý tay (ở đây: gán URL cho dòng NULL, xoá dòng rỗng) rồi chạy lại được.
        with conn.cursor() as cur:
            cur.execute("UPDATE job_sources_log SET source_url = 'https://topcv.vn/fixed-' || log_id "
                        "WHERE source_url IS NULL")
            cur.execute("DELETE FROM job_sources_log WHERE source_url = ''")
        conn.commit()
        assert migrate_one(conn, tmp_path, _MIGRATION) == [_MIGRATION]
        assert _column_is_not_null(conn) and _has_check(conn)


def test_migration_stops_when_only_blank_rows_exist(tmp_path):
    with temp_database() as url, connect(url) as conn:
        legacy_db_before(conn, tmp_path, 47)
        with conn.cursor() as cur:
            _raw_log(cur, _raw_job(cur, _company(cur)), "")
        conn.commit()
        with pytest.raises(psycopg2.Error) as exc:
            migrate_one(conn, tmp_path, _MIGRATION)
        assert "0 dòng source_url NULL" in str(exc.value) and "1 dòng source_url rỗng" in str(exc.value)


# ============================================================ khoá và thứ tự triển khai
def test_migration_times_out_instead_of_waiting_when_crawl_holds_a_lock(tmp_path):
    with temp_database() as url, connect(url) as conn, connect(url) as other:
        legacy_db_before(conn, tmp_path, 47)
        with other.cursor() as cur:
            cur.execute("SELECT count(*) FROM job_sources_log")               # giao dịch khác đang đọc, chưa kết thúc
        with pytest.raises(psycopg2.errors.LockNotAvailable):
            migrate_one(conn, tmp_path, _MIGRATION, sql_edit=lambda t: t.replace("'10s'", "'300ms'"))
        conn.rollback()
        assert not _column_is_not_null(conn) and not _has_check(conn)
        other.rollback()
        assert migrate_one(conn, tmp_path, _MIGRATION) == [_MIGRATION]


@pytest.mark.parametrize("migrated", [False, True])
def test_current_write_paths_work_before_and_after_the_migration(tmp_path, migrated):
    """Code hiện tại luôn ghi URL (crawl: URL thật; nhập tay: manual://<uuid>), nên chạy được ở cả hai trạng thái."""
    with temp_database() as url, connect(url) as conn:
        legacy_db_before(conn, tmp_path, 47)
        if migrated:
            migrate_one(conn, tmp_path, _MIGRATION)
        crawled = _insert_job(conn, f"https://example.com/{uuid.uuid4()}")
        assert db.link_repost_source(conn, crawled, source_name="Fake", source_url=f"https://example.com/{uuid.uuid4()}",
                                     deadline=date(2100, 1, 1))
        with conn.cursor() as cur:
            company = _company(cur)
        manual = db.create_manual_job(conn, job_title="Nhập tay", company_id=company)
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM job_sources_log WHERE job_id = %s", (crawled,))
            assert cur.fetchone()[0] == 2
            cur.execute("SELECT source_url FROM job_sources_log WHERE job_id = %s", (manual,))
            assert cur.fetchone()[0].startswith("manual://")


def test_empty_url_from_code_is_rejected_after_the_migration(tmp_path):
    """insert_listing với URL rỗng: trước migration lọt qua (một dòng), sau migration bị CHECK chặn, và việc tạo
    job rollback sạch (không để lại job mồ côi), đúng cách các lỗi ghi khác của pipeline được xử lý."""
    with temp_database() as url, connect(url) as conn:
        legacy_db_before(conn, tmp_path, 47)
        migrate_one(conn, tmp_path, _MIGRATION)
        with pytest.raises(psycopg2.errors.CheckViolation):
            _insert_job(conn, "")
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM job_postings")
            assert cur.fetchone()[0] == 0


# ============================================================ schema.sql
def test_schema_sql_has_the_same_constraints_as_the_migration():
    with temp_database() as url, connect(url) as conn:
        db.apply_schema(conn, SCHEMA)
        conn.commit()
        assert _column_is_not_null(conn) and _has_check(conn)
