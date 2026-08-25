"""
Crawl watchdog — quét định kỳ bảng crawl_runs, tự đánh dấu 'error' cho
lượt crawl bị TREO (started_at quá lâu mà chưa 'done'/'error') để giải
phóng UNIQUE INDEX idx_crawl_runs_one_active_per_source, tránh 1 nguồn
bị "khoá" crawl mãi mãi (08/2026, xem sql/migration_add_crawl_runs.sql).

Đây là LỚP DỰ PHÒNG THỨ 2, bổ sung cho
db.reconcile_orphaned_crawl_runs() (chạy 1 lần lúc app khởi động, xem
api/app.py::lifespan) — lớp đó chỉ bắt được trường hợp SERVER RESTART.
Watchdog này bắt thêm trường hợp process KHÔNG restart nhưng 1
background task bị treo (network timeout vô hạn, deadlock...).

Đăng ký chạy qua APScheduler trong lifespan của api/app.py, DÙNG CHUNG
1 scheduler instance với cleanup import_previews (api/services/
preview_cleanup.py) — không tạo thêm scheduler riêng, cùng tinh thần
"1 process, ít tài nguyên" xuyên suốt project.
"""

import logging

import db as db_module
from config import CRAWL_STALE_TIMEOUT_MINUTES

logger = logging.getLogger(__name__)


def run_crawl_watchdog_once() -> None:
    """1 lượt quét — mượn 1 connection từ pool, reconcile, trả lại pool.
    Bọc try/except riêng: lỗi ở đây KHÔNG được làm crash cả scheduler
    (cùng lý do run_cleanup_once() ở preview_cleanup.py)."""
    conn = db_module.get_pooled_connection()
    try:
        count = db_module.reconcile_stale_crawl_runs(conn, CRAWL_STALE_TIMEOUT_MINUTES)
        if count:
            logger.warning(
                "Crawl watchdog: đã tự đánh dấu 'error' %d lượt crawl treo quá %d phút.",
                count, CRAWL_STALE_TIMEOUT_MINUTES,
            )
    except Exception:
        conn.rollback()
        logger.exception("Crawl watchdog thất bại.")
    finally:
        db_module.release_connection(conn)
