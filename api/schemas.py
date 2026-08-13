"""
Pydantic schemas — contract cho request/response của API layer.

CỐ Ý tách riêng khỏi models.RawJobRecord: RawJobRecord là contract nội bộ
giữa adapter <-> pipeline (dữ liệu thô, optional nhiều field). Schema ở
đây là contract API <-> frontend (dữ liệu đã có trong DB, đầy đủ hơn, có
join thêm company_name/level_code/province_name cho tiện hiển thị) — 2
mục đích khác nhau, gộp chung sẽ rối khi 1 bên cần đổi mà bên kia không.
"""

from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel, Field


# ------------------------------------------------------------------
# Jobs
# ------------------------------------------------------------------

class JobOut(BaseModel):
    job_id: str
    job_title: str
    matching_industry: Optional[str] = None
    work_type: Optional[str] = None
    currency: Optional[str] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    salary_type: Optional[str] = None
    deadline: Optional[date] = None
    job_status: Optional[str] = None
    source_url: Optional[str] = None
    company_id: str
    company_name: str
    level_code: Optional[str] = None
    province_name: Optional[str] = None
    created_at: datetime
    updated_at: datetime
    created_by: Optional[str] = Field(
        default=None,
        description="ss_user_id người tạo job này qua POST /jobs (JWT bắt buộc từ 08/2026). "
                    "null = job crawl tự động, không phải người nhập tay.",
    )
    updated_by: Optional[str] = Field(
        default=None,
        description="ss_user_id người sửa job này GẦN NHẤT qua PATCH /jobs/{id}. "
                    "null = chưa từng bị sửa qua route có JWT.",
    )

    class Config:
        from_attributes = True


class JobDetailOut(JobOut):
    parsed_content: Optional[dict] = None
    ss_team_notes: Optional[str] = None


class PaginatedJobs(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[JobOut]


# ------------------------------------------------------------------
# Ghi từ frontend (thêm 08/2026) — team tự thêm/sửa/đóng job qua web,
# KHÔNG qua crawl. Company chọn từ danh sách ĐÃ CÓ trong DB (người dùng
# tự tra cứu tax_id/thông tin công ty rồi tạo company trước qua
# POST /companies nếu công ty chưa tồn tại — xem CompanyCreate bên dưới)
# — route không tự tạo company mới kèm theo job, tránh nhập nhằng giữa
# "công ty đã chọn đúng" và "công ty gõ nhầm tên tạo trùng".
# ------------------------------------------------------------------

class JobCreate(BaseModel):
    job_title: str = Field(..., min_length=1)
    company_id: str = Field(..., description="company_id đã có trong DB — dùng GET /companies?keyword= để tìm, hoặc POST /companies để tạo mới nếu công ty chưa tồn tại")
    matching_industry: Optional[str] = Field(default=None, examples=["Data Analysis"])
    level_code: Optional[str] = Field(default=None, description="Intern | Fresher | Junior | Middle | Senior | Lead | Manager")
    province_name: Optional[str] = Field(default=None, examples=["Hà Nội"])
    work_type: Optional[str] = Field(default=None, description="FULL_TIME | PART_TIME | INTERNSHIP | OTHER")
    currency: str = Field(default="VNĐ", description="VNĐ | USD")
    salary_min: Optional[int] = Field(default=None, ge=0)
    salary_max: Optional[int] = Field(default=None, ge=0)
    salary_type: str = Field(default="NEGOTIABLE", description="RANGE | EXACT | UPTO | STARTING_FROM | NEGOTIABLE | UNPAID")
    deadline: Optional[date] = None


class JobUpdate(BaseModel):
    """Mọi field optional — chỉ gửi field muốn sửa, field không gửi giữ
    nguyên giá trị cũ. Dùng field job_status='CLOSED' để "xoá mềm" 1 job
    (xem chi tiết trong docstring db.update_job())."""
    job_title: Optional[str] = Field(default=None, min_length=1)
    matching_industry: Optional[str] = None
    level_code: Optional[str] = Field(default=None, description="Intern | Fresher | Junior | Middle | Senior | Lead | Manager")
    province_name: Optional[str] = None
    work_type: Optional[str] = Field(default=None, description="FULL_TIME | PART_TIME | INTERNSHIP | OTHER")
    currency: Optional[str] = None
    salary_min: Optional[int] = Field(default=None, ge=0)
    salary_max: Optional[int] = Field(default=None, ge=0)
    salary_type: Optional[str] = Field(default=None, description="RANGE | EXACT | UPTO | STARTING_FROM | NEGOTIABLE | UNPAID")
    deadline: Optional[date] = None
    job_status: Optional[str] = Field(default=None, description="OPEN | EXPIRED | CLOSED — dùng CLOSED để 'xoá mềm'")
    ss_team_notes: Optional[str] = None


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

    class Config:
        from_attributes = True


class CompanyDetailOut(CompanyOut):
    products_services: Optional[str] = None
    jobs: list[JobOut] = Field(default_factory=list)


class PaginatedCompanies(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[CompanyOut]


class CompanyCreate(BaseModel):
    """Tạo công ty mới THỦ CÔNG từ frontend — dùng khi công ty chưa có
    trong DB (GET /companies?keyword= tìm không ra) để lấy company_id
    trước khi tạo job qua POST /jobs.

    Nếu tax_id điền vào TRÙNG với công ty đã có sẵn (vd công ty này đã
    được crawl từ TopCV/VietnamWorks trước đó) — route tự động dùng
    LẠI company đã có đó, KHÔNG tạo bản ghi trùng (tái dùng đúng
    get_or_create_company_by_profile() đã dùng cho pipeline crawl)."""
    company_name: str = Field(..., min_length=1)
    tax_id: Optional[str] = Field(default=None, description="Mã số thuế — nếu điền đúng, tự match với công ty đã crawl trước đó (nếu có), tránh tạo trùng")
    website: Optional[str] = None
    industry: Optional[str] = None
    company_size: Optional[str] = None
    address: Optional[str] = None
    province_name: Optional[str] = None
    fanpage_url: Optional[str] = None
    linkedin_url: Optional[str] = None


# ------------------------------------------------------------------
# Stats
# ------------------------------------------------------------------

class IndustryCount(BaseModel):
    matching_industry: str
    n: int


class SourceCount(BaseModel):
    source_name: str
    n: int


class StatsOut(BaseModel):
    total_jobs: int
    total_companies: int
    companies_with_social: int
    by_industry: list[IndustryCount]
    by_source: list[SourceCount]


# ------------------------------------------------------------------
# Crawl trigger
# ------------------------------------------------------------------

class CrawlRequest(BaseModel):
    source: str = Field(..., examples=["topcv", "vietnamworks"])
    category: str = Field(..., examples=["data-analyst", "data-engineer", "software-engineering"])
    # Optional (khác bản cũ default=3) — để phân biệt được "không truyền
    # pages" với "truyền đúng giá trị mặc định", giống cách main.py CLI
    # xử lý --pages/--max-jobs (xem crawl_runner.resolve_effective_pages()).
    # Nếu bỏ trống CẢ pages lẫn max_jobs, route sẽ tự áp DEFAULT_MAX_PAGES.
    pages: Optional[int] = Field(default=None, ge=1, le=20)
    # Giới hạn TỔNG SỐ JD sẽ crawl, dừng ngay khi đủ (không cần đợi hết
    # pages) — khớp 1-1 với --max-jobs đã có ở CLI (main.py). Có thể
    # dùng CÙNG pages (dừng ở điều kiện nào tới trước); nếu chỉ truyền
    # max_jobs mà không truyền pages, tự động nới pages đủ lớn để
    # max_jobs là giới hạn thực sự (xem crawl_runner.py).
    max_jobs: Optional[int] = Field(default=None, ge=1, le=1000)


class CrawlAccepted(BaseModel):
    run_id: str
    status: str  # "queued" | "running" | "done" | "error"


class CrawlStatusOut(BaseModel):
    run_id: str
    status: str
    source: str
    category: str
    pages: int
    max_jobs: Optional[int] = None
    started_at: datetime
    finished_at: Optional[datetime] = None
    stats: Optional[dict] = None
    error: Optional[str] = None


# ------------------------------------------------------------------
# Auth — đăng nhập TỪNG NGƯỜI qua JWT (thêm 08/2026)
#
# KHÁC API_KEY tĩnh (không có schema riêng, chỉ 1 header cố định) — nhóm
# schema dưới đây phục vụ luồng login/refresh/đổi mật khẩu/admin tạo
# user cho frontend, xem api/routers/auth.py.
# ------------------------------------------------------------------

class LoginRequest(BaseModel):
    email: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class TokenPairOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    must_change_password: bool = False


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1)


class AccessTokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class ChangePasswordRequest(BaseModel):
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

    class Config:
        from_attributes = True


class UserCreateByAdmin(BaseModel):
    """Admin tạo tài khoản MỚI — KHÔNG có luồng tự đăng ký công khai
    (xem README.md mục Auth). Mật khẩu TẠM được server tự sinh
    (security.generate_temp_password()), trả về ĐÚNG 1 LẦN trong response
    (xem UserCreatedOut) — admin tự đưa cho người dùng qua kênh khác
    (Slack/nói miệng), KHÔNG có luồng gửi email."""
    full_name: str = Field(..., min_length=1)
    email: str = Field(..., min_length=1)
    role: str = Field(default="member", description="admin | member")


class UserCreatedOut(UserOut):
    """Như UserOut, thêm temp_password — CHỈ xuất hiện trong response
    NGAY LÚC TẠO, không có endpoint nào khác trả lại được mật khẩu tạm
    này sau đó (không lưu bản rõ, chỉ lưu hash)."""
    temp_password: str
