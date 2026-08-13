"""
Router đăng nhập TỪNG NGƯỜI (JWT + refresh token xoay vòng) — xem
docstring api/security.py và sql/migration_add_auth.sql để hiểu toàn bộ
thiết kế trước khi đọc file này.

Mọi endpoint ở đây vẫn nằm SAU lớp API_KEY (đăng ký cấp app trong
api/app.py) — tức client vẫn cần đúng X-API-Key để gọi TỚI ĐƯỢC những
route này, JWT là lớp thứ 2 xác định "user thật nào" bên trong.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

import db as db_module
from api import security
from api.deps import get_db, get_current_user, require_admin, require_role
from api.schemas import (
    AccessTokenOut, ChangePasswordRequest, LoginRequest, RefreshRequest,
    TokenPairOut, UserCreateByAdmin, UserCreatedOut, UserOut, UserRoleUpdate,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


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
