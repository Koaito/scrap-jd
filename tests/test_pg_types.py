"""
M2a: các hàm đọc dòng ở scrapjd/db/pg_types.py.

Chúng chỉ bọc `fetchone()` / `fetchall()` của psycopg2: phải trả ĐÚNG đối tượng psycopg2 trả (không sao chép),
và chỉ `fetch_scalar` / `fetch_one_row` được từ chối khi câu SQL không có dòng nào.

Phần cursor giả không cần DB. Phần cuối chạy trên Postgres thật nếu đặt TEST_DATABASE_URL (tên database phải
chứa "test"), giống các test test_pg_*: không đặt thì bỏ qua.
"""
import os
from unittest.mock import MagicMock
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
import pytest

from scrapjd.db.pg_types import (
    fetch_all_rows,
    fetch_one_row,
    fetch_optional_row,
    fetch_count,
    fetch_scalar,
)


def _cursor(*, one=None, all_=None):
    cur = MagicMock()
    cur.fetchone.return_value = one
    cur.fetchall.return_value = all_
    return cur


def test_fetch_scalar_returns_first_column():
    assert fetch_scalar(_cursor(one=(42, "x"))) == 42


def test_fetch_scalar_keeps_falsy_first_column():
    # count(*) = 0 và NULL đều là giá trị hợp lệ, không được nhầm với "không có dòng".
    assert fetch_scalar(_cursor(one=(0,))) == 0
    assert fetch_scalar(_cursor(one=(None,))) is None


def test_fetch_count_returns_first_column_as_is():
    assert fetch_count(_cursor(one=(7,))) == 7
    assert fetch_count(_cursor(one=(0,))) == 0  # count(*) = 0 là giá trị hợp lệ


def test_fetch_count_without_row_raises_assertion_error():
    with pytest.raises(AssertionError, match="đúng 1 dòng"):
        fetch_count(_cursor(one=None))


def test_fetch_scalar_without_row_raises_assertion_error():
    with pytest.raises(AssertionError, match="đúng 1 dòng"):
        fetch_scalar(_cursor(one=None))


def test_fetch_one_row_returns_the_same_object():
    row = {"total": 3}
    assert fetch_one_row(_cursor(one=row)) is row


def test_fetch_one_row_without_row_raises_assertion_error():
    with pytest.raises(AssertionError, match="đúng 1 dòng"):
        fetch_one_row(_cursor(one=None))


def test_fetch_optional_row_passes_through_none_and_row():
    row = {"id": 1}
    assert fetch_optional_row(_cursor(one=row)) is row
    assert fetch_optional_row(_cursor(one=None)) is None


def test_fetch_all_rows_returns_the_same_list_without_copying():
    rows = [{"a": 1}, {"a": 2}]
    assert fetch_all_rows(_cursor(all_=rows)) is rows
    empty: list = []
    assert fetch_all_rows(_cursor(all_=empty)) is empty


# ---------------------------------------------------------------------------
# Postgres thật
# ---------------------------------------------------------------------------

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)


@pytest.fixture
def pg_conn():
    dbname = urlparse(TEST_DATABASE_URL).path.lstrip("/")
    if "test" not in dbname.lower():
        pytest.fail(f"Từ chối chạy: database '{dbname}' không chứa 'test' trong tên.")
    conn = psycopg2.connect(TEST_DATABASE_URL)
    try:
        yield conn
    finally:
        conn.rollback()
        conn.close()


@needs_pg
def test_helpers_against_real_cursors(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM generate_series(1, 3)")
        assert fetch_scalar(cur) == 3
        cur.execute("SELECT 1 WHERE false")
        with pytest.raises(AssertionError):
            fetch_scalar(cur)

    with pg_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT count(*) AS total FROM generate_series(1, 3)")
        assert fetch_one_row(cur)["total"] == 3

        cur.execute("SELECT 1 AS n WHERE false")
        assert fetch_optional_row(cur) is None

        cur.execute("SELECT g AS n FROM generate_series(1, 3) g ORDER BY g")
        rows = fetch_all_rows(cur)
        assert [r["n"] for r in rows] == [1, 2, 3]
        # Vẫn là dòng thật của RealDictCursor, không bị đổi kiểu lúc chạy.
        assert all(isinstance(r, psycopg2.extras.RealDictRow) for r in rows)
