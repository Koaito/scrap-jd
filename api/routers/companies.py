from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

import db as db_module
from api.deps import get_db
from api.schemas import CompanyDetailOut, PaginatedCompanies

router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("", response_model=PaginatedCompanies)
def list_companies(
    keyword: Optional[str] = Query(None, description="Tìm trong company_name"),
    province: Optional[str] = Query(None, description="Lọc theo tên tỉnh/thành"),
    has_social: Optional[bool] = Query(
        None,
        description="true = chỉ công ty đã có fanpage/linkedin; "
                    "false = chỉ công ty còn thiếu cả hai (ứng viên cho "
                    "get_company_fb_linkedin_link.py)",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    conn=Depends(get_db),
):
    rows, total = db_module.list_companies(
        conn, keyword=keyword, has_social=has_social, province_name=province,
        limit=limit, offset=offset,
    )
    return PaginatedCompanies(total=total, limit=limit, offset=offset, items=rows)


@router.get("/{company_id}", response_model=CompanyDetailOut)
def get_company(company_id: str, conn=Depends(get_db)):
    row = db_module.get_company_by_id(conn, company_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy công ty")
    jobs = db_module.get_jobs_by_company_id(conn, company_id)
    return {**row, "jobs": jobs}
