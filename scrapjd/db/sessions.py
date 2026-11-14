"""
db.sessions — phiên đăng nhập "mờ" lưu trong DB (Mục 3, Pha A; bảng auth_sessions, xem
sql/0045_add_auth_sessions.sql để hiểu thiết kế).

Hàm ở đây chỉ chạy SQL, không biết gì về HTTP. Quy tắc nghiệp vụ (mã lỗi nào cho lý do thu hồi nào, hạn
bao nhiêu ngày) nằm ở scrapjd/api/deps.py và scrapjd/api/security.py.

QUY ƯỚC THỜI GIAN: mọi phép tính và so sánh thời gian trong file này dùng clock_timestamp(), KHÔNG dùng
now(). Lý do: now() là thời điểm BẮT ĐẦU transaction hiện tại. Connection mượn từ pool (scrapjd/api/deps.py::
get_db) không được rollback/commit khi request chỉ đọc, nên transaction của request trước có thể còn mở và
now() đứng yên ở quá khứ (có thể hàng giờ). Dùng now() sẽ khiến phiên đã hết hạn vẫn bị coi là còn hạn.

CÁC HÀM KHÔNG TỰ COMMIT. Route gọi commit() (xem login()/logout() ở scrapjd/api/routers/auth_session.py), riêng
touch trong get_current_user() tự commit vì nó chạy trước body route và không có gì khác đang chờ.
"""

from typing import Optional

import psycopg2.extras

from scrapjd.db.pg_types import Conn, Row, fetch_scalar

# Lý do thu hồi. Phải khớp CHECK ck_auth_sessions_revoked_reason trong DB.
REVOKE_LOGOUT = "logout"
REVOKE_REPLACED = "replaced"
REVOKE_PASSWORD_CHANGED = "password_changed"
REVOKE_ADMIN = "admin"
REVOKE_INACTIVE = "inactive"


def create_session(
    conn: Conn,
    *,
    session_id: str,
    ss_user_id: str,
    token_hash: str,
    absolute_days: int,
    idle_days: int,
    user_agent: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> Row:
    """Tạo phiên mới, trả {session_id, expires_at, idle_expires_at}. token_hash là SHA-256 hex của token thô
    (security.hash_session_token), KHÔNG PHẢI token thô — DB có CHECK chặn giá trị không phải 64 ký tự hex.

    session_id do nơi gọi truyền vào (không để DB tự sinh) vì nó phải trùng app_users.active_session_id và
    claim `sid` của JWT cùng lần đăng nhập. Hạn tính bằng giờ của DB (clock_timestamp) để so sánh về sau cùng
    một đồng hồ; idle_days được cắt không vượt absolute_days (CHECK ck_auth_sessions_idle_within_absolute).

    Hai hạn tính từ MỘT mốc thời gian duy nhất (CTE `t`). clock_timestamp() là hàm volatile: gọi hai lần trong
    cùng một câu lệnh cho hai giá trị khác nhau vài micro-giây, nên khi idle_days == absolute_days hạn trượt sẽ
    lệch hơn hạn tuyệt đối và vi phạm CHECK (đăng nhập lỗi 500). Đã có test chặn lỗi này."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            WITH t AS (SELECT clock_timestamp() AS ts)
            INSERT INTO auth_sessions
                (session_id, ss_user_id, token_hash, expires_at, idle_expires_at, user_agent, ip_address)
            SELECT %s, %s, %s,
                   t.ts + make_interval(days => %s),
                   t.ts + make_interval(days => LEAST(%s, %s)),
                   %s, %s
              FROM t
            RETURNING session_id, expires_at, idle_expires_at
            """,
            (session_id, ss_user_id, token_hash, absolute_days, idle_days, absolute_days,
             user_agent, ip_address),
        )
        row = cur.fetchone()
    assert row is not None  # INSERT ... RETURNING luôn trả đúng 1 dòng
    return row


def get_session_for_auth(conn: Conn, token_hash: str, *, touch_interval_seconds: int) -> Optional[Row]:
    """Tra phiên theo token_hash KÈM thông tin người dùng, trong MỘT câu SELECT (thay cho hai lượt query như
    luồng JWT: get_user_by_id + so sánh sid). Trả None nếu không có dòng nào (token lạ, giả, hoặc đã bị dọn).

    Hàm CHỈ tra cứu, không quyết định gì: nơi gọi (deps.get_current_user) tự xét thu hồi, hết hạn, khoá tài
    khoản. Các cột *_expired / needs_touch tính ngay trong SQL bằng cùng một đồng hồ (xem QUY ƯỚC THỜI GIAN
    ở đầu file). Không SELECT password_hash hay token_hash ra khỏi DB."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT s.session_id,
                   s.ss_user_id,
                   s.revoked_at,
                   s.revoked_reason,
                   (s.expires_at <= clock_timestamp())      AS absolute_expired,
                   (s.idle_expires_at <= clock_timestamp()) AS idle_expired,
                   (s.last_seen_at <= clock_timestamp() - make_interval(secs => %s)) AS needs_touch,
                   u.email,
                   u.role,
                   u.is_active
            FROM auth_sessions s
            JOIN app_users u ON u.ss_user_id = s.ss_user_id
            WHERE s.token_hash = %s
            """,
            (touch_interval_seconds, token_hash),
        )
        return cur.fetchone()


def touch_session(conn: Conn, session_id: str, *, idle_days: int, min_interval_seconds: int) -> bool:
    """Ghi last_seen_at và đẩy hạn trượt (idle_expires_at) lên, KHÔNG BAO GIỜ vượt hạn tuyệt đối. Chỉ ghi khi
    lần ghi trước cách đây >= min_interval_seconds, để không biến mọi GET thành một UPDATE (get_session_for_
    auth đã báo needs_touch, điều kiện lặp lại ở đây là để hai request song song cùng thấy needs_touch chỉ
    có một cái ghi thật). Không bao giờ đánh thức phiên đã thu hồi hoặc đã hết hạn. Trả True nếu có ghi."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE auth_sessions
               SET last_seen_at = clock_timestamp(),
                   idle_expires_at = LEAST(expires_at, clock_timestamp() + make_interval(days => %s))
             WHERE session_id = %s
               AND revoked_at IS NULL
               AND expires_at > clock_timestamp()
               AND idle_expires_at > clock_timestamp()
               AND last_seen_at <= clock_timestamp() - make_interval(secs => %s)
            """,
            (idle_days, session_id, min_interval_seconds),
        )
        return cur.rowcount > 0


def get_session_by_token_hash(conn: Conn, token_hash: str) -> Optional[Row]:
    """Tra phiên thuần theo token_hash, không JOIN người dùng — dùng cho logout (cần biết phiên của ai và đã
    thu hồi chưa). Trả None nếu không có."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT session_id, ss_user_id, revoked_at, revoked_reason "
            "FROM auth_sessions WHERE token_hash = %s",
            (token_hash,),
        )
        return cur.fetchone()


def revoke_session(conn: Conn, session_id: str, reason: str) -> bool:
    """Thu hồi đúng một phiên. Không đè lý do nếu phiên đã thu hồi từ trước (giữ lý do đầu tiên). Trả True nếu
    vừa thu hồi."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE auth_sessions SET revoked_at = clock_timestamp(), revoked_reason = %s "
            "WHERE session_id = %s AND revoked_at IS NULL",
            (reason, session_id),
        )
        return cur.rowcount > 0


def revoke_all_sessions_for_user(conn: Conn, ss_user_id: str, reason: str) -> int:
    """Thu hồi mọi phiên CÒN HIỆU LỰC của một người dùng, trả số phiên vừa thu hồi. Dùng cho: đăng nhập mới
    (reason 'replaced' — single-session), đổi/đặt lại mật khẩu, khoá tài khoản, logout theo credential cũ.

    Cố ý CHỈ đụng phiên chưa hết hạn: phiên đã hết hạn giữ nguyên trạng thái "hết hạn" thay vì bị gán nhãn
    'replaced' một cách sai lịch sử, để client cũ nhận đúng thông báo (hết hạn, không phải bị đá)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE auth_sessions SET revoked_at = clock_timestamp(), revoked_reason = %s "
            "WHERE ss_user_id = %s AND revoked_at IS NULL "
            "AND expires_at > clock_timestamp() AND idle_expires_at > clock_timestamp()",
            (reason, ss_user_id),
        )
        return cur.rowcount


def clear_active_session_id_if(conn: Conn, ss_user_id: str, session_id: str) -> bool:
    """Xoá app_users.active_session_id CHỈ KHI nó đang đúng bằng session_id này. Dùng khi logout bằng session
    token: JWT cùng lần đăng nhập (mang sid = session_id) chết theo, nhưng không đụng tới phiên mới hơn nếu
    người dùng đã đăng nhập lại ở nơi khác giữa chừng. Trả True nếu có xoá."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_users SET active_session_id = NULL "
            "WHERE ss_user_id = %s AND active_session_id = %s",
            (ss_user_id, session_id),
        )
        return cur.rowcount > 0


def delete_stale_sessions(conn: Conn, *, grace_days: int = 1) -> int:
    """Xoá phiên không còn dùng được nữa, trả số dòng đã xoá. Giữ lại mọi dòng cho tới khi HẾT HẠN TUYỆT ĐỐI
    cộng grace_days ngày: dòng đã thu hồi phải còn đó để client cũ (cookie còn sống) nhận đúng
    session_replaced/session_revoked thay vì lỗi hết hạn chung chung. Phiên chưa thu hồi mà đã quá hạn
    TRƯỢT cộng grace_days cũng được xoá sớm (client cũ nhận lỗi hết hạn, đúng bản chất). Bảng vì thế bị chặn
    kích thước theo hạn tuyệt đối, không phình vô hạn."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM auth_sessions "
            "WHERE expires_at < clock_timestamp() - make_interval(days => %s) "
            "   OR (revoked_at IS NULL "
            "       AND idle_expires_at < clock_timestamp() - make_interval(days => %s))",
            (grace_days, grace_days),
        )
        return cur.rowcount


def count_sessions_for_user(conn: Conn, ss_user_id: str, *, only_active: bool = True) -> int:
    """Đếm phiên của một người dùng (test và chẩn đoán). only_active=True chỉ đếm phiên chưa thu hồi và còn
    hạn."""
    query = "SELECT count(*) FROM auth_sessions WHERE ss_user_id = %s"
    if only_active:
        query += (" AND revoked_at IS NULL AND expires_at > clock_timestamp()"
                  " AND idle_expires_at > clock_timestamp()")
    with conn.cursor() as cur:
        cur.execute(query, (ss_user_id,))
        return int(fetch_scalar(cur))
