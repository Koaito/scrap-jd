from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import db as db_module
from api.deps import get_db, require_role
from api.rate_limit import limiter
from api.schemas import JobApplicantOut, JobCreate, JobDetailOut, JobSaverOut, JobUpdate, PaginatedJobs

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
    created_by: Optional[str] = Query(
        None, description="Lọc job do 1 thành viên ss_team/admin cụ thể TỰ NHẬP TAY (ss_user_id) — job crawl tự động (created_by NULL trong DB) không bao giờ khớp filter này."
    ),
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
    if created_by is not None and not db_module.is_valid_uuid(created_by):
        raise HTTPException(status_code=400, detail=f"created_by '{created_by}' không đúng định dạng UUID.")
    rows, total = db_module.list_jobs(
        conn,
        industry=industry,
        province_name=province,
        level_code=level,
        work_type=work_type,
        job_status=status,
        keyword=keyword,
        created_by=created_by,
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

    # create_manual_job() là IDEMPOTENT (trả job_id ĐÃ CÓ nếu trùng —
    # xem docstring), nên phải tự kiểm tra trùng TRƯỚC để biết job vừa
    # trả về là MỚI hay TÁI SỬ DỤNG — chỉ ghi CREATE_JOB khi thật sự
    # tạo mới, tránh log spam mỗi lần double-click Submit.
    was_duplicate = db_module.find_manual_job_duplicate(
        conn, company_id=payload.company_id, job_title=payload.job_title,
        level_id=level_id, province_id=province_id,
    ) is not None

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
    if not was_duplicate:
        # Ghi audit log CÙNG transaction với việc tạo job (trước
        # commit) — xem docstring db.log_action(). CREATE_JOB không
        # thuộc log thủ công, không cần note (xem db.ACTION_LOG_RULES).
        db_module.log_action(
            conn, actor_id=user["sub"], action_type="CREATE_JOB",
            entity_type="JOB", entity_id=job_id, entity_label=payload.job_title,
            company_id=payload.company_id,
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

    # Lấy trạng thái CŨ trước khi patch — cần để tính diff cho audit log
    # (xem db.diff_changed_fields). Cũng đóng vai trò kiểm tra tồn tại
    # SỚM (404 rõ ràng trước khi build câu UPDATE), thay vì chỉ dựa vào
    # rowcount của update_job().
    existing = db_module.get_job_by_id(conn, job_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")

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

    # payload_fields: CHỈ field client THỰC SỰ gửi lên (khác payload đầy
    # đủ) — dùng exclude_unset để phân biệt "không gửi" (giữ nguyên, KHÔNG
    # tính vào diff) với "gửi giá trị trùng cũ" (có gửi nhưng không đổi —
    # diff_changed_fields() tự lọc trường hợp này). "note" không phải
    # field nghiệp vụ của job, loại khỏi diff.
    payload_fields = payload.model_dump(exclude_unset=True, exclude={"note", "level_code", "province_name"})
    # level_code/province_name so sánh riêng bằng TÊN hiển thị (không
    # phải id) để khớp đúng field trong `existing` (existing['level_code']
    # là chuỗi, không phải level_id) — id đã resolve ở trên chỉ để truyền
    # cho update_job(), không dùng để diff.
    if payload.level_code is not None:
        payload_fields["level_code"] = payload.level_code
    if payload.province_name is not None:
        payload_fields["province_name"] = payload.province_name

    if payload_fields:
        changes = db_module.diff_changed_fields(existing, payload_fields)
        if changes:
            # job_status chuyển sang CLOSED trong lượt patch này -> coi
            # là hành động "xoá mềm JD" (DELETE_JOB), KHÔNG PHẢI sửa
            # thường — kể cả khi patch còn kèm field khác cùng lúc, cả
            # thao tác này được ghi thành 1 dòng DELETE_JOB duy nhất
            # (không tách 2 dòng UPDATE_JOB + DELETE_JOB cho 1 lần bấm
            # Save). Không phân biệt CLOSED vì "xoá" hay vì lý do khác ở
            # tầng job_status (xem thảo luận note = nơi giải thích lý do).
            is_delete = (
                "job_status" in changes
                and changes["job_status"]["new"] == "CLOSED"
            )
            db_module.log_action(
                conn, actor_id=user["sub"],
                action_type="DELETE_JOB" if is_delete else "UPDATE_JOB",
                entity_type="JOB", entity_id=job_id,
                entity_label=existing["job_title"],
                company_id=existing["company_id"],
                changes=changes, note=payload.note,
            )

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


@router.get("/{job_id}/saved-jobs", response_model=list[JobSaverOut])
def list_job_savers(
    job_id: str,
    user: dict = Depends(require_role("ss_team")),
    conn=Depends(get_db),
):
    """Thêm 08/2026 — mirror ĐÚNG list_job_applications() ở trên nhưng
    cho chiều 'lưu' thay vì 'ứng tuyển': ai đã lưu (bookmark) job này,
    role 'ss_team' trở lên. Trước đây saved_jobs cố ý bị coi là riêng
    tư 100% của học viên, không route nào cho staff xem — đổi quyết
    định vì SS team/admin không có cách nào theo dõi học viên đang
    quan tâm JD nào để chủ động hỗ trợ (xem db.list_saved_jobs_for_job()
    để biết chi tiết lý do đảo ngược)."""
    if not db_module.is_valid_uuid(job_id):
        raise HTTPException(status_code=400, detail=f"job_id '{job_id}' không đúng định dạng UUID.")
    if db_module.get_job_by_id(conn, job_id) is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")

    return db_module.list_saved_jobs_for_job(conn, job_id)
