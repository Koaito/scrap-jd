"""
db.auth_cleanup — xoá định kỳ các dòng xác thực đã chết hẳn, để bảng và cột không giữ vô hạn thứ không còn dùng
được: refresh token hết hạn (auth_refresh_tokens) và token xác minh email / đặt lại mật khẩu hết hạn (cột của
app_users). Đối xứng db.sessions.delete_stale_sessions (phiên đăng nhập). Gọi từ
scrapjd/api/services/token_cleanup.py qua APScheduler.

Cùng quy ước lớp db: hàm không tự commit, nơi gọi chịu trách nhiệm.

KHOẢNG CHỜ (grace_days, mặc định 7 ngày) tính từ lúc token HẾT HẠN, không xoá ngay:
  - Refresh token đã revoke mà bị gửi lại là tín hiệu nghi đánh cắp (auth_session.refresh thu hồi toàn bộ token của
    user). Token quá hạn không còn đổi được access token nào, nên sau khoảng chờ mới xoá là đủ an toàn.
  - Link xác minh / đặt lại mật khẩu quá hạn vẫn ra thông báo "hết hạn" (kèm hướng dẫn xin link mới) trong
    khoảng chờ; xoá rồi thì link cũ ra "không hợp lệ".
"""

from scrapjd.db.pg_types import Conn

DEFAULT_GRACE_DAYS = 7


def delete_stale_refresh_tokens(conn: Conn, *, grace_days: int = DEFAULT_GRACE_DAYS) -> int:
    """Xoá refresh token đã hết hạn quá grace_days ngày, trả số dòng đã xoá.

    replaced_by_token_id là khoá ngoại tự tham chiếu: token A (đã xoay vòng) trỏ sang B. Không xoá được B khi
    còn dòng chưa bị xoá trỏ tới nó, nên bỏ qua B ở lượt này (lượt sau xoá khi dòng trỏ tới đã đủ tuổi). Hai dòng
    cùng đủ tuổi thì xoá chung trong một câu lệnh, khoá ngoại không vướng."""
    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM auth_refresh_tokens t
             WHERE t.expires_at < clock_timestamp() - make_interval(days => %(g)s)
               AND NOT EXISTS (
                     SELECT 1 FROM auth_refresh_tokens r
                      WHERE r.replaced_by_token_id = t.refresh_token_id
                        AND r.expires_at >= clock_timestamp() - make_interval(days => %(g)s))
            """,
            {"g": grace_days},
        )
        return cur.rowcount


def clear_expired_user_tokens(conn: Conn, *, grace_days: int = DEFAULT_GRACE_DAYS) -> int:
    """Gỡ token xác minh email và token đặt lại mật khẩu đã hết hạn quá grace_days ngày khỏi app_users (đặt cả cột
    token và cột hạn về NULL), trả số TOKEN đã gỡ (một user có thể có cả hai). Không đụng dòng user nào ngoài hai
    cặp cột này. Token không có hạn (expires NULL) không bị đụng: đó là dữ liệu bất thường cần xem tay, không phải
    token đã hết hạn."""
    cleared = 0
    with conn.cursor() as cur:
        for token_col, expires_col in (("email_verify_token", "email_verify_expires"),
                                       ("password_reset_token", "password_reset_expires")):
            cur.execute(
                f"UPDATE app_users SET {token_col} = NULL, {expires_col} = NULL "
                f"WHERE {token_col} IS NOT NULL "
                f"  AND {expires_col} < clock_timestamp() - make_interval(days => %s)",
                (grace_days,),
            )
            cleared += cur.rowcount
    return cleared
