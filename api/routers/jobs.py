from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

import db as db_module
from api.deps import get_db
from api.schemas import JobDetailOut, PaginatedJobs

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=PaginatedJobs)
def list_jobs(
    industry: Optional[str] = Query(None, description="Lọc theo matching_industry, vd 'Data Analysis'"),
    province: Optional[str] = Query(None, description="Lọc theo tên tỉnh/thành, vd 'Hà Nội'"),
    level: Optional[str] = Query(None, description="Lọc theo level_code, vd 'Junior'"),
    work_type: Optional[str] = Query(None, description="FULL_TIME | PART_TIME | INTERNSHIP | OTHER"),
    status: Optional[str] = Query(None, description="OPEN | EXPIRED | CLOSED"),
    keyword: Optional[str] = Query(None, description="Tìm trong job_title (không phân biệt hoa/thường)"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    conn=Depends(get_db),
):
    """Danh sách job, hỗ trợ filter + phân trang. Không filter gì -> trả
    toàn bộ job, mới nhất trước."""
    rows, total = db_module.list_jobs(
        conn,
        industry=industry,
        province_name=province,
        level_code=level,
        work_type=work_type,
        job_status=status,
        keyword=keyword,
        limit=limit,
        offset=offset,
    )
    return PaginatedJobs(total=total, limit=limit, offset=offset, items=rows)


@router.get("/{job_id}", response_model=JobDetailOut)
def get_job(job_id: str, conn=Depends(get_db)):
    row = db_module.get_job_by_id(conn, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")
    return row
