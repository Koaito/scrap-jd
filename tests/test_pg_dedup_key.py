"""
A3 nửa 1/2 trên POSTGRES THẬT: migration 0039 (job_postings.dedup_key / dedup_key_version, hàm
job_dedup_key, trigger set_job_dedup_key). Chưa liên quan tới code Python: nửa này chỉ là schema +
backfill.

Chứng minh:
  - backfill điền khoá cho mọi dòng, đúng công thức (so với sha256 tính bằng Python), không đổi
    updated_at và không đổi content_hash;
  - khoá bỏ level, bỏ qua hoa/thường và khoảng trắng thừa, bỏ qua trạng thái OPEN/CLOSED, phân biệt
    công ty và tỉnh, coi tỉnh NULL là một giá trị riêng;
  - chạy lại file migration không lỗi, và tính lại đúng các dòng còn mang phiên bản cũ;
  - trigger tính lại khoá ở mọi INSERT/UPDATE (kể cả khi ai đó ghi tay giá trị khác), INSERT không liệt
    kê hai cột mới vẫn chạy (đúng cách code đang deploy ghi), hai cột NOT NULL, có index dùng được.

Cách chạy như tests/test_pg_migrations.py: đặt TEST_DATABASE_URL (tên database chứa "test"), test tự
tạo/xoá database tạm; không đặt thì cả file được bỏ qua.
"""
import hashlib
import os
import pathlib
import re
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
_BEFORE = ("0037_add_job_closed_reason.sql", "0038_add_reopen_job_audit_action.sql")
_ONLY_0039 = "0039_add_job_dedup_key.sql"
_UPDATED_AT = "2026-02-02 02:02:02"


@contextmanager
def _temp_database():
    parsed = urlparse(TEST_DATABASE_URL)
    base = parsed.path.lstrip("/")
    if "test" not in base.lower():
        pytest.fail(f"Từ chối chạy: database '{base}' không chứa 'test' trong tên.")
    name = f"{base}_dk_{uuid.uuid4().hex[:8]}"
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
    """DB ở trạng thái ngay trước 0037: baseline 0036 + 36 migration cũ đã ghi nhận."""
    db.apply_schema(conn, _BASELINE)
    legacy = [f for f in db.connection._list_migration_files(str(_SQL_DIR))
              if f.startswith("migration_")]
    db.connection._ensure_schema_migrations_table(conn)
    with conn.cursor() as cur:
        for f in legacy:
            cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (f,))
    conn.commit()


def _migrate_only(conn, tmp_path, *names):
    """Chạy đúng các migration đánh số `names` (copy sang thư mục riêng cho khỏi chạy cả 0040+)."""
    for n in names:
        (tmp_path / n).write_text((_SQL_DIR / n).read_text(encoding="utf-8"), encoding="utf-8")
    return db.apply_migrations(conn, str(tmp_path))


def _company(cur):
    cid = str(uuid.uuid4())
    cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)",
                (cid, f"Công ty {cid[:8]}"))
    return cid


def _province(cur, name):
    cur.execute("SELECT province_id FROM provinces WHERE province_name = %s", (name,))
    return cur.fetchone()[0]


def _level(cur, code):
    cur.execute("SELECT level_id FROM levels WHERE level_code = %s", (code,))
    return cur.fetchone()[0]


def _job(cur, company_id, title, *, province_id=None, level_id=None, status="OPEN"):
    """INSERT KHÔNG liệt kê dedup_key/dedup_key_version: đúng cách code đang chạy ghi job."""
    job_id = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO job_postings (job_id, company_id, job_title, province_id, level_id, "
        "job_status, created_at, updated_at) "
        f"VALUES (%s, %s, %s, %s, %s, %s, '2026-01-01', '{_UPDATED_AT}')",
        (job_id, company_id, title, province_id, level_id, status))
    return job_id


def _row(cur, job_id):
    cur.execute("SELECT dedup_key, dedup_key_version, content_hash, updated_at::text "
                "FROM job_postings WHERE job_id = %s", (job_id,))
    return cur.fetchone()


def _expected_key(company_id, title, province_id):
    """Công thức phiên bản 1, tính bằng Python (chỉ dùng tiêu đề ASCII để không phụ thuộc locale)."""
    norm = re.sub(r"\s+", " ", title.strip().lower())
    raw = f"{company_id}|{norm}|{'' if province_id is None else province_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------- backfill
def test_backfill_fills_every_row_with_the_documented_key(tmp_path):
    with _temp_database() as url, _connect(url) as conn:
        _legacy_baselined_db(conn)
        _migrate_only(conn, tmp_path, *_BEFORE)
        with conn.cursor() as cur:
            c1, c2 = _company(cur), _company(cur)
            hn = _province(cur, "Hà Nội")
            hcm = _province(cur, "Hồ Chí Minh")
            junior, senior = _level(cur, "Junior"), _level(cur, "Senior")
            j1 = _job(cur, c1, "Backend Developer", province_id=hn, level_id=junior)
            # cùng khoá với j1: khác level, khác hoa/thường, khoảng trắng thừa
            j2 = _job(cur, c1, "  backend   DEVELOPER ", province_id=hn, level_id=senior)
            j3 = _job(cur, c1, "Backend Developer", province_id=hcm, level_id=junior)
            j4 = _job(cur, c1, "Backend Developer", province_id=None)
            # cùng khoá với j4: trạng thái không nằm trong khoá
            j5 = _job(cur, c1, "Backend Developer", province_id=None, status="CLOSED")
            j6 = _job(cur, c2, "Backend Developer", province_id=hn, level_id=junior)
            cur.execute("SELECT content_hash FROM job_postings ORDER BY job_id")
            hashes_before = cur.fetchall()
        conn.commit()

        assert _migrate_only(conn, tmp_path, _ONLY_0039) == [_ONLY_0039]

        with conn.cursor() as cur:
            rows = {j: _row(cur, j) for j in (j1, j2, j3, j4, j5, j6)}
            # mọi dòng có khoá + version 1, đúng công thức
            for j, r in rows.items():
                assert r[0] and len(r[0]) == 64 and r[1] == 1, j
            assert rows[j1][0] == _expected_key(c1, "Backend Developer", hn)
            assert rows[j3][0] == _expected_key(c1, "Backend Developer", hcm)
            assert rows[j4][0] == _expected_key(c1, "Backend Developer", None)
            assert rows[j6][0] == _expected_key(c2, "Backend Developer", hn)
            # bỏ level, bỏ hoa/thường + khoảng trắng, bỏ trạng thái
            assert rows[j1][0] == rows[j2][0]
            assert rows[j4][0] == rows[j5][0]
            # nhưng phân biệt tỉnh, tỉnh NULL, và công ty
            assert len({rows[j1][0], rows[j3][0], rows[j4][0], rows[j6][0]}) == 4
            # content_hash vẫn gồm level nên j1 và j2 vẫn khác hash: migration không đụng nó
            assert rows[j1][2] != rows[j2][2]
            cur.execute("SELECT content_hash FROM job_postings ORDER BY job_id")
            assert cur.fetchall() == hashes_before
            # backfill không đụng updated_at của dòng nào
            for r in rows.values():
                assert r[3].startswith(_UPDATED_AT)


def test_rerun_is_idempotent_and_recomputes_rows_with_an_old_version(tmp_path):
    with _temp_database() as url, _connect(url) as conn:
        _legacy_baselined_db(conn)
        _migrate_only(conn, tmp_path, *_BEFORE)
        with conn.cursor() as cur:
            c = _company(cur)
            a = _job(cur, c, "Tester")
            b = _job(cur, c, "Designer")
        conn.commit()
        _migrate_only(conn, tmp_path, _ONLY_0039)

        sql_text = (_SQL_DIR / _ONLY_0039).read_text(encoding="utf-8")
        with conn.cursor() as cur:
            good_a = _row(cur, a)
            # Chạy lại nguyên file: không lỗi, không đổi gì.
            cur.execute(sql_text)
            assert _row(cur, a) == good_a

            # Giả lập dòng còn mang khoá của phiên bản cũ (phải tắt trigger mới ghi tay được).
            cur.execute("ALTER TABLE job_postings DISABLE TRIGGER set_job_dedup_key")
            cur.execute("UPDATE job_postings SET dedup_key = 'cu', dedup_key_version = 0 "
                        "WHERE job_id = %s", (b,))
            cur.execute("ALTER TABLE job_postings ENABLE TRIGGER set_job_dedup_key")
            stale = _row(cur, b)
            assert (stale[0], stale[1]) == ("cu", 0)

            cur.execute(sql_text)
            fixed = _row(cur, b)
            assert fixed[0] == _expected_key(c, "Designer", None) and fixed[1] == 1
            assert fixed[3] == stale[3]           # backfill lại cũng không đổi updated_at
            assert _row(cur, a) == good_a


# ----------------------------------------------------------------------------- trigger, hàm, ràng buộc
@pytest.fixture()
def fresh():
    """DB dựng từ schema.sql mới nhất (đã gồm 0039)."""
    with _temp_database() as url, _connect(url) as conn:
        db.apply_schema(conn, _SCHEMA)
        conn.commit()
        yield conn


def test_insert_without_naming_the_new_columns_fills_them(fresh):
    with fresh.cursor() as cur:
        c = _company(cur)
        j = _job(cur, c, "Data Analyst")
        key, version, _, _ = _row(cur, j)
        assert key == _expected_key(c, "Data Analyst", None) and version == 1
        cur.execute("SELECT job_dedup_key_version()")
        assert cur.fetchone()[0] == 1


def test_trigger_overrides_values_written_by_hand(fresh):
    with fresh.cursor() as cur:
        c = _company(cur)
        j = str(uuid.uuid4())
        cur.execute("INSERT INTO job_postings (job_id, company_id, job_title, dedup_key, "
                    "dedup_key_version) VALUES (%s, %s, 'QA Engineer', 'tay', 99)", (j, c))
        assert _row(cur, j)[:2] == (_expected_key(c, "QA Engineer", None), 1)
        cur.execute("UPDATE job_postings SET dedup_key = 'tay2', dedup_key_version = 98 "
                    "WHERE job_id = %s", (j,))
        assert _row(cur, j)[:2] == (_expected_key(c, "QA Engineer", None), 1)


def test_key_follows_title_and_province_but_not_level_or_other_columns(fresh):
    with fresh.cursor() as cur:
        c = _company(cur)
        hn = _province(cur, "Hà Nội")
        j = _job(cur, c, "DevOps", province_id=hn, level_id=_level(cur, "Junior"))
        key0, _, hash0, _ = _row(cur, j)

        # đổi level: khoá giữ nguyên, content_hash đổi (đúng lý do cần khoá mới)
        cur.execute("UPDATE job_postings SET level_id = %s WHERE job_id = %s",
                    (_level(cur, "Senior"), j))
        key1, _, hash1, _ = _row(cur, j)
        assert key1 == key0 and hash1 != hash0

        # đổi cột không liên quan: khoá giữ nguyên
        cur.execute("UPDATE job_postings SET ss_team_notes = 'ghi chú' WHERE job_id = %s", (j,))
        assert _row(cur, j)[0] == key0

        # đổi tiêu đề (kể cả chỉ đổi hoa/thường): khoá giữ nguyên; đổi nội dung: khoá đổi
        cur.execute("UPDATE job_postings SET job_title = '  DEVOPS ' WHERE job_id = %s", (j,))
        assert _row(cur, j)[0] == key0
        cur.execute("UPDATE job_postings SET job_title = 'SRE' WHERE job_id = %s", (j,))
        key2 = _row(cur, j)[0]
        assert key2 == _expected_key(c, "SRE", hn) and key2 != key0

        # đổi tỉnh: khoá đổi
        cur.execute("UPDATE job_postings SET province_id = NULL WHERE job_id = %s", (j,))
        assert _row(cur, j)[0] == _expected_key(c, "SRE", None)


def test_job_dedup_key_function_semantics(fresh):
    with fresh.cursor() as cur:
        c1, c2 = _company(cur), _company(cur)

        def key(company, title, province):
            cur.execute("SELECT job_dedup_key(%s, %s, %s)", (company, title, province))
            return cur.fetchone()[0]

        base = key(c1, "Product Owner", 1)
        assert base == _expected_key(c1, "Product Owner", 1)
        assert key(c1, "  PRODUCT \t owner  ", 1) == base      # hoa/thường, khoảng trắng, tab
        assert key(c2, "Product Owner", 1) != base               # khác công ty
        assert key(c1, "Product Owner", 2) != base               # khác tỉnh
        assert key(c1, "Product Owner", None) != base            # thiếu tỉnh khác có tỉnh
        assert key(c1, "Product Owner", None) == key(c1, "product owner", None)
        assert key(c1, "Product Owner Lead", 1) != base          # tiêu đề khác thật sự


def test_both_columns_are_not_null_when_the_trigger_is_missing(fresh):
    with fresh.cursor() as cur:
        c = _company(cur)
        cur.execute("SAVEPOINT s")
        cur.execute("ALTER TABLE job_postings DISABLE TRIGGER set_job_dedup_key")
        with pytest.raises(psycopg2.errors.NotNullViolation):
            _job(cur, c, "Không có khoá")
        cur.execute("ROLLBACK TO SAVEPOINT s")   # hoàn tác cả lệnh DISABLE TRIGGER


def test_lookup_by_key_can_use_the_index(fresh):
    with fresh.cursor() as cur:
        c = _company(cur)
        for i in range(5):
            _job(cur, c, f"Vị trí {i}")
        cur.execute("SET LOCAL enable_seqscan = off")
        cur.execute("EXPLAIN SELECT job_id FROM job_postings "
                    "WHERE dedup_key = job_dedup_key(%s, %s, %s)", (c, "Vị trí 1", None))
        plan = "\n".join(r[0] for r in cur.fetchall())
        assert "idx_job_postings_dedup_key" in plan, plan
