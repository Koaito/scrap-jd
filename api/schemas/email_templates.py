"""
Email templates — schema request/response cho GET/POST/PATCH/DELETE
/email-templates (thêm 08/2026, xem sql/migration_add_email_templates.sql
+ db/email_templates.py). Tách theo domain, cùng pattern các submodule
khác trong package này (xem docstring api/schemas/__init__.py).
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator

# 5 placeholder cố định — GIỮ NGUYÊN theo đúng yêu cầu đã chốt (không tự
# do thêm placeholder mới), chỉ hiển thị cho staff xem cách điền đúng khi
# soạn/sửa mẫu. Any/PLACEHOLDER_HELP nằm ở đây (không phải constants.py)
# vì chỉ email_templates dùng, tránh làm phình constants.py dùng chung
# toàn repo cho 1 tính năng hẹp.
PLACEHOLDER_HELP: dict[str, str] = {
    "{{LOI_CHAO}}": "Câu chào đầu thư — tự động ghép 'Kính gửi <tên người liên hệ>' "
                     "(kèm chức danh nếu có). Không tự gõ tay 'Kính gửi...' nữa vì đã có sẵn ở đây.",
    "{{TEN_CONG_TY}}": "Tên công ty của người liên hệ đang xem/sửa mẫu (tự điền theo đúng công ty).",
    "{{TEN_NGUOI_LIEN_HE}}": "Tên người liên hệ (HR) đang xem/sửa mẫu — dùng để nhắc lại tên "
                             "trong thân email cho tự nhiên, ví dụ 'nhờ {{TEN_NGUOI_LIEN_HE}} xem giúp em'.",
    "{{CHUC_DANH}}": "Chức danh của người liên hệ (vd 'HR Manager') — để trống nếu contact "
                      "chưa có thông tin này, không lỗi gì cả.",
    "{{TEN_STAFF}}": "Tên bạn (người đang đăng nhập, tự lấy từ hồ sơ tài khoản) — dùng để ký tên cuối thư.",
}


class EmailTemplateOut(BaseModel):
    template_id: str
    title: str
    description: Optional[str] = None
    body: str
    recommended_for: list[str] = Field(default_factory=list)
    display_order: int
    created_at: datetime
    updated_at: datetime
    created_by: Optional[str] = None
    updated_by: Optional[str] = None

    class Config:
        from_attributes = True


class PlaceholderHelpOut(BaseModel):
    """GET /email-templates/placeholder-help — bảng chú giải hiển thị
    trong UI thêm/sửa mẫu, để staff biết điền {{...}} nào cho đúng ý khi
    dùng trong popup chọn mẫu (fillPlaceholders() phía frontend)."""
    placeholders: dict[str, str] = Field(default_factory=lambda: dict(PLACEHOLDER_HELP))


class EmailTemplateCreate(BaseModel):
    """Tạo mẫu mới — note KHÔNG bắt buộc (giống CREATE_CONTACT/CREATE_JOB),
    xem ACTION_LOG_RULES trong db/audit_logs.py."""
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=500)
    body: str = Field(..., min_length=1, description="Nội dung mẫu, dùng {{...}} cho các placeholder cố định.")
    recommended_for: list[str] = Field(
        default_factory=list,
        description="Gợi ý mẫu này cho (các) trạng thái contact nào — mảng con của "
                    "UNCONTACTED | EMAIL_SENT | RESPONDED | IN_PARTNERSHIP. Để trống = "
                    "không gợi ý riêng cho trạng thái nào (mẫu dùng chung).",
    )
    display_order: int = Field(default=0, description="Thứ tự hiển thị trong danh sách chọn mẫu — số nhỏ hơn hiện trước.")
    note: Optional[str] = Field(
        default=None,
        description="Ghi chú cho log thủ công — TUỲ CHỌN, vd lý do thêm mẫu này.",
    )

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("title không được để trống hoặc chỉ chứa khoảng trắng")
        return v

    @field_validator("body")
    @classmethod
    def _body_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("body không được để trống hoặc chỉ chứa khoảng trắng")
        return v

    @field_validator("recommended_for")
    @classmethod
    def _recommended_for_valid(cls, v: list[str]) -> list[str]:
        valid = {"UNCONTACTED", "EMAIL_SENT", "RESPONDED", "IN_PARTNERSHIP"}
        invalid = [s for s in v if s not in valid]
        if invalid:
            raise ValueError(f"recommended_for chứa giá trị không hợp lệ: {invalid} — chỉ nhận {sorted(valid)}.")
        return v


class EmailTemplateUpdate(BaseModel):
    """Sửa TỰ DO — mọi field optional, chỉ field có mặt trong body mới
    bị ghi đè, giống CompanyContactUpdate.

    note: BẮT BUỘC (theo đúng yêu cầu đã chốt: "sửa/xoá bắt buộc phải có
    note") NẾU thực sự có field nào ở trên bị đổi giá trị — router tự
    kiểm tra diff trước khi enforce, patch rỗng/trùng giá trị cũ thì note
    không bắt buộc (chưa có gì để giải thích lý do sửa)."""
    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=500)
    body: Optional[str] = Field(default=None, min_length=1)
    recommended_for: Optional[list[str]] = None
    display_order: Optional[int] = None
    note: Optional[str] = Field(
        default=None,
        description="BẮT BUỘC nếu có field nào ở trên thực sự thay đổi giá trị — "
                    "lý do sửa mẫu email này, để các ss_team khác xem lại được.",
    )

    @field_validator("recommended_for")
    @classmethod
    def _recommended_for_valid(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        if v is None:
            return v
        valid = {"UNCONTACTED", "EMAIL_SENT", "RESPONDED", "IN_PARTNERSHIP"}
        invalid = [s for s in v if s not in valid]
        if invalid:
            raise ValueError(f"recommended_for chứa giá trị không hợp lệ: {invalid} — chỉ nhận {sorted(valid)}.")
        return v


class EmailTemplateDeleteRequest(BaseModel):
    """Body cho DELETE /email-templates/{template_id} (XOÁ HẲN, không
    soft-delete) — note BẮT BUỘC theo đúng yêu cầu đã chốt."""
    note: str = Field(
        ..., min_length=1,
        description="BẮT BUỘC — lý do xoá mẫu email này, để các ss_team khác biết "
                    "vì sao (vd: không còn phù hợp, trùng nội dung mẫu khác...).",
    )

    @field_validator("note")
    @classmethod
    def _note_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("note không được để trống hoặc chỉ chứa khoảng trắng")
        return v
