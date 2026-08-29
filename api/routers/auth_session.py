"""
Router phiên đăng nhập (JWT + refresh token xoay vòng) — xem docstring
api/security.py và sql/migration_add_auth.sql để hiểu toàn bộ thiết kế
trước khi đọc file này.

Tách ra từ api/routers/auth.py (08/2026) — file gốc gộp 4 nhóm concern
khác nhau (session, quản trị user, đăng ký công khai) trong 1 file
738 dòng/14 endpoint, khó theo dõi khi sửa. auth.py giờ chỉ còn là
facade gộp lại router của 3 file con (xem docstring auth.py) — app.py
và mọi nơi khác KHÔNG cần đổi gì.

File này chứa 5 route "quản lý phiên của CHÍNH người dùng đang đăng
nhập": login, refresh, logout, me, change-password. Tất cả vẫn nằm
SAU lớp X-API-Key (app.py include auth.router kèm
dependencies=[Depends(require_api_key)]) — JWT là lớp thứ 2 xác định
"user thật nào" bên trong, khác 3 route đăng ký công khai ở
auth_registration.py (public_router, KHÔNG cần X-API-Key).
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

import db as db_module
from api import security
from api.deps import get_db, get_current_user
from api.rate_limit import get_user_id_or_ip, limiter
from api.schemas import (
    AccessTokenOut, ChangePasswordRequest, LoginRequest, RefreshRequest,
    TokenPairOut, UserOut,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _issue_token_pair(conn, user_row, request: Request, session_id: str) -> tuple[str, str]:
    """Sinh CẢ access token lẫn refresh token mới cho 1 user — dùng
    chung cho login lẫn refresh (rotation), tránh lặp code.

    session_id (08/2026, single-session — xem
    sql/migration_add_single_session.sql): login() truyền session_id
    MỚI (vừa ghi vào app_users.active_session_id), refresh() truyền lại
    session_id HIỆN TẠI của phiên đang xoay vòng (không đổi) — xem
    docstring security.create_access_token()."""
    access_token = security.create_access_token(
        ss_user_id=str(user_row["ss_user_id"]),
        role=user_row["role"],
        email=user_row["email"],
        session_id=session_id,
    )
    raw_refresh_token = security.generate_refresh_token()
    db_module.create_refresh_token(
        conn,
        ss_user_id=str(user_row["ss_user_id"]),
        token_hash=security.hash_refresh_token(raw_refresh_token),
        expires_at=security.refresh_token_expiry(),
        user_agent=request.headers.get("user-agent"),
        ip_address=request.client.host if request.client else None,
    )
    return access_token, raw_refresh_token


@router.post("/login", response_model=TokenPairOut)
@limiter.limit("20/minute")
def login(payload: LoginRequest, request: Request, conn=Depends(get_db)):
    """20/minute theo IP — thêm 08/2026 cùng đợt rà soát rate-limit tổng
    thể (xem api/rate_limit.py). Route công khai duy nhất KHÔNG có giới
    hạn nào trước đó ngoài khoá tài khoản is_account_locked() — nhưng
    khoá đó chỉ chặn brute-force VÀO 1 tài khoản cụ thể (5 lần sai liên
    tiếp, xem security.FAILED_LOGIN_LOCK_THRESHOLD), không chặn được kiểu
    "credential stuffing" dò dàn trải qua NHIỀU email khác nhau (mỗi email
    chỉ thử vài lần, không đủ ngưỡng khoá riêng lẻ). 20/minute đủ rộng
    cho người dùng thật gõ nhầm vài lần, nhưng chặn được script quét
    nhanh nhiều tài khoản từ cùng 1 IP.

    Đăng nhập bằng email + mật khẩu. Không tiết lộ qua thông báo lỗi
    việc email có tồn tại hay không (luôn trả cùng 1 message 401 chung
    cho cả 2 trường hợp 'không tìm thấy email' và 'sai mật khẩu') — tránh
    lộ thông tin cho kẻ dò email hợp lệ."""
    _WRONG_CREDENTIALS_MSG = "Email hoặc mật khẩu không đúng."

    user = db_module.get_user_by_email(conn, payload.email)
    if user is None or not user.get("password_hash"):
        raise HTTPException(status_code=401, detail=_WRONG_CREDENTIALS_MSG)

    if not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="Tài khoản đã bị vô hiệu hoá.")

    if not user.get("email_verified", True):
        # default=True: tài khoản tạo TRƯỚC Phần 2 (qua POST /auth/users,
        # không có cột này lúc code) coi như đã verify — migration đã tự
        # set true hàng loạt cho dữ liệu cũ (xem
        # sql/migration_add_email_verification.sql), default ở đây chỉ
        # là phòng hờ thêm 1 lớp, KHÔNG phải nguồn sự thật chính.
        raise HTTPException(
            status_code=403,
            detail="Email chưa được xác thực — kiểm tra hộp thư hoặc gọi "
                   "POST /auth/resend-verification để gửi lại link xác thực.",
        )

    if db_module.is_account_locked(user):
        raise HTTPException(
            status_code=403,
            detail="Tài khoản tạm thời bị khoá do đăng nhập sai nhiều lần "
                   "liên tiếp — thử lại sau ít phút.",
        )

    if not security.verify_password(payload.password, user["password_hash"]):
        just_locked = db_module.record_failed_login(
            conn, str(user["ss_user_id"]),
            lock_threshold=security.FAILED_LOGIN_LOCK_THRESHOLD,
            lock_minutes=security.FAILED_LOGIN_LOCK_MINUTES,
        )
        conn.commit()
        if just_locked:
            raise HTTPException(
                status_code=403,
                detail=f"Sai mật khẩu quá {security.FAILED_LOGIN_LOCK_THRESHOLD} lần "
                       f"liên tiếp — tài khoản bị khoá tạm "
                       f"{security.FAILED_LOGIN_LOCK_MINUTES} phút.",
            )
        raise HTTPException(status_code=401, detail=_WRONG_CREDENTIALS_MSG)

    # Đăng nhập ĐÚNG — nâng cấp hash nếu tham số Argon2 đã đổi từ lúc
    # tạo mật khẩu này (xem docstring security.needs_rehash()).
    if security.needs_rehash(user["password_hash"]):
        db_module.update_user_password(
            conn, str(user["ss_user_id"]),
            security.hash_password(payload.password),
            must_change_password=user["must_change_password"],
        )

    db_module.reset_failed_login(conn, str(user["ss_user_id"]))

    # SINGLE SESSION (08/2026, xem sql/migration_add_single_session.sql):
    # login MỚI luôn thắng — thu hồi TOÀN BỘ refresh token cũ (nếu có,
    # từ phiên khác đang active) rồi sinh session_id mới, ghi đè
    # active_session_id. Access token của phiên cũ (nếu ai đang cầm) sẽ
    # bị get_current_user() từ chối NGAY ở lần gọi kế tiếp, không đợi
    # hết hạn 30 phút — đây là điểm khác với hành vi cũ (nhiều phiên
    # song song thoải mái).
    ss_user_id = str(user["ss_user_id"])
    db_module.revoke_all_refresh_tokens_for_user(conn, ss_user_id)
    new_session_id = security.generate_session_id()
    db_module.set_active_session_id(conn, ss_user_id, new_session_id)

    access_token, refresh_token = _issue_token_pair(conn, user, request, new_session_id)
    conn.commit()

    return TokenPairOut(
        access_token=access_token,
        refresh_token=refresh_token,
        must_change_password=user["must_change_password"],
    )


@router.post("/refresh", response_model=AccessTokenOut)
# 30/minute theo IP — thêm cùng đợt rà soát rate-limit (trước đó route
# này KHÔNG có giới hạn nào). Rủi ro chính không phải "đoán được token"
# (refresh token 48 byte ngẫu nhiên, đoán được là bất khả thi) mà là
# chặn bớt việc gọi lặp lại dồn dập vô ích (script lỗi loop, hoặc lạm
# dụng để dò phản ứng server) — 30/minute vẫn dư sức cho use case thật
# (access token 30 phút mới hết hạn 1 lần, không ai cần refresh nhanh
# hơn thế nhiều).
@limiter.limit("30/minute")
def refresh(payload: RefreshRequest, request: Request, conn=Depends(get_db)):
    """Xoay vòng refresh token: đổi lấy 1 CẶP token mới (cả access lẫn
    refresh), thu hồi token cũ ngay lập tức. Nếu token gửi lên là 1 token
    ĐÃ BỊ THU HỒI TỪ TRƯỚC (revoked_at đã có giá trị) — đây là dấu hiệu
    token bị đánh cắp (người dùng hợp lệ không có lý do dùng lại token đã
    đổi), phản ứng bằng cách thu hồi TOÀN BỘ token của user này, buộc
    đăng nhập lại trên mọi thiết bị."""
    token_hash = security.hash_refresh_token(payload.refresh_token)
    stored = db_module.get_refresh_token_by_hash(conn, token_hash)

    if stored is None:
        raise HTTPException(status_code=401, detail="Refresh token không hợp lệ.")

    if stored["revoked_at"] is not None:
        # Token cũ đã bị revoke (do đã xoay vòng trước đó) nhưng vẫn có
        # người gửi lên -> nghi bị đánh cắp -> thu hồi hết, chặn toàn bộ.
        revoked_count = db_module.revoke_all_refresh_tokens_for_user(
            conn, str(stored["ss_user_id"])
        )
        conn.commit()
        logger.warning(
            "Phát hiện refresh token bị tái sử dụng sau khi đã revoke "
            "(nghi bị đánh cắp) — đã thu hồi %d token của user %s.",
            revoked_count, stored["ss_user_id"],
        )
        raise HTTPException(
            status_code=401,
            detail="Refresh token đã bị thu hồi trước đó — vì lý do an "
                   "toàn, toàn bộ phiên đăng nhập của tài khoản này đã bị "
                   "đăng xuất. Đăng nhập lại.",
        )

    expires_at = stored["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise HTTPException(status_code=401, detail="Refresh token đã hết hạn — đăng nhập lại.")

    user = db_module.get_user_by_id(conn, str(stored["ss_user_id"]))
    if user is None or not user.get("is_active", True):
        raise HTTPException(status_code=403, detail="Tài khoản không còn hoạt động.")

    # refresh() KHÔNG sinh session_id mới (khác login()) — giữ nguyên
    # session của phiên đang xoay vòng. active_session_id chỉ NULL cho
    # tài khoản có refresh token còn hạn TỪ TRƯỚC lúc migration này
    # được deploy (chưa từng login lại để có session_id) — tự chữa lành
    # bằng cách sinh session_id lần đầu ở đây, tránh bắt buộc phải logout
    # thủ công toàn bộ user đang có phiên hợp lệ ngay lúc deploy.
    session_id = user.get("active_session_id")
    if session_id is None:
        session_id = security.generate_session_id()
        db_module.set_active_session_id(conn, str(user["ss_user_id"]), session_id)
    else:
        session_id = str(session_id)

    access_token, new_refresh_token = _issue_token_pair(conn, user, request, session_id)

    # Lấy refresh_token_id VỪA tạo để nối replaced_by_token_id — tra lại
    # bằng hash vì create_refresh_token() chỉ trả refresh_token_id dạng
    # str, cần bản ghi mới nhất để lấy đúng id nối vào token cũ.
    new_token_row = db_module.get_refresh_token_by_hash(
        conn, security.hash_refresh_token(new_refresh_token)
    )
    db_module.revoke_refresh_token(
        conn, str(stored["refresh_token_id"]),
        replaced_by_token_id=str(new_token_row["refresh_token_id"]) if new_token_row else None,
    )
    conn.commit()

    return AccessTokenOut(access_token=access_token, refresh_token=new_refresh_token)


@router.post("/logout", status_code=204)
# 30/minute theo IP — cùng lý do refresh() ở trên (chặn gọi lặp vô ích,
# không phải vì token đoán được).
@limiter.limit("30/minute")
def logout(request: Request, payload: RefreshRequest, conn=Depends(get_db)):
    """Đăng xuất — thu hồi ĐÚNG refresh token gửi lên (không đụng tới
    token của thiết bị khác). Không lỗi nếu token không tồn tại/đã thu
    hồi từ trước (đăng xuất nhiều lần vẫn coi là thành công, tránh lộ
    thông tin qua khác biệt response)."""
    stored = db_module.get_refresh_token_by_hash(
        conn, security.hash_refresh_token(payload.refresh_token)
    )
    if stored is not None:
        db_module.revoke_refresh_token(conn, str(stored["refresh_token_id"]))
        # Single-session: clear luôn active_session_id — access token
        # còn sống (chưa hết 30 phút) của phiên này cũng bị từ chối
        # ngay ở get_current_user(), không cần đợi tự hết hạn.
        db_module.set_active_session_id(conn, str(stored["ss_user_id"]), None)
        conn.commit()
    return None


@router.get("/me", response_model=UserOut)
def get_me(user: dict = Depends(get_current_user), conn=Depends(get_db)):
    """Thông tin user hiện tại — JWT chỉ chứa sub/role/email, nên vẫn
    cần 1 lượt query DB để lấy đủ field khác (full_name, is_active mới
    nhất...) cho frontend hiển thị, KHÔNG tin tưởng field nào ngoài
    sub/role/email trực tiếp từ token cho việc hiển thị (token có thể
    đã cũ hơn dữ liệu DB vài phút)."""
    row = db_module.get_user_by_id(conn, user["sub"])
    if row is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản.")
    return row


@router.post("/change-password", response_model=UserOut)
@limiter.limit("10/hour", key_func=get_user_id_or_ip)
def change_password(
    request: Request,
    payload: ChangePasswordRequest,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """Tự đổi mật khẩu. Nếu tài khoản đang must_change_password=True
    (mới tạo/vừa bị reset), CHO PHÉP bỏ qua old_password (người dùng chỉ
    có mật khẩu tạm admin đưa, không có 'mật khẩu cũ của riêng họ' theo
    đúng nghĩa) — mọi trường hợp khác BẮT BUỘC đúng old_password.

    Sau khi đổi thành công: thu hồi TOÀN BỘ refresh token hiện có (đăng
    xuất mọi thiết bị khác) — thực hành bảo mật chuẩn khi đổi mật khẩu,
    phòng trường hợp thiết bị khác đã bị lộ phiên đăng nhập.

    Rate limit 10/hour theo user_id (thêm 08/2026) — route này BẮT BUỘC
    JWT hợp lệ (get_current_user) nên không lo bot vô danh, nhưng nếu 1
    access token bị lộ (XSS, thiết bị bị chiếm...) kẻ tấn công có thể dò
    old_password không giới hạn số lần trước đây — cùng loại rủi ro với
    /auth/login (đã có limiter 20/minute từ trước), route đổi mật khẩu
    lại chưa có nên bổ sung cho nhất quán."""
    row = db_module.get_user_by_id(conn, user["sub"])
    if row is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản.")

    if not row["must_change_password"]:
        if not payload.old_password or not security.verify_password(
            payload.old_password, row["password_hash"]
        ):
            raise HTTPException(status_code=401, detail="Mật khẩu cũ không đúng.")

    db_module.update_user_password(
        conn, user["sub"], security.hash_password(payload.new_password),
        must_change_password=False,
    )
    db_module.revoke_all_refresh_tokens_for_user(conn, user["sub"])
    # Single-session: clear active_session_id — access token đang cầm
    # (kể cả của chính request này) cũng hết hiệu lực ngay từ request
    # kế tiếp, nhất quán với logout(). Người dùng cần đăng nhập lại để
    # lấy phiên mới, kể cả trên chính thiết bị vừa đổi mật khẩu.
    db_module.set_active_session_id(conn, user["sub"], None)
    conn.commit()

    return db_module.get_user_by_id(conn, user["sub"])
