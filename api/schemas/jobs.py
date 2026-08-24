"""
Jobs — schema request/response cho POST /jobs, PATCH /jobs, GET /jobs
(list/detail). Tách từ api/schemas.py (08/2026, xem entity_specs.py cho
đợt tách tương tự bên import/export) — schemas/__init__.py re-export lại
mọi tên cũ, KHÔNG cần sửa `from api.schemas import X` ở nơi gọi.
"""

from datetime import date, datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


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

