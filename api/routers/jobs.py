from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

import db as db_module
from api.deps import get_db
from api.schemas import JobCreate, JobDetailOut, JobUpdate, PaginatedJobs

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


@router.post("", response_model=JobDetailOut, status_code=201)
def create_job(payload: JobCreate, conn=Depends(get_db)):
    """Tạo 1 job THỦ CÔNG (không qua crawl) — company_id PHẢI đã tồn tại
    trong DB (dùng GET /companies?keyword= để tìm, hoặc POST /companies
    để tạo mới trước nếu công ty chưa có). Route KHÔNG tự tạo company
    kèm theo job, tránh nhập nhằng "chọn đúng company có sẵn" với "gõ
    tên tạo company mới trùng lặp" — xem quyết định thiết kế trong
    API_README.md.

    level_code/province_name: người dùng gõ text thường (giống crawl),
    route tự map sang level_id/province_id qua các hàm db.* đã có."""
    company = db_module.get_company_by_id(conn, payload.company_id)
    if company is None:
        raise HTTPException(
            status_code=404,
            detail=f"company_id '{payload.company_id}' không tồn tại — "
                   f"tạo công ty trước bằng POST /companies.",
        )

    level_id = db_module.get_level_id(conn, payload.level_code) if payload.level_code else None
    province_id = (
        db_module.get_or_create_province(conn, payload.province_name)
        if payload.province_name else None
    )

    job_id = db_module.create_manual_job(
        conn,
        job_title=payload.job_title,
        company_id=payload.company_id,
        matching_industry=payload.matching_industry or "",
        level_id=level_id,
        province_id=province_id,
        work_type=payload.work_type,
        currency=payload.currency,
        salary_min=payload.salary_min,
        salary_max=payload.salary_max,
        salary_type=payload.salary_type,
        deadline=payload.deadline,
    )
    conn.commit()

    row = db_module.get_job_by_id(conn, job_id)
    return row


@router.patch("/{job_id}", response_model=JobDetailOut)
def patch_job(job_id: str, payload: JobUpdate, conn=Depends(get_db)):
    """Sửa TỰ DO các field của 1 job đã tồn tại (crawl hay nhập tay đều
    được — team không phân quyền, xem API_README.md). Chỉ field có mặt
    trong body mới bị ghi đè, field không gửi giữ nguyên giá trị cũ.

    Dùng {"job_status": "CLOSED"} để "xoá mềm" — KHÔNG có endpoint DELETE
    thật, vì job đã xoá thật sẽ bị crawl lại tạo trùng ở lượt crawl sau
    (get_job_probe_by_source_url() không còn thấy job này nữa)."""
    level_id = (
        db_module.get_level_id(conn, payload.level_code)
        if payload.level_code is not None else None
    )
    province_id = (
        db_module.get_or_create_province(conn, payload.province_name)
        if payload.province_name is not None else None
    )

    updated = db_module.update_job(
        conn, job_id,
        job_title=payload.job_title,
        matching_industry=payload.matching_industry,
        level_id=level_id,
        province_id=province_id,
        work_type=payload.work_type,
        currency=payload.currency,
        salary_min=payload.salary_min,
        salary_max=payload.salary_max,
        salary_type=payload.salary_type,
        deadline=payload.deadline,
        job_status=payload.job_status,
        ss_team_notes=payload.ss_team_notes,
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")
    conn.commit()

    row = db_module.get_job_by_id(conn, job_id)
    return row
