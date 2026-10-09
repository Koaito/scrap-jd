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
         patch.object(script.db, "get_open_jobs_with_source_url",
                      return_value=[("job-1", "Data Analyst", URL, FUTURE)]), \
         patch.object(script.db, "update_job") as update_job, \
         patch.object(script.db, "mark_listing_seen") as seen, \
         patch.object(script, "_Throttled404Checker", return_value=_Checker(code)):
        stats = script.run(skip_cv_cleanup=True, **kw)
    return stats, seen, update_job


@pytest.mark.parametrize("code", [200, 204, 299])
def test_alive_url_marks_listing_seen_and_commits(mock_conn, code):
    stats, seen, update_job = _run(mock_conn, code)
    seen.assert_called_once_with(mock_conn, URL)
    mock_conn.commit.assert_called()
    update_job.assert_not_called()
    assert stats["still_alive"] == 1


@pytest.mark.parametrize("code", [404, 410, 403, 500, 301, None])
def test_dead_or_unclear_url_does_not_mark_listing_seen(mock_conn, code):
    _, seen, _ = _run(mock_conn, code)
    seen.assert_not_called()


def test_dry_run_does_not_mark_listing_seen(mock_conn):
    stats, seen, _ = _run(mock_conn, 200, dry_run=True)
    seen.assert_not_called()
    assert stats["still_alive"] == 1


def test_dead_url_closes_job_with_expired_auto(mock_conn):
    stats, seen, update_job = _run(mock_conn, 404)
    update_job.assert_called_once_with(mock_conn, "job-1", job_status="CLOSED", closed_reason="expired_auto")
    assert stats["expired_by_source_dead"] == 1
