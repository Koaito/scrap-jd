"""
Company contacts (HR contact) — schema request/response cho
POST/PATCH/DELETE /contacts (thêm 08/2026, xem db.py mục cùng tên).
Tách từ api/schemas.py (08/2026) — xem docstring api/schemas/__init__.py.
"""

from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


# ------------------------------------------------------------------
# Company contacts (HR contact) — thêm 08/2026, xem db.py mục cùng tên
# ------------------------------------------------------------------

class CompanyContactOut(BaseModel):
    contact_id: str
    company_id: str
    contact_name: str
    job_title: Optional[str] = None
    work_email: Optional[str] = None
    social_link: Optional[str] = None
    phone_number: Optional[str] = None
    found_source: Optional[str] = None
    collected_date: Optional[date] = None
    last_contacted_date: Optional[date] = None
    contact_status: str
    is_active: bool
    assigned_ss_user: Optional[str] = None
    created_by: Optional[str] = None
    updated_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class CompanyContactWithCompanyOut(CompanyContactOut):
    """Giống CompanyContactOut, thêm company_name — dùng cho GET /contacts
    (danh sách gộp mọi công ty, xem api/routers/contacts.py::list_all_contacts),
    vì CompanyContactOut không có tên công ty (route cũ GET
    /companies/{company_id}/contacts đã biết company_id sẵn từ path nên
    không cần)."""
    company_name: str


class CompanyContactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    
    contact_name: str = Field(..., min_length=1)
    job_title: Optional[str] = None
    work_email: Optional[str] = None
    social_link: Optional[str] = None
    phone_number: Optional[str] = None
    found_source: Optional[str] = None
    assigned_ss_user: Optional[str] = Field(
        None, description="ss_user_id của thành viên ss_team/admin phụ trách contact này ngay từ lúc tạo — có thể bỏ trống, gán sau qua PATCH /contacts/{contact_id}/assign."
    )
    note: Optional[str] = Field(
        default=None,
        description="Ghi chú cho log thủ công — TUỲ CHỌN, vd nguồn tìm được contact "
                    "này ngoài found_source, hoặc bối cảnh liên quan.",
    )


class CompanyContactUpdate(BaseModel):
    """Mọi field optional — chỉ field có mặt trong body mới bị ghi đè,
    giống pattern JobUpdate.

    note: BẮT BUỘC (khác mọi field khác trong class này) NẾU thực sự có
    field nào ở trên bị đổi giá trị — sửa HR contact là 1 trong 4 action
    bị CHẶN CỨNG nếu thiếu note (xem ACTION_LOG_RULES trong db.py):
    thiếu note khi có thay đổi thật -> 422, KHÔNG lưu, KHÔNG ghi log.
    Nếu body không đổi field nào (patch rỗng hoặc trùng giá trị cũ) thì
    note không bắt buộc, vì bản chất chưa có gì để "giải thích lý do sửa"."""
    model_config = ConfigDict(extra="forbid")
    
    contact_name: Optional[str] = None
    job_title: Optional[str] = None
    work_email: Optional[str] = None
    social_link: Optional[str] = None
    phone_number: Optional[str] = None
    # BUG FIX (08/2026): found_source CÓ trong CompanyContactCreate (tạo
    # mới) nhưng bị THIẾU hẳn ở đây từ đầu — nghĩa là "Nguồn tìm thấy"
    # chỉ nhập được lúc tạo, sau đó KHÔNG BAO GIỜ sửa được qua PATCH dù
    # UI (add_contact.html, dùng chung cho cả thêm/sửa) vẫn có ô nhập
    # này ở form sửa. extra="forbid" bên dưới khiến nếu FE có lỡ gửi
    # field này lên cũng bị 422 luôn, chứ không phải chỉ bị "bỏ qua êm".
    found_source: Optional[str] = None
    contact_status: Optional[str] = Field(
        None, description="UNCONTACTED | EMAIL_SENT | RESPONDED | IN_PARTNERSHIP"
    )
    last_contacted_date: Optional[date] = None
    note: Optional[str] = Field(
        default=None,
        description="BẮT BUỘC nếu có field nào ở trên thực sự thay đổi giá trị — "
                    "lý do sửa contact này, để các ss_team khác xem lại được.",
    )


class ContactAssignUpdate(BaseModel):
    """Route riêng PATCH /contacts/{contact_id}/assign (xem
    api/routers/contacts.py::assign_contact) — KHÔNG dùng chung
    CompanyContactUpdate ở trên vì pattern "field != None mới ghi đè"
    của route update thường không phân biệt được "không gửi field" với
    "cố ý set về NULL để bỏ gán". Ở đây assigned_ss_user LUÔN bắt buộc
    có mặt trong body (có thể là null để bỏ gán, hoặc 1 UUID để gán/đổi
    người phụ trách) — không optional/thiếu field như CompanyContactUpdate.

    note: BẮT BUỘC nếu assigned_ss_user thực sự đổi giá trị so với hiện
    tại (gán mới/đổi người/bỏ gán) — cùng nhóm CHẶN CỨNG với sửa contact."""
    model_config = ConfigDict(extra="forbid")
    
    assigned_ss_user: Optional[str] = Field(
        None, description="ss_user_id của thành viên ss_team/admin phụ trách contact này — null để bỏ gán (chưa ai phụ trách)."
    )
    note: Optional[str] = Field(
        default=None,
        description="BẮT BUỘC nếu việc gán này thực sự đổi người phụ trách — lý do "
                    "gán/đổi/bỏ gán, để các ss_team khác xem lại được.",
    )


class ContactDeleteRequest(BaseModel):
    """Body cho DELETE /companies/{company_id}/contacts/{contact_id} (xoá
    MỀM) — note BẮT BUỘC, cùng nhóm CHẶN CỨNG với sửa/gán contact và xoá
    company (xem ACTION_LOG_RULES trong db.py)."""
    note: str = Field(
        ..., min_length=1,
        description="BẮT BUỘC — lý do xoá contact này, để các ss_team khác biết "
                    "vì sao (vd: nghỉ việc, sai thông tin, trùng lặp...).",
    )

    @field_validator("note")
    @classmethod
    def _note_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("note không được để trống hoặc chỉ chứa khoảng trắng")
        return v


