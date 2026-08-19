from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import db as db_module
from api.deps import get_db, require_role
from api.rate_limit import limiter
from api.schemas import JobApplicantOut, JobCreate, JobDetailOut, JobUpdate, PaginatedJobs

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=PaginatedJobs)
@limiter.limit("60/minute")
def list_jobs(
    request: Request,
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
    toàn bộ job, mới nhất trước.

    Rate limit 60/minute theo IP (thêm 08/2026) — route public, mỗi lần
    đổi filter ở frontend (index.html) là 1 query đầy đủ kèm COUNT(*)
    xuống Postgres, không giới hạn trước đó. 60/minute = trung bình 1
    request/giây, đủ rộng cho người dùng đổi filter nhanh tay lẫn
    debounce phía frontend (nếu sau này thêm), chỉ chặn kiểu spam script
    gọi liên tục."""
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
    if not db_module.is_valid_uuid(job_id):
        raise HTTPException(status_code=400, detail=f"job_id '{job_id}' không đúng định dạng UUID.")
    row = db_module.get_job_by_id(conn, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")
    return row


@router.post("", response_model=JobDetailOut, status_code=201)
def create_job(
    payload: JobCreate,
    conn=Depends(get_db),
    user: dict = Depends(require_role("ss_team")),
):
    """Tạo 1 job THỦ CÔNG (không qua crawl) — company_id PHẢI đã tồn tại
    trong DB (dùng GET /companies?keyword= để tìm, hoặc POST /companies
    để tạo mới trước nếu công ty chưa có). Route KHÔNG tự tạo company
    kèm theo job, tránh nhập nhằng "chọn đúng company có sẵn" với "gõ
    tên tạo company mới trùng lặp" — xem quyết định thiết kế trong
    API_README.md.

    level_code/province_name: người dùng gõ text thường (giống crawl),
    route tự map sang level_id/province_id qua các hàm db.* đã có.

    IDEMPOTENT: gọi lại nhiều lần với data y hệt (company_id + job_title
    + level_code + province_name giống nhau) sẽ KHÔNG tạo job trùng —
    trả về đúng job đã có (xem db.create_manual_job()).

    BẮT BUỘC đăng nhập VÀ role 'ss_team' trở lên (require_role("ss_team"),
    đổi từ chỉ-cần-đăng-nhập sang có phân cấp — 08/2026, xem
    sql/migration_add_role_hierarchy.sql) — để ghi lại
    job_postings.created_by (audit trail "ai tạo job này"), đồng thời
    chặn role 'user' (chỉ xem) không sửa được dữ liệu. Vẫn cần header
    X-API-Key NHƯ CŨ (2 lớp xếp chồng, xem docstring api/deps.py), CỘNG
    THÊM header Authorization: Bearer <access_token> lấy từ POST
    /auth/login."""
    if not db_module.is_valid_uuid(payload.company_id):
        # BUG ĐÃ VÁ (08/2026, phát hiện qua test thật): trước đây company_id
        # sai định dạng UUID (vd còn sót placeholder mẫu, gõ nhầm) sẽ được
        # đưa thẳng vào query Postgres -> psycopg2 raise lỗi không bắt được
        # -> 500 Internal Server Error mù mờ. Validate ở đây để trả 400 rõ
        # ràng, chỉ đúng nguyên nhân, TRƯỚC KHI chạm tới DB.
        raise HTTPException(
            status_code=400,
            detail=f"company_id '{payload.company_id}' không đúng định dạng UUID "
                   f"— kiểm tra lại đã thay đúng company_id THẬT lấy từ response "
                   f"của POST /companies (hoặc GET /companies?keyword=) chưa, "
                   f"không phải chuỗi mẫu/placeholder.",
        )

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
        salary_period=payload.salary_period,
        deadline=payload.deadline,
        parsed_content=payload.parsed_content.model_dump(exclude_none=True) if payload.parsed_content else None,
        created_by=user["sub"],
    )
    conn.commit()

    row = db_module.get_job_by_id(conn, job_id)
    return row


@router.patch("/{job_id}", response_model=JobDetailOut)
def patch_job(
    job_id: str,
    payload: JobUpdate,
    conn=Depends(get_db),
    user: dict = Depends(require_role("ss_team")),
):
    """Sửa TỰ DO các field của 1 job đã tồn tại (crawl hay nhập tay đều
    được — team không phân quyền chi tiết hơn theo route này, chỉ cần
    role 'ss_team' trở lên, xem API_README.md). Chỉ field có mặt trong
    body mới bị ghi đè, field không gửi giữ nguyên giá trị cũ.

    Dùng {"job_status": "CLOSED"} để "xoá mềm" — KHÔNG có endpoint DELETE
    thật, vì job đã xoá thật sẽ bị crawl lại tạo trùng ở lượt crawl sau
    (get_job_probe_by_source_url() không còn thấy job này nữa).

    BẮT BUỘC đăng nhập VÀ role 'ss_team' trở lên (đổi từ chỉ-cần-đăng-nhập,
    08/2026) — giống POST /jobs, ghi lại job_postings.updated_by = người
    vừa sửa, đồng thời chặn role 'user' không sửa được."""
    if not db_module.is_valid_uuid(job_id):
        raise HTTPException(status_code=400, detail=f"job_id '{job_id}' không đúng định dạng UUID.")

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
        salary_period=payload.salary_period,
        deadline=payload.deadline,
        parsed_content=payload.parsed_content.model_dump(exclude_none=True) if payload.parsed_content else None,
        job_status=payload.job_status,
        ss_team_notes=payload.ss_team_notes,
        updated_by=user["sub"],
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")
    conn.commit()

    row = db_module.get_job_by_id(conn, job_id)
    return row


@router.get("/{job_id}/applications", response_model=list[JobApplicantOut])
def list_job_applications(
    job_id: str,
    user: dict = Depends(require_role("ss_team")),
    conn=Depends(get_db),
):
    """Ai đã ứng tuyển job này — role 'ss_team' trở lên (giống contacts,
    thông tin full_name/email người ứng tuyển được coi là nhạy cảm
    tương tự HR contact, 'user' không thấy được đơn của người khác, chỉ
    thấy đơn của chính mình qua GET /me/applications)."""
    if not db_module.is_valid_uuid(job_id):
        raise HTTPException(status_code=400, detail=f"job_id '{job_id}' không đúng định dạng UUID.")
    if db_module.get_job_by_id(conn, job_id) is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")

    return db_module.list_applications_for_job(conn, job_id)
