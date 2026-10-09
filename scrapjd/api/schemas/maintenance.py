"""
Maintenance trigger — schema request/response cho POST
/maintenance/{job_type}, GET /maintenance/{run_id} (08/2026, xem lịch sử
trao đổi "phương án B — generic runner dùng chung", đối xứng
api/schemas/crawl.py nhưng generic hoá theo job_type + params thay vì
source/category riêng cho crawl).
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


# ------------------------------------------------------------------
# Đăng ký 5 job_type — NGUỒN SỰ THẬT DUY NHẤT cho nhãn hiển thị +
# validate. Khớp đúng _JOB_RUNNERS ở api/maintenance_runner.py và enum
# maintenance_job_type_enum ở sql/migration_add_maintenance_runs.sql.
# Frontend (mindx-jobs) giữ nhãn tiếng Việt riêng của nó (giống
# _SOURCE_LABELS ở blueprints/crawl.py) — dict này chỉ phục vụ validate
# + mô tả ngắn ở phía backend.
# ------------------------------------------------------------------

MAINTENANCE_JOB_TYPES = (
    "backfill_company_profiles",
    "enrich_profile_from_website",
    "enrich_web_info",
    "get_fb_linkedin",
    "check_expired_jobs",
)

# 08/2026 — job này gọi Tavily + Gemini (TỐN PHÍ THẬT) — router BẮT
# BUỘC phải truyền "limit" (không cho để trống = chạy hết toàn bộ
# company chưa có dữ liệu), tránh admin bấm nhầm trên web đốt quota
# (rào cản tự nhiên khi gõ tay CLI — biết --limit — không còn khi bấm
# nút trên web, xem lịch sử trao đổi).
#
# 08/2026 (sửa bug) — TRƯỚC ĐÂY còn có "get_fb_linkedin" trong set này
# theo giả định sai. get_fb_linkedin (get_company_fb_linkedin_link.py)
# KHÔNG gọi Tavily/Gemini — chỉ fetch HTML thô từ website công ty đã có
# sẵn (companies.website) bằng curl_cffi + BeautifulSoup, hoàn toàn
# miễn phí. Comment mô tả hiển thị đã được sửa đúng từ trước, nhưng
# quên gỡ job_type này khỏi set bắt buộc limit — khiến "Tìm Facebook/
# LinkedIn" trên web luôn ép nhập limit dù không cần thiết (không đốt
# phí gì nếu để trống chạy hết).
MAINTENANCE_JOB_TYPES_REQUIRE_LIMIT = frozenset({"enrich_web_info"})

# Chỉ job_type này nhận dry_run/check_deadline_only — router trả 400
# nếu job_type khác mà vẫn truyền 2 field này (tránh hiểu lầm "dry_run"
# áp dụng được cho cả backfill/enrich, vốn không hỗ trợ tham số này).
_CHECK_EXPIRED_JOBS = "check_expired_jobs"


class MaintenanceRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Optional ở tầng schema (validate "bắt buộc cho enrich_web_info/
    # get_fb_linkedin" nằm ở router — phụ thuộc job_type lấy từ path,
    # Pydantic model này không tự biết job_type nào đang gọi nó).
    limit: Optional[int] = Field(
        default=None, ge=1, le=5000,
        examples=[50],
        description="Giới hạn số công ty/job xử lý — BẮT BUỘC cho "
                     "enrich_web_info (tốn phí Tavily/Gemini), "
                     "optional cho 4 job còn lại (bỏ trống = chạy hết).",
    )
    # CHỈ dùng cho job_type='check_expired_jobs' — router trả 400 nếu
    # truyền field này ở job_type khác.
    dry_run: Optional[bool] = Field(
        default=None,
        description="Chỉ dành cho check_expired_jobs — true = chỉ xem "
                     "trước job SẼ bị đóng, không ghi gì vào DB.",
    )
    check_deadline_only: Optional[bool] = Field(
        default=None,
        description="Chỉ dành cho check_expired_jobs — true = chỉ check "
                     "deadline quá hạn, không fetch mạng tới source_url.",
    )


class MaintenanceAccepted(BaseModel):
    run_id: str
    job_type: str
    status: str  # "queued"


class MaintenanceStatusOut(BaseModel):
    run_id: str
    job_type: str
    params: dict
    status: str  # "queued" | "running" | "done" | "error"
    stats: Optional[dict] = None
    error: Optional[str] = None
    triggered_by: Optional[str] = None
    triggered_by_name: Optional[str] = Field(
        default=None, description="full_name của admin đã bấm — null nếu "
                                   "triggered_by null hoặc tài khoản đã bị xoá.",
    )
    started_at: datetime
    finished_at: Optional[datetime] = None


class PaginatedMaintenanceRuns(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[MaintenanceStatusOut]


# ------------------------------------------------------------------
# Log live — GET /maintenance/{run_id}/logs — đối xứng CrawlLogOut/
# CrawlLogsOut ở api/schemas/crawl.py
# ------------------------------------------------------------------

class MaintenanceLogOut(BaseModel):
    id: int
    level: str
    message: str
    created_at: datetime


class MaintenanceLogsOut(BaseModel):
    last_id: int
    items: list[MaintenanceLogOut]
