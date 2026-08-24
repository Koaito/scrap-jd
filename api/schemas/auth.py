"""
Auth — schema request/response cho đăng nhập/đăng ký/đổi mật khẩu/quản
lý user qua JWT (thêm 08/2026, xem api/routers/auth.py). Tách từ
api/schemas.py (08/2026) — xem docstring api/schemas/__init__.py.
"""

import re
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator


# ------------------------------------------------------------------
# Auth — đăng nhập TỪNG NGƯỜI qua JWT (thêm 08/2026)
#
# KHÁC API_KEY tĩnh (không có schema riêng, chỉ 1 header cố định) — nhóm
# schema dưới đây phục vụ luồng login/refresh/đổi mật khẩu/admin tạo
# user cho frontend, xem api/routers/auth.py.
# ------------------------------------------------------------------

class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    
    email: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class TokenPairOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    must_change_password: bool = False


class RefreshRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    
    refresh_token: str = Field(..., min_length=1)


class AccessTokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    
    # Optional: KHÔNG bắt buộc khi must_change_password=True (tài khoản
    # mới tạo/vừa bị admin reset — người dùng chưa từng có mật khẩu
    # "thật" của riêng họ để xác nhận, chỉ có mật khẩu tạm admin đưa).
    # Route (api/routers/auth.py) tự quyết định có bắt buộc field này
    # hay không dựa theo must_change_password hiện tại của user.
    old_password: Optional[str] = None
    new_password: str = Field(..., min_length=8)


class UserOut(BaseModel):
    """KHÔNG bao giờ chứa password_hash — dùng cho mọi response trả
    thông tin user ra ngoài (GET /auth/me, danh sách user cho admin)."""
    ss_user_id: str
    full_name: str
    email: str
    role: str
    is_active: bool
    must_change_password: bool
    last_login_at: Optional[datetime] = None
    created_at: datetime
    # Thêm 08/2026 (xem sql/migration_add_phone_track.sql) — CHỈ có ý
    # nghĩa với role='user' (học viên); chỉ POST /auth/register (luôn
    # role='user') mới ghi được 2 field này, POST /auth/users (admin
    # tạo ss_team/admin) dùng schema UserCreateByAdmin không có phone/
    # track nên staff luôn NULL sẵn ở DB — nhưng vẫn ép rõ ràng ở đây
    # (model_validator bên dưới) thay vì trông chờ NULL tình cờ, để
    # không lộ "field thừa luôn null" ra response của staff, và để
    # đúng ngay cả nếu sau này có ai lỡ ghi giá trị vào 2 cột này cho
    # 1 tài khoản staff (sửa tay DB, hoặc route khác sau này).
    phone: Optional[str] = None
    track: Optional[str] = None

    @model_validator(mode="after")
    def _hide_phone_track_for_staff(self):
        if self.role != "user":
            self.phone = None
            self.track = None
        return self

    class Config:
        from_attributes = True


class UserCreateByAdmin(BaseModel):
    """Admin tạo tài khoản MỚI — KHÔNG có luồng tự đăng ký công khai
    (xem README.md mục Auth). Mật khẩu TẠM được server tự sinh
    (security.generate_temp_password()), trả về ĐÚNG 1 LẦN trong response
    (xem UserCreatedOut) — admin tự đưa cho người dùng qua kênh khác
    (Slack/nói miệng), KHÔNG có luồng gửi email."""
    model_config = ConfigDict(extra="forbid")
    
    full_name: str = Field(..., min_length=1)
    email: str = Field(..., min_length=1)
    role: str = Field(default="user", description="user | ss_team | admin")


class UserCreatedOut(UserOut):
    """Như UserOut, thêm temp_password — CHỈ xuất hiện trong response
    NGAY LÚC TẠO, không có endpoint nào khác trả lại được mật khẩu tạm
    này sau đó (không lưu bản rõ, chỉ lưu hash)."""
    temp_password: str


class UserRoleUpdate(BaseModel):
    """Body cho PATCH /auth/users/{id}/role (admin-only, thêm 08/2026)."""
    model_config = ConfigDict(extra="forbid")
    
    role: str = Field(..., description="user | ss_team | admin")


class UserActiveStatusUpdate(BaseModel):
    """Body cho PATCH /auth/users/{id}/active-status (admin-only).
    Khoá VĨNH VIỄN 1 tài khoản (is_active=false) — KHÁC locked_until (khoá
    TẠM THỜI tự hết hạn do sai mật khẩu nhiều lần, xem
    sql/migration_add_auth.sql). Dùng khi 1 người rời nhóm/vi phạm và cần
    chặn đăng nhập ngay lập tức, không chờ tự hết hạn."""
    model_config = ConfigDict(extra="forbid")
    
    is_active: bool = Field(..., description="true = kích hoạt lại, false = vô hiệu hoá")


# ------------------------------------------------------------------
# Đăng ký công khai + xác thực email (thêm 08/2026, xem
# sql/migration_add_email_verification.sql, api/email_service.py) —
# KHÁC UserCreateByAdmin (admin tạo hộ) ở chỗ AI CŨNG gọi được (không
# cần JWT), tự chọn mật khẩu (không có must_change_password), luôn cố
# định role='user' — route (KHÔNG phải schema) tự gán cứng role, người
# gọi không truyền/chọn được field này qua request.
# ------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class RegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    
    full_name: str = Field(..., min_length=1)
    email: str = Field(..., min_length=1)
    password: str = Field(..., min_length=8)
    # Thêm 08/2026 (xem sql/migration_add_phone_track.sql) — trước đó
    # frontend đã gửi 2 field này lên nhưng bị Pydantic âm thầm bỏ qua
    # vì không khai báo ở đây, không phải vì cố tình optional-và-bỏ-qua.
    phone: Optional[str] = Field(default=None, max_length=30)
    track: Optional[str] = Field(default=None, max_length=100)

    def model_post_init(self, __context) -> None:
        # Pydantic v2: EmailStr cần cài thêm 'email-validator' (chưa có
        # trong requirements.txt) — tự viết regex đơn giản để KHÔNG
        # thêm dependency mới cho 1 việc nhỏ. Không cần chuẩn RFC 5322
        # đầy đủ, chỉ cần chặn input rõ ràng sai (thiếu @, thiếu domain).
        if not _EMAIL_RE.match(self.email):
            raise ValueError("Email không đúng định dạng.")


class RegisterOut(BaseModel):
    """KHÔNG trả access_token/refresh_token — đăng ký xong PHẢI xác
    thực email trước mới login được (xem api/routers/auth.py login()),
    nên trả về thông báo hướng dẫn thay vì token."""
    ss_user_id: str
    email: str
    message: str = "Đăng ký thành công — kiểm tra email để xác thực tài khoản trước khi đăng nhập."


class ResendVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    
    email: str = Field(..., min_length=1)


class ForgotPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    
    email: str = Field(..., min_length=1)


class ResetPasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    
    token: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=8)


class MessageOut(BaseModel):
    """Response chung cho các action chỉ cần xác nhận đã thực hiện,
    không có dữ liệu cụ thể để trả (resend-verification, forgot-password...)."""
    message: str


