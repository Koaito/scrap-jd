"""
Stats — schema response cho GET /stats, GET /stats/engagement.
Tách từ api/schemas.py (08/2026) — xem docstring api/schemas/__init__.py.
"""

from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel


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
    # Thêm 09/2026 — jobs_by_status dict để frontend lấy "Job đang còn tuyển"
    # không cần tải 1000+ jobs array. Key = job status string, value = count
    jobs_by_status: dict[str, int]
    # Thêm 09/2026 — total_students (role='user') để frontend hiển thị KPI
    # "Học viên đã đăng ký", không cần gọi GET /auth/users riêng (admin only)
    total_students: int


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


