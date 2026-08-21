"""
Cleanup task — xoá định kỳ mọi import_previews đã hết hạn (Requirement
9). Đăng ký chạy qua APScheduler trong lifespan của api/app.py (thêm 08/2026),
mỗi 15 phút — cùng tinh thần "job nền" như crawl scheduler nếu sau này có,
KHÔNG chặn request nào của FE khi chạy (chạy nền trong process, tách
riêng connection ngắn hạn rồi trả lại pool ngay).
"""

import logging

import db as db_module
from api.services import preview_manager

logger = logging.getLogger(__name__)

CLEANUP_INTERVAL_MINUTES = 15


def run_cleanup_once() -> None:
    """1 lượt cleanup — mượn 1 connection từ pool, xoá, trả lại pool.
    Bọc try/except riêng: lỗi cleanup KHÔNG được làm crash cả scheduler
    (APScheduler tự log exception nhưng vẫn nên bắt ở đây để log rõ
    ràng theo format chung của project)."""
    conn = db_module.get_pooled_connection()
    try:
        deleted = preview_manager.cleanup_expired_previews(conn)
        conn.commit()
        if deleted:
            logger.info("Cleanup import_previews: đã xoá %d preview hết hạn.", deleted)
    except Exception:
        conn.rollback()
        logger.exception("Cleanup import_previews thất bại.")
    finally:
        db_module.release_connection(conn)
