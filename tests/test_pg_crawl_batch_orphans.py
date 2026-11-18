"""
Batch crawl mồ côi (db.close_orphaned_crawl_batches) — chạy trên Postgres thật.

Batch chỉ được nối tiếp bởi task đang chạy run của nó. Task chết (restart, run
treo) thì crawl_runs được reconcile nhưng crawl_batches kẹt 'running' mãi. Các
ca dưới đây dựng lại đúng 3 hình dạng batch kẹt thấy trong dữ liệu thật (11/2026)
và các ca cần KHÔNG đóng nhầm.

Cách chạy giống tests/test_pg_job_sync.py: đặt TEST_DATABASE_URL (tên chứa "test");
không đặt thì bỏ qua.
"""

import json
import os
import uuid
from urllib.parse import urlparse

import psycopg2
import pytest

from scrapjd import db
from scrapjd.api import crawl_runner

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")
CATS = ["data-analyst", "data-engineer", "data-scientist", "software-engineering"]


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
    db.apply_schema(conn, _SCHEMA_PATH)
    db.baseline_migrations(conn)
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture(autouse=True)
def clean(pg_conn):
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE crawl_runs, crawl_batches CASCADE")
    pg_conn.commit()
    yield
    pg_conn.rollback()


def _batch(conn, source="vietnamworks", cats=CATS, created="now() - interval '3 days'"):
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO crawl_batches (source, categories, pages, created_at) "
            f"VALUES (%s, %s, 1, {created}) RETURNING batch_id",
            (source, json.dumps(cats)),
        )
        bid = str(cur.fetchone()[0])
    conn.commit()
    return bid


def _run(conn, batch_id, pos, status, finished="now() - interval '3 days'", source="vietnamworks"):
    """Một run con. Run 'queued'/'running' không có finished_at."""
    fin = "NULL" if status in ("queued", "running") else finished
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO crawl_runs (source, category, pages, status, batch_id, batch_position, finished_at) "
            f"VALUES (%s, %s, 1, %s, %s, %s, {fin})",
            (source, CATS[pos], status, batch_id, pos),
        )
    conn.commit()


def _get(conn, batch_id):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status::text, error, finished_at, "
            "(SELECT max(finished_at) FROM crawl_runs WHERE batch_id = %s), created_at "
            "FROM crawl_batches WHERE batch_id = %s",
            (batch_id, batch_id),
        )
        return cur.fetchone()


def test_all_runs_created_but_last_one_died_is_done(pg_conn):
    """161d7e3e / 770686dc trong dữ liệu thật: đủ run, run cuối bị restart giết."""
    b = _batch(pg_conn, cats=CATS)
    for i, st in enumerate(["done", "done", "done", "error"]):
        _run(pg_conn, b, i, st)

    assert db.close_orphaned_crawl_batches(pg_conn) == 1

    status, error, finished, last_run_finished, _ = _get(pg_conn, b)
    assert status == "done" and error is None
    assert finished == last_run_finished  # mốc lịch sử, không phải ngày dọn


def test_died_midway_is_error_with_counts(pg_conn):
    """0be6f60d / f2ebbfc5...: chết ở category 3/4, 3 category sau chưa từng được tạo."""
    b = _batch(pg_conn, cats=CATS)
    _run(pg_conn, b, 0, "done")
    _run(pg_conn, b, 1, "error")

    assert db.close_orphaned_crawl_batches(pg_conn) == 1

    status, error, finished, last_run_finished, _ = _get(pg_conn, b)
    assert status == "error"
    assert "2/4" in error
    assert finished == last_run_finished


def test_batch_with_no_runs_is_error_and_finished_at_is_created_at(pg_conn):
    """b2e8327a: batch không có run nào."""
    b = _batch(pg_conn, cats=["data-analyst"])

    assert db.close_orphaned_crawl_batches(pg_conn) == 1

    status, error, finished, _, created = _get(pg_conn, b)
    assert status == "error" and "0/1" in error
    assert finished == created


def test_batch_with_active_run_is_left_alone(pg_conn):
    for st in ("running", "queued"):
        b = _batch(pg_conn, cats=CATS)
        _run(pg_conn, b, 0, "done")
        _run(pg_conn, b, 1, st, source="vietnamworks")
        assert db.close_orphaned_crawl_batches(pg_conn) == 0
        assert _get(pg_conn, b)[0] == "running"
        with pg_conn.cursor() as cur:
            cur.execute("TRUNCATE crawl_runs, crawl_batches CASCADE")
        pg_conn.commit()


def test_finished_batches_are_not_touched(pg_conn):
    b = _batch(pg_conn, cats=["data-analyst"])
    _run(pg_conn, b, 0, "done")
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE crawl_batches SET status='done', finished_at=now() WHERE batch_id=%s", (b,))
    pg_conn.commit()
    before = _get(pg_conn, b)

    assert db.close_orphaned_crawl_batches(pg_conn) == 0
    assert _get(pg_conn, b) == before


def test_grace_protects_batch_between_two_runs(pg_conn):
    """Run vừa xong, advance_batch() chưa kịp tạo run kế: không được đóng nhầm."""
    b = _batch(pg_conn, cats=CATS, created="now() - interval '1 hour'")
    _run(pg_conn, b, 0, "done", finished="now() - interval '5 seconds'")

    assert db.close_orphaned_crawl_batches(pg_conn, grace_minutes=10) == 0
    assert _get(pg_conn, b)[0] == "running"


def test_grace_closes_batch_after_it_has_been_idle_long_enough(pg_conn):
    b = _batch(pg_conn, cats=CATS)
    _run(pg_conn, b, 0, "error", finished="now() - interval '11 minutes'")

    assert db.close_orphaned_crawl_batches(pg_conn, grace_minutes=10) == 1
    assert _get(pg_conn, b)[0] == "error"


def test_is_idempotent(pg_conn):
    b = _batch(pg_conn, cats=CATS)
    _run(pg_conn, b, 0, "error")
    assert db.close_orphaned_crawl_batches(pg_conn) == 1
    assert db.close_orphaned_crawl_batches(pg_conn) == 0
    assert _get(pg_conn, b)[0] == "error"


def test_startup_sequence_closes_runs_then_batches(pg_conn):
    """Đúng thứ tự trong lifespan: run 'running' mồ côi được đóng trước, rồi mới
    thấy batch không còn run nào chạy. Đảo thứ tự thì batch bị bỏ sót."""
    b = _batch(pg_conn, cats=CATS)
    _run(pg_conn, b, 0, "done")
    _run(pg_conn, b, 1, "running")

    assert db.close_orphaned_crawl_batches(pg_conn) == 0  # run còn 'running' -> chưa đóng
    assert db.reconcile_orphaned_crawl_runs(pg_conn) == 1
    assert db.close_orphaned_crawl_batches(pg_conn) == 1

    status, error, *_ = _get(pg_conn, b)
    assert status == "error" and "2/4" in error


def test_advance_batch_does_not_spawn_run_for_closed_batch(pg_conn):
    """Task cũ tỉnh dậy sau khi batch đã bị đóng: không đẻ thêm run."""
    b = _batch(pg_conn, cats=CATS)
    _run(pg_conn, b, 0, "error")
    db.close_orphaned_crawl_batches(pg_conn)

    assert db.advance_crawl_batch(pg_conn, b, 0) is None
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM crawl_runs WHERE batch_id = %s", (b,))
        assert cur.fetchone()[0] == 1


def test_advance_batch_still_works_for_running_batch(pg_conn):
    """Đối chứng: batch đang chạy bình thường vẫn tạo được category kế."""
    b = _batch(pg_conn, cats=CATS)
    _run(pg_conn, b, 0, "done")

    run_id = db.advance_crawl_batch(pg_conn, b, 0)

    assert run_id is not None
    with pg_conn.cursor() as cur:
        cur.execute("SELECT category, batch_position FROM crawl_runs WHERE run_id = %s", (run_id,))
        assert cur.fetchone() == (CATS[1], 1)


def test_start_batch_removes_batch_when_first_run_is_rejected(pg_conn, monkeypatch):
    """Nguồn đang có lượt chạy -> 409; batch vừa tạo không được để lại 'running' mãi."""
    monkeypatch.setattr(db, "get_pooled_connection", lambda: pg_conn)
    monkeypatch.setattr(db, "release_connection", lambda c: None)
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO crawl_runs (source, category, pages, status) "
            "VALUES ('vietnamworks', 'data-analyst', 1, 'running')"
        )
    pg_conn.commit()

    with pytest.raises(db.ActiveCrawlExistsError):
        crawl_runner.start_batch("vietnamworks", ["a", "b"], pages=1)

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM crawl_batches")
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM crawl_runs")
        assert cur.fetchone()[0] == 1  # run đang chạy của người khác còn nguyên


def test_start_batch_success_keeps_batch_and_first_run(pg_conn, monkeypatch):
    monkeypatch.setattr(db, "get_pooled_connection", lambda: pg_conn)
    monkeypatch.setattr(db, "release_connection", lambda c: None)

    batch_id, run_id = crawl_runner.start_batch("vietnamworks", ["a", "b"], pages=1)

    uuid.UUID(batch_id), uuid.UUID(run_id)
    assert _get(pg_conn, batch_id)[0] == "running"
