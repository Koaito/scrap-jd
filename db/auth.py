"""
db.auth — tách từ db.py (God module) theo domain.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)


def get_user_by_email(conn, email: str):
    """Trả dict đầy đủ field (kể cả password_hash, failed_login_count,
    locked_until — CHỈ dùng nội bộ cho luồng login, KHÔNG lộ ra response
    API, xem api/schemas.py UserOut không có các field này) hoặc None
    nếu không tìm thấy. So khớp email KHÔNG phân biệt hoa/thường."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM app_users WHERE lower(email) = lower(%s)",
            (email,),
        )
        return cur.fetchone()


def get_user_by_id(conn, ss_user_id: str):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM app_users WHERE ss_user_id = %s", (ss_user_id,))
        return cur.fetchone()


def create_user(conn, *, full_name: str, email: str, password_hash: str,
                 role: str = "user", must_change_password: bool = True) -> str:
    """Tạo 1 tài khoản MỚI — qua POST /auth/users (admin tạo hộ, mọi
    role) hoặc CLI `python main.py create-admin` (tạo admin đầu tiên).
    Từ Phần 2 (đăng ký công khai) sẽ có thêm luồng tự đăng ký, luôn cố
    định role='user' ở tầng route, không cho tự chọn.

    role: 1 trong 3 giá trị 'user' < 'ss_team' < 'admin' (xem
    api.deps.ROLE_HIERARCHY, sql/migration_add_role_hierarchy.sql) —
    mặc định 'user' (thấp nhất, chỉ xem), KHÔNG tự cấp quyền CRUD như
    hành vi cũ (trước đây mặc định 'member' = toàn quyền CRUD)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app_users
                (full_name, email, role, password_hash, must_change_password, is_active)
            VALUES (%s, %s, %s, %s, %s, true)
            RETURNING ss_user_id
            """,
            (full_name, email, role, password_hash, must_change_password),
        )
        return str(cur.fetchone()[0])


def update_user_password(conn, ss_user_id: str, password_hash: str,
                          must_change_password: bool = False) -> None:
    """Ghi mật khẩu MỚI — dùng khi user tự đổi mật khẩu (must_change_password
    thường = False sau đó) hoặc admin reset hộ (thường = True, ép đổi lại
    ngay lần đăng nhập kế tiếp — xem docstring cột trong migration)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_users SET password_hash = %s, must_change_password = %s "
            "WHERE ss_user_id = %s",
            (password_hash, must_change_password, ss_user_id),
        )


def record_failed_login(conn, ss_user_id: str, *, lock_threshold: int, lock_minutes: int) -> bool:
    """Tăng failed_login_count lên 1; nếu vừa CHẠM ngưỡng lock_threshold,
    khoá tài khoản lock_minutes phút (set locked_until) và reset
    failed_login_count về 0 (để lần khoá SAU tính lại từ đầu, không cộng
    dồn vô hạn). Trả True nếu tài khoản VỪA bị khoá ở lần gọi này (route
    dùng để trả thông báo phù hợp), False nếu chỉ tăng đếm bình thường."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT failed_login_count FROM app_users WHERE ss_user_id = %s",
            (ss_user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        new_count = row[0] + 1

        if new_count >= lock_threshold:
            cur.execute(
                "UPDATE app_users SET failed_login_count = 0, "
                "locked_until = now() + (%s || ' minutes')::interval "
                "WHERE ss_user_id = %s",
                (lock_minutes, ss_user_id),
            )
            return True

        cur.execute(
            "UPDATE app_users SET failed_login_count = %s WHERE ss_user_id = %s",
            (new_count, ss_user_id),
        )
        return False


def is_account_locked(user_row) -> bool:
    """Kiểm tra thuần Python (không query thêm) — user_row lấy từ
    get_user_by_email()/get_user_by_id(), đọc field locked_until có sẵn."""
    locked_until = user_row.get("locked_until") if user_row else None
    if locked_until is None:
        return False
    now = datetime.now(timezone.utc)
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    return locked_until > now


def reset_failed_login(conn, ss_user_id: str) -> None:
    """Gọi sau khi đăng nhập ĐÚNG mật khẩu — xoá đếm sai, mở khoá (nếu
    có), cập nhật last_login_at."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_users SET failed_login_count = 0, locked_until = NULL, "
            "last_login_at = now() WHERE ss_user_id = %s",
            (ss_user_id,),
        )


def create_refresh_token(conn, *, ss_user_id: str, token_hash: str, expires_at,
                          user_agent: Optional[str] = None,
                          ip_address: Optional[str] = None) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO auth_refresh_tokens
                (ss_user_id, token_hash, expires_at, user_agent, ip_address)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING refresh_token_id
            """,
            (ss_user_id, token_hash, expires_at, user_agent, ip_address),
        )
        return str(cur.fetchone()[0])


def get_refresh_token_by_hash(conn, token_hash: str):
    """Trả dict (refresh_token_id, ss_user_id, expires_at, revoked_at,
    replaced_by_token_id...) hoặc None. Route tự kiểm tra hết hạn/đã
    revoke — hàm này chỉ tra cứu thuần, không tự raise/chặn gì."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM auth_refresh_tokens WHERE token_hash = %s", (token_hash,)
        )
        return cur.fetchone()


def revoke_refresh_token(conn, refresh_token_id: str,
                          replaced_by_token_id: Optional[str] = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE auth_refresh_tokens SET revoked_at = now(), replaced_by_token_id = %s "
            "WHERE refresh_token_id = %s AND revoked_at IS NULL",
            (replaced_by_token_id, refresh_token_id),
        )


def set_active_session_id(conn, ss_user_id: str, session_id: Optional[str]) -> None:
    """Ghi session_id đang active của 1 tài khoản — dùng CHUNG cho 2
    tình huống trái ngược (08/2026, single-session, xem
    sql/migration_add_single_session.sql):
      - login() gọi với session_id MỚI (uuid4) -> mọi access token cũ
        (mang session_id khác) bị get_current_user() từ chối ngay từ
        lần gọi kế tiếp, dù chưa hết hạn 30 phút.
      - logout()/change_password()/reset_password() gọi với
        session_id=None -> access token hiện có (nếu còn ai đang cầm)
        cũng bị từ chối ngay, không đợi tự hết hạn."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_users SET active_session_id = %s WHERE ss_user_id = %s",
            (session_id, ss_user_id),
        )


def revoke_all_refresh_tokens_for_user(conn, ss_user_id: str) -> int:
    """Thu hồi TOÀN BỘ refresh token còn sống của 1 user — dùng khi: phát
    hiện refresh token bị TÁI SỬ DỤNG sau khi đã revoke (dấu hiệu bị đánh
    cắp, xem docstring cột replaced_by_token_id trong migration), hoặc
    khi đổi mật khẩu (đăng xuất mọi thiết bị khác cho an toàn), hoặc admin
    reset mật khẩu hộ người khác. Trả số token vừa bị thu hồi."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE auth_refresh_tokens SET revoked_at = now() "
            "WHERE ss_user_id = %s AND revoked_at IS NULL",
            (ss_user_id,),
        )
        return cur.rowcount


def list_users(conn):
    """Danh sách thành viên team (không lộ password_hash) — ss_team trở
    lên xem được (GET /auth/users, thêm 08/2026), dùng cho trang quản lý
    user phía frontend. phone/track thêm vào SELECT 08/2026 (xem
    migration_add_phone_track.sql) để khớp UserOut mới, không bắt buộc
    frontend phải dùng ngay."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT ss_user_id, full_name, email, role, is_active, "
            "must_change_password, last_login_at, created_at, phone, track "
            "FROM app_users ORDER BY created_at"
        )
        return cur.fetchall()


def update_user_role(conn, ss_user_id: str, new_role: str) -> bool:
    """Đổi role của 1 user — CHỈ gọi từ route admin-only (PATCH
    /auth/users/{id}/role). Route tự chặn admin đổi role CHÍNH MÌNH
    TRƯỚC KHI gọi hàm này (xem api/routers/auth.py) — hàm ở đây không tự
    biết "ai đang gọi", chỉ thực thi UPDATE thuần, tránh trộn logic
    nghiệp vụ vào tầng DB. Trả False nếu ss_user_id không tồn tại."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_users SET role = %s WHERE ss_user_id = %s",
            (new_role, ss_user_id),
        )
        return cur.rowcount > 0


def update_user_active_status(conn, ss_user_id: str, is_active: bool) -> bool:
    """Khoá/mở khoá VĨNH VIỄN 1 tài khoản — CHỈ gọi từ route admin-only
    (PATCH /auth/users/{id}/active-status). Route tự chặn admin tự khoá
    CHÍNH MÌNH TRƯỚC KHI gọi hàm này, cùng nguyên tắc với
    update_user_role() ở trên. Trả False nếu ss_user_id không tồn tại.

    Vô hiệu hoá KHÔNG revoke refresh token đang có — access token cũ
    (JWT, tối đa 30 phút) vẫn dùng được tới khi hết hạn tự nhiên, nhưng
    request refresh token tiếp theo sẽ bị chặn vì login()/refresh() đều
    kiểm tra is_active (xem api/routers/auth.py). Chấp nhận độ trễ tối
    đa 30 phút này — revoke JWT đang active cần thêm cơ chế blacklist,
    không cần thiết ở quy mô team nhỏ."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_users SET is_active = %s WHERE ss_user_id = %s",
            (is_active, ss_user_id),
        )
        return cur.rowcount > 0


def create_user_pending_verification(conn, *, full_name: str, email: str,
                                      password_hash: str, verify_token_hash: str,
                                      verify_expires,
                                      phone: Optional[str] = None,
                                      track: Optional[str] = None) -> str:
    """Tạo tài khoản role='user' CHƯA xác thực — KHÁC create_user() ở
    chỗ must_change_password=False (mật khẩu do CHÍNH người dùng tự đặt
    lúc đăng ký, không phải mật khẩu tạm admin sinh hộ, không cần ép đổi
    lại) và có thêm email_verify_token/expires. is_active vẫn true ngay
    từ đầu (is_active là cờ RIÊNG cho admin khoá tài khoản, KHÁC
    email_verified — 2 khái niệm độc lập, xem docstring migration).

    verify_token_hash: HASH của token (security.hash_verification_token()),
    KHÔNG PHẢI token thô — sửa bảo mật, xem docstring hàm đó. Cột DB
    email_verify_token vẫn tên cũ (không cần migration, chỉ đổi Ý NGHĨA
    giá trị lưu vào) nhưng giờ luôn chứa hash, không còn token đọc được
    trực tiếp. Token thô CHỈ tồn tại trong email gửi cho người dùng, KHÔNG
    bao giờ chạm tới DB.

    phone/track: thêm 08/2026 (xem sql/migration_add_phone_track.sql) —
    trước đó frontend đã gửi 2 field này lên nhưng không có chỗ lưu."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app_users
                (full_name, email, role, password_hash, must_change_password,
                 is_active, email_verified, email_verify_token, email_verify_expires,
                 phone, track)
            VALUES (%s, %s, 'user', %s, false, true, false, %s, %s, %s, %s)
            RETURNING ss_user_id
            """,
            (full_name, email, password_hash, verify_token_hash, verify_expires, phone, track),
        )
        return str(cur.fetchone()[0])


def get_user_by_verify_token_hash(conn, verify_token_hash: str):
    """Trả dict user (đủ field, kể cả email_verify_expires) hoặc None
    nếu token không tồn tại — KHÔNG tự kiểm tra hết hạn ở đây, route tự
    so sánh email_verify_expires với thời gian hiện tại (tách trách
    nhiệm: hàm này chỉ tra cứu, route quyết định logic nghiệp vụ).

    Tra cứu bằng HASH (security.hash_verification_token(token thô từ
    query string) — route tự hash trước khi gọi hàm này), KHÔNG PHẢI
    token thô — sửa bảo mật, xem docstring security.hash_verification_
    token()."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM app_users WHERE email_verify_token = %s",
            (verify_token_hash,),
        )
        return cur.fetchone()


def mark_email_verified(conn, ss_user_id: str) -> None:
    """Đánh dấu đã xác thực + XOÁ token (đặt NULL) — token chỉ dùng
    được ĐÚNG 1 LẦN, xoá ngay sau khi verify thành công để không ai
    verify lại lần 2 bằng link cũ (link cũ giờ vô nghĩa, không trỏ tới
    token nào còn tồn tại trong DB nữa)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_users SET email_verified = true, "
            "email_verify_token = NULL, email_verify_expires = NULL "
            "WHERE ss_user_id = %s",
            (ss_user_id,),
        )


def set_new_verify_token(conn, ss_user_id: str, verify_token_hash: str, verify_expires) -> None:
    """Ghi ĐÈ token xác thực mới — dùng cho POST /auth/resend-verification
    (token cũ hết hạn hoặc email thất lạc, user xin gửi lại). Token cũ
    (nếu còn) bị thay thế hoàn toàn, không dùng lại được nữa.

    verify_token_hash: HASH, không phải token thô — xem docstring
    create_user_pending_verification()."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_users SET email_verify_token = %s, "
            "email_verify_expires = %s WHERE ss_user_id = %s",
            (verify_token_hash, verify_expires, ss_user_id),
        )


def set_password_reset_token(conn, ss_user_id: str, reset_token_hash: str, reset_expires) -> None:
    """Ghi token reset mật khẩu — gọi bởi POST /auth/forgot-password.
    Ghi ĐÈ token cũ nếu có (user xin gửi lại nhiều lần), token cũ (nếu
    còn) hết hiệu lực ngay vì không còn tồn tại trong DB để đối chiếu.

    reset_token_hash: HASH của token (security.hash_verification_token()),
    KHÔNG PHẢI token thô — sửa bảo mật cùng đợt với email_verify_token,
    xem docstring create_user_pending_verification()."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_users SET password_reset_token = %s, "
            "password_reset_expires = %s WHERE ss_user_id = %s",
            (reset_token_hash, reset_expires, ss_user_id),
        )


def get_user_by_reset_token_hash(conn, reset_token_hash: str):
    """Trả dict user (đủ field, kể cả password_reset_expires) hoặc None
    nếu token không tồn tại — KHÔNG tự kiểm tra hết hạn ở đây, route tự
    so sánh password_reset_expires với thời gian hiện tại (tách trách
    nhiệm, giống get_user_by_verify_token_hash()).

    Tra cứu bằng HASH, không phải token thô — xem docstring
    set_password_reset_token()."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM app_users WHERE password_reset_token = %s",
            (reset_token_hash,),
        )
        return cur.fetchone()


def reset_password_with_token(conn, ss_user_id: str, password_hash: str) -> None:
    """Ghi mật khẩu MỚI + XOÁ token reset (đặt NULL) trong CÙNG 1 câu
    UPDATE — token chỉ dùng được ĐÚNG 1 LẦN, xoá ngay sau khi dùng để
    không ai reset lại lần 2 bằng link cũ. must_change_password=false
    (khác update_user_password() mặc định — ở đây user VỪA TỰ CHỌN mật
    khẩu mới thật sự qua link email, không phải mật khẩu tạm admin sinh
    hộ, nên không cần ép đổi lại lần nữa)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_users SET password_hash = %s, must_change_password = false, "
            "password_reset_token = NULL, password_reset_expires = NULL "
            "WHERE ss_user_id = %s",
            (password_hash, ss_user_id),
        )
