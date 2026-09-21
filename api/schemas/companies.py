"""
Companies — schema request/response cho POST/PATCH/DELETE/GET /companies.
Tách từ api/schemas.py (08/2026) — xem docstring api/schemas/__init__.py.
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator

from api.schemas.jobs import JobOut  # CompanyDetailOut.jobs
from api.schemas.validators import validate_note_not_blank


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


# Thêm khi migrate Next.js (Phần 5 mục 16 của plan): response riêng cho
# POST /companies — KHÔNG gộp field was_existing vào CompanyOut dùng
# chung (GET /companies list/detail cũng dùng CompanyOut/CompanyDetailOut,
# field này chỉ có ý nghĩa đúng 1 lần tại thời điểm tạo, đưa vào schema
# chung sẽ để lại 1 field vô nghĩa "was_existing: null" ở mọi response
# GET). create_company() (api/routers/companies.py) tự tính được biến
# was_existing ngay trong hàm (dùng để quyết định có ghi audit log
# CREATE_COMPANY hay không — công ty "vá thêm thông tin" do trùng
# tax_id/tên thì không log tạo mới) nhưng TRƯỚC ĐÂY biến này bị bỏ đi,
# không đưa vào response — client hoàn toàn không có cách nào biết 1
# lời gọi "tạo công ty" vừa rồi thật sự tạo bản ghi mới hay chỉ âm thầm
# vá vào công ty đã có sẵn (cùng dạng thiếu sót với `reactivated` ở
# luồng import, Phần 5 mục 11).
class CompanyCreateResult(CompanyOut):
    was_existing: bool = Field(
        description="true = company trả về đã tồn tại từ trước (trùng "
                    "tax_id hoặc tên), request này chỉ vá thêm thông tin, "
                    "KHÔNG tạo bản ghi mới. false = company vừa được tạo "
                    "mới thật sự.",
    )


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

    # Validator dùng CHUNG (api/schemas/validators.py) — xem docstring
    # ở đó để biết lý do tách ra thay vì định nghĩa lặp ở từng schema.
    _note_not_blank = field_validator("note")(validate_note_not_blank)


