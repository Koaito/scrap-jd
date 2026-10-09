"""
check_expired_source_jobs ghi bằng chứng "còn sống" cho listing (C1 nửa 2/2): URL trả HTTP 2xx thì
db.mark_listing_seen (chỉ last_seen_at, không đổi trạng thái) rồi commit; 404/410, lỗi mạng, 403... thì
không ghi; --dry-run không ghi gì. Mock db và checker mạng, không cần Postgres.

Chạy: pytest tests/test_check_expired_listing_seen.py -v
"""
from datetime import date, timedelta
from unittest.mock import patch

import pytest

import check_expired_source_jobs as script

URL = "https://x/job-1"
FUTURE = date.today() + timedelta(days=30)


class _Checker:
    def __init__(self, code):
        self.code = code

    def check(self, source_url):
        return self.code


def _run(mock_conn, code, **kw):
    with patch.object(script.db, "get_connection", return_value=mock_conn), \
         patch.object(script.db, "list_checkable_listings",
                      return_value=[("job-1", "Data Analyst", URL, FUTURE)]), \
         patch.object(script.db, "close_listing_dead", return_value=True) as close_dead, \
         patch.object(script.db, "sync_job_from_listings", return_value={}), \
         patch.object(script.db, "mark_listing_seen") as seen, \
         patch.object(script, "_Throttled404Checker", return_value=_Checker(code)):
        stats = script.run(skip_cv_cleanup=True, **kw)
    return stats, seen, close_dead


@pytest.mark.parametrize("code", [200, 204, 299])
def test_alive_url_marks_listing_seen_and_commits(mock_conn, code):
    stats, seen, close_dead = _run(mock_conn, code)
    seen.assert_called_once_with(mock_conn, URL)
    mock_conn.commit.assert_called()
    close_dead.assert_not_called()
    assert stats["still_alive"] == 1


@pytest.mark.parametrize("code", [404, 410, 403, 500, 301, None])
def test_dead_or_unclear_url_does_not_mark_listing_seen(mock_conn, code):
    _, seen, _ = _run(mock_conn, code)
    seen.assert_not_called()


def test_dry_run_does_not_mark_listing_seen(mock_conn):
    stats, seen, _ = _run(mock_conn, 200, dry_run=True)
    seen.assert_not_called()
    assert stats["still_alive"] == 1


def test_dead_url_closes_listing_then_syncs_job(mock_conn):
    stats, seen, close_dead = _run(mock_conn, 404)
    close_dead.assert_called_once_with(mock_conn, "job-1", URL)
    assert stats["listings_expired_by_source_dead"] == 1
    seen.assert_not_called()
