"""
C3a — nhánh HẠN của check_expired_source_jobs chạy trên LISTING (mock db, không cần Postgres):
listing OPEN/UNKNOWN có hạn đã qua thì đóng qua db.close_expired_listings, job đồng bộ qua
db.sync_job_from_listings, commit từng job; số liệu đếm đúng; --dry-run không ghi; --check-deadline không
đụng mạng; nhánh mạng bỏ qua job vừa (hoặc sẽ) đóng vì hạn.

Chạy: pytest tests/test_check_expired_deadline_branch.py -v
"""
from datetime import date, timedelta
from unittest.mock import patch

import check_expired_source_jobs as script

TODAY = date.today()
PAST = TODAY - timedelta(days=2)
FUTURE = TODAY + timedelta(days=30)
CLOSED_CHANGE = {"job_status": ("OPEN", "CLOSED"), "closed_reason": (None, "expired_auto")}


class _Checker:
    def __init__(self, code=200):
        self.code, self.calls = code, []

    def check(self, url):
        self.calls.append(url)
        return self.code


def _run(mock_conn, listings, *, sync=None, code=200, **kw):
    """listings = [(job_id, title, url, deadline)] cho list_checkable_listings. Sau nhánh hạn, lần đọc thứ hai
    (nhánh mạng) trả cùng danh sách trừ các listing hạn đã qua (như DB thật, vì chúng đã bị đóng)."""
    checker = _Checker(code)
    calls = {"n": 0}

    def list_checkable(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            return listings
        return [r for r in listings if not (r[3] is not None and r[3] < TODAY)]

    with patch.object(script.db, "get_connection", return_value=mock_conn), \
         patch.object(script.db, "list_checkable_listings", side_effect=list_checkable) as list_mock, \
         patch.object(script.db, "close_expired_listings", side_effect=kw.pop("close_side", None)
                      or (lambda conn, job_id, today: 1)) as close, \
         patch.object(script.db, "close_listing_dead", return_value=True), \
         patch.object(script.db, "sync_job_from_listings",
                      side_effect=sync or (lambda conn, job_id: CLOSED_CHANGE)) as sync_mock, \
         patch.object(script.db, "mark_listing_seen"), \
         patch.object(script, "_Throttled404Checker", return_value=checker):
        stats = script.run(skip_cv_cleanup=True, **kw)
    return stats, close, sync_mock, None, checker, list_mock


def test_expired_listing_closes_listing_then_syncs_job(mock_conn):
    stats, close, sync, _, _, _ = _run(mock_conn, [("j1", "DA", "https://x/1", PAST)], check_deadline_only=True)
    close.assert_called_once_with(mock_conn, "j1", TODAY)
    sync.assert_called_once_with(mock_conn, "j1")
    mock_conn.commit.assert_called()
    assert stats["expired_by_deadline"] == 1 and stats["listings_expired_by_deadline"] == 1
    assert stats["checked"] == 1


def test_future_or_missing_deadline_is_left_alone(mock_conn):
    stats, close, sync, *_ = _run(
        mock_conn, [("j1", "DA", "https://x/1", FUTURE), ("j2", "DB", "https://x/2", None)],
        check_deadline_only=True)
    close.assert_not_called()
    sync.assert_not_called()
    assert stats["expired_by_deadline"] == 0 and stats["checked"] == 2


def test_deadline_of_today_is_not_expired(mock_conn):
    stats, close, *_ = _run(mock_conn, [("j1", "DA", "https://x/1", TODAY)])
    close.assert_not_called()
    assert stats["expired_by_deadline"] == 0


def test_partial_expiry_counts_listing_but_not_job(mock_conn):
    # job còn một listing chưa hết hạn: sync không đóng job (trả {}), chỉ listing được đếm
    listings = [("j1", "DA", "https://x/old", PAST), ("j1", "DA", "https://x/new", FUTURE)]
    stats, close, sync, *_ = _run(mock_conn, listings, sync=lambda conn, job_id: {"source_url": ("a", "b")})
    close.assert_called_once()
    assert stats["expired_by_deadline"] == 0
    assert stats["listings_expired_by_deadline"] == 1
    assert stats["checked"] == 1                        # đếm theo JOB, không theo listing


def test_all_listings_expired_counts_one_job(mock_conn):
    listings = [("j1", "DA", "https://x/a", PAST), ("j1", "DA", "https://x/b", PAST)]
    stats, *_ = _run(mock_conn, listings, close_side=lambda conn, job_id, today: 2)
    assert stats["expired_by_deadline"] == 1 and stats["listings_expired_by_deadline"] == 2


def test_dry_run_writes_nothing_but_counts(mock_conn):
    listings = [("j1", "DA", "https://x/a", PAST), ("j2", "DB", "https://x/b", PAST),
                ("j2", "DB", "https://x/c", FUTURE)]
    stats, close, sync, *_ = _run(mock_conn, listings, dry_run=True, check_deadline_only=True)
    close.assert_not_called()
    sync.assert_not_called()
    mock_conn.commit.assert_not_called()
    assert stats["expired_by_deadline"] == 1            # chỉ j1 sẽ đóng cả job
    assert stats["listings_expired_by_deadline"] == 2


def test_check_deadline_only_skips_network_branch(mock_conn):
    stats, _, _, _, checker, list_mock = _run(
        mock_conn, [("j1", "DA", "https://x/1", FUTURE)], check_deadline_only=True)
    assert list_mock.call_count == 1                    # không đọc lại danh sách cho nhánh mạng
    assert checker.calls == [] and stats["still_alive"] == 0


def test_network_branch_skips_jobs_closed_by_deadline(mock_conn):
    listings = [("j1", "DA", "https://x/1", PAST), ("j2", "DB", "https://x/2", FUTURE)]
    stats, _, _, _, checker, _ = _run(
        mock_conn, listings,
        sync=lambda conn, job_id: CLOSED_CHANGE if job_id == "j1" else {})
    assert checker.calls == ["https://x/2"]
    assert stats["still_alive"] == 1


def test_dry_run_network_branch_skips_jobs_that_would_close(mock_conn):
    listings = [("j1", "DA", "https://x/1", PAST), ("j2", "DB", "https://x/2", FUTURE)]
    _, _, _, _, checker, _ = _run(mock_conn, listings, dry_run=True)
    assert checker.calls == ["https://x/2"]


def test_limit_applies_to_jobs_in_both_branches(mock_conn):
    listings = [("j1", "DA", "https://x/1", FUTURE), ("j1", "DA", "https://x/1b", FUTURE),
                ("j2", "DB", "https://x/2", FUTURE), ("j3", "DC", "https://x/3", FUTURE)]
    stats, _, _, _, checker, _ = _run(mock_conn, listings, limit=2)
    assert stats["checked"] == 2
    assert checker.calls == ["https://x/1", "https://x/1b", "https://x/2"]   # mọi listing của 2 job đầu


def test_error_on_one_job_rolls_back_and_continues(mock_conn):
    def close(conn, job_id, today):
        if job_id == "j1":
            raise RuntimeError("boom")
        return 1

    listings = [("j1", "DA", "https://x/1", PAST), ("j2", "DB", "https://x/2", PAST)]
    stats, *_ = _run(mock_conn, listings, close_side=close)
    mock_conn.rollback.assert_called_once()
    assert stats["expired_by_deadline"] == 1            # j2 vẫn xử lý được
