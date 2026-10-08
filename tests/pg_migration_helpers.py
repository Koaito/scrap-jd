"""
Hạ tầng DÙNG CHUNG cho test migration trên Postgres thật (tests/test_pg_*_migration.py).

Mỗi test dựng một database tạm, đưa nó về ĐÚNG trạng thái ngay trước migration cần thử (baseline 0036 + các
migration cũ ghi nhận + mọi migration đánh số nhỏ hơn số đó), nhét dữ liệu mẫu, chạy migration bằng chính
bộ chạy migration thật (db.apply_migrations) rồi kiểm kết quả. Nhờ vậy test thử đúng đường nâng cấp DB có
dữ liệu, không phải đường dựng DB mới từ schema.sql (đường đó do tests/test_pg_migrations.py lo).

Cần TEST_DATABASE_URL (tên database chứa "test"); không đặt thì các file test dùng module này tự bỏ qua.
"""
import os
import pathlib
import re
import shutil
import uuid
from contextlib import contextmanager
from urllib.parse import urlparse

import psycopg2
import pytest
from psycopg2 import sql as pgsql

import db

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

requires_pg = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

SQL_DIR = pathlib.Path(__file__).resolve().parent.parent / "sql"
SCHEMA = str(SQL_DIR / "schema.sql")
BASELINE = str(SQL_DIR / "baseline" / "0036_schema.sql")


@contextmanager
def temp_database(tag: str = "mig"):
    """Tạo database tạm (tên = <db test>_<tag>_<mã ngẫu nhiên>), trả URL của nó, xoá khi xong."""
    parsed = urlparse(TEST_DATABASE_URL)
    base = parsed.path.lstrip("/")
    if "test" not in base.lower():
        pytest.fail(f"Từ chối chạy: database '{base}' không chứa 'test' trong tên.")
    name = f"{base}_{tag}_{uuid.uuid4().hex[:8]}"
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
def connect(url):
    conn = psycopg2.connect(url)
    conn.autocommit = False
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


def numbered_migrations_before(number: int) -> list:
    """Tên các migration đánh số NNNN_*.sql có NNNN < number, theo thứ tự số."""
    out = []
    for f in sorted(os.listdir(SQL_DIR)):
        m = re.match(r"^(\d{4})_[a-z0-9_]+\.sql$", f)
        if m and int(m.group(1)) < number:
            out.append(f)
    return out


def legacy_db_before(conn, workdir: pathlib.Path, number: int) -> None:
    """Đưa DB về trạng thái ngay trước migration số `number`: baseline 0036, 36 migration cũ được ghi
    nhận là đã áp dụng, rồi chạy mọi migration đánh số < number. `workdir` là thư mục tạm (tmp_path) chứa
    bản sao các file migration để bộ chạy thật áp dụng; sau đó dùng migrate_one() cho migration cần thử."""
    db.apply_schema(conn, BASELINE)
    legacy = [f for f in db.connection._list_migration_files(str(SQL_DIR))
              if f.startswith("migration_")]
    db.connection._ensure_schema_migrations_table(conn)
    with conn.cursor() as cur:
        for f in legacy:
            cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (f,))
    conn.commit()
    pre = numbered_migrations_before(number)
    for n in pre:
        shutil.copy(SQL_DIR / n, workdir / n)
    assert db.apply_migrations(conn, str(workdir)) == pre


def migrate_one(conn, workdir: pathlib.Path, filename: str, *, sql_edit=None) -> list:
    """Chạy migration `filename` (và chỉ nó, vì các file trước đã được ghi nhận) bằng bộ chạy thật.
    sql_edit(text) -> text cho phép test chỉnh nội dung (ví dụ rút ngắn lock_timeout)."""
    text = (SQL_DIR / filename).read_text(encoding="utf-8")
    if sql_edit is not None:
        text = sql_edit(text)
    (workdir / filename).write_text(text, encoding="utf-8")
    return db.apply_migrations(conn, str(workdir))
