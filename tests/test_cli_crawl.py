"""
main.py cmd_crawl đợt 3.5 (10/2026): CLI chạy qua cùng đường với nút Crawl
trên web (api/crawl_runner.execute) để lượt chạy trên máy cũng có dòng
crawl_runs, cờ blocked/degraded và snapshot — KHÔNG cần database/internet.
"""

import argparse
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import api.crawl_runner as runner
import main
from adapters.base import CrawlBlockedError


class ActiveCrawlExistsError(Exception):
    pass


def _args(**kw):
    base = dict(source="topcv", category="data-analyst", pages=2, max_jobs=None, no_track=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _install(monkeypatch, *, row=None, create_exc=None, execute_exc=None, pipeline=None):
    fdb = MagicMock()
    fdb.ActiveCrawlExistsError = ActiveCrawlExistsError
    fdb.create_crawl_run.return_value = "run-1"
    if create_exc:
        fdb.create_crawl_run.side_effect = create_exc
    fdb.get_crawl_run.return_value = row or {"status": "done", "stats": {"fetched": 3, "inserted": 2}}
    fdb.count_jobs.return_value = 99
    monkeypatch.setattr(main, "db", fdb)
    execute = MagicMock(side_effect=execute_exc)
    monkeypatch.setattr(runner, "execute", execute)
    run_pipeline = MagicMock(side_effect=pipeline) if callable(pipeline) else MagicMock(
        return_value={"fetched": 1, "inserted": 1, "skipped_duplicate": 0, "errors": 0})
    monkeypatch.setattr(main, "run_pipeline", run_pipeline)
    return fdb, execute, run_pipeline


def test_tracked_run_creates_row_executes_and_prints(monkeypatch, capsys):
    fdb, execute, run_pipeline = _install(monkeypatch)

    main.cmd_crawl(_args(max_jobs=5))

    kwargs = fdb.create_crawl_run.call_args.kwargs
    assert kwargs["source"] == "topcv" and kwargs["category"] == "data-analyst"
    assert kwargs["pages"] == 2 and kwargs["max_jobs"] == 5 and kwargs["triggered_by"] is None
    execute.assert_called_once_with("run-1")
    run_pipeline.assert_not_called()          # không chạy 2 lần
    out = capsys.readouterr().out
    assert "KẾT QUẢ" in out and "Tổng job crawl được : 3" in out and "run-1" in out


def test_blocked_run_exits_2_and_prints_partial_counts(monkeypatch, capsys):
    row = {"status": "error", "error": "3 lần fetch liên tiếp thất bại",
           "stats": {"blocked": True, "fetched": 7, "inserted": 5}}
    _install(monkeypatch, row=row)

    with pytest.raises(SystemExit) as exc_info:
        main.cmd_crawl(_args())

    assert exc_info.value.code == 2
    out = capsys.readouterr().out
    assert "3 lần fetch" in out and "7 job" in out and "5" in out


def test_ordinary_error_exits_1(monkeypatch):
    _install(monkeypatch, row={"status": "error", "error": "lỗi parse", "stats": None})
    with pytest.raises(SystemExit) as exc_info:
        main.cmd_crawl(_args())
    assert exc_info.value.code == 1


def test_source_already_running_exits_1_without_executing(monkeypatch, capsys):
    fdb, execute, _ = _install(monkeypatch, create_exc=ActiveCrawlExistsError("đang chạy run-9"))

    with pytest.raises(SystemExit) as exc_info:
        main.cmd_crawl(_args())

    assert exc_info.value.code == 1
    execute.assert_not_called()
    assert "đang chạy run-9" in capsys.readouterr().out


def test_ctrl_c_marks_run_error_and_exits_130(monkeypatch):
    fdb, _, _ = _install(monkeypatch, execute_exc=KeyboardInterrupt())

    with pytest.raises(SystemExit) as exc_info:
        main.cmd_crawl(_args())

    assert exc_info.value.code == 130
    args = fdb.mark_crawl_run_error.call_args.args
    assert args[1] == "run-1" and "Ctrl+C" in args[2]


def test_missing_table_falls_back_to_untracked_run(monkeypatch, capsys):
    """DB local chưa migrate (không có crawl_runs): vẫn crawl được như cũ."""
    fdb, execute, run_pipeline = _install(monkeypatch, create_exc=RuntimeError('relation "crawl_runs" does not exist'))

    main.cmd_crawl(_args())

    fdb_conn = fdb.get_connection.return_value
    fdb_conn.rollback.assert_called()
    execute.assert_not_called()
    run_pipeline.assert_called_once()
    out = capsys.readouterr().out
    assert "migrate" in out and "KẾT QUẢ" in out


def test_no_track_flag_skips_run_row_entirely(monkeypatch):
    fdb, execute, run_pipeline = _install(monkeypatch)

    main.cmd_crawl(_args(no_track=True))

    fdb.create_crawl_run.assert_not_called()
    execute.assert_not_called()
    run_pipeline.assert_called_once()


def test_untracked_blocked_exits_2(monkeypatch, capsys):
    def boom(*a, **kw):
        exc = CrawlBlockedError("bị chặn")
        exc.stats = {"fetched": 4, "inserted": 3}
        raise exc
    _install(monkeypatch, pipeline=boom)

    with pytest.raises(SystemExit) as exc_info:
        main.cmd_crawl(_args(no_track=True))
    assert exc_info.value.code == 2
    assert "4 job" in capsys.readouterr().out


def test_unknown_source_and_category_still_rejected(monkeypatch):
    _install(monkeypatch)
    with pytest.raises(SystemExit):
        main.cmd_crawl(_args(source="nope"))
    with pytest.raises(SystemExit):
        main.cmd_crawl(_args(category="khong-co"))


def test_degraded_warning_is_printed(monkeypatch, capsys):
    stats = {"fetched": 12, "inserted": 12, "degraded": {"reasons": [
        {"type": "field_empty", "group": "detail", "field": "job_description",
         "empty": 12, "total": 12, "rate": 1.0}]}}
    _install(monkeypatch, row={"status": "done", "stats": stats})

    main.cmd_crawl(_args())
    out = capsys.readouterr().out
    assert "DEGRADED" in out and "job_description" in out
