"""
C3b — nhánh MẠNG của check_expired_source_jobs chạy trên LISTING (mock db và checker, không cần Postgres):
hỏi MỌI listing OPEN/UNKNOWN của job OPEN (không chỉ URL hiện hành); 404/410 đóng đúng listing đó rồi đồng bộ
job; 2xx chỉ ghi last_seen_at (UNKNOWN không thành OPEN); manual:// không bị hỏi; --dry-run không ghi.

Chạy: pytest tests/test_check_expired_url_branch.py -v
"""
from datetime import date, timedelta
from unittest.mock import patch

import check_expired_source_jobs as script

TODAY = date.today()
FUTURE = TODAY + timedelta(days=30)
PAST = TODAY - timedelta(days=2)
CLOSED_CHANGE = {"job_status": ("OPEN", "CLOSED"), "closed_reason": (None, "expired_auto")}


class _Checker:
    def __init__(self, codes):
        self.codes, self.calls = codes, []

    def check(self, url):
        self.calls.append(url)
        code = self.codes[url] if isinstance(self.codes, dict) else self.codes
        return code


def _run(mock_conn, listings, codes, *, sync=None, close_dead=None, **kw):
    checker = _Checker(codes)
    with patch.object(script.db, "get_connection", return_value=mock_conn), \
         patch.object(script.db, "list_checkable_listings", return_value=listings), \
         patch.object(script.db, "close_listing_dead", side_effect=close_dead or (lambda c, j, u: True)) as dead, \
         patch.object(script.db, "close_expired_listings", return_value=0), \
         patch.object(script.db, "sync_job_from_listings",
                      side_effect=sync or (lambda c, j: CLOSED_CHANGE)) as sync_mock, \
         patch.object(script.db, "mark_listing_seen") as seen, \
         patch.object(script, "_Throttled404Checker", return_value=checker):
        stats = script.run(skip_cv_cleanup=True, **kw)
    return stats, dead, sync_mock, seen, checker


def test_checks_every_live_listing_of_a_job_not_only_the_current_one(mock_conn):
    listings = [("j1", "DA", "https://x/a", FUTURE), ("j1", "DA", "https://x/b", None),
                ("j1", "DA", "https://x/c", FUTURE)]
    stats, _, _, seen, checker = _run(mock_conn, listings, 200)
    assert checker.calls == ["https://x/a", "https://x/b", "https://x/c"]
    assert seen.call_count == 3 and stats["still_alive"] == 3 and stats["checked"] == 1


def test_dead_listing_closes_only_that_listing_and_job_stays_open(mock_conn):
    listings = [("j1", "DA", "https://x/a", FUTURE), ("j1", "DA", "https://x/b", FUTURE)]
    stats, dead, sync, seen, _ = _run(
        mock_conn, listings, {"https://x/a": 404, "https://x/b": 200}, sync=lambda c, j: {"source_url": ("a", "b")})
    dead.assert_called_once_with(mock_conn, "j1", "https://x/a")
    sync.assert_called_once_with(mock_conn, "j1")
    seen.assert_called_once_with(mock_conn, "https://x/b")
    assert stats["listings_expired_by_source_dead"] == 1 and stats["expired_by_source_dead"] == 0


def test_last_dead_listing_closes_the_job(mock_conn):
    listings = [("j1", "DA", "https://x/a", FUTURE), ("j1", "DA", "https://x/b", FUTURE)]
    stats, *_ = _run(mock_conn, listings, 410)
    assert stats["listings_expired_by_source_dead"] == 2
    # sync trả CLOSED_CHANGE cả hai lần (mock) nên đếm 2 job; thực tế chỉ lần cuối đổi trạng thái
    assert stats["expired_by_source_dead"] >= 1


def test_2xx_does_not_turn_unknown_into_open(mock_conn):
    """Chỉ last_seen_at được ghi; không có hàm nào đổi listing_status."""
    with patch.object(script.db, "close_listing_dead") as dead:
        stats, _, sync, seen, _ = _run(mock_conn, [("j1", "DA", "https://x/a", None)], 200)
    seen.assert_called_once_with(mock_conn, "https://x/a")
    dead.assert_not_called()
    sync.assert_not_called()                           # mark_listing_seen tự đồng bộ, script không đồng bộ thêm
    assert stats["still_alive"] == 1


def test_unclear_codes_are_counted_for_manual_check(mock_conn):
    listings = [("j1", "DA", "https://x/a", None), ("j1", "DA", "https://x/b", None)]
    stats, dead, _, seen, _ = _run(mock_conn, listings, {"https://x/a": 403, "https://x/b": None})
    dead.assert_not_called()
    seen.assert_not_called()
    assert stats["needs_manual_check"] == 2


def test_manual_listings_are_not_fetched(mock_conn):
    listings = [("j1", "DA", "manual://abc", FUTURE), ("j2", "DB", "https://x/b", FUTURE)]
    stats, _, _, _, checker = _run(mock_conn, listings, 200)
    assert checker.calls == ["https://x/b"]
    assert stats["needs_manual_check"] == 0


def test_expired_by_deadline_listings_are_not_fetched(mock_conn):
    listings = [("j1", "DA", "https://x/old", PAST), ("j1", "DA", "https://x/new", FUTURE)]
    _, _, _, _, checker = _run(mock_conn, listings, 200)
    assert checker.calls == ["https://x/new"]


def test_dry_run_writes_nothing_and_predicts_job_close(mock_conn):
    listings = [("j1", "DA", "https://x/a", FUTURE), ("j1", "DA", "https://x/b", FUTURE),
                ("j2", "DB", "https://x/c", FUTURE), ("j2", "DB", "manual://m", FUTURE)]
    codes = {"https://x/a": 404, "https://x/b": 404, "https://x/c": 404}
    stats, dead, sync, seen, _ = _run(mock_conn, listings, codes, dry_run=True)
    dead.assert_not_called()
    sync.assert_not_called()
    seen.assert_not_called()
    mock_conn.commit.assert_not_called()
    assert stats["listings_expired_by_source_dead"] == 3
    assert stats["expired_by_source_dead"] == 1        # j1 đóng cả job; j2 còn listing manual:// sống


def test_error_closing_one_listing_rolls_back_and_continues(mock_conn):
    def dead(conn, job_id, url):
        if url == "https://x/a":
            raise RuntimeError("boom")
        return True

    listings = [("j1", "DA", "https://x/a", FUTURE), ("j2", "DB", "https://x/b", FUTURE)]
    stats, *_ = _run(mock_conn, listings, 404, close_dead=dead)
    mock_conn.rollback.assert_called_once()
    assert stats["listings_expired_by_source_dead"] == 1


def test_limit_keeps_the_same_jobs_in_the_network_branch(mock_conn):
    listings = [("j1", "DA", "https://x/1", FUTURE), ("j2", "DB", "https://x/2", FUTURE),
                ("j3", "DC", "https://x/3", FUTURE)]
    _, _, _, _, checker = _run(mock_conn, listings, 200, limit=2)
    assert checker.calls == ["https://x/1", "https://x/2"]
