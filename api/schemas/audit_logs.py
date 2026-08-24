"""
Audit logs — schema response cho GET /audit-logs, PATCH
/audit-logs/{log_id}/note (lịch sử thao tác ss_team/admin, 08/2026, xem
db.py mục "AUDIT LOGS" + sql/migration_add_audit_logs.sql). Tách từ
api/schemas.py (08/2026) — xem docstring api/schemas/__init__.py.
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


# ------------------------------------------------------------------
# Audit logs — lịch sử thao tác ss_team/admin (08/2026, xem db.py mục
# "AUDIT LOGS" + sql/migration_add_audit_logs.sql).
# ------------------------------------------------------------------

class AuditLogOut(BaseModel):
    log_id: str
    actor_id: Optional[str] = Field(
        default=None, description="null = thao tác tự động (crawl), không phải người tạo."
    )
    actor_name: Optional[str] = Field(
        default=None, description="full_name của actor tại THỜI ĐIỂM TRUY VẤN (join sống, "
                                   "không phải snapshot) — null nếu actor_id null hoặc tài khoản đã bị xoá."
    )
    action_type: str = Field(
        description="CREATE_JOB | UPDATE_JOB | DELETE_JOB | CREATE_COMPANY | "
                    "UPDATE_COMPANY | DELETE_COMPANY | CREATE_CONTACT | "
                    "UPDATE_CONTACT | DELETE_CONTACT | ASSIGN_CONTACT"
    )
    entity_type: str = Field(description="JOB | COMPANY | CONTACT")
    entity_id: str
    entity_label: Optional[str] = Field(
        default=None, description="Tên JD/company/contact SNAPSHOT tại thời điểm log — "
                                   "vẫn hiển thị đúng dù entity sau này đổi tên/bị xoá."
    )
    company_id: Optional[str] = None
    company_name: Optional[str] = Field(
        default=None, description="Tên company HIỆN TẠI (join sống) — có thể khác entity_label "
                                   "nếu action_type liên quan company và company đã đổi tên sau đó."
    )
    changes: Optional[dict] = Field(
        default=None,
        description="{field: {old, new}} — chỉ có ở action UPDATE_*, null cho CREATE/DELETE/ASSIGN.",
    )
    is_manual_log: bool = Field(
        description="true = action này nằm trong view 'log thủ công' (subset các action nhạy "
                    "cảm: sửa/xoá JD, sửa/xoá company, mọi thao tác HR contact)."
    )
    note_required: bool = Field(
        description="true = action này BẮT BUỘC phải có note lúc thao tác (đã chặn cứng ở "
                    "tầng API, nên nếu note_required=true thì note LUÔN có giá trị)."
    )
    note: Optional[str] = None
    note_updated_by: Optional[str] = None
    note_updated_at: Optional[datetime] = None
    created_at: datetime

    class Config:
        from_attributes = True


class PaginatedAuditLogs(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[AuditLogOut]


class AuditLogNoteUpdate(BaseModel):
    """Body cho PATCH /audit-logs/{log_id}/note — CHỈ dùng để bổ sung/sửa
    note của log thuộc nhóm TUỲ CHỌN (note_required=false, vd sửa/xoá JD,
    sửa company, tạo contact). Log thuộc nhóm BẮT BUỘC đã CÓ note ngay
    lúc tạo (chặn cứng, xem ACTION_LOG_RULES trong db.py) nên route này
    vẫn CHO sửa lại (chỉnh câu chữ), nhưng KHÔNG cho set về rỗng nếu
    note_required=true (route trả 422 nếu cố tình xoá note của log bắt
    buộc — xem api/routers/audit_logs.py).

    Chỉ actor_id GỐC của log mới gọi được route này — kiểm tra ở router,
    không ở schema."""
    model_config = ConfigDict(extra="forbid")
    
    note: str = Field(..., min_length=1, description="Nội dung note mới.")

    @field_validator("note")
    @classmethod
    def _note_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("note không được để trống hoặc chỉ chứa khoảng trắng")
        return v


