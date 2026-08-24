"""
Job applications + saved jobs — schema request/response (thêm 08/2026,
xem db.py mục cùng tên). Tách từ api/schemas.py (08/2026) — xem docstring
api/schemas/__init__.py.
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict


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


