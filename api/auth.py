"""
Router đăng nhập TỪNG NGƯỜI (JWT + refresh token xoay vòng) — xem
docstring api/security.py và sql/migration_add_auth.sql để hiểu toàn bộ
thiết kế trước khi đọc file này.

File này khai báo 2 router (08/2026, sửa sau bug link verify-email luôn
401 vì trình duyệt không tự gắn được X-API-Key khi bấm link từ email):

  - `router`: mọi endpoint vẫn nằm SAU lớp API_KEY (app.py include kèm
    dependencies=[Depends(require_api_key)]) — client cần đúng
    X-API-Key để gọi TỚI ĐƯỢC, JWT là lớp thứ 2 xác định "user thật
    nào" bên trong.
  - `public_router`: 3 route đăng ký/xác thực email công khai (register,
    verify-email, resend-verification) — KHÔNG cần X-API-Key (app.py
    include KHÔNG kèm dependencies), đúng ý nghĩa "ai cũng gọi được".
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

import db as db_module
from api import security
from api.deps import get_db, get_current_user, require_admin, require_role
from api.email_service import send_verification_email, send_password_reset_email
from api.schemas import (
    AccessTokenOut, ChangePasswordRequest, ForgotPasswordRequest, LoginRequest,
    MessageOut, RefreshRequest, RegisterOut, RegisterRequest, ResendVerificationRequest,
    ResetPasswordRequest, TokenPairOut, UserActiveStatusUpdate, UserCreateByAdmin,
    UserCreatedOut, UserOut, UserRoleUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

# public_router: 3 route đăng ký/xác thực email công khai (register,
# verify-email, resend-verification) — KHÔNG đi qua lớp X-API-Key đăng
# ký ở app.py (khác `router` ở trên, vẫn nằm sau X-API-Key như bình
# thường). Lý do tách riêng: GET /auth/verify-email được người dùng BẤM
# THẲNG từ email, trình duyệt không thể tự gắn header X-API-Key vào
# request đó -> nếu vẫn nằm trong lớp X-API-Key, link xác thực email sẽ
# LUÔN 401, không cách nào sửa được từ phía người dùng. register/
# resend-verification không bị giới hạn kỹ thuật này (có thể gọi qua
# code kèm key), nhưng gom chung 3 route vào public_router cho ĐÚNG Ý
# NGHĨA "đăng ký công khai" (ai cũng gọi được, không cần biết API_KEY
# nội bộ của team) — khớp đúng docstring đã ghi ngay trước 3 route này
# lúc code (xem bên dưới). app.py include_router(auth.public_router) mà
# KHÔNG kèm dependencies=[Depends(require_api_key)].
public_router = APIRouter(prefix="/auth", tags=["auth"])

# Link xác thực email hết hạn sau 24h — đủ dài để người dùng không bị
# gấp gáp (khác OTP thường vài phút), đủ ngắn để không treo lơ lửng tài
# khoản CHƯA xác thực quá lâu trong DB. Hết hạn thì gọi POST
# /auth/resend-verification xin token mới, không cần đăng ký lại.
EMAIL_VERIFY_EXPIRE_HOURS = 24

# Link reset mật khẩu hết hạn sau 1h — ngắn hơn hẳn link xác thực email
# (24h) vì reset mật khẩu là thao tác nhạy cảm hơn (ai có link = đổi
# được mật khẩu người khác nếu email bị lộ), không cần cho nhiều thời
# gian như link xác thực đăng ký (chỉ xác nhận sở hữu email, không đổi
# được gì). Hết hạn thì gọi lại POST /auth/forgot-password xin link mới.
PASSWORD_RESET_EXPIRE_HOURS = 1


def _generate_verify_token() -> str:
    """Chuỗi ngẫu nhiên URL-safe — tái dùng CÙNG cơ chế
    security.generate_refresh_token() (secrets.token_urlsafe) nhưng
    KHÔNG gọi thẳng hàm đó để giữ 2 khái niệm tách biệt rõ trong code
    (refresh token vs email verify token, dù cùng cách sinh)."""
    return secrets.token_urlsafe(32)


def _issue_token_pair(conn, user_row, request: Request) -> tuple[str, str]:
    """Sinh CẢ access token lẫn refresh token mới cho 1 user — dùng
    chung cho login lẫn refresh (rotation), tránh lặp code."""
    access_token = security.create_access_token(
        ss_user_id=str(user_row["ss_user_id"]),
        role=user_row["role"],
        email=user_row["email"],
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
def login(payload: LoginRequest, request: Request, conn=Depends(get_db)):
    """Đăng nhập bằng email + mật khẩu. Không tiết lộ qua thông báo lỗi
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
    access_token, refresh_token = _issue_token_pair(conn, user, request)
    conn.commit()

    return TokenPairOut(
        access_token=access_token,
        refresh_token=refresh_token,
        must_change_password=user["must_change_password"],
    )


@router.post("/refresh", response_model=AccessTokenOut)
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

    access_token, new_refresh_token = _issue_token_pair(conn, user, request)

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
def logout(payload: RefreshRequest, conn=Depends(get_db)):
    """Đăng xuất — thu hồi ĐÚNG refresh token gửi lên (không đụng tới
    token của thiết bị khác). Không lỗi nếu token không tồn tại/đã thu
    hồi từ trước (đăng xuất nhiều lần vẫn coi là thành công, tránh lộ
    thông tin qua khác biệt response)."""
    stored = db_module.get_refresh_token_by_hash(
        conn, security.hash_refresh_token(payload.refresh_token)
    )
    if stored is not None:
        db_module.revoke_refresh_token(conn, str(stored["refresh_token_id"]))
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
def change_password(
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
    phòng trường hợp thiết bị khác đã bị lộ phiên đăng nhập."""
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
    conn.commit()

    return db_module.get_user_by_id(conn, user["sub"])


@router.post("/users", response_model=UserCreatedOut, status_code=201)
def create_user(
    payload: UserCreateByAdmin,
    admin: dict = Depends(require_admin),
    conn=Depends(get_db),
):
    """CHỈ admin gọi được (require_admin). Mật khẩu TẠM được server tự
    sinh, trả về ĐÚNG 1 LẦN trong response này — admin tự đưa cho người
    dùng qua kênh khác (Slack/nói miệng), KHÔNG có luồng gửi email (xem
    README.md mục Auth). Tài khoản mới luôn must_change_password=True,
    bắt đổi mật khẩu ngay lần đăng nhập đầu."""
    if payload.role not in ("user", "ss_team", "admin"):
        raise HTTPException(
            status_code=400,
            detail="role phải là 1 trong: user, ss_team, admin.",
        )

    existing = db_module.get_user_by_email(conn, payload.email)
    if existing is not None:
        raise HTTPException(status_code=409, detail="Email này đã có tài khoản.")

    temp_password = security.generate_temp_password()
    ss_user_id = db_module.create_user(
        conn,
        full_name=payload.full_name,
        email=payload.email,
        password_hash=security.hash_password(temp_password),
        role=payload.role,
        must_change_password=True,
    )
    conn.commit()

    row = db_module.get_user_by_id(conn, ss_user_id)
    return {**row, "temp_password": temp_password}


@router.get("/users", response_model=list[UserOut])
def list_users(
    user: dict = Depends(require_role("ss_team")),
    conn=Depends(get_db),
):
    """Danh sách toàn bộ tài khoản (thêm 08/2026) — ss_team trở lên xem
    được (khác POST /auth/users tạo tài khoản, vẫn admin-only), dùng cho
    mục "xem danh sách tài khoản" trong dashboard ss_team đã thống nhất."""
    return db_module.list_users(conn)


@router.patch("/users/{ss_user_id}/role", response_model=UserOut)
def update_user_role(
    ss_user_id: str,
    payload: UserRoleUpdate,
    admin: dict = Depends(require_admin),
    conn=Depends(get_db),
):
    """CHỈ admin gọi được. Đổi role của 1 user khác — CHẶN admin tự đổi
    role CHÍNH MÌNH (tránh tự khoá mình khỏi quyền admin do bấm nhầm;
    muốn đổi role của chính mình thì nhờ admin khác, hoặc sửa thẳng
    trong DB nếu là admin duy nhất — xem lịch sử trao đổi trước khi
    code phần này)."""
    if payload.role not in ("user", "ss_team", "admin"):
        raise HTTPException(
            status_code=400,
            detail="role phải là 1 trong: user, ss_team, admin.",
        )
    if ss_user_id == admin["sub"]:
        raise HTTPException(
            status_code=400,
            detail="Không thể tự đổi role của chính mình — nhờ admin "
                   "khác thực hiện thao tác này.",
        )

    updated = db_module.update_user_role(conn, ss_user_id, payload.role)
    if not updated:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản.")
    conn.commit()

    return db_module.get_user_by_id(conn, ss_user_id)


@router.patch("/users/{ss_user_id}/active-status", response_model=UserOut)
def update_user_active_status(
    ss_user_id: str,
    payload: UserActiveStatusUpdate,
    admin: dict = Depends(require_admin),
    conn=Depends(get_db),
):
    """CHỈ admin gọi được. Khoá/mở khoá VĨNH VIỄN 1 tài khoản khác —
    CHẶN admin tự khoá CHÍNH MÌNH (cùng lý do với update_user_role() ở
    trên — tránh tự khoá mình khỏi hệ thống do bấm nhầm, đặc biệt nguy
    hiểm hơn tự đổi role vì is_active=false chặn đăng nhập hoàn toàn,
    không có role nào cứu được).

    Dùng khi 1 người rời nhóm/vi phạm cần chặn đăng nhập ngay — KHÁC
    locked_until (khoá TẠM THỜI, tự hết hạn do sai mật khẩu liên tiếp,
    xem db.record_failed_login()). Vô hiệu hoá không revoke JWT access
    token đang có hiệu lực (tối đa 30 phút) — xem docstring
    db.update_user_active_status()."""
    if ss_user_id == admin["sub"]:
        raise HTTPException(
            status_code=400,
            detail="Không thể tự vô hiệu hoá/kích hoạt chính mình — nhờ "
                   "admin khác thực hiện thao tác này.",
        )

    updated = db_module.update_user_active_status(conn, ss_user_id, payload.is_active)
    if not updated:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản.")
    conn.commit()

    return db_module.get_user_by_id(conn, ss_user_id)


# ------------------------------------------------------------------
# Đăng ký công khai + xác thực email (thêm 08/2026, xem
# sql/migration_add_email_verification.sql, api/email_service.py) —
# 3 route dưới đây KHÔNG cần JWT/API_KEY-per-user, ai cũng gọi được
# (đúng ý nghĩa "đăng ký công khai").
# ------------------------------------------------------------------

@public_router.post("/register", response_model=RegisterOut, status_code=201)
def register(payload: RegisterRequest, conn=Depends(get_db)):
    """Tự đăng ký — luôn tạo role='user' (thấp nhất, xem
    api.deps.ROLE_HIERARCHY), KHÔNG cho tự chọn role qua request (khác
    POST /auth/users admin-only có thể chọn role). Muốn lên 'ss_team'
    phải nhờ admin nâng cấp qua PATCH /auth/users/{id}/role SAU KHI đã
    đăng ký + xác thực email (đúng luồng đã thống nhất — xem lịch sử
    trao đổi).

    Tài khoản tạo xong CHƯA login được ngay — phải xác thực email trước
    (xem GET /auth/verify-email, login() chặn nếu email_verified=false).
    Nếu gửi email lỗi (Resend down/rate-limit...), tài khoản VẪN đã tạo
    thành công — người dùng tự gọi POST /auth/resend-verification sau,
    KHÔNG mất dữ liệu đã nhập, không cần đăng ký lại (xem
    api/email_service.py để hiểu lý do không raise khi gửi lỗi)."""
    existing = db_module.get_user_by_email(conn, payload.email)
    if existing is not None:
        raise HTTPException(status_code=409, detail="Email này đã có tài khoản.")

    verify_token = _generate_verify_token()
    verify_expires = datetime.now(timezone.utc) + timedelta(hours=EMAIL_VERIFY_EXPIRE_HOURS)

    ss_user_id = db_module.create_user_pending_verification(
        conn,
        full_name=payload.full_name,
        email=payload.email,
        password_hash=security.hash_password(payload.password),
        verify_token=verify_token,
        verify_expires=verify_expires,
        phone=payload.phone,
        track=payload.track,
    )
    conn.commit()

    send_verification_email(
        to_email=payload.email,
        full_name=payload.full_name,
        verify_token=verify_token,
    )

    return RegisterOut(ss_user_id=ss_user_id, email=payload.email)


@public_router.get("/verify-email", response_class=HTMLResponse)
def verify_email(token: str, conn=Depends(get_db)):
    """Endpoint người dùng BẤM TỪ EMAIL (không phải gọi qua code/frontend
    — xem api/email_service.py dựng link này), nên trả HTML tĩnh đơn
    giản thay vì JSON (frontend CHƯA xong lúc code phần này — xem lịch
    sử trao đổi). Khi có frontend thật, đổi route này sang redirect(302)
    tới URL frontend — KHÔNG cần sửa gì phần logic verify bên dưới, chỉ
    đổi câu return cuối cùng."""
    user = db_module.get_user_by_verify_token(conn, token)

    if user is None:
        return HTMLResponse(
            "<h2>Liên kết xác thực không hợp lệ</h2>"
            "<p>Liên kết đã được dùng trước đó hoặc không đúng — thử "
            "đăng ký lại hoặc xin gửi lại email xác thực.</p>",
            status_code=400,
        )

    expires_at = user["email_verify_expires"]
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            return HTMLResponse(
                "<h2>Liên kết xác thực đã hết hạn</h2>"
                "<p>Gọi POST /auth/resend-verification để nhận email "
                "xác thực mới.</p>",
                status_code=400,
            )

    db_module.mark_email_verified(conn, str(user["ss_user_id"]))
    conn.commit()

    return HTMLResponse(
        "<h2>✅ Xác thực thành công</h2>"
        "<p>Tài khoản của bạn đã được xác thực — bây giờ có thể đăng "
        "nhập.</p>"
    )


@public_router.post("/resend-verification", response_model=MessageOut)
def resend_verification(payload: ResendVerificationRequest, conn=Depends(get_db)):
    """Xin gửi lại email xác thực — dùng khi token cũ hết hạn (24h) hoặc
    email thất lạc. LUÔN trả cùng 1 message dù email có tồn tại hay
    không, và dù tài khoản đã verify từ trước hay chưa (giống nguyên tắc
    _WRONG_CREDENTIALS_MSG ở login() — tránh lộ qua thông báo lỗi việc
    email nào đã có tài khoản trong hệ thống, chống dò email hàng loạt)."""
    _GENERIC_MSG = (
        "Nếu email này có tài khoản CHƯA xác thực, một email xác thực "
        "mới đã được gửi tới đó."
    )

    user = db_module.get_user_by_email(conn, payload.email)
    if user is None or user.get("email_verified", True):
        return MessageOut(message=_GENERIC_MSG)

    verify_token = _generate_verify_token()
    verify_expires = datetime.now(timezone.utc) + timedelta(hours=EMAIL_VERIFY_EXPIRE_HOURS)

    db_module.set_new_verify_token(conn, str(user["ss_user_id"]), verify_token, verify_expires)
    conn.commit()

    send_verification_email(
        to_email=user["email"],
        full_name=user["full_name"],
        verify_token=verify_token,
    )

    return MessageOut(message=_GENERIC_MSG)


# ------------------------------------------------------------------
# Quên mật khẩu (thêm 08/2026, xem sql/migration_add_password_reset.sql,
# api/email_service.py) — 2 route công khai, KHÔNG cần JWT (đúng bản
# chất: user quên mật khẩu thì KHÔNG login được để lấy JWT trước).
# ------------------------------------------------------------------

@public_router.post("/forgot-password", response_model=MessageOut)
def forgot_password(payload: ForgotPasswordRequest, conn=Depends(get_db)):
    """Xin link đặt lại mật khẩu — LUÔN trả cùng 1 message dù email có
    tồn tại hay không (giống hệt nguyên tắc resend_verification() ở
    trên — chống dò email hàng loạt: kẻ tấn công không phân biệt được
    'email không tồn tại' với 'email tồn tại, email đã gửi' qua response).

    KHÔNG chặn nếu tài khoản chưa xác thực email (khác login() cấm đăng
    nhập trước khi verify) — quên mật khẩu và chưa-xác-thực-email là 2
    vấn đề độc lập, user vẫn nên đặt lại mật khẩu được dù tài khoản
    chưa verify (họ vẫn cần link xác thực RIÊNG nếu muốn login sau đó,
    nhưng không có lý do gì chặn họ đổi mật khẩu trước)."""
    _GENERIC_MSG = (
        "Nếu email này có tài khoản, một email đặt lại mật khẩu đã "
        "được gửi tới đó."
    )

    user = db_module.get_user_by_email(conn, payload.email)
    if user is None:
        return MessageOut(message=_GENERIC_MSG)

    reset_token = _generate_verify_token()  # cùng cơ chế sinh token (secrets.token_urlsafe), khác tên biến cho rõ ngữ cảnh
    reset_expires = datetime.now(timezone.utc) + timedelta(hours=PASSWORD_RESET_EXPIRE_HOURS)

    db_module.set_password_reset_token(conn, str(user["ss_user_id"]), reset_token, reset_expires)
    conn.commit()

    send_password_reset_email(
        to_email=user["email"],
        full_name=user["full_name"],
        reset_token=reset_token,
    )

    return MessageOut(message=_GENERIC_MSG)


@public_router.post("/reset-password", response_model=MessageOut)
def reset_password(payload: ResetPasswordRequest, conn=Depends(get_db)):
    """Đặt mật khẩu mới bằng token nhận từ email — token dùng ĐÚNG 1 LẦN
    (xoá ngay sau khi dùng, xem db.reset_password_with_token()).

    Sau khi đổi thành công, THU HỒI TOÀN BỘ refresh token hiện có của
    user (revoke_all_refresh_tokens_for_user) — nếu lý do quên mật khẩu
    là bị lộ mật khẩu/máy bị chiếm quyền, phiên đăng nhập cũ (nếu kẻ tấn
    công đang có access/refresh token còn hạn) sẽ bị đá ra ngay, không
    đợi access token 30 phút tự hết hạn."""
    user = db_module.get_user_by_reset_token(conn, payload.token)
    if user is None:
        raise HTTPException(status_code=400, detail="Link đặt lại mật khẩu không hợp lệ hoặc đã được dùng.")

    expires_at = user["password_reset_expires"]
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="Link đặt lại mật khẩu đã hết hạn — gọi lại POST /auth/forgot-password để xin link mới.")

    ss_user_id = str(user["ss_user_id"])
    db_module.reset_password_with_token(conn, ss_user_id, security.hash_password(payload.new_password))
    db_module.revoke_all_refresh_tokens_for_user(conn, ss_user_id)
    conn.commit()

    return MessageOut(message="Đặt lại mật khẩu thành công — vui lòng đăng nhập lại bằng mật khẩu mới.")
