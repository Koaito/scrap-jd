"""
Cleanup task — dọn định kỳ refresh token hết hạn và token xác minh email / đặt lại mật khẩu hết hạn (xem
db.auth_cleanup). Chạy qua APScheduler trong lifespan của scrapjd/api/app.py, cùng scheduler và cùng nhịp với dọn
phiên (session_cleanup.py) nhưng là một job riêng, để lỗi ở một việc dọn không chặn việc kia.

Nếu sau này chạy nhiều instance, mỗi process tự dọn riêng — không sai (xoá theo điều kiện thời gian là
idempotent), chỉ hơi thừa công, giống session_cleanup.
"""

import logging

from scrapjd import db as db_module

logger = logging.getLogger(__name__)

TOKEN_CLEANUP_INTERVAL_MINUTES = 60


def run_token_cleanup_once() -> None:
    """1 lượt dọn — mượn 1 connection từ pool, dọn, commit, trả lại pool. Bọc try/except riêng: lỗi dọn KHÔNG được
    làm sập scheduler. Không log token hay hash, chỉ log số dòng."""
    conn = db_module.get_pooled_connection()
    try:
        refresh_deleted = db_module.delete_stale_refresh_tokens(conn)
        user_tokens_cleared = db_module.clear_expired_user_tokens(conn)
        conn.commit()
        if refresh_deleted or user_tokens_cleared:
            logger.info(
                "Cleanup token xác thực: xoá %d refresh token hết hạn, gỡ %d token xác minh/đặt lại mật khẩu hết hạn.",
                refresh_deleted, user_tokens_cleared,
            )
    except Exception:
        conn.rollback()
        logger.exception("Cleanup token xác thực thất bại.")
    finally:
        db_module.release_connection(conn)
