"""
Maintenance watchdog — quét định kỳ bảng maintenance_runs, tự đánh dấu
'error' cho lượt chạy bị TREO (started_at quá lâu mà chưa 'done'/'error')
để giải phóng UNIQUE INDEX idx_maintenance_runs_one_active_per_job_type,
tránh 1 job_type bị "khoá" chạy mãi mãi (08/2026, xem
sql/migration_add_maintenance_runs.sql) — ĐỐI XỨNG HOÀN TOÀN
api/services/crawl_watchdog.py, xem docstring file đó để biết đầy đủ lý
do (LỚP DỰ PHÒNG THỨ 2, bổ sung cho
db.reconcile_orphaned_maintenance_runs() chạy lúc app khởi động).

Đăng ký chạy qua APScheduler trong lifespan của api/app.py, DÙNG CHUNG
1 scheduler instance với cleanup import_previews + crawl watchdog —
không tạo thêm process/thread riêng.
"""

import logging

import db as db_module
from config import MAINTENANCE_STALE_TIMEOUT_MINUTES

logger = logging.getLogger(__name__)


def run_maintenance_watchdog_once() -> None:
    """1 lượt quét — mượn 1 connection từ pool, reconcile, trả lại
    pool. Bọc try/except riêng: lỗi ở đây KHÔNG được làm crash cả
    scheduler (cùng lý do run_crawl_watchdog_once())."""
    conn = db_module.get_pooled_connection()
    try:
        count = db_module.reconcile_stale_maintenance_runs(conn, MAINTENANCE_STALE_TIMEOUT_MINUTES)
        if count:
            logger.warning(
                "Maintenance watchdog: đã tự đánh dấu 'error' %d lượt chạy treo quá %d phút.",
                count, MAINTENANCE_STALE_TIMEOUT_MINUTES,
            )
    except Exception:
        conn.rollback()
        logger.exception("Maintenance watchdog thất bại.")
    finally:
        db_module.release_connection(conn)
