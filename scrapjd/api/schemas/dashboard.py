"""
Dashboard — schema response cho GET /dashboard/insights/{students,
companies,monthly} (Phần 5 mục 8 của plan Next.js). Mỗi endpoint ứng với
đúng 1 tab của trang /dashboard để Next.js tải theo yêu cầu từng tab.
Xem db/dashboard.py để biết logic từng khối.

Quy ước: field trả về theo tên backend (job_id/company_name...), KHÔNG
theo tên Flask (id/company/position). Chuỗi nhãn tiếng Việt do frontend
tự gắn (backend chỉ trả mã/số).
"""

from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field


# ------------------------------------------------------------------
# Tab "Gợi ý học viên"
# ------------------------------------------------------------------

class PushJobRow(BaseModel):
    job_id: str
    job_title: str
    company_id: str
    company_name: str
    deadline: date
    created_at: datetime
    days_left: int = Field(description="Số ngày từ hôm nay (giờ VN) tới deadline, luôn trong khoảng 7..14.")


class StaleJobRow(BaseModel):
    job_id: str
    job_title: str
    company_id: str
    company_name: str
    created_at: datetime
    age_days: int = Field(description="Số ngày kể từ ngày thu thập (giờ VN), luôn >= 30.")


class SkillCount(BaseModel):
    skill: str
    count: int


class SalaryRangeRow(BaseModel):
    industry: str
    level: str
    avg_min: Optional[float] = None
    avg_max: Optional[float] = None
    sample_size: int


class StudentInsightsOut(BaseModel):
    jd_needing_push: list[PushJobRow]
    jd_stale: list[StaleJobRow]
    top_skills: list[SkillCount]
    salary_ranges: list[SalaryRangeRow]


# ------------------------------------------------------------------
# Tab "Doanh nghiệp"
# ------------------------------------------------------------------

class HighPotentialCompanyRow(BaseModel):
    company_id: str
    company_name: str
    city: Optional[str] = None
    last_contacted: Optional[date] = None
    reason: Literal["no_contact", "contact_gone_cold", "never_contacted"] = Field(
        description="no_contact = chưa có contact nào; contact_gone_cold = có contact, "
                    "từng liên hệ nhưng đã nguội; never_contacted = có contact nhưng chưa "
                    "từng liên hệ lần nào.",
    )


class FollowupContactRow(BaseModel):
    contact_id: str
    contact_name: str
    job_title: Optional[str] = None
    contact_status: str
    company_id: str
    company_name: str
    last_contacted_date: Optional[date] = None
    collected_date: Optional[date] = None
    quiet_days: int
    never_contacted: bool


class ExpandingCompanyRow(BaseModel):
    company_id: str
    company_name: str
    city: Optional[str] = None
    recent_job_count: int
    recent_jobs: list[str]


class QuietCompanyRow(BaseModel):
    company_id: str
    company_name: str
    city: Optional[str] = None
    quiet_days: int
    last_job_date: date


class CompanyInsightsOut(BaseModel):
    followup_days: int = Field(description="Ngưỡng đã dùng cho contacts_needing_followup (7 | 14 | 30).")
    companies_no_contact: list[HighPotentialCompanyRow]
    contacts_needing_followup: list[FollowupContactRow]
    companies_expanding: list[ExpandingCompanyRow]
    companies_quiet: list[QuietCompanyRow]


# ------------------------------------------------------------------
# Tab "Báo cáo tháng"
# ------------------------------------------------------------------

class TopIndustryRow(BaseModel):
    industry: str
    count: int
    pct_change: Optional[int] = Field(
        default=None, description="% so với tháng trước; null nếu tháng trước = 0.",
    )


class TopCompanyRow(BaseModel):
    company_id: str
    company_name: str
    count: int


class MonthlyInsightsOut(BaseModel):
    this_month_start: date = Field(description="Ngày đầu tháng hiện tại theo giờ VN.")
    last_month_start: date
    jobs_new: int
    jobs_new_pct: Optional[int] = None
    jobs_expired: int
    companies_new: int
    companies_new_pct: Optional[int] = None
    top_industries: list[TopIndustryRow]
    top_companies: list[TopCompanyRow]
    applications_this_month: int
    applications_pct: Optional[int] = None
    saved_jobs_this_month: int
    saved_jobs_pct: Optional[int] = None
