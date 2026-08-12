from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

import db as db_module
from api.deps import get_db
from api.schemas import CompanyCreate, CompanyDetailOut, CompanyOut, PaginatedCompanies

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


@router.post("", response_model=CompanyOut, status_code=201)
def create_company(payload: CompanyCreate, conn=Depends(get_db)):
    """Tạo công ty THỦ CÔNG — dùng trước POST /jobs khi công ty chưa có
    trong DB (GET /companies?keyword= tìm không ra). Nếu tax_id điền vào
    trùng với công ty đã crawl trước đó, tự động DÙNG LẠI company đã có
    (không tạo trùng) — xem docstring db.get_or_create_company_by_profile().

    Trả về company đầy đủ (kể cả khi thực ra là company đã có sẵn từ
    trước do trùng tax_id) — frontend luôn dùng company_id trong response
    này cho bước tạo job tiếp theo, không giả định trùng ID với request."""
    province_id = db_module.get_or_create_province(conn, payload.province_name or "")
    company_id = db_module.get_or_create_company_by_profile(
        conn, payload.company_name, province_id, tax_id=payload.tax_id or "",
    )
    db_module.update_company_profile(
        conn, company_id,
        tax_id=payload.tax_id or "",
        website=payload.website or "",
        industry=payload.industry or "",
        company_size=payload.company_size or "",
        address=payload.address or "",
    )
    if payload.fanpage_url or payload.linkedin_url:
        db_module.update_company_social_links(
            conn, company_id,
            fanpage_url=payload.fanpage_url or "",
            linkedin_url=payload.linkedin_url or "",
        )
    conn.commit()

    row = db_module.get_company_by_id(conn, company_id)
    return row
