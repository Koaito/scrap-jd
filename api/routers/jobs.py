import base64
import binascii
from datetime import datetime
from typing import Optional

import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Query, Request

import db as db_module
from api import error_codes
from api import storage as cv_storage
from api.deps import get_db, require_role
from api.rate_limit import get_user_id_or_ip, limiter
from api.schemas import JobApplicantOut, JobCreate, JobDataHealth, JobDetailOut, JobSaverOut, JobUpdate, PaginatedJobs

router = APIRouter(prefix="/jobs", tags=["jobs"])

_CURSOR_SEP = "|"


def _encode_cursor(created_at: datetime, job_id: str) -> str:
    """tuple (created_at, job_id) -> chuỗi opaque base64 cho client —
    KHÔNG để client thấy/tự dựng cấu trúc bên trong, để sau này đổi
    khóa cursor (vd thêm field) mà không phá client cũ (xem docstring
    PaginatedJobs.next_cursor)."""
    raw = f"{created_at.isoformat()}{_CURSOR_SEP}{job_id}"
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple:
    """Ngược lại _encode_cursor() — raise ValueError nếu cursor sai
    định dạng (client tự chế/sửa tay chuỗi, hoặc cursor từ 1 phiên bản
    schema cũ/khác), router bắt ValueError để trả 422 rõ ràng thay vì
    để lỗi 500 lộ traceback."""
    raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("utf-8")
    created_at_str, _, job_id = raw.partition(_CURSOR_SEP)
    if not job_id:
        raise ValueError("cursor thiếu job_id")
    return datetime.fromisoformat(created_at_str), job_id


@router.get("", response_model=PaginatedJobs)
@limiter.limit("60/minute")
def list_jobs(
    request: Request,
    industry: Optional[str] = Query(None, description="Lọc theo matching_industry, vd 'Data Analysis'"),
    province: Optional[str] = Query(None, description="Lọc theo tên tỉnh/thành, vd 'Hà Nội'"),
    level: Optional[str] = Query(None, description="Lọc theo level_code, vd 'Junior'"),
    work_type: Optional[str] = Query(None, description="FULL_TIME | PART_TIME | INTERNSHIP | OTHER"),
    status: Optional[str] = Query(None, description="OPEN | CLOSED"),
    keyword: Optional[str] = Query(None, description="Tìm trong job_title (không phân biệt hoa/thường)"),
    created_by: Optional[str] = Query(
        None, description="Lọc job do 1 thành viên ss_team/admin cụ thể TỰ NHẬP TAY (ss_user_id) — job crawl tự động (created_by NULL trong DB) không bao giờ khớp filter này."
    ),
    include_content: bool = Query(
        False,
        description="Mặc định false (giữ nguyên hành vi cũ, KHÔNG trả parsed_content "
                    "để payload nhẹ — route này public, kể cả trang tuyển dụng công "
                    "khai gọi). Truyền true khi cần đủ nội dung JD (job_description/"
                    "requirements/perks/required_skills) ngay ở list, thay vì gọi "
                    "riêng GET /jobs/{job_id} cho từng job — vd tab 'Tình trạng dữ "
                    "liệu' (08/2026).",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    cursor: Optional[str] = Query(
        None,
        description="Cursor opaque cho chế độ 'cuộn vô hạn' (thêm 09/2026) — "
                    "lấy từ `next_cursor` của response GỌI TRƯỚC, ĐỂ TRỐNG ở lần "
                    "gọi đầu tiên. KHÔNG dùng cùng lúc với `offset` khác 0 (422 "
                    "nếu vi phạm) — 2 tham số phục vụ 2 chế độ phân trang khác "
                    "nhau ở frontend (xem PaginatedJobs.next_cursor).",
    ),
    conn=Depends(get_db),
):
    """Danh sách job, hỗ trợ filter + phân trang. Không filter gì -> trả
    toàn bộ job, mới nhất trước.

    Rate limit 60/minute theo IP (thêm 08/2026) — route public, mỗi lần
    đổi filter ở frontend (index.html) là 1 query đầy đủ kèm COUNT(*)
    xuống Postgres, không giới hạn trước đó. 60/minute = trung bình 1
    request/giây, đủ rộng cho người dùng đổi filter nhanh tay lẫn
    debounce phía frontend (nếu sau này thêm), chỉ chặn kiểu spam script
    gọi liên tục. Chế độ cursor (xem tham số `cursor`) tính CHUNG vào
    limit này — nếu sau này thấy cuộn nhanh hay chạm rate limit, tăng
    limit riêng cho route này hoặc tăng page-size mặc định phía
    frontend cho chế độ vô hạn, không tăng cho mọi client."""
    if created_by is not None and not db_module.is_valid_uuid(created_by):
        raise HTTPException(status_code=400, detail={"error_code": error_codes.JOB_CREATED_BY_INVALID_UUID, "message": f"created_by '{created_by}' không đúng định dạng UUID.", "params": {"value": created_by}})

    if cursor is not None and offset != 0:
        raise HTTPException(
            status_code=422,
            detail={
                "error_code": error_codes.JOB_CURSOR_WITH_OFFSET_NOT_ALLOWED,
                "message": "Không thể truyền cả cursor lẫn offset cùng lúc — dùng "
                           "offset cho chế độ 'Trang X/Y', dùng cursor cho chế độ "
                           "cuộn vô hạn.",
                "params": {"cursor": cursor, "offset": offset},
            },
        )

    decoded_cursor = None
    if cursor is not None:
        try:
            decoded_cursor = _decode_cursor(cursor)
        except (ValueError, binascii.Error, UnicodeDecodeError):
            raise HTTPException(
                status_code=422,
                detail={
                    "error_code": error_codes.JOB_CURSOR_INVALID,
                    "message": "cursor không đúng định dạng — chỉ dùng giá trị "
                               "next_cursor lấy từ response GET /jobs trước đó.",
                    "params": {"cursor": cursor},
                },
            )

    rows, total, next_cursor_tuple = db_module.list_jobs(
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
        cursor=decoded_cursor,
        include_content=include_content,
    )
    next_cursor = _encode_cursor(*next_cursor_tuple) if next_cursor_tuple else None
    return PaginatedJobs(total=total, limit=limit, offset=offset, items=rows, next_cursor=next_cursor)


@router.get("/data-health", response_model=JobDataHealth)
@limiter.limit("60/minute")
def get_job_data_health(request: Request, conn=Depends(get_db)):
    """GET /jobs/data-health — thay thế cho việc frontend
    (blueprints/crawl_status.py bên mindx-jobs, tab "Tình trạng dữ
    liệu") từng phải gọi list_all_jobs(include_content=True) — kéo
    TOÀN BỘ job kèm cột parsed_content (JSONB dài) của cả hệ thống về
    Flask, rồi tự đếm/group/tìm trùng bằng Python. Route này tính sẵn
    bằng SQL (xem db.get_job_data_health()) — KHÔNG BAO GIỜ serialize
    parsed_content qua network, chi phí không tăng theo tổng số job
    toàn hệ thống nữa.

    Public (không require_role) — GIỐNG GET /jobs (không có thông tin
    nhạy cảm như contact bên /companies/data-health, chỉ đếm/nhóm field
    nội dung JD sẵn công khai). Rate limit 60/minute cùng lý do GET /jobs.

    PHẢI khai báo route này TRƯỚC GET /{job_id} bên dưới — path cố định
    "/data-health" nếu đặt SAU sẽ bị FastAPI khớp nhầm vào job_id (rồi
    400 vì "data-health" không phải UUID hợp lệ), xem cùng lý do đã
    giải thích ở /companies/data-health."""
    return db_module.get_job_data_health(conn)


# Thêm khi migrate Next.js (Phần 5 mục 11 của plan): route này TRƯỚC ĐÂY
# nằm ở GET /me/applications/{application_id}/cv-url — gây hiểu nhầm là
# hành động tự phục vụ của CHÍNH học viên (mọi route khác dưới /me đều
# vậy, ss_user_id luôn lấy từ JWT của chính người gọi, KHÔNG nhận qua
# path/body — xem docstring đầu api/routers/me.py). Route này thì NGƯỢC
# LẠI hoàn toàn: application_id là của NGƯỜI KHÁC (học viên đã nộp đơn),
# và require_role("ss_team") đã luôn chặn "user" thường gọi route này từ
# trước tới giờ — bản chất đây là hành động STAFF xem hồ sơ người khác,
# không phải "của tôi". Dời sang dưới /jobs (namespace staff-facing, đã
# có GET /{job_id}/applications cùng mục đích "staff xem thông tin ứng
# tuyển") để tên route phản ánh đúng ai gọi được. HÀNH VI GIỮ NGUYÊN
# 100% — cùng logic, cùng mã lỗi (PROFILE_* giữ nguyên, KHÔNG đổi sang
# JOB_* để không phá FE đang bắt theo error_code cũ), cùng rate limit,
# chỉ đổi path. PHẢI khai báo TRƯỚC GET /{job_id} bên dưới cùng lý do
# /data-health ở trên — "applications" không phải UUID hợp lệ nhưng vẫn
# cần path cố định này được match trước khi rơi vào {job_id}.
@router.get("/applications/{application_id}/cv-url")
@limiter.limit("30/minute", key_func=get_user_id_or_ip)
def get_cv_signed_url(
    request: Request,
    application_id: str,
    user: dict = Depends(require_role("ss_team")),  # Chỉ Staff / Admin mới có quyền lấy
    conn=Depends(get_db),
):
    """Staff lấy Signed URL để tải và xem CV học viên.

    Rate limit 30/minute theo user_id (thêm 08/2026) — mỗi lần gọi tốn
    1 lệnh gọi thật tới storage provider để sinh signed URL mới. Mốc
    30/minute chỉ nhằm chặn lỗi loop/script gọi lặp ngoài ý muốn, không
    ảnh hưởng thao tác bình thường của staff (xem qua nhiều CV liên
    tục trong lúc duyệt hồ sơ vẫn thoải mái nằm trong hạn mức này)."""
    if not db_module.is_valid_uuid(application_id):
        raise HTTPException(status_code=400, detail={"error_code": error_codes.PROFILE_INVALID, "message": "application_id không hợp lệ."})

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT cv_url FROM job_applications WHERE application_id = %s", (application_id,))
        row = cur.fetchone()

    if not row or not row["cv_url"]:
        raise HTTPException(status_code=404, detail={"error_code": error_codes.PROFILE_CV_NOT_SUBMITTED, "message": "Học viên chưa nộp CV cho đơn này."})

    signed_url = cv_storage.get_signed_url(row["cv_url"])
    if not signed_url:
        raise HTTPException(status_code=500, detail={"error_code": error_codes.PROFILE_CANNOT_CREATE, "message": "Không thể tạo link tải file lúc này."})

    return {"signed_url": signed_url}


@router.get("/{job_id}", response_model=JobDetailOut)
def get_job(job_id: str, conn=Depends(get_db)):
    if not db_module.is_valid_uuid(job_id):
        raise HTTPException(status_code=400, detail={"error_code": error_codes.JOB_JOB_ID_INVALID_UUID, "message": f"job_id '{job_id}' không đúng định dạng UUID.", "params": {"value": job_id}})
    row = db_module.get_job_by_id(conn, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail={"error_code": error_codes.JOB_JOB_NOT_FOUND, "message": "Không tìm thấy job"})
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
            detail={"error_code": error_codes.JOB_COMPANY_ID_INVALID_UUID, "message": f"company_id '{payload.company_id}' không đúng định dạng UUID "
                   f"— kiểm tra lại đã thay đúng company_id THẬT lấy từ response "
                   f"của POST /companies (hoặc GET /companies?keyword=) chưa, "
                   f"không phải chuỗi mẫu/placeholder.", "params": {"value": payload.company_id}},
        )

    company = db_module.get_company_by_id(conn, payload.company_id)
    if company is None:
        raise HTTPException(
            status_code=404,
            detail={"error_code": error_codes.JOB_COMPANY_NOT_FOUND, "message": f"company_id '{payload.company_id}' không tồn tại — "
                   f"tạo công ty trước bằng POST /companies.", "params": {"value": payload.company_id}},
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
        raise HTTPException(status_code=400, detail={"error_code": error_codes.JOB_JOB_ID_INVALID_UUID, "message": f"job_id '{job_id}' không đúng định dạng UUID.", "params": {"value": job_id}})

    # Lấy trạng thái CŨ trước khi patch — cần để tính diff cho audit log
    # (xem db.diff_changed_fields). Cũng đóng vai trò kiểm tra tồn tại
    # SỚM (404 rõ ràng trước khi build câu UPDATE), thay vì chỉ dựa vào
    # rowcount của update_job().
    existing = db_module.get_job_by_id(conn, job_id)
    if existing is None:
        raise HTTPException(status_code=404, detail={"error_code": error_codes.JOB_JOB_NOT_FOUND, "message": "Không tìm thấy job"})

    level_id = (
        db_module.get_level_id(conn, payload.level_code)
        if payload.level_code is not None else None
    )
    province_id = (
        db_module.get_or_create_province(conn, payload.province_name)
        if payload.province_name is not None else None
    )

    # BUG FIX (migrate Next.js, Phần 1 mục 3.3 của plan): truyền
    # db_module.JOB_UNSET (không phải payload.salary_min/max trực tiếp)
    # khi field KHÔNG có mặt trong body PATCH — dựa vào
    # payload.model_fields_set (cùng cơ chế exclude_unset đã dùng để
    # tính payload_fields/diff audit log bên dưới). Nếu truyền thẳng
    # payload.salary_min, Pydantic trả None cho field không gửi HỆT
    # như field gửi giá trị null có chủ đích — 2 tình huống này PHẢI
    # tạo ra 2 lời gọi update_job() khác nhau (có mặt tham số hay
    # không), không chỉ khác giá trị.
    salary_min = (
        payload.salary_min if "salary_min" in payload.model_fields_set
        else db_module.JOB_UNSET
    )
    salary_max = (
        payload.salary_max if "salary_max" in payload.model_fields_set
        else db_module.JOB_UNSET
    )

    updated = db_module.update_job(
        conn, job_id,
        job_title=payload.job_title,
        matching_industry=payload.matching_industry,
        level_id=level_id,
        province_id=province_id,
        work_type=payload.work_type,
        currency=payload.currency,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_type=payload.salary_type,
        salary_period=payload.salary_period,
        deadline=payload.deadline,
        parsed_content=payload.parsed_content.model_dump(exclude_none=True) if payload.parsed_content else None,
        job_status=payload.job_status,
        ss_team_notes=payload.ss_team_notes,
        updated_by=user["sub"],
    )
    if not updated:
        raise HTTPException(status_code=404, detail={"error_code": error_codes.JOB_JOB_NOT_FOUND, "message": "Không tìm thấy job"})

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
        raise HTTPException(status_code=400, detail={"error_code": error_codes.JOB_JOB_ID_INVALID_UUID, "message": f"job_id '{job_id}' không đúng định dạng UUID.", "params": {"value": job_id}})
    if db_module.get_job_by_id(conn, job_id) is None:
        raise HTTPException(status_code=404, detail={"error_code": error_codes.JOB_JOB_NOT_FOUND, "message": "Không tìm thấy job"})

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
        raise HTTPException(status_code=400, detail={"error_code": error_codes.JOB_JOB_ID_INVALID_UUID, "message": f"job_id '{job_id}' không đúng định dạng UUID.", "params": {"value": job_id}})
    if db_module.get_job_by_id(conn, job_id) is None:
        raise HTTPException(status_code=404, detail={"error_code": error_codes.JOB_JOB_NOT_FOUND, "message": "Không tìm thấy job"})

    return db_module.list_saved_jobs_for_job(conn, job_id)
