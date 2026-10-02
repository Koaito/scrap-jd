"""
Test api/services/crawl_watchdog.run_crawl_watchdog_once() — KHÔNG cần
database (mock db_module). Logic SQL của reconcile_stale_runs được test trên
Postgres thật ở tests/test_pg_integration.py.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.services import crawl_watchdog
from config import CRAWL_STALE_NO_PROGRESS_MINUTES, CRAWL_STALE_TIMEOUT_MINUTES


@pytest.fixture
def fake_db(monkeypatch):
    fdb = MagicMock()
    monkeypatch.setattr(crawl_watchdog, "db_module", fdb)
    return fdb


def test_passes_both_thresholds_and_releases_connection(fake_db):
    fake_db.reconcile_stale_crawl_runs.return_value = 0

    crawl_watchdog.run_crawl_watchdog_once()

    fake_db.reconcile_stale_crawl_runs.assert_called_once_with(
        fake_db.get_pooled_connection.return_value,
        CRAWL_STALE_TIMEOUT_MINUTES,
        no_progress_minutes=CRAWL_STALE_NO_PROGRESS_MINUTES,
    )
    fake_db.release_connection.assert_called_once_with(
        fake_db.get_pooled_connection.return_value
    )


def test_error_is_swallowed_rolled_back_and_connection_released(fake_db):
    fake_db.reconcile_stale_crawl_runs.side_effect = RuntimeError("DB lỗi")
    conn = fake_db.get_pooled_connection.return_value

    crawl_watchdog.run_crawl_watchdog_once()  # không được raise ra scheduler

    conn.rollback.assert_called_once()
    fake_db.release_connection.assert_called_once_with(conn)


def test_no_progress_threshold_is_shorter_than_queued_timeout():
    """Ngưỡng 'running không tiến độ' phải nhỏ hơn ngưỡng 'queued' (xếp hàng
    chờ semaphore có thể rất lâu) — đảo ngược sẽ vô nghĩa."""
    assert 1 <= CRAWL_STALE_NO_PROGRESS_MINUTES < CRAWL_STALE_TIMEOUT_MINUTES
