"""
scrapjd/api/crawl_runner._execute_one() đợt 3 (10/2026): lượt bị chặn ghi stats tạm +
cờ blocked, dừng cả batch; snapshot được lưu kể cả khi lỗi; cảnh báo nguồn vừa
bị chặn — KHÔNG cần database (mock module db), KHÔNG cần internet.
"""

import logging
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scrapjd.api.crawl_runner as runner
from scrapjd.adapters.base import BaseAdapter, CrawlBlockedError

RUN = {"run_id": "run-1", "source": "fakesrc", "category": "data-analyst",
       "pages": 3, "max_jobs": None, "batch_id": None, "batch_position": None}


class FakeAdapter(BaseAdapter):
    source_name = "Fake"

    def fetch_jobs(self, category_key, max_pages):
        return iter(())


def _install(monkeypatch, *, run=None, pipeline=None, recent_blocked=None):
    fdb = MagicMock()
    fdb.get_crawl_run.return_value = dict(run or RUN)
    fdb.get_recent_blocked_crawl_run.return_value = recent_blocked
    fdb.advance_crawl_batch.return_value = "run-2"
    fdb.save_crawl_snapshots.return_value = 1
    monkeypatch.setattr(runner, "db_module", fdb)
    monkeypatch.setattr(runner, "_SOURCE_ADAPTERS", {"fakesrc": FakeAdapter})
    monkeypatch.setattr(runner, "run_pipeline", pipeline or (lambda *a, **kw: {"fetched": 0, "inserted": 0}))
    return fdb


def _blocked_pipeline(stats):
    def fake(adapter, conn, category, pages, **kw):
        # giả lập adapter đã gom được 1 snapshot trước khi bị chặn
        adapter.snapshot_recorder.offer("listing", "https://x", "<html>challenge</html>",
                                        reason="listing_empty")
        exc = CrawlBlockedError("3 lần fetch liên tiếp thất bại")
        exc.stats = stats
        raise exc
    return fake


def test_blocked_run_marks_error_with_partial_stats_and_blocked_flag(monkeypatch):
    fdb = _install(monkeypatch, pipeline=_blocked_pipeline({"fetched": 7, "inserted": 5, "errors": 0}))

    assert runner._execute_one("run-1") is None

    args, kwargs = fdb.mark_crawl_run_error.call_args
    assert args[1] == "run-1" and "3 lần fetch" in args[2]
    assert kwargs["stats"]["blocked"] is True
    assert kwargs["stats"]["inserted"] == 5
    fdb.mark_crawl_run_done.assert_not_called()


def test_blocked_without_exc_stats_still_flags_blocked(monkeypatch):
    def fake(adapter, conn, category, pages, **kw):
        raise CrawlBlockedError("trang đầu thất bại")   # adapter ngoài pipeline: không có .stats
    fdb = _install(monkeypatch, pipeline=fake)

    runner._execute_one("run-1")
    assert fdb.mark_crawl_run_error.call_args.kwargs["stats"] == {"blocked": True}


def test_ordinary_error_has_no_stats_and_no_blocked_flag(monkeypatch):
    def fake(*a, **kw):
        raise ValueError("lỗi bình thường")
    fdb = _install(monkeypatch, pipeline=fake)

    runner._execute_one("run-1")
    assert fdb.mark_crawl_run_error.call_args.kwargs == {}     # như hành vi cũ


def test_blocked_run_in_batch_stops_the_batch(monkeypatch):
    run = dict(RUN, batch_id="batch-1", batch_position=0)
    fdb = _install(monkeypatch, run=run, pipeline=_blocked_pipeline({"fetched": 1}))

    assert runner._execute_one("run-1") is None            # KHÔNG trả run kế tiếp
    fdb.advance_crawl_batch.assert_not_called()
    args = fdb.mark_crawl_batch_error.call_args.args
    assert args[1] == "batch-1" and "data-analyst" in args[2]


def test_ordinary_error_in_batch_still_advances(monkeypatch):
    """Lỗi thường (không phải bị chặn) giữ hành vi cũ: batch chạy tiếp."""
    run = dict(RUN, batch_id="batch-1", batch_position=0)

    def fake(*a, **kw):
        raise ValueError("lỗi parse")
    fdb = _install(monkeypatch, run=run, pipeline=fake)

    assert runner._execute_one("run-1") == "run-2"
    fdb.mark_crawl_batch_error.assert_not_called()


def test_successful_run_in_batch_advances(monkeypatch):
    run = dict(RUN, batch_id="batch-1", batch_position=0)
    fdb = _install(monkeypatch, run=run)
    assert runner._execute_one("run-1") == "run-2"
    fdb.mark_crawl_run_done.assert_called_once()


def test_snapshots_saved_even_when_run_is_blocked(monkeypatch):
    fdb = _install(monkeypatch, pipeline=_blocked_pipeline({"fetched": 0}))

    runner._execute_one("run-1")

    args = fdb.save_crawl_snapshots.call_args.args
    assert args[1] == "run-1" and args[2] == "fakesrc"
    assert [(i.kind, i.reason) for i in args[3]] == [("listing", "listing_empty")]


def test_no_snapshots_no_db_call(monkeypatch):
    fdb = _install(monkeypatch)
    runner._execute_one("run-1")
    fdb.save_crawl_snapshots.assert_not_called()


def test_snapshot_failure_does_not_change_run_result(monkeypatch):
    fdb = _install(monkeypatch, pipeline=_blocked_pipeline({"fetched": 0}))
    fdb.save_crawl_snapshots.side_effect = RuntimeError("DB lỗi")

    runner._execute_one("run-1")           # không raise
    fdb.mark_crawl_run_error.assert_called_once()


def test_adapter_gets_a_recorder(monkeypatch):
    seen = {}

    def fake(adapter, conn, category, pages, **kw):
        seen["rec"] = adapter.snapshot_recorder
        return {"fetched": 0, "inserted": 0}
    _install(monkeypatch, pipeline=fake)

    runner._execute_one("run-1")
    assert seen["rec"] is not None


def test_warns_when_source_recently_blocked(monkeypatch, caplog):
    recent = {"run_id": "run-0", "finished_at": datetime(2026, 10, 2, 8, 0, tzinfo=timezone.utc)}
    _install(monkeypatch, recent_blocked=recent)

    with caplog.at_level(logging.WARNING):
        runner._execute_one("run-1")

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("vừa có lượt crawl bị chặn" in w and "run-0" in w for w in warnings)


def test_no_warning_when_not_recently_blocked(monkeypatch, caplog):
    _install(monkeypatch, recent_blocked=None)
    with caplog.at_level(logging.WARNING):
        runner._execute_one("run-1")
    assert not [r for r in caplog.records if "vừa có lượt crawl bị chặn" in r.getMessage()]


def test_cooldown_lookup_failure_does_not_break_the_run(monkeypatch):
    fdb = _install(monkeypatch)
    fdb.get_recent_blocked_crawl_run.side_effect = RuntimeError("DB lỗi")

    runner._execute_one("run-1")
    fdb.mark_crawl_run_done.assert_called_once()


def test_cooldown_zero_skips_lookup(monkeypatch):
    fdb = _install(monkeypatch)
    monkeypatch.setattr(runner, "CRAWL_BLOCK_COOLDOWN_MINUTES", 0)
    runner._execute_one("run-1")
    fdb.get_recent_blocked_crawl_run.assert_not_called()
