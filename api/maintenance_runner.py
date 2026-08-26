"""
Chạy 1 trong 5 script bảo trì dữ liệu (backfill_company_profiles.py,
enrich_company_profile_from_website.py, enrich_company_web_info.py,
get_company_fb_linkedin_link.py, check_expired_source_jobs.py) ở NỀN,
kích hoạt từ 1 HTTP request (POST /maintenance/{job_type}) — ĐỐI XỨNG
HOÀN TOÀN api/crawl_runner.py, cho phép admin bấm nút trên web thay vì
gõ tay CLI từ máy local (08/2026, xem lịch sử trao đổi "phương án B —
generic runner dùng chung", sql/migration_add_maintenance_runs.sql).

CÁCH HOẠT ĐỘNG — y hệt crawl_runner.py:
  1. POST /maintenance/{job_type} -> start_run() insert 1 dòng
     status='queued' vào bảng maintenance_runs, trả về run_id NGAY.
  2. FastAPI BackgroundTasks chạy execute() sau khi response đã trả —
     execute() tự mở connection DB riêng, đổi status -> 'running', gọi
     thẳng hàm run(**params) của script tương ứng (_JOB_RUNNERS bên
     dưới) y hệt CLI (python backfill_company_profiles.py --limit N)
     đang làm, rồi đổi status -> 'done'/'error'.
  3. Client gọi GET /maintenance/{run_id} để poll tiến độ + GET
     /maintenance/{run_id}/logs để xem log live — đọc thẳng từ
     maintenance_runs/maintenance_run_logs.

KHÁC crawl_runner.py:
  - Khoá đồng thời theo job_type (không phải source) — xem docstring
    sql/migration_add_maintenance_runs.sql.
  - Không có khái niệm batch/nhiều category nối tiếp — mỗi lượt bấm là
    1 run độc lập, KHÔNG tự tạo run tiếp theo sau khi xong (khác
    execute() ở crawl_runner.py).
  - Không có heartbeat/progress theo thời gian thực (crawl_runs.progress)
    — 5 hàm run() hiện tại không hỗ trợ callback on_progress như
    pipeline.run_pipeline(), chỉ có log live (đủ dùng để theo dõi, các
    lượt chạy ngắn hơn nhiều so với crawl nhiều trang). Có thể thêm sau
    nếu cần, không làm sớm.

CHỌN ĐÚNG HÀM run() THEO job_type — _JOB_RUNNERS là nơi ĐĂNG KÝ DUY
NHẤT, giống _SOURCE_ADAPTERS ở sources_registry.py: thêm job thứ 6 sau
này chỉ cần thêm 1 entry vào đây + enum maintenance_job_type_enum ở
migration + 1 entry label ở frontend, KHÔNG cần bảng/router mới.
"""

import logging
from typing import Optional

import db as db_module

import backfill_company_profiles
import enrich_company_profile_from_website
import enrich_company_web_info
import get_company_fb_linkedin_link
import check_expired_source_jobs

logger = logging.getLogger(__name__)

# Nguồn sự thật duy nhất cho "job_type nào gọi hàm run() nào" — khớp
# đúng enum maintenance_job_type_enum trong
# sql/migration_add_maintenance_runs.sql (khác tên do đặt ngắn gọn hơn
# tên file .py, xem api/schemas/maintenance.py để biết nhãn hiển thị).
_JOB_RUNNERS = {
    "backfill_company_profiles": backfill_company_profiles.run,
    "enrich_profile_from_website": enrich_company_profile_from_website.run,
    "enrich_web_info": enrich_company_web_info.run,
    "get_fb_linkedin": get_company_fb_linkedin_link.run,
    "check_expired_jobs": check_expired_source_jobs.run,
}


class _RunLogHandler(logging.Handler):
    """Đối xứng api.crawl_runner._RunLogHandler — cùng cách gắn tạm vào
    ROOT logger trong lúc execute() chạy, cùng đánh đổi CHẤP NHẬN ĐƯỢC
    khi 2 job_type chạy song song (log lẫn vào cả 2 run's log, xem
    docstring gốc ở crawl_runner.py để biết đầy đủ lý do)."""

    def __init__(self, run_id: str):
        super().__init__(level=logging.INFO)
        self.run_id = run_id
        self._conn = db_module.get_connection()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            db_module.append_maintenance_run_log(
                self._conn, self.run_id, record.levelname, self.format(record),
            )
        except Exception:  # noqa: BLE001 - lỗi ghi log KHÔNG được làm job thật bị dừng
            pass

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        super().close()


def get_run(run_id: str) -> Optional[dict]:
    """Đọc 1 lượt chạy từ bảng maintenance_runs — dùng cho GET
    /maintenance/{run_id}."""
    conn = db_module.get_connection()
    try:
        return db_module.get_maintenance_run(conn, run_id)
    finally:
        conn.close()


def list_runs(*, job_type: Optional[str] = None, status: Optional[str] = None,
               triggered_by: Optional[str] = None, limit: int = 50, offset: int = 0):
    """Đọc danh sách lịch sử — dùng cho GET /maintenance."""
    conn = db_module.get_connection()
    try:
        return db_module.list_maintenance_runs(
            conn, job_type=job_type, status=status, triggered_by=triggered_by,
            limit=limit, offset=offset,
        )
    finally:
        conn.close()


def get_latest_run_per_job_type() -> dict:
    """Đọc lượt chạy GẦN NHẤT của MỖI job_type (bất kể status) — dùng
    cho khung "Log live" ở trang web, mỗi card job_type tự có 1 run_id
    để hiện log ngay cả khi không có lượt nào đang chạy."""
    conn = db_module.get_connection()
    try:
        return db_module.get_latest_maintenance_run_per_job_type(conn)
    finally:
        conn.close()


def get_logs(run_id: str, after_id: int = 0, limit: int = 500):
    """Đọc các dòng log MỚI (id > after_id) của 1 lượt chạy — dùng cho
    GET /maintenance/{run_id}/logs?after_id=N."""
    conn = db_module.get_connection()
    try:
        return db_module.get_maintenance_run_logs(conn, run_id, after_id=after_id, limit=limit)
    finally:
        conn.close()


def start_run(job_type: str, params: dict, triggered_by: Optional[str] = None) -> str:
    """Tạo 1 run mới (INSERT vào maintenance_runs, status='queued'),
    trả về run_id NGAY (chưa chạy thật) — router chịu trách nhiệm add
    background task gọi execute() sau, đối xứng crawl_runner.start_crawl().

    triggered_by: ss_user_id của admin đang gọi POST
    /maintenance/{job_type} (user["sub"] từ JWT, xem
    Depends(require_admin) ở router).

    Raise db.ActiveMaintenanceRunExistsError nếu job_type này đang có 1
    lượt 'queued'/'running' chưa xong — router bắt lỗi này để trả 409."""
    conn = db_module.get_connection()
    try:
        return db_module.create_maintenance_run(
            conn, job_type=job_type, params=params, triggered_by=triggered_by,
        )
    finally:
        conn.close()


def execute(run_id: str) -> None:
    """Chạy job THẬT — gọi từ BackgroundTasks, KHÔNG gọi trực tiếp
    trong request handler. Tự mở/đóng connection riêng, đối xứng
    crawl_runner.execute() (bản KHÔNG có batch — mỗi run độc lập, không
    tự tạo run kế tiếp)."""
    conn = db_module.get_connection()
    try:
        run = db_module.get_maintenance_run(conn, run_id)
        if run is None:
            logger.error("execute() gọi với run_id không tồn tại: %s", run_id)
            return

        run_func = _JOB_RUNNERS.get(run["job_type"])
        if run_func is None:
            db_module.mark_maintenance_run_error(
                conn, run_id, f"job_type '{run['job_type']}' không có hàm run() đăng ký.",
            )
            return

        db_module.mark_maintenance_run_running(conn, run_id)

        log_handler = _RunLogHandler(run_id)
        root_logger = logging.getLogger()
        root_logger.addHandler(log_handler)

        try:
            params = run["params"] or {}
            stats = run_func(**params)
            db_module.mark_maintenance_run_done(conn, run_id, stats)
        except Exception as exc:  # noqa: BLE001 - ghi lại lỗi vào run, không làm chết background task
            logger.error("Maintenance run %s (%s) lỗi: %s", run_id, run["job_type"], exc)
            db_module.mark_maintenance_run_error(conn, run_id, str(exc))
        finally:
            root_logger.removeHandler(log_handler)
            log_handler.close()
    finally:
        conn.close()
