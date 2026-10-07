"""
D2 trên POSTGRES THẬT: migration 0041 (job_sources_log UNIQUE (source_url)) và hai chỗ code ghi log
nguồn (db.insert_job, db.link_repost_source).

Chứng minh:
  - migration thêm ràng buộc, một URL không còn nằm ở hai job, NULL vẫn được nhiều dòng, câu tra
    theo riêng source_url dùng index thay vì quét cả bảng;
  - DB đang có URL trùng giữa hai job thì migration DỪNG với thông báo có số lượng và ví dụ, không
    xoá gì, không ghi dấu "đã áp dụng", không tạo ràng buộc;
  - chạy lại file migration không lỗi;
  - link_repost_source: URL đã thuộc job KHÁC thì không ghi đè, trả False và để lại cảnh báo; URL đã
    thuộc chính job đó thì trả False như trước; URL mới trả True;
  - insert_job với URL đã thuộc job khác thì raise UniqueViolation và không để lại job mồ côi.

Cách chạy như tests/test_pg_migrations.py: đặt TEST_DATABASE_URL (tên database chứa "test"); không đặt
thì cả file được bỏ qua. Nhóm migration tự tạo/xoá database tạm.
"""
import logging
import os
import pathlib
import uuid
from contextlib import contextmanager
from urllib.parse import urlparse

import psycopg2
import psycopg2.errors
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
_MIGRATION = "0041_unique_source_url_job_sources_log.sql"
_CONSTRAINT = "uq_job_sources_log_source_url"


# ----------------------------------------------------------------------------- hạ tầng test
@contextmanager
def _temp_database():
    parsed = urlparse(TEST_DATABASE_URL)
    base = parsed.path.lstrip("/")
    if "test" not in base.lower():
        pytest.fail(f"Từ chối chạy: database '{base}' không chứa 'test' trong tên.")
    name = f"{base}_su_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(TEST_DATABASE_URL)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(pgsql.SQL("CREATE DATABASE {}").format(pgsql.Identifier(name)))
        try:
            yield parsed._replace(path=f"/{name}").geturl()
        finally:
            with admin.cursor() as cur:
                cur.execute(pgsql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                            .format(pgsql.Identifier(name)))
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


def _legacy_baselined_db(conn):
    """DB trước 0037: baseline 0036 + 36 migration cũ đã ghi nhận. job_sources_log lúc này chỉ có
    uq_job_source (job_id, source_url), đúng trạng thái trước D2."""
    db.apply_schema(conn, _BASELINE)
    legacy = [f for f in db.connection._list_migration_files(str(_SQL_DIR))
              if f.startswith("migration_")]
    db.connection._ensure_schema_migrations_table(conn)
    with conn.cursor() as cur:
        for f in legacy:
            cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (f,))
    conn.commit()


def _migrate_only_0041(conn, tmp_path):
    (tmp_path / _MIGRATION).write_text((_SQL_DIR / _MIGRATION).read_text(encoding="utf-8"),
                                       encoding="utf-8")
    return db.apply_migrations(conn, str(tmp_path))


def _company(cur):
    cid = str(uuid.uuid4())
    cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)",
                (cid, f"Công ty {cid[:8]}"))
    return cid


def _raw_job(cur, company_id, title):
    job_id = str(uuid.uuid4())
    cur.execute("INSERT INTO job_postings (job_id, company_id, job_title) VALUES (%s, %s, %s)",
                (job_id, company_id, title))
    return job_id


def _raw_log(cur, job_id, url):
    cur.execute("INSERT INTO job_sources_log (job_id, source_name, source_url) VALUES (%s, 'TopCV', %s)",
                (job_id, url))


def _has_constraint(cur):
    cur.execute("SELECT count(*) FROM pg_constraint WHERE conname = %s "
                "AND conrelid = 'job_sources_log'::regclass", (_CONSTRAINT,))
    return cur.fetchone()[0] == 1


# ----------------------------------------------------------------------------- migration 0041
def test_migration_adds_constraint_and_blocks_one_url_in_two_jobs(tmp_path):
    with _temp_database() as url, _connect(url) as conn:
        _legacy_baselined_db(conn)
        with conn.cursor() as cur:
            c = _company(cur)
            a, b = _raw_job(cur, c, "Job A"), _raw_job(cur, c, "Job B")
            _raw_log(cur, a, "https://topcv.vn/a")
            assert not _has_constraint(cur)
        conn.commit()

        assert _migrate_only_0041(conn, tmp_path) == [_MIGRATION]

        with conn.cursor() as cur:
            assert _has_constraint(cur)
            # cùng URL ở job khác: bị chặn
            with pytest.raises(psycopg2.errors.UniqueViolation):
                _raw_log(cur, b, "https://topcv.vn/a")
        conn.rollback()

        with conn.cursor() as cur:
            # cùng URL cùng job: vẫn bị chặn (bởi ràng buộc cũ hoặc mới)
            with pytest.raises(psycopg2.errors.UniqueViolation):
                _raw_log(cur, a, "https://topcv.vn/a")
        conn.rollback()

        with conn.cursor() as cur:
            # URL khác thì ghi bình thường; NULL được nhiều dòng (Postgres coi NULL là khác nhau)
            _raw_log(cur, b, "https://topcv.vn/b")
            _raw_log(cur, a, None)
            _raw_log(cur, b, None)
            cur.execute("SELECT count(*) FROM job_sources_log")
            assert cur.fetchone()[0] == 4


def test_lookup_by_source_url_uses_the_new_index(tmp_path):
    with _temp_database() as url, _connect(url) as conn:
        _legacy_baselined_db(conn)
        with conn.cursor() as cur:
            c = _company(cur)
            j = _raw_job(cur, c, "Job")
            for i in range(50):
                _raw_log(cur, j, f"https://topcv.vn/{i}")
        conn.commit()
        _migrate_only_0041(conn, tmp_path)

        with conn.cursor() as cur:
            cur.execute("ANALYZE job_sources_log")
            cur.execute("SET LOCAL enable_seqscan = off")
            cur.execute("EXPLAIN SELECT 1 FROM job_sources_log WHERE source_url = 'https://topcv.vn/7'")
            plan = "\n".join(r[0] for r in cur.fetchall())
        assert _CONSTRAINT in plan, plan


def test_migration_stops_with_a_clear_message_when_a_url_is_in_two_jobs(tmp_path):
    with _temp_database() as url, _connect(url) as conn:
        _legacy_baselined_db(conn)
        with conn.cursor() as cur:
            c = _company(cur)
            a, b, d = (_raw_job(cur, c, t) for t in ("Job A", "Job B", "Job C"))
            _raw_log(cur, a, "https://topcv.vn/dup1")
            _raw_log(cur, b, "https://topcv.vn/dup1")   # URL trùng giữa hai job
            _raw_log(cur, a, "https://topcv.vn/dup2")
            _raw_log(cur, d, "https://topcv.vn/dup2")   # URL trùng thứ hai
            _raw_log(cur, d, "https://topcv.vn/ok")
        conn.commit()

        with pytest.raises(psycopg2.Error) as exc:
            _migrate_only_0041(conn, tmp_path)
        message = str(exc.value)
        assert "2 URL" in message and "4 dòng" in message and "https://topcv.vn/dup1" in message
        assert "GROUP BY source_url HAVING count(*) > 1" in message
        conn.rollback()

        with conn.cursor() as cur:
            assert not _has_constraint(cur)
            cur.execute("SELECT count(*) FROM job_sources_log")
            assert cur.fetchone()[0] == 5            # không xoá gì
            cur.execute("SELECT count(*) FROM schema_migrations WHERE filename = %s", (_MIGRATION,))
            assert cur.fetchone()[0] == 0            # không ghi dấu đã áp dụng

        # Dọn trùng rồi chạy lại thì qua.
        with conn.cursor() as cur:
            cur.execute("DELETE FROM job_sources_log WHERE job_id = ANY(%s::uuid[]) "
                        "AND source_url IN ('https://topcv.vn/dup1', 'https://topcv.vn/dup2')",
                        ([b, d],))
        conn.commit()
        assert _migrate_only_0041(conn, tmp_path) == [_MIGRATION]


def test_rerunning_the_migration_file_is_a_noop(tmp_path):
    with _temp_database() as url, _connect(url) as conn:
        _legacy_baselined_db(conn)
        _migrate_only_0041(conn, tmp_path)
        sql_text = (_SQL_DIR / _MIGRATION).read_text(encoding="utf-8")
        with conn.cursor() as cur:
            cur.execute(sql_text)
            cur.execute(sql_text)
            assert _has_constraint(cur)
        conn.commit()


def test_schema_sql_has_the_same_constraint():
    with _temp_database() as url, _connect(url) as conn:
        db.apply_schema(conn, _SCHEMA)
        with conn.cursor() as cur:
            assert _has_constraint(cur)


# ----------------------------------------------------------------------------- code ghi log nguồn
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
    db.apply_schema(conn, _SCHEMA)
    yield conn
    conn.rollback()
    conn.close()


def _make_job(conn, url, title="Data Analyst"):
    with conn.cursor() as cur:
        cid = str(uuid.uuid4())
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)",
                    (cid, f"Công ty {cid[:8]}"))
    job_id = db.insert_job(
        conn, company_id=cid, job_title=title, matching_industry="Data",
        level_id=None, province_id=None, work_type=None, currency="VND",
        salary_min=None, salary_max=None, salary_type="NEGOTIABLE",
        source_url=url, source_name="TopCV", raw_jd_content="gốc",
    )
    conn.commit()
    return job_id


def _owner(conn, url):
    with conn.cursor() as cur:
        cur.execute("SELECT job_id::text FROM job_sources_log WHERE source_url = %s", (url,))
        rows = cur.fetchall()
    return [r[0] for r in rows]


def test_link_repost_source_new_url_returns_true(pg_conn):
    job = _make_job(pg_conn, f"https://topcv.vn/{uuid.uuid4()}")
    url = f"https://topcv.vn/{uuid.uuid4()}"
    assert db.link_repost_source(pg_conn, job, source_name="TopCV", source_url=url) is True
    pg_conn.commit()
    assert _owner(pg_conn, url) == [job]


def test_link_repost_source_same_job_same_url_returns_false_without_warning(pg_conn, caplog):
    url = f"https://topcv.vn/{uuid.uuid4()}"
    job = _make_job(pg_conn, url)
    with caplog.at_level(logging.WARNING, logger="db.job_recrawl"):
        assert db.link_repost_source(pg_conn, job, source_name="TopCV", source_url=url) is False
    pg_conn.commit()
    assert _owner(pg_conn, url) == [job]
    assert not [r for r in caplog.records if "đã thuộc job" in r.getMessage()]


def test_link_repost_source_url_owned_by_another_job_is_kept_and_warned(pg_conn, caplog):
    url = f"https://topcv.vn/{uuid.uuid4()}"
    owner = _make_job(pg_conn, url, "Việc của chủ URL")
    other = _make_job(pg_conn, f"https://topcv.vn/{uuid.uuid4()}", "Việc khác")
    with caplog.at_level(logging.WARNING, logger="db.job_recrawl"):
        assert db.link_repost_source(pg_conn, other, source_name="TopCV", source_url=url,
                                     raw_jd_content="không được ghi") is False
    pg_conn.commit()
    assert _owner(pg_conn, url) == [owner]           # không bị chuyển, không nhân đôi
    with pg_conn.cursor() as cur:
        cur.execute("SELECT raw_jd_content FROM job_sources_log WHERE source_url = %s", (url,))
        assert cur.fetchone()[0] == "gốc"            # nội dung của dòng cũ không bị đè
    warnings = [r.getMessage() for r in caplog.records if "đã thuộc job" in r.getMessage()]
    assert len(warnings) == 1 and owner in warnings[0] and other in warnings[0]


def test_insert_job_with_url_of_another_job_raises_and_leaves_no_orphan_job(pg_conn):
    url = f"https://topcv.vn/{uuid.uuid4()}"
    _make_job(pg_conn, url)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM job_postings")
        jobs_before = cur.fetchone()[0]
        cid = str(uuid.uuid4())
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)", (cid, "Công ty X"))
    pg_conn.commit()

    with pytest.raises(psycopg2.errors.UniqueViolation):
        db.insert_job(
            pg_conn, company_id=cid, job_title="Việc khác hẳn", matching_industry="Data",
            level_id=None, province_id=None, work_type=None, currency="VND",
            salary_min=None, salary_max=None, salary_type="NEGOTIABLE",
            source_url=url, source_name="TopCV", raw_jd_content="trùng URL",
        )
    pg_conn.rollback()                                # đúng việc vòng lặp pipeline làm khi gặp lỗi

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM job_postings")
        assert cur.fetchone()[0] == jobs_before      # job vừa insert đã được rollback theo
