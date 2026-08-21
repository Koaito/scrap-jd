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
from pydantic import BaseModel, Field, field_validator, model_validator


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
    job_description: Optional[str] = None
    requirements: Optional[str] = None
    perks: Optional[str] = None
    required_skills: Optional[list[str]] = None


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
    role: str = Field(..., description="user | ss_team | admin")


class UserActiveStatusUpdate(BaseModel):
    """Body cho PATCH /auth/users/{id}/active-status (admin-only).
    Khoá VĨNH VIỄN 1 tài khoản (is_active=false) — KHÁC locked_until (khoá
    TẠM THỜI tự hết hạn do sai mật khẩu nhiều lần, xem
    sql/migration_add_auth.sql). Dùng khi 1 người rời nhóm/vi phạm và cần
    chặn đăng nhập ngay lập tức, không chờ tự hết hạn."""
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
    full_name: str = Field(..., min_length=1)
    email: str = Field(..., min_length=1)
    password: str = Field(..., min_length=8)
    # Thêm 08/2026 (xem sql/migration_add_phone_track.sql) — trước đó
    # frontend đã gửi 2 field này lên nhưng bị Pydantic âm thầm bỏ qua
    # vì không khai báo ở đây, không phải vì cố tình optional-và-bỏ-qua.
    phone: Optional[str] = Field(default=None, max_length=30)
    track: Optional[str] = Field(default=None, max_length=100)

    class Config:
        str_strip_whitespace = True  # tự trim khoảng trắng thừa TRƯỚC khi validate email/full_name

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
    email: str = Field(..., min_length=1)


class ForgotPasswordRequest(BaseModel):
    email: str = Field(..., min_length=1)


class ResetPasswordRequest(BaseModel):
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
    contact_name: Optional[str] = None
    job_title: Optional[str] = None
    work_email: Optional[str] = None
    social_link: Optional[str] = None
    phone_number: Optional[str] = None
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
<<<<<<< HEAD
    cv_url: Optional[str] = None
=======
>>>>>>> 30bf9a43af4e25374ed7eade1dce9557ac563b8a

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
<<<<<<< HEAD
    cv_url: Optional[str] = None
=======
>>>>>>> 30bf9a43af4e25374ed7eade1dce9557ac563b8a

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
    note: str = Field(..., min_length=1, description="Nội dung note mới.")

    @field_validator("note")
    @classmethod
    def _note_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("note không được để trống hoặc chỉ chứa khoảng trắng")
        return v
<<<<<<< HEAD
=======


# ------------------------------------------------------------------
# Import / Export (thêm 08/2026)
# ------------------------------------------------------------------

class ImportUploadResponse(BaseModel):
    """Response của POST /import/{entity_type}/preview khi file hợp lệ
    (Requirement 4.1). `preview` là JSON tự do (cấu trúc mô tả trong
    api/services/preview_manager.py — rows[] + summary{}), KHÔNG ép kiểu
    Pydantic chi tiết từng field vì rows[].data thay đổi shape theo
    entity_type (Job/Company/Contact có field khác nhau hoàn toàn)."""
    preview_id: str
    entity_type: str
    summary: dict
    rows: list[dict]


class ImportValidationErrorOut(BaseModel):
    row_number: int
    field_name: str
    rule: str
    message: str


class RowResolution(BaseModel):
    """1 mục trong resolution map gửi lên lúc confirm — key ngoài (JSON
    object) là row_index dạng string, value là RowResolution này."""
    action: str = Field(..., description="skip | update | create")
    company_id: Optional[str] = Field(
        default=None,
        description="BẮT BUỘC nếu dòng này ở trạng thái "
                    "pending_company_resolution lúc preview — company_id "
                    "staff chọn từ danh sách gợi ý, hoặc company_id company "
                    "MỚI sẽ được tạo (client tự POST /companies trước, "
                    "hoặc để trống để hệ thống tự tạo company mới theo "
                    "company_name trong file).",
    )
    confirm_reactivate: bool = Field(
        default=False,
        description="BẮT BUỘC true nếu action=update cho dòng conflict_inactive "
                    "(record trùng đang inactive/CLOSED) — xác nhận staff CHẮC "
                    "CHẮN muốn ghi đè + kích hoạt lại record đó.",
    )

    @field_validator("action")
    @classmethod
    def _valid_action(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("skip", "update", "create"):
            raise ValueError("action chỉ nhận 'skip' | 'update' | 'create'")
        return v


class ImportConfirmRequest(BaseModel):
    """Body cho POST /import/{entity_type}/confirm. `note` BẮT BUỘC
    (quyết định thiết kế: mọi lượt import cần staff giải thích lý do,
    ghi vào audit_logs.note — xem ACTION_LOG_RULES trong db.py, action
    BULK_IMPORT_* luôn note_required=True)."""
    preview_id: str
    resolutions: dict[str, RowResolution] = Field(default_factory=dict)
    note: str = Field(..., min_length=1, description="BẮT BUỘC — lý do thực hiện đợt import này.")

    @field_validator("note")
    @classmethod
    def _note_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("note không được để trống hoặc chỉ chứa khoảng trắng")
        return v


class ImportConfirmResult(BaseModel):
    created: int
    updated: int
    skipped: int


class CompanySuggestionOut(BaseModel):
    company_id: str
    company_name: str
    tax_id: Optional[str] = None
    is_active: bool
    similarity: float


class CompanySuggestionsResponse(BaseModel):
    suggestions: list[CompanySuggestionOut]
>>>>>>> 30bf9a43af4e25374ed7eade1dce9557ac563b8a
