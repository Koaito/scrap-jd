"""
Router đăng ký công khai + xác thực email + quên mật khẩu — xem
docstring api/security.py và sql/migration_add_auth.sql để hiểu toàn
bộ thiết kế trước khi đọc file này.

Tách ra từ api/routers/auth.py (08/2026) — xem docstring auth.py
(facade) và auth_session.py để biết lý do tách 738 dòng/14 endpoint
thành 3 file theo domain. File này chứa TOÀN BỘ route công khai (ai
cũng gọi được, không cần biết API_KEY nội bộ của team):
register, verify-email, resend-verification, forgot-password,
reset-password — đều nằm trên `public_router`.

`public_router` (khác `router` ở auth_session.py/auth_users.py) —
KHÔNG cần X-API-Key (app.py include KHÔNG kèm dependencies). Lý do:
GET /auth/verify-email được người dùng BẤM THẲNG từ email, trình
duyệt không thể tự gắn header X-API-Key vào request đó -> nếu vẫn
nằm trong lớp X-API-Key, link xác thực email sẽ LUÔN 401, không cách
nào sửa được từ phía người dùng. register/resend-verification/
forgot-password/reset-password không bị giới hạn kỹ thuật này (có thể
gọi qua code kèm key), nhưng gom chung vào public_router cho ĐÚNG Ý
NGHĨA "đăng ký công khai".
"""

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

import db as db_module
from api import security
from api.deps import get_db
from api.email_service import FRONTEND_BASE_URL, send_verification_email, send_password_reset_email
from api.rate_limit import limiter
from api.schemas import (
    ForgotPasswordRequest, MessageOut, RegisterOut, RegisterRequest,
    ResendVerificationRequest, ResetPasswordRequest,
)

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


@public_router.post("/register", response_model=RegisterOut, status_code=201)
# 5 lần/giờ/IP — đủ cho người dùng thật đăng ký lại nếu gõ sai vài lần,
# chặn được script tạo tài khoản rác hàng loạt (xem api/rate_limit.py).
@limiter.limit("5/hour")
def register(payload: RegisterRequest, request: Request, conn=Depends(get_db)):
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

    # Token THÔ chỉ tồn tại trong biến này + email gửi cho người dùng —
    # DB chỉ nhận HASH (security.hash_verification_token()), không bao
    # giờ lưu token đọc được trực tiếp (sửa bảo mật, xem docstring hàm
    # đó và create_user_pending_verification()).
    ss_user_id = db_module.create_user_pending_verification(
        conn,
        full_name=payload.full_name,
        email=payload.email,
        password_hash=security.hash_password(payload.password),
        verify_token_hash=security.hash_verification_token(verify_token),
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


@public_router.get("/verify-email")
# 30/hour theo IP — thêm cùng đợt rà soát rate-limit (trước đó route
# này KHÔNG có giới hạn nào, khác 3 route "chị em" resend-verification/
# forgot-password/reset-password đã có từ đầu). Token 32 byte urlsafe
# gần như không thể đoán được nên đây chỉ là lớp phòng thủ thêm, không
# phải lớp chính — 30/hour đủ rộng cho người dùng bấm link vài lần (vd
# double-click, hoặc trình quét link an toàn của Outlook/Gmail tự mở
# link 1 lần trước khi người dùng bấm) mà vẫn chặn được request lặp bất
# thường.
@limiter.limit("30/hour")
def verify_email(token: str, request: Request, conn=Depends(get_db)):
    """Endpoint người dùng BẤM TỪ EMAIL (không phải gọi qua code/frontend
    — xem api/email_service.py dựng link này). Route này KHÔNG tự vẽ
    giao diện — chỉ xử lý token rồi redirect(302) NGAY về trang
    /verify-email của FRONTEND (mindx-jobs, xem FRONTEND_BASE_URL trong
    api/email_service.py — dùng chung biến với link reset mật khẩu) kèm
    ?status=success|expired|invalid, để frontend tự hiển thị đúng theme
    của site (trước đây trả HTML tĩnh viết tay ở chính route này — bỏ
    từ lúc frontend đã có trang riêng, xem lịch sử trao đổi 08/2026).

    token nhận từ query string LUÔN là token THÔ (đúng giá trị trong link
    email) — hash lại tại đây trước khi tra DB (DB chỉ lưu hash, xem
    docstring register())."""
    user = db_module.get_user_by_verify_token_hash(
        conn, security.hash_verification_token(token)
    )

    if user is None:
        return RedirectResponse(f"{FRONTEND_BASE_URL}/verify-email?status=invalid", status_code=302)

    expires_at = user["email_verify_expires"]
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            return RedirectResponse(f"{FRONTEND_BASE_URL}/verify-email?status=expired", status_code=302)

    db_module.mark_email_verified(conn, str(user["ss_user_id"]))
    conn.commit()

    return RedirectResponse(f"{FRONTEND_BASE_URL}/verify-email?status=success", status_code=302)


@public_router.post("/resend-verification", response_model=MessageOut)
# 3 lần/giờ/IP — thấp hơn register vì route này trigger gửi email ngay
# lập tức mỗi lần gọi, dễ bị lợi dụng "bomb" email tới 1 địa chỉ.
@limiter.limit("3/hour")
def resend_verification(payload: ResendVerificationRequest, request: Request, conn=Depends(get_db)):
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

    # DB chỉ nhận hash, token thô chỉ tồn tại ở đây + email gửi đi — xem
    # docstring register()/set_new_verify_token().
    db_module.set_new_verify_token(
        conn, str(user["ss_user_id"]),
        security.hash_verification_token(verify_token), verify_expires,
    )
    conn.commit()

    send_verification_email(
        to_email=user["email"],
        full_name=user["full_name"],
        verify_token=verify_token,
    )

    return MessageOut(message=_GENERIC_MSG)


@public_router.post("/forgot-password", response_model=MessageOut)
# 3 lần/giờ/IP — cùng lý do resend_verification() ở trên (gửi email
# thật mỗi lần gọi, cùng nguy cơ bị lợi dụng "bomb" email).
@limiter.limit("3/hour")
def forgot_password(payload: ForgotPasswordRequest, request: Request, conn=Depends(get_db)):
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

    # DB chỉ nhận hash, token thô chỉ tồn tại ở đây + email gửi đi — xem
    # docstring register()/set_password_reset_token().
    db_module.set_password_reset_token(
        conn, str(user["ss_user_id"]),
        security.hash_verification_token(reset_token), reset_expires,
    )
    conn.commit()

    send_password_reset_email(
        to_email=user["email"],
        full_name=user["full_name"],
        reset_token=reset_token,
    )

    return MessageOut(message=_GENERIC_MSG)


@public_router.post("/reset-password", response_model=MessageOut)
# 10 lần/giờ/IP — cao hơn 2 route trên vì không gửi email, chỉ là lớp
# phòng thủ thêm chống dò token (token 32 byte urlsafe gần như không
# thể đoán được trong phạm vi 10 lần, xem docstring api/rate_limit.py).
@limiter.limit("10/hour")
def reset_password(payload: ResetPasswordRequest, request: Request, conn=Depends(get_db)):
    """Đặt mật khẩu mới bằng token nhận từ email — token dùng ĐÚNG 1 LẦN
    (xoá ngay sau khi dùng, xem db.reset_password_with_token()).

    Sau khi đổi thành công, THU HỒI TOÀN BỘ refresh token hiện có của
    user (revoke_all_refresh_tokens_for_user) — nếu lý do quên mật khẩu
    là bị lộ mật khẩu/máy bị chiếm quyền, phiên đăng nhập cũ (nếu kẻ tấn
    công đang có access/refresh token còn hạn) sẽ bị đá ra ngay, không
    đợi access token 30 phút tự hết hạn.

    payload.token là token THÔ từ email — hash lại trước khi tra DB
    (DB chỉ lưu hash, xem docstring set_password_reset_token())."""
    user = db_module.get_user_by_reset_token_hash(
        conn, security.hash_verification_token(payload.token)
    )
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
    # Single-session: clear active_session_id — nhất quán với
    # change_password()/logout() (xem docstring change_password()).
    db_module.set_active_session_id(conn, ss_user_id, None)
    conn.commit()

    return MessageOut(message="Đặt lại mật khẩu thành công — vui lòng đăng nhập lại bằng mật khẩu mới.")
