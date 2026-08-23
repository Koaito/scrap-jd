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
import re
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
    salary_period: Optional[str] = Field(
        default=None,
        description="MONTH | YEAR — chu kỳ trả lương của salary_min/salary_max. "
                    "Job cũ crawl trước 08/2026 (trước khi có cột này) mặc định "
                    "'MONTH' ở tầng DB, không phải giá trị đã xác nhận thật.",
    )
    deadline: Optional[date] = None
    job_status: Optional[str] = None
    source_url: Optional[str] = None
    source_name: Optional[str] = Field(
        default=None,
        description="TopCV | VietnamWorks | CareerViet | MANUAL — tên nguồn "
                    "crawl gần nhất (từ job_sources_log). Trước 08/2026 field "
                    "này chỉ xuất hiện ở /stats (SourceCount), chưa join vào "
                    "job list/detail. null = job crawl trước khi có cột này, "
                    "hoặc chưa từng ghi log nguồn.",
    )
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

class ParsedContent(BaseModel):
    """Mô tả JD chi tiết — cùng cấu trúc parsed_content mà pipeline crawl
    build (xem pipeline._build_parsed_content_and_raw()), giờ mở thêm
    cho job NHẬP TAY qua JobCreate/JobUpdate (trước 08/2026 chỉ job
    crawl mới có, job nhập tay để trống — thiếu hẳn mô tả/yêu cầu/quyền
    lợi/kỹ năng, xem lịch sử trao đổi). Lưu nguyên vào
    job_postings.parsed_content (JSONB), KHÔNG có cột riêng nào cho
    từng field — đây là field tự do, không filter/index theo được."""
    model_config = ConfigDict(extra="forbid")
    
    job_description: Optional[str] = None
    requirements: Optional[str] = None
    perks: Optional[str] = None
    required_skills: Optional[list[str]] = None


class JobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    
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
    salary_period: str = Field(
        default="MONTH",
        description="MONTH | YEAR — chu kỳ trả lương của salary_min/salary_max. "
                    "Job nhập tay KHÔNG qua normalize_salary() nên KHÔNG tự suy "
                    "luận được từ text — nếu nhập lương NĂM, phải tự truyền "
                    "'YEAR', không thì mặc định hiểu nhầm là lương/tháng.",
    )
    deadline: Optional[date] = None
    parsed_content: Optional[ParsedContent] = Field(
        default=None,
        description="Mô tả JD chi tiết (job_description/requirements/perks/required_skills) cho job nhập tay",
    )


class JobUpdate(BaseModel):
    """Mọi field optional — chỉ gửi field muốn sửa, field không gửi giữ
    nguyên giá trị cũ. Dùng field job_status='CLOSED' để "xoá mềm" 1 job
    (xem chi tiết trong docstring db.update_job())."""
    model_config = ConfigDict(extra="forbid")
    
    job_title: Optional[str] = Field(default=None, min_length=1)
    matching_industry: Optional[str] = None
    level_code: Optional[str] = Field(default=None, description="Intern | Fresher | Junior | Middle | Senior | Lead | Manager")
    province_name: Optional[str] = None
    work_type: Optional[str] = Field(default=None, description="FULL_TIME | PART_TIME | INTERNSHIP | OTHER")
    currency: Optional[str] = None
    salary_min: Optional[int] = Field(default=None, ge=0)
    salary_max: Optional[int] = Field(default=None, ge=0)
    salary_type: Optional[str] = Field(default=None, description="RANGE | EXACT | UPTO | STARTING_FROM | NEGOTIABLE | UNPAID")
    salary_period: Optional[str] = Field(default=None, description="MONTH | YEAR — không gửi thì giữ nguyên giá trị cũ")
    deadline: Optional[date] = None
    job_status: Optional[str] = Field(default=None, description="OPEN | EXPIRED | CLOSED — dùng CLOSED để 'xoá mềm'")
    ss_team_notes: Optional[str] = None
    parsed_content: Optional[ParsedContent] = Field(
        default=None,
        description="Mô tả JD chi tiết — gửi field này sẽ GHI ĐÈ TOÀN BỘ parsed_content cũ (không merge từng key con)",
    )
    note: Optional[str] = Field(
        default=None,
        description="Ghi chú cho log thủ công — TUỲ CHỌN, giải thích lý do sửa/xoá "
                    "(job_status='CLOSED') JD này để các ss_team khác xem lại được. "
                    "Không liên quan ss_team_notes (note nội bộ hiển thị trên JD, "
                    "field này chỉ dùng cho audit_logs.note).",
    )


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
    # Thêm 08/2026 — tổng số job_applications toàn hệ thống (không phân
    # biệt job/user), dùng cho dashboard frontend. Xem db.get_stats_summary().
    total_applications: int
    # Thêm 08/2026 cùng lúc với việc cho staff xem saved_jobs (xem
    # JobSaverOut bên dưới) — tổng số saved_jobs toàn hệ thống, cân xứng
    # với total_applications ở trên.
    total_saved_jobs: int


class JobEngagementOut(BaseModel):
    """1 dòng trong GET /stats/engagement — job kèm số lượt lưu/ứng
    tuyển gộp sẵn, để frontend tự lọc "JD ế" (đăng lâu, 0 lượt quan
    tâm) mà không cần gọi N+1 request cho từng job."""
    job_id: str
    job_title: str
    deadline: Optional[date] = None
    created_at: Optional[datetime] = None
    application_count: int
    saved_count: int


class MonthlyCountOut(BaseModel):
    this_month: int
    last_month: int


class MonthlyEngagementOut(BaseModel):
    applications: MonthlyCountOut
    saved_jobs: MonthlyCountOut


class EngagementStatsOut(BaseModel):
    """GET /stats/engagement — thêm 08/2026 riêng cho dashboard tab
    'Gợi ý học viên'/'Báo cáo tháng' (xem trao đổi thiết kế), tách
    khỏi GET /stats hiện có (StatsOut) vì 2 query bên dưới tốn hơn
    (JOIN + GROUP BY theo từng job, FILTER theo tháng) — không muốn
    dashboard tổng quan hiện tại (gọi /stats liên tục) chậm đi vì
    thêm việc không phải lúc nào cũng cần."""
    jobs: list[JobEngagementOut]
    monthly: MonthlyEngagementOut


# ------------------------------------------------------------------
# Crawl trigger
# ------------------------------------------------------------------

class CrawlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    
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


# ------------------------------------------------------------------
# Job applications + saved jobs — thêm 08/2026, xem db.py mục cùng tên
# ------------------------------------------------------------------

class JobApplicationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    
    job_id: str
    note: Optional[str] = None


class JobApplicationOut(BaseModel):
    application_id: str
    ss_user_id: str
    job_id: str
    note: Optional[str] = None
    applied_at: datetime
    job_title: str
    job_status: Optional[str] = None
    company_name: str
    cv_url: Optional[str] = None

    class Config:
        from_attributes = True


class JobApplicantOut(BaseModel):
    """Dùng cho GET /jobs/{job_id}/applications (staff xem ai đã ứng
    tuyển) — khác JobApplicationOut (dùng cho GET /me/applications,
    học viên xem đơn của chính mình): ở đây cần full_name/email/phone
    người ứng tuyển thay vì thông tin job (staff đã biết job nào rồi).
    phone thêm 08/2026 (xem sql/migration_add_phone_track.sql) — đúng
    mục đích ban đầu của cột này: để staff liên hệ trực tiếp, không chỉ
    qua email."""
    application_id: str
    ss_user_id: str
    job_id: str
    note: Optional[str] = None
    applied_at: datetime
    full_name: str
    email: str
    phone: Optional[str] = None
    cv_url: Optional[str] = None

    class Config:
        from_attributes = True


class JobSaverOut(BaseModel):
    """Thêm 08/2026 — dùng cho GET /jobs/{job_id}/saved-jobs (staff xem
    ai đã LƯU job này, khác ứng tuyển). Mirror ĐÚNG JobApplicantOut ở
    trên, chỉ khác không có 'note' (saved_jobs không có cột note — chỉ
    là bookmark, không có ghi chú như application). Trước đây saved_jobs
    cố ý không có route nào cho staff xem (xem comment ở
    db.list_saved_jobs_for_job()) — đổi quyết định vì SS team/admin cần
    theo dõi học viên đang quan tâm JD nào để chủ động hỗ trợ."""
    saved_job_id: str
    ss_user_id: str
    job_id: str
    created_at: datetime
    full_name: str
    email: str
    phone: Optional[str] = None

    class Config:
        from_attributes = True


class SavedJobCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    
    job_id: str


class SavedJobOut(BaseModel):
    saved_job_id: str
    ss_user_id: str
    job_id: str
    created_at: datetime
    job_title: str
    job_status: Optional[str] = None
    company_name: str

    class Config:
        from_attributes = True


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


# ------------------------------------------------------------------
# Import/Export — Company resolution schemas
# ------------------------------------------------------------------

class CompanySuggestionOut(BaseModel):
    """Gợi ý công ty tương tự cho import resolution — dùng trong
    GET /import/{entity_type}/preview/{preview_id}/rows/{row_index}/suggest-companies"""
    company_id: str
    company_name: str
    tax_id: Optional[str] = None
    is_active: bool
    similarity: float = Field(
        ..., 
        description="Độ tương đồng tên công ty (pg_trgm similarity, 0-1)"
    )


class CompanySuggestionsResponse(BaseModel):
    """Response wrapper cho danh sách gợi ý công ty"""
    suggestions: list[CompanySuggestionOut] = Field(default_factory=list)


class ImportUploadResponse(BaseModel):
    """Response cho POST /import/{entity_type}/preview và
    GET /import/{entity_type}/preview/{preview_id}
    
    Chứa preview_id, entity_type, summary (tổng hợp số dòng), 
    và rows (chi tiết từng dòng với trạng thái ready/needs_resolution)
    """
    preview_id: str
    entity_type: str
    summary: dict  # {"total_rows", "new_records", "conflicts", "conflicts_inactive",
                    #  "pending_company_resolution", "pending_level_resolution",
                    #  "id_field"} — xem
                    # api/services/preview_manager.py::build_preview() cho cấu
                    # trúc đầy đủ + comment đầu file (nguồn sự thật thật sự,
                    # dict comment ở đây chỉ để đọc lướt nhanh).
    rows: list[dict]  # Chi tiết từng dòng import


class RowResolution(BaseModel):
    """Resolution cho 1 dòng cần xử lý thủ công - dùng trong ImportConfirmRequest.

    Sửa 08/2026 (fix bug reactivate không hoạt động): action enum ở đây
    TRƯỚC ĐÂY là "create_new | use_existing | skip" và field
    "confirm_reactivate" hoàn toàn không tồn tại — trong khi
    api/services/import_executor.py (nơi thực thi thật) lại đọc
    action là "skip"|"create"|"update" và đọc resolution.get(
    "confirm_reactivate") cho dòng conflict_inactive (xem
    RowResolutionError docstring + dòng 121-124 file đó). Vì router
    convert RowResolution -> dict bằng .model_dump() (xem
    api/routers/import_export.py), field lạ "confirm_reactivate" gửi
    từ frontend bị Pydantic ÂM THẦM loại bỏ — nghĩa là flow "ghi đè +
    kích hoạt lại record inactive" không bao giờ chạy được, dù cả
    frontend lẫn import_executor.py đều đã code đúng phần của mình.
    Sửa lại enum + bổ sung field cho khớp đúng những gì
    import_executor.py thực sự đọc.

    Thêm 08/2026 (action lan truyền cho conflict_in_batch): dòng
    conflict_status="conflict_in_batch" (trùng với 1 dòng KHÁC trong
    CHÍNH file import, không phải DB — xem BatchDuplicateMatchOut) giờ
    nhận thêm 3 giá trị action, mỗi giá trị áp dụng cho CẢ CẶP 2 dòng
    trùng nhau CHỈ TỪ 1 resolution duy nhất (backend tự điền resolution
    cho dòng kia qua duplicate_in_batch.other_row_index — xem
    import_executor.BATCH_PROPAGATING_ACTIONS +
    _expand_conflict_in_batch_resolutions()):
      - "keep_this"   : giữ dòng đang gửi resolution này, bỏ dòng kia
                        (2 dòng là CÙNG 1 người, dòng này đúng).
      - "keep_other"  : ngược lại — bỏ dòng đang gửi, giữ dòng kia.
      - "import_both" : xác nhận 2 dòng là 2 người KHÁC NHAU, giữ cả 2.
    Vẫn có thể tiếp tục gửi resolution RIÊNG cho từng dòng bằng
    skip/create như trước (không bắt buộc dùng action lan truyền) — nếu
    gửi cả 2 kiểu cho 1 cặp mà mâu thuẫn nhau, backend raise 422 rõ
    nguyên nhân thay vì tự đoán."""
    model_config = ConfigDict(extra="forbid")
    
    action: str = Field(
        ..., 
        description="skip | create | update. Riêng dòng conflict_status="
                     "'conflict_in_batch' (thêm 08/2026, trùng với 1 dòng KHÁC "
                     "trong CHÍNH file import, không phải DB — xem "
                     "BatchDuplicateMatchOut): nhận skip/create như trên "
                     "(resolve RIÊNG từng dòng trong cặp) HOẶC 1 trong 3 action "
                     "LAN TRUYỀN 'keep_this'/'keep_other'/'import_both' (áp dụng "
                     "1 lần cho CẢ CẶP, chỉ cần gửi cho 1 trong 2 dòng — backend "
                     "tự điền resolution cho dòng kia, xem import_executor."
                     "BATCH_PROPAGATING_ACTIONS); 'update' không hợp lệ (không có "
                     "existing_record để update) và resolution cho dòng này là "
                     "BẮT BUỘC tường minh (trực tiếp hoặc do lan truyền từ dòng "
                     "kia), KHÔNG được để mặc định (xem "
                     "import_executor.execute_import)."
    )
    company_id: Optional[str] = Field(
        None, 
        description="Bắt buộc nếu dòng needs_company_resolve và action='create'/'update'"
    )
    confirm_reactivate: bool = Field(
        False,
        description="Bắt buộc =true nếu action='update' cho dòng conflict_status="
                     "'conflict_inactive' (ghi đè + kích hoạt lại record đã ngừng "
                     "hoạt động) — xem import_executor.RowResolutionError.",
    )
    level_code: Optional[str] = Field(
        None,
        description="Bắt buộc (1 trong LEVEL_CODE_VALUES — xem constants.py) nếu "
                     "dòng needs_level_resolve=true (Job, level_code trong file "
                     "không khớp danh sách hợp lệ dù đã chuẩn hoá hoa/thường) — "
                     "staff chọn lại qua dropdown tĩnh ở FE, xem "
                     "import_executor.RowResolutionError.",
    )
    field_fixes: Optional[dict[str, str]] = Field(
        None,
        description="Thêm 08/2026: map field_name -> giá trị staff đã sửa trực "
                     "tiếp trên bảng preview, BẮT BUỘC chứa đủ mọi field còn "
                     "trong needs_field_fix/field_errors của dòng này nếu "
                     "action != 'skip' (xem preview_manager.build_preview -> "
                     "entry['field_errors']). Giá trị LUÔN là string thô "
                     "(giống format trong file gốc, vd ngày 'YYYY-MM-DD') — "
                     "import_executor.py::_apply_field_fixes() re-validate lại "
                     "bằng đúng validate_single_field() dùng lúc build preview, "
                     "không tin ngầm dữ liệu FE gửi lên.",
    )


class FieldVerifyRequest(BaseModel):
    """Body cho POST /import/{entity_type}/preview/{preview_id}/rows/
    {row_index}/verify-field — staff sửa 1 ô trên bảng preview rồi bấm
    nút "Xác nhận" cạnh ô đó (thêm 08/2026, xem trao đổi thiết kế
    "cảnh báo trùng contact sau khi sửa field lỗi").

    Re-validate format field_name NGAY (dùng đúng validate_single_field()
    dùng lúc build preview) + với contact, re-check trùng mờ theo
    company_id + tối thiểu 1/3 trong (work_email, social_link,
    phone_number) — xem preview_manager.apply_field_fix()."""
    model_config = ConfigDict(extra="forbid")

    field_name: str = Field(..., description="Tên field vừa sửa, vd 'work_email'")
    value: str = Field(..., description="Giá trị mới (string thô, giống format trong file gốc)")


class DuplicateMatchOut(BaseModel):
    """Kết quả match mờ khi phát hiện dòng vừa sửa trùng với 1 contact đã
    có trong DB — chỉ có giá trị khi conflict_status chuyển sang
    "conflict" NGAY TẠI apply_field_fix() (khác conflict phát hiện lúc
    build preview ban đầu, vốn không có field này)."""
    match_score: float = Field(
        ..., description="Số cột khớp / 3 (0.33 / 0.67 / 1.0) — càng cao càng chắc trùng."
    )
    matched_fields: list[str] = Field(
        ..., description="Các cột khớp, trong (work_email, social_link, phone_number)."
    )


class BatchDuplicateMatchOut(BaseModel):
    """Kết quả match mờ khi phát hiện dòng vừa sửa trùng với 1 dòng KHÁC
    TRONG CHÍNH file đang import (thêm 08/2026) — khác DuplicateMatchOut
    ở trên vốn so với record ĐÃ CÓ trong DB. Xem conflict_detector.
    find_duplicate_rows_in_batch() + preview_manager.apply_field_fix().

    Chỉ có giá trị khi conflict_status của dòng chuyển "conflict_in_batch"
    — dòng này KHÔNG có existing_record thật (existing_record vẫn null),
    other_row_index mới là thứ FE cần để biết đang trùng với dòng nào
    trong bảng preview (không phải record DB). FE dùng other_row_index
    này để tự gửi resolution cho dòng kia theo cách cũ (skip/create
    riêng từng dòng), HOẶC (thêm 08/2026) chỉ cần gửi 1 resolution với
    action lan truyền (keep_this/keep_other/import_both — xem
    RowResolution.action) cho MỘT trong 2 dòng, backend tự áp dụng cho
    dòng kia."""
    match_score: float = Field(
        ..., description="Số cột khớp / 3 (0.33 / 0.67 / 1.0) — càng cao càng chắc trùng."
    )
    matched_fields: list[str] = Field(
        ..., description="Các cột khớp, trong (work_email, social_link, phone_number)."
    )
    other_row_index: int = Field(
        ...,
        description="row_index của dòng KIA trong CÙNG file bị phát hiện trùng "
                     "(khớp key trong preview_data['rows']) — FE dùng để "
                     "highlight/liên kết sang dòng đó trên bảng preview. Khi 1 "
                     "dòng nhận duplicate_in_batch mới, dòng có row_index này "
                     "CŨNG bị cập nhật duplicate_in_batch/conflict_status trong "
                     "preview đã lưu DB (xử lý 2 chiều), dù response của "
                     "verify-field chỉ trả về đúng dòng vừa sửa — FE cần tự "
                     "biết dòng kia cũng vừa đổi (vd tải lại preview nếu cần "
                     "hiển thị chính xác ngay lập tức)."
    )


class FieldVerifyResponse(BaseModel):
    """Response cho POST .../verify-field.

    field_error != None -> field vẫn KHÔNG hợp lệ sau khi sửa (giữ
    nguyên field_errors/needs_field_fix cũ trong preview, KHÔNG lưu gì
    mới) — FE hiện lỗi ngay tại ô, không cho staff tưởng đã xác nhận
    thành công.

    field_error == None -> đã lưu field mới vào preview. row trả về là
    TOÀN BỘ entry của dòng đó sau khi cập nhật (đúng cấu trúc 1 phần tử
    trong ImportUploadResponse.rows) — FE ghi đè PREVIEW_DATA[row_index]
    bằng row này, tự cập nhật lại UI (field_errors còn lại, conflict_status
    mới nếu có, duplicate_match nếu phát hiện trùng DB, duplicate_in_batch
    (xem BatchDuplicateMatchOut) nếu phát hiện trùng với 1 dòng KHÁC
    trong CHÍNH file — LƯU Ý: case duplicate_in_batch ảnh hưởng 2 CHIỀU,
    dòng other_row_index cũng vừa đổi trong preview đã lưu DB dù KHÔNG
    nằm trong response này)."""
    row: dict
    field_error: Optional[dict] = Field(
        None, description="{'rule','message'} nếu vẫn lỗi, None nếu đã hợp lệ và đã lưu."
    )


class ResolveCompanyRequest(BaseModel):
    """Body cho POST /import/{entity_type}/preview/{preview_id}/rows/
    {row_index}/resolve-company — staff chọn 1 công ty (hoặc "Tạo công ty
    mới") trong modal chọn công ty ở bước preview, cho dòng
    conflict_status="pending_company_resolution" (chỉ job/contact — xem
    api/services/preview_manager.py::resolve_company_selection()).

    Re-check conflict NGAY với company_id thật vừa chọn, thay vì để treo
    tới lúc confirm (xem trao đổi thiết kế "vấn đề 2 & 3", 08/2026)."""
    model_config = ConfigDict(extra="forbid")

    company_id: Optional[str] = Field(
        None,
        description="UUID công ty staff chọn trong danh sách gợi ý. "
        "None (hoặc '__new__') = staff xác nhận không công ty nào đúng, "
        "sẽ tạo công ty mới theo company_name trong file.",
    )


class ResolveCompanyResponse(BaseModel):
    """Response cho POST .../resolve-company — row trả về là TOÀN BỘ entry
    của dòng đó sau khi cập nhật (đúng cấu trúc 1 phần tử trong
    ImportUploadResponse.rows, cùng shape FieldVerifyResponse.row) — FE ghi
    đè PREVIEW_DATA[row_index] bằng row này rồi renderPage() lại."""
    row: dict


class ImportConfirmRequest(BaseModel):
    """Body cho POST /import/{entity_type}/confirm
    
    Staff xác nhận import sau khi đã xem preview và resolve các dòng cần xử lý
    """
    model_config = ConfigDict(extra="forbid")
    
    preview_id: str
    note: str = Field(
        ..., 
        min_length=1,
        description="Ghi chú về lần import này (bắt buộc cho audit log)"
    )
    resolutions: dict[str, RowResolution] = Field(
        default_factory=dict,
        description="Map row_index -> resolution cho các dòng needs_resolution"
    )


class ImportConfirmResult(BaseModel):
    """Response cho POST /import/{entity_type}/confirm
    
    Tổng kết số bản ghi đã tạo mới, cập nhật, và bỏ qua
    """
    created: int
    updated: int
    skipped: int
