"""
Companies — schema request/response cho POST/PATCH/DELETE/GET /companies.
Tách từ api/schemas.py (08/2026) — xem docstring api/schemas/__init__.py.
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.schemas.jobs import JobOut  # CompanyDetailOut.jobs


# ------------------------------------------------------------------
# Companies
# ------------------------------------------------------------------

class CompanyOut(BaseModel):
    company_id: str
    company_name: str
    tax_id: Optional[str] = None
    website: Optional[str] = None
    industry: Optional[str] = None
    company_size: Optional[str] = None
    address: Optional[str] = None
    fanpage_url: Optional[str] = None
    linkedin_url: Optional[str] = None
    partnership_potential: str = Field(
        default="UNVERIFIED",
        description="HIGH | MEDIUM | LOW | UNVERIFIED — staff tự chấm tay qua "
                    "PATCH /companies/{id}, không có rule tự động gán. "
                    "UNVERIFIED = mặc định, nghĩa là 'chưa đánh giá', KHÔNG "
                    "phải 'tiềm năng thấp'.",
    )
    province_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    created_by: Optional[str] = Field(
        default=None,
        description="ss_user_id người tạo company này qua POST /companies. "
                    "null = company crawl tự động.",
    )
    updated_by: Optional[str] = Field(
        default=None,
        description="ss_user_id người sửa company này GẦN NHẤT qua POST /companies "
                    "(trùng tax_id, chỉ vá thêm thông tin).",
    )
    is_active: bool = Field(
        default=True,
        description="false = công ty đã bị xoá mềm qua DELETE /companies/{id} "
                    "(xem sql/migration_add_company_soft_delete.sql) — GET "
                    "/companies mặc định không trả company này, xem lại qua "
                    "?include_inactive=true.",
    )

    class Config:
        from_attributes = True


class CompanyDetailOut(CompanyOut):
    jobs: list[JobOut] = Field(default_factory=list)


class PaginatedCompanies(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[CompanyOut]


class PartnershipSignals(BaseModel):
    """1 dòng kết quả GET /companies/partnership-signals — xem docstring
    db.get_partnership_signals() cho ý nghĩa từng field. KHÔNG gồm
    is_hn_hcm/has_company_size — 2 field đó đã có sẵn trên CompanyOut
    (province_name/company_size), không cần tính lại ở đây."""
    has_open_entry_job: bool
    matches_target_industry: bool
    has_responded: bool


class FieldHealthRow(BaseModel):
    """1 dòng thống kê "thiếu field" — dùng chung cho cả company và job
    (GET /companies/data-health, GET /jobs/data-health), khớp đúng dict
    trả về từ count_missing_fields() bên Flask trước đây."""
    field: str
    label: str
    missing: int
    total: int
    pct_missing: int


class CompanyDataHealth(BaseModel):
    """GET /companies/data-health — thay cho việc frontend tự đếm field
    rỗng bằng Python trên list_all_companies()/list_all_contacts() kéo
    về đầy đủ. Xem docstring db.get_company_data_health()."""
    company_health_rows: list[FieldHealthRow]
    company_health_total: int
    company_no_contact_missing: int
    company_no_contact_total: int


class CompanyCreate(BaseModel):
    """Tạo công ty mới THỦ CÔNG từ frontend — dùng khi công ty chưa có
    trong DB (GET /companies?keyword= tìm không ra) để lấy company_id
    trước khi tạo job qua POST /jobs.

    Nếu tax_id điền vào TRÙNG với công ty đã có sẵn (vd công ty này đã
    được crawl từ TopCV/VietnamWorks trước đó) — route tự động dùng
    LẠI company đã có đó, KHÔNG tạo bản ghi trùng (tái dùng đúng
    get_or_create_company_by_profile() đã dùng cho pipeline crawl)."""
    model_config = ConfigDict(extra="forbid")
    
    company_name: str = Field(..., min_length=1)
    tax_id: Optional[str] = Field(default=None, description="Mã số thuế — nếu điền đúng, tự match với công ty đã crawl trước đó (nếu có), tránh tạo trùng")
    website: Optional[str] = None
    industry: Optional[str] = None
    company_size: Optional[str] = None
    address: Optional[str] = None
    province_name: Optional[str] = None
    fanpage_url: Optional[str] = None
    linkedin_url: Optional[str] = None
    partnership_potential: Optional[str] = Field(
        default=None,
        description="HIGH | MEDIUM | LOW | UNVERIFIED — bỏ trống sẽ giữ mặc "
                    "định UNVERIFIED của DB (chưa đánh giá).",
    )


class CompanyUpdate(BaseModel):
    """Sửa TỰ DO mọi field của 1 company đã tồn tại — thêm 08/2026 (xem
    lịch sử trao đổi: trước đây company chỉ tạo được, không sửa lại
    được nếu gõ sai/thông tin đổi). Mọi field optional, giống JobUpdate
    — CHỈ field có mặt trong body mới bị ghi đè, field không gửi giữ
    nguyên giá trị cũ.

    KHÁC CompanyCreate/POST /companies (vốn dùng
    db.update_company_profile(), pattern "vá thêm" — chỉ field có giá
    trị TRUTHY mới ghi đè, gửi "" bị bỏ qua): route PATCH dùng hàm
    riêng db.patch_company_profile() phân biệt None (không gửi, giữ
    nguyên) với "" (gửi rỗng có chủ đích, XOÁ giá trị cũ) — đúng ngữ
    nghĩa PATCH thật sự, tương tự salary_min/salary_max ở JobUpdate.

    KHÔNG có field để xoá công ty (chưa có is_active/soft-delete —
    xem lịch sử trao đổi, việc này để sau)."""
    model_config = ConfigDict(extra="forbid")
    
    company_name: Optional[str] = Field(default=None, min_length=1)
    tax_id: Optional[str] = None
    website: Optional[str] = None
    industry: Optional[str] = None
    company_size: Optional[str] = None
    address: Optional[str] = None
    province_name: Optional[str] = None
    fanpage_url: Optional[str] = None
    linkedin_url: Optional[str] = None
    partnership_potential: Optional[str] = Field(
        default=None,
        description="HIGH | MEDIUM | LOW | UNVERIFIED — gửi field này để "
                    "staff cập nhật lại đánh giá tiềm năng hợp tác.",
    )
    note: Optional[str] = Field(
        default=None,
        description="Ghi chú cho log thủ công — TUỲ CHỌN, giải thích lý do sửa "
                    "company này để các ss_team khác xem lại được.",
    )


class CompanyDeleteRequest(BaseModel):
    """Body cho DELETE /companies/{company_id} (thêm 08/2026, xem
    sql/migration_add_company_soft_delete.sql). note BẮT BUỘC — khác mọi
    field 'note' optional khác trong file này — vì xoá company là 1 trong
    4 action bị CHẶN CỨNG nếu thiếu note (xem ACTION_LOG_RULES trong
    db.py): thiếu note -> 422, KHÔNG xoá công ty, KHÔNG ghi log."""
    note: str = Field(
        ..., min_length=1,
        description="BẮT BUỘC — lý do xoá công ty này, để các ss_team khác "
                    "biết vì sao (vd: trùng lặp, công ty đã đóng cửa, sai "
                    "thông tin nhập nhầm...).",
    )

    @field_validator("note")
    @classmethod
    def _note_not_blank(cls, v: str) -> str:
        # min_length=1 chỉ đếm SỐ KÝ TỰ, không chặn chuỗi toàn khoảng
        # trắng (vd "   " vẫn qua được min_length=1) — validator này
        # chặn nốt trường hợp đó, vì note toàn khoảng trắng thực chất
        # tương đương "không có note".
        v = v.strip()
        if not v:
            raise ValueError("note không được để trống hoặc chỉ chứa khoảng trắng")
        return v


