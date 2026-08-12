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
    pages: int = Field(default=3, ge=1, le=20)


class CrawlAccepted(BaseModel):
    run_id: str
    status: str  # "queued" | "running" | "done" | "error"


class CrawlStatusOut(BaseModel):
    run_id: str
    status: str
    source: str
    category: str
    pages: int
    started_at: datetime
    finished_at: Optional[datetime] = None
    stats: Optional[dict] = None
    error: Optional[str] = None
