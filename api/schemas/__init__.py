"""
Pydantic schemas — contract cho request/response của API layer.

CỐ Ý tách riêng khỏi models.RawJobRecord: RawJobRecord là contract nội bộ
giữa adapter <-> pipeline (dữ liệu thô, optional nhiều field). Schema ở
đây là contract API <-> frontend (dữ liệu đã có trong DB, đầy đủ hơn, có
join thêm company_name/level_code/province_name cho tiện hiển thị) — 2
mục đích khác nhau, gộp chung sẽ rối khi 1 bên cần đổi mà bên kia không.

TÁCH THÀNH PACKAGE (08/2026): file gốc api/schemas.py đã phình tới 1165
dòng/65 class (đúng như cảnh báo lúc review kiến trúc lần trước) — trộn
9 domain không liên quan (job, company, stats, crawl, auth, contact, job
application/saved job, audit log, import/export) trong 1 file. Tách theo
domain, MIRROR đúng cách đã tách db.py -> db/ package trước đó (cùng tên
domain: jobs/companies/contacts/auth/audit_logs/applications/stats, cộng
thêm crawl.py + import_export.py chỉ có ở schemas vì đây là 2 domain
riêng của API layer, không có tương ứng bên db/).

File này re-export TOÀN BỘ 65 tên cũ — `from api.schemas import X` ở mọi
router/test KHÔNG cần sửa gì, hoạt động y hệt trước khi tách (import ngầm
qua package thay vì module đơn). Thêm/sửa schema mới CHỈ cần sửa đúng 1
submodule domain tương ứng — không phải kéo cả 1165 dòng vào context như
trước.

LƯU Ý PHỤ THUỘC: companies.py import JobOut từ jobs.py (CompanyDetailOut.
jobs: list[JobOut]) — MỘT CHIỀU (jobs.py không phụ thuộc ngược lại
companies.py) — không có circular import. Mọi submodule khác độc lập,
không tham chiếu chéo lẫn nhau.
"""

from api.schemas.jobs import (
    JobOut,
    JobDetailOut,
    PaginatedJobs,
    ParsedContent,
    JobCreate,
    JobUpdate,
)
from api.schemas.companies import (
    CompanyOut,
    CompanyDetailOut,
    PaginatedCompanies,
    CompanyCreate,
    CompanyUpdate,
    CompanyDeleteRequest,
)
from api.schemas.stats import (
    IndustryCount,
    SourceCount,
    StatsOut,
    JobEngagementOut,
    MonthlyCountOut,
    MonthlyEngagementOut,
    EngagementStatsOut,
)
from api.schemas.crawl import (
    CrawlRequest,
    CrawlAccepted,
    CrawlStatusOut,
    PaginatedCrawlRuns,
    CrawlLogOut,
    CrawlLogsOut,
    CrawlBatchRequest,
    CrawlBatchAccepted,
    CrawlBatchStatusOut,
    CrawlBatchSummaryOut,
    PaginatedCrawlBatches,
)
# 08/2026 (xem lịch sử trao đổi "phương án B — generic runner dùng
# chung") — "Bảo trì dữ liệu" từ web, đối xứng crawl.py ở trên.
from api.schemas.maintenance import (
    MAINTENANCE_JOB_TYPES,
    MAINTENANCE_JOB_TYPES_REQUIRE_LIMIT,
    MaintenanceRunRequest,
    MaintenanceAccepted,
    MaintenanceStatusOut,
    PaginatedMaintenanceRuns,
    MaintenanceLogOut,
    MaintenanceLogsOut,
)
from api.schemas.auth import (
    LoginRequest,
    TokenPairOut,
    RefreshRequest,
    AccessTokenOut,
    ChangePasswordRequest,
    UserOut,
    UserCreateByAdmin,
    UserCreatedOut,
    UserRoleUpdate,
    UserActiveStatusUpdate,
    RegisterRequest,
    RegisterOut,
    ResendVerificationRequest,
    ForgotPasswordRequest,
    ResetPasswordRequest,
    MessageOut,
)
from api.schemas.contacts import (
    CompanyContactOut,
    CompanyContactWithCompanyOut,
    CompanyContactCreate,
    CompanyContactUpdate,
    ContactAssignUpdate,
    ContactDeleteRequest,
)
from api.schemas.email_templates import (
    EmailTemplateOut,
    PlaceholderHelpOut,
    EmailTemplateCreate,
    EmailTemplateUpdate,
    EmailTemplateDeleteRequest,
    PLACEHOLDER_HELP,
)
from api.schemas.applications import (
    JobApplicationCreate,
    JobApplicationOut,
    JobApplicantOut,
    JobSaverOut,
    SavedJobCreate,
    SavedJobOut,
)
from api.schemas.audit_logs import (
    AuditLogOut,
    PaginatedAuditLogs,
    AuditLogNoteUpdate,
)
from api.schemas.import_export import (
    CompanySuggestionOut,
    CompanySuggestionsResponse,
    ImportUploadResponse,
    RowResolution,
    FieldVerifyRequest,
    DuplicateMatchOut,
    BatchDuplicateMatchOut,
    FieldVerifyResponse,
    ResolveCompanyRequest,
    ResolveCompanyResponse,
    ImportConfirmRequest,
    ImportConfirmResult,
    ExportPreviewResponse,
)

__all__ = [
    # jobs
    "JobOut", "JobDetailOut", "PaginatedJobs", "ParsedContent", "JobCreate", "JobUpdate",
    # companies
    "CompanyOut", "CompanyDetailOut", "PaginatedCompanies", "CompanyCreate",
    "CompanyUpdate", "CompanyDeleteRequest",
    # stats
    "IndustryCount", "SourceCount", "StatsOut", "JobEngagementOut",
    "MonthlyCountOut", "MonthlyEngagementOut", "EngagementStatsOut",
    # crawl
    "CrawlRequest", "CrawlAccepted", "CrawlStatusOut", "PaginatedCrawlRuns",
    "CrawlLogOut", "CrawlLogsOut",
    "CrawlBatchRequest", "CrawlBatchAccepted", "CrawlBatchStatusOut",
    "CrawlBatchSummaryOut", "PaginatedCrawlBatches",
    "MAINTENANCE_JOB_TYPES", "MAINTENANCE_JOB_TYPES_REQUIRE_LIMIT",
    "MaintenanceRunRequest", "MaintenanceAccepted", "MaintenanceStatusOut",
    "PaginatedMaintenanceRuns", "MaintenanceLogOut", "MaintenanceLogsOut",
    # auth
    "LoginRequest", "TokenPairOut", "RefreshRequest", "AccessTokenOut",
    "ChangePasswordRequest", "UserOut", "UserCreateByAdmin", "UserCreatedOut",
    "UserRoleUpdate", "UserActiveStatusUpdate", "RegisterRequest", "RegisterOut",
    "ResendVerificationRequest", "ForgotPasswordRequest", "ResetPasswordRequest",
    "MessageOut",
    # contacts
    "CompanyContactOut", "CompanyContactWithCompanyOut", "CompanyContactCreate",
    "CompanyContactUpdate", "ContactAssignUpdate", "ContactDeleteRequest",
    # email templates
    "EmailTemplateOut", "PlaceholderHelpOut", "EmailTemplateCreate",
    "EmailTemplateUpdate", "EmailTemplateDeleteRequest", "PLACEHOLDER_HELP",
    # applications / saved jobs
    "JobApplicationCreate", "JobApplicationOut", "JobApplicantOut", "JobSaverOut",
    "SavedJobCreate", "SavedJobOut",
    # audit logs
    "AuditLogOut", "PaginatedAuditLogs", "AuditLogNoteUpdate",
    # import/export
    "CompanySuggestionOut", "CompanySuggestionsResponse", "ImportUploadResponse",
    "RowResolution", "FieldVerifyRequest", "DuplicateMatchOut", "BatchDuplicateMatchOut",
    "FieldVerifyResponse", "ResolveCompanyRequest", "ResolveCompanyResponse",
    "ImportConfirmRequest", "ImportConfirmResult", "ExportPreviewResponse",
]
