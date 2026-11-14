"""
Cleanup task — xoá định kỳ phiên đăng nhập (auth_sessions) không còn dùng được nữa (Mục 3, Pha A). Chạy qua
APScheduler trong lifespan của scrapjd/api/app.py, cùng scheduler với dọn import_previews và các watchdog.

Giữ bảng không phình vô hạn: mỗi lần đăng nhập sinh một dòng (single-session thu hồi dòng cũ nhưng không xoá
ngay, để client cũ còn nhận đúng thông báo session_replaced/session_revoked). Quy tắc chi tiết xem
db.delete_stale_sessions(): dòng được giữ tới hết hạn tuyệt đối cộng một ngày.

Nếu sau này chạy nhiều instance, mỗi process tự dọn riêng — không sai (DELETE theo điều kiện thời gian là
idempotent), chỉ hơi thừa công, giống preview_cleanup.
"""

import logging

from scrapjd import db as db_module

logger = logging.getLogger(__name__)

SESSION_CLEANUP_INTERVAL_MINUTES = 60


def run_session_cleanup_once() -> None:
    """1 lượt dọn — mượn 1 connection từ pool, xoá, trả lại pool. Bọc try/except riêng: lỗi dọn KHÔNG được làm
    sập scheduler. Không log token hay hash, chỉ log số dòng."""
    conn = db_module.get_pooled_connection()
    try:
        deleted = db_module.delete_stale_sessions(conn)
        conn.commit()
        if deleted:
            logger.info("Cleanup auth_sessions: đã xoá %d phiên hết hạn.", deleted)
    except Exception:
        conn.rollback()
        logger.exception("Cleanup auth_sessions thất bại.")
    finally:
        db_module.release_connection(conn)
