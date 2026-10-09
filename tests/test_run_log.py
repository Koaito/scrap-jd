"""
scrapjd/api/run_log.capture_run_logs(): log live tách riêng theo từng lượt chạy
(trước đây handler gắn vào root logger nhận mọi record nên 2 lượt chạy song
song ghi lẫn log của nhau) — KHÔNG cần database, KHÔNG cần internet.
"""

import logging
import os
import sys
import threading
from unittest.mock import MagicMock


sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scrapjd.api.crawl_runner as crawl_runner
import scrapjd.api.maintenance_runner as maintenance_runner
from scrapjd.api.run_log import capture_run_logs

log = logging.getLogger("tests.run_log")


class Sink:
    """Giả lập db.append_*_run_log: gom (run_id, level, message)."""

    def __init__(self):
        self.rows = []
        self.conn = MagicMock(name="log_conn")

    def open_connection(self):
        return self.conn

    def append(self, conn, run_id, level, message):
        assert conn is self.conn
        self.rows.append((run_id, level, message))

    def messages(self):
        return [m for _, _, m in self.rows]


def _capture(run_id, sink):
    return capture_run_logs(run_id, open_connection=sink.open_connection,
                            append_log=sink.append)


def setup_function(_):
    logging.getLogger().setLevel(logging.INFO)


def test_logs_inside_block_are_captured_with_level():
    sink = Sink()
    with _capture("run-1", sink):
        log.info("xin chào")
        log.warning("cảnh báo")
    assert sink.rows == [("run-1", "INFO", "xin chào"), ("run-1", "WARNING", "cảnh báo")]


def test_debug_logs_are_not_captured():
    sink = Sink()
    logging.getLogger().setLevel(logging.DEBUG)
    with _capture("run-1", sink):
        log.debug("chi tiết")
    assert sink.rows == []


def test_handler_removed_and_connection_closed_on_exit():
    sink = Sink()
    before = list(logging.getLogger().handlers)
    with _capture("run-1", sink):
        assert len(logging.getLogger().handlers) == len(before) + 1
    assert logging.getLogger().handlers == before
    sink.conn.close.assert_called_once()
    log.info("sau khi thoát")
    assert "sau khi thoát" not in sink.messages()


def test_cleanup_happens_when_block_raises():
    sink = Sink()
    before = list(logging.getLogger().handlers)
    try:
        with _capture("run-1", sink):
            raise ValueError("lỗi giữa chừng")
    except ValueError:
        pass
    assert logging.getLogger().handlers == before
    sink.conn.close.assert_called_once()


def test_two_parallel_runs_do_not_mix_logs():
    """Ca lỗi thật trước đây: 2 lượt chạy đồng thời, mỗi handler nhận cả log
    của lượt kia. Barrier ép cả 2 thread cùng nằm trong khối capture rồi ghi
    xen kẽ."""
    sinks = {"A": Sink(), "B": Sink()}
    barrier = threading.Barrier(2)

    def worker(name):
        with _capture(f"run-{name}", sinks[name]):
            barrier.wait()
            for i in range(20):
                log.info("%s-%d", name, i)
            barrier.wait()

    threads = [threading.Thread(target=worker, args=(n,)) for n in sinks]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    for name, sink in sinks.items():
        assert len(sink.rows) == 20
        assert {rid for rid, _, _ in sink.rows} == {f"run-{name}"}
        assert all(m.startswith(f"{name}-") for m in sink.messages())


def test_logs_from_unrelated_thread_are_not_captured():
    """Log của request API/uvicorn chạy ở thread khác trong lúc lượt chạy
    không được lọt vào log của lượt."""
    sink = Sink()
    with _capture("run-1", sink):
        t = threading.Thread(target=lambda: log.info("từ thread khác"))
        t.start()
        t.join()
        log.info("từ chính lượt chạy")
    assert sink.messages() == ["từ chính lượt chạy"]


def test_same_run_id_captured_twice_sequentially_is_independent():
    first, second = Sink(), Sink()
    with _capture("run-1", first):
        log.info("lần 1")
    with _capture("run-1", second):
        log.info("lần 2")
    assert first.messages() == ["lần 1"]
    assert second.messages() == ["lần 2"]


def test_nested_capture_restores_outer_scope():
    outer, inner = Sink(), Sink()
    with _capture("outer", outer):
        log.info("a")
        with _capture("inner", inner):
            log.info("b")
        log.info("c")
    assert outer.messages() == ["a", "c"]
    assert inner.messages() == ["b"]


def test_append_failure_does_not_break_the_job():
    sink = Sink()
    sink.append = MagicMock(side_effect=RuntimeError("DB mất kết nối"))
    with _capture("run-1", sink):
        log.info("vẫn chạy tiếp")          # không được raise
    assert sink.append.called


# ---------------------------------------------------------------------
# Tích hợp với 2 runner (mock module db như test_crawl_runner_blocked.py)
# ---------------------------------------------------------------------

def _install_crawl(monkeypatch, sink, pipeline):
    from scrapjd.adapters.base import BaseAdapter

    class FakeAdapter(BaseAdapter):
        source_name = "Fake"

        def fetch_jobs(self, category_key, max_pages):
            return iter(())

    fdb = MagicMock()
    fdb.get_crawl_run.return_value = {
        "run_id": "run-1", "source": "fakesrc", "category": "x", "pages": 1,
        "max_jobs": None, "batch_id": None, "batch_position": None,
    }
    fdb.get_recent_blocked_crawl_run.return_value = None
    # lần gọi get_connection() thứ 2 là của handler: trả conn của sink
    conns = iter([MagicMock(name="main_conn"), sink.conn])
    fdb.get_connection.side_effect = lambda: next(conns)
    fdb.append_crawl_run_log.side_effect = sink.append
    monkeypatch.setattr(crawl_runner, "db_module", fdb)
    monkeypatch.setattr(crawl_runner, "_SOURCE_ADAPTERS", {"fakesrc": FakeAdapter})
    monkeypatch.setattr(crawl_runner, "run_pipeline", pipeline)
    return fdb


def test_crawl_runner_logs_only_its_own_run(monkeypatch):
    sink = Sink()

    def pipeline(adapter, conn, category, pages, **kw):
        log.info("log của pipeline")
        t = threading.Thread(target=lambda: log.info("log của request khác"))
        t.start()
        t.join()
        return {"fetched": 0, "inserted": 0}

    _install_crawl(monkeypatch, sink, pipeline)
    before = list(logging.getLogger().handlers)

    crawl_runner._execute_one("run-1")

    assert sink.messages() == ["log của pipeline"]
    assert {rid for rid, _, _ in sink.rows} == {"run-1"}
    assert logging.getLogger().handlers == before        # handler đã gỡ
    sink.conn.close.assert_called_once()


def test_crawl_runner_error_line_is_still_captured(monkeypatch):
    """Dòng log lỗi do chính runner ghi (logger.error trong except) phải nằm
    trong log của lượt — người xem log live cần thấy nguyên nhân."""
    sink = Sink()

    def pipeline(*a, **kw):
        raise ValueError("hỏng")

    _install_crawl(monkeypatch, sink, pipeline)
    crawl_runner._execute_one("run-1")
    assert any("lỗi: hỏng" in m for m in sink.messages())


def test_maintenance_runner_logs_only_its_own_run(monkeypatch):
    sink = Sink()
    fdb = MagicMock()
    fdb.get_maintenance_run.return_value = {
        "run_id": "m-1", "job_type": "fakejob", "params": {},
    }
    conns = iter([MagicMock(name="main_conn"), sink.conn])
    fdb.get_connection.side_effect = lambda: next(conns)
    fdb.append_maintenance_run_log.side_effect = sink.append

    def fake_job():
        log.info("log của job maintenance")
        t = threading.Thread(target=lambda: log.info("log của crawl chạy song song"))
        t.start()
        t.join()
        return {"ok": True}

    monkeypatch.setattr(maintenance_runner, "db_module", fdb)
    monkeypatch.setattr(maintenance_runner, "_JOB_RUNNERS", {"fakejob": fake_job})
    before = list(logging.getLogger().handlers)

    maintenance_runner._execute_locked("m-1")

    assert sink.messages() == ["log của job maintenance"]
    assert logging.getLogger().handlers == before
    fdb.mark_maintenance_run_done.assert_called_once()
