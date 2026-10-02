"""
db.crawl_runs đợt 3: mark_error() ghi stats tạm khi bị chặn,
get_recent_blocked_run() — mock cursor, KHÔNG cần database. Câu SQL thật
(stats->>'blocked', make_interval) được kiểm ở test_pg_integration.py.
"""

import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.crawl_runs import get_recent_blocked_run, mark_error


def _conn(fetchone=None):
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = fetchone
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


def test_mark_error_without_stats_does_not_touch_stats_column():
    conn, cur = _conn()
    mark_error(conn, "run-1", "lỗi")
    sql, params = cur.execute.call_args.args
    assert "stats" not in sql
    assert params[0] == "lỗi" and params[2] == "run-1"
    conn.commit.assert_called_once()


def test_mark_error_with_stats_writes_json():
    conn, cur = _conn()
    mark_error(conn, "run-1", "bị chặn", stats={"blocked": True, "inserted": 5})
    sql, params = cur.execute.call_args.args
    assert "stats = %s" in sql
    assert json.loads(params[2]) == {"blocked": True, "inserted": 5}
    assert params[3] == "run-1"


def test_get_recent_blocked_run_returns_row():
    when = datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)
    conn, cur = _conn(fetchone=("abc", when))
    result = get_recent_blocked_run(conn, "topcv", 60)
    assert result == {"run_id": "abc", "finished_at": when}
    sql, params = cur.execute.call_args.args
    assert "stats->>'blocked' = 'true'" in sql
    assert params == ("topcv", 60)


def test_get_recent_blocked_run_none_when_no_row():
    conn, _ = _conn(fetchone=None)
    assert get_recent_blocked_run(conn, "topcv", 60) is None


def test_get_recent_blocked_run_zero_window_skips_query():
    conn, cur = _conn()
    assert get_recent_blocked_run(conn, "topcv", 0) is None
    cur.execute.assert_not_called()
