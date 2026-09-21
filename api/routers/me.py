"""
/me/... — hành động của CHÍNH học viên đang đăng nhập: ứng tuyển job
(job_applications) và lưu job để xem lại sau (saved_jobs). Thêm 08/2026
(xem sql/migration_add_applications_saved_jobs.sql).

require_role("user") — bậc thấp nhất trong 3 role (xem api/deps.py) —
tức MỌI tài khoản đã đăng nhập đều gọi được (staff test thử cũng được,
không riêng học viên). ss_user_id lấy từ chính JWT (user["sub"]),
KHÔNG nhận qua path/body — 1 người chỉ thao tác được trên đơn/bookmark
của chính mình, không có route nào cho phép truyền ss_user_id tuỳ ý.

Chỉ ứng tuyển được job đang job_status='OPEN' — job đã CLOSED/EXPIRED
bị chặn 400 ngay ở POST /me/applications (không chặn ở tầng saved-jobs,
vì lưu job đã đóng để xem lại vẫn hợp lý).

Rate limit (thêm 08/2026): POST /me/applications và POST /me/saved-jobs
dùng key_func=get_user_id_or_ip (api/rate_limit.py) — khoá theo
ss_user_id trong JWT thay vì IP, vì route này luôn có người đăng nhập
sẵn. Lý do khoá theo user thay vì IP mặc định của limiter: nhiều học
viên dùng chung 1 mạng (KTX, wifi lớp học) sẽ có cùng 1 IP, nếu khoá
theo IP thì 1 học viên bấm nhanh có thể vô tình làm nghẽn hạn mức của
người khác chung mạng — không công bằng và không đúng mục tiêu (mục
tiêu là chặn 1 người dùng cụ thể spam, không phải chặn cả dải IP).
saved-jobs cho phép cao hơn applications (30/minute vs 15/minute) vì
đây là nút toggle lưu/bỏ lưu (frontend AJAX, xem CHANGELOG_frontend_fixes
#4) — người dùng có thể lưu/bỏ lưu qua lại vài lần khi cân nhắc, trong
khi ứng tuyển là hành động 1 chiều, ít lý do bấm nhiều lần liên tiếp.
"""

from typing import Optional
import psycopg2.errors
import psycopg2.extras
from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, File, Form

import db as db_module
from api import error_codes
from api import storage as cv_storage
from api.deps import get_db, require_role
from api.rate_limit import get_user_id_or_ip, limiter
from api.routers.jobs import get_cv_signed_url as _get_cv_signed_url
from api.schemas import (
    JobApplicationOut,
    SavedJobCreate,
    SavedJobOut,
    SavedJobToggleResult,
)

router = APIRouter(prefix="/me", tags=["me"])


@router.post("/applications", response_model=JobApplicationOut, status_code=201)
@limiter.limit("15/minute", key_func=get_user_id_or_ip)
def apply_to_job(
    request: Request,
    job_id: str = Form(...),
    note: Optional[str] = Form(None),
    cv_file: UploadFile = File(..., description="File PDF CV của học viên (max 5MB)"),
    user: dict = Depends(require_role("user")),
    conn=Depends(get_db),
):
    if not db_module.is_valid_uuid(job_id):
        raise HTTPException(status_code=400, detail={"error_code": error_codes.PROFILE_JOB_ID_INVALID_UUID, "message": f"job_id '{job_id}' không đúng định dạng UUID.", "params": {"value": job_id}})
    
    job = db_module.get_job_by_id(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail={"error_code": error_codes.PROFILE_JOB_NOT_FOUND, "message": "Không tìm thấy job"})
    if job["job_status"] != "OPEN":
        raise HTTPException(
            status_code=400,
            detail={"error_code": error_codes.PROFILE_JOB_STATUS_NOT_APPLICABLE, "message": f"Job đang ở trạng thái '{job['job_status']}', không thể ứng tuyển.", "params": {"value": job['job_status']}},
        )

    # 1. Kiểm tra file PDF
    if not cv_file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail={"error_code": error_codes.PROFILE_CV_FORMAT_INVALID, "message": "Chỉ chấp nhận file CV định dạng .pdf."})
    
    file_bytes = cv_file.file.read()
    if len(file_bytes) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail={"error_code": error_codes.PROFILE_CV_FILE_TOO_LARGE, "message": "Dung lượng file CV tối đa là 5MB."})

    # 2. Tạo bản ghi ban đầu để lấy application_id
    try:
        application_id = db_module.create_job_application(
            conn, ss_user_id=user["sub"], job_id=job_id, note=note, cv_url=None,
        )
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        raise HTTPException(status_code=409, detail={"error_code": error_codes.PROFILE_ALREADY_APPLIED, "message": "Bạn đã ứng tuyển job này rồi."})

    # 3. Upload file lên Supabase Storage
    try:
        cv_path = cv_storage.upload_cv(
            file_bytes=file_bytes,
            user_id=user["sub"],
            application_id=application_id,
        )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE job_applications SET cv_url = %s WHERE application_id = %s",
                (cv_path, application_id),
            )
    except RuntimeError as exc:
        conn.rollback()
        raise HTTPException(status_code=500, detail={"error_code": error_codes.PROFILE_CV_UPLOAD_FAILED, "message": str(exc)})

    # Ghi audit log CÙNG transaction với việc tạo application + upload CV
    # (trước commit) — xem docstring db.log_action(). APPLY_JOB không
    # thuộc log thủ công, không cần note (xem db.ACTION_LOG_RULES).
    # entity_type='APPLICATION' + entity_id=application_id (KHÔNG dùng
    # entity_type='JOB' vì 1 job có nhiều lượt ứng tuyển khác nhau).
    db_module.log_action(
        conn, actor_id=user["sub"], action_type="APPLY_JOB",
        entity_type="APPLICATION", entity_id=application_id,
        entity_label=job["job_title"], company_id=job["company_id"],
    )

    conn.commit()

    applications = db_module.list_applications_for_user(conn, user["sub"])
    return next(a for a in applications if str(a["application_id"]) == application_id)


@router.get("/applications", response_model=list[JobApplicationOut])
def list_my_applications(
    user: dict = Depends(require_role("user")),
    conn=Depends(get_db),
):
    return db_module.list_applications_for_user(conn, user["sub"])


# Route GET /applications/{application_id}/cv-url đã DỜI sang
# GET /jobs/applications/{application_id}/cv-url (api/routers/jobs.py) —
# xem comment ở đó. Phần 5 mục 11 của plan: route này chỉ staff
# (require_role("ss_team")) gọi được, không phải hành động tự phục vụ
# của học viên, nên không nên nằm dưới namespace /me.
#
# ALIAS TƯƠNG THÍCH NGƯỢC (deprecated) — Flask (mindx-jobs/backend_auth.py::
# get_cv_signed_url) vẫn gọi path CŨ /me/applications/{id}/cv-url cho tới
# khi cutover sang Next.js xong; dời route mà không giữ alias thì trang
# chi tiết job/học viên của Flask trả 404 khi staff bấm xem CV, và bắt
# buộc phải deploy backend + Flask ĐÚNG THỨ TỰ cùng lúc. Alias dùng lại
# NGUYÊN hàm ở jobs.py (cùng auth ss_team, cùng rate limit, cùng mã lỗi),
# include_in_schema=False để OpenAPI (nguồn sinh type cho Next.js) chỉ
# thấy path mới. XOÁ khối này khi Flask đã tắt hẳn.
router.add_api_route(
    "/applications/{application_id}/cv-url",
    _get_cv_signed_url,
    methods=["GET"],
    include_in_schema=False,
)


@router.delete("/applications/{job_id}", status_code=204)
def withdraw_application(
    job_id: str,
    note: Optional[str] = Query(
        None,
        description="Lý do huỷ ứng tuyển — học viên tự ghi, không bắt buộc. "
                    "Lưu vào audit_logs.note (WITHDRAW_JOB_APPLICATION) để "
                    "team SS xem lại sau, KHÔNG lưu vào job_applications vì "
                    "record đó bị xoá thật ngay trong request này.",
    ),
    user: dict = Depends(require_role("user")),
    conn=Depends(get_db),
):
    """Huỷ ứng tuyển (thêm 08/2026, xem db.delete_job_application()) —
    học viên chỉ huỷ được đơn của CHÍNH mình (ss_user_id lấy từ JWT,
    không nhận qua path/body, giống mọi route khác trong file này).
    Huỷ xong có thể POST /me/applications lại nếu muốn ứng tuyển lại."""
    if not db_module.is_valid_uuid(job_id):
        raise HTTPException(status_code=400, detail={"error_code": error_codes.PROFILE_JOB_ID_INVALID_UUID, "message": f"job_id '{job_id}' không đúng định dạng UUID.", "params": {"value": job_id}})

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT ja.application_id, ja.cv_url, jp.job_title, jp.company_id
            FROM job_applications ja
            JOIN job_postings jp ON jp.job_id = ja.job_id
            WHERE ja.ss_user_id = %s AND ja.job_id = %s
            """,
            (user["sub"], job_id),
        )
        row = cur.fetchone()

    deleted = db_module.delete_job_application(conn, ss_user_id=user["sub"], job_id=job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail={"error_code": error_codes.PROFILE_NOT_APPLIED_YET, "message": "Bạn chưa ứng tuyển job này."})

    # Ghi audit log CÙNG transaction với việc xoá job_applications (trước
    # commit) — xem docstring db.log_action(). WITHDRAW_JOB_APPLICATION
    # không thuộc log thủ công, không bắt buộc note (xem db.ACTION_LOG_RULES)
    # — nhưng vẫn NHẬN note nếu học viên có ghi lý do huỷ (log_action() tự
    # strip/None-hoá chuỗi rỗng, xem docstring). entity_id vẫn dùng
    # application_id dù record đã bị xoá thật khỏi job_applications —
    # audit_logs.entity_id là snapshot, không có FK ràng buộc tới
    # job_applications (xem migration_add_application_audit_log.sql).
    if row:
        db_module.log_action(
            conn, actor_id=user["sub"], action_type="WITHDRAW_JOB_APPLICATION",
            entity_type="APPLICATION", entity_id=str(row["application_id"]),
            entity_label=row["job_title"], company_id=row["company_id"],
            note=note,
        )

    conn.commit()

    # Dọn dẹp file PDF trên storage
    if row and row.get("cv_url"):
        cv_storage.delete_cv(row["cv_url"])

    return None


@router.post("/saved-jobs", response_model=SavedJobOut, status_code=201)
@limiter.limit("30/minute", key_func=get_user_id_or_ip)
def save_job(
    request: Request,
    payload: SavedJobCreate,
    user: dict = Depends(require_role("user")),
    conn=Depends(get_db),
):
    if not db_module.is_valid_uuid(payload.job_id):
        raise HTTPException(status_code=400, detail={"error_code": error_codes.PROFILE_JOB_ID_INVALID_UUID, "message": f"job_id '{payload.job_id}' không đúng định dạng UUID.", "params": {"value": payload.job_id}})
    if db_module.get_job_by_id(conn, payload.job_id) is None:
        raise HTTPException(status_code=404, detail={"error_code": error_codes.PROFILE_JOB_NOT_FOUND, "message": "Không tìm thấy job"})

    try:
        saved_job_id = db_module.create_saved_job(conn, ss_user_id=user["sub"], job_id=payload.job_id)
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        raise HTTPException(status_code=409, detail={"error_code": error_codes.PROFILE_JOB_ALREADY_SAVED, "message": "Job này đã được lưu rồi"})
    conn.commit()

    saved = db_module.list_saved_jobs_for_user(conn, user["sub"])
    return next(s for s in saved if str(s["saved_job_id"]) == saved_job_id)


# Thêm khi migrate Next.js (Phần 5 mục 9 của plan): trước đây FE phải tự gọi
# POST /saved-jobs, bắt lỗi 409 (đã lưu rồi) rồi mới gọi DELETE
# /saved-jobs/{job_id} để bỏ lưu — 1 nút bấm cần biết trước trạng thái hiện
# tại VÀ xử lý race giữa 2 lần gọi. Route này gộp lại: luôn thử tạo trước
# (INSERT), UniqueViolation (đã lưu từ trước) -> coi là "người dùng muốn bỏ
# lưu", chuyển sang DELETE ngay trong CÙNG request — 1 lần gọi, 1 round-trip,
# không đổi hành vi 2 route POST/DELETE gốc (vẫn giữ nguyên, route mới không
# thay thế route cũ, tránh phá FE nào đang gọi trực tiếp 2 route đó).
@router.post("/saved-jobs/toggle", response_model=SavedJobToggleResult)
@limiter.limit("30/minute", key_func=get_user_id_or_ip)
def toggle_saved_job(
    request: Request,
    payload: SavedJobCreate,
    user: dict = Depends(require_role("user")),
    conn=Depends(get_db),
):
    if not db_module.is_valid_uuid(payload.job_id):
        raise HTTPException(status_code=400, detail={"error_code": error_codes.PROFILE_JOB_ID_INVALID_UUID, "message": f"job_id '{payload.job_id}' không đúng định dạng UUID.", "params": {"value": payload.job_id}})
    if db_module.get_job_by_id(conn, payload.job_id) is None:
        raise HTTPException(status_code=404, detail={"error_code": error_codes.PROFILE_JOB_NOT_FOUND, "message": "Không tìm thấy job"})

    try:
        saved_job_id = db_module.create_saved_job(conn, ss_user_id=user["sub"], job_id=payload.job_id)
    except psycopg2.errors.UniqueViolation:
        # Đã lưu từ trước -> ý định thật sự của toggle lúc này là bỏ lưu.
        conn.rollback()
        db_module.delete_saved_job(conn, ss_user_id=user["sub"], job_id=payload.job_id)
        conn.commit()
        return SavedJobToggleResult(saved=False, data=None)

    conn.commit()
    saved = db_module.list_saved_jobs_for_user(conn, user["sub"])
    saved_row = next(s for s in saved if str(s["saved_job_id"]) == saved_job_id)
    return SavedJobToggleResult(saved=True, data=saved_row)


@router.get("/saved-jobs", response_model=list[SavedJobOut])
def list_my_saved_jobs(
    user: dict = Depends(require_role("user")),
    conn=Depends(get_db),
):
    return db_module.list_saved_jobs_for_user(conn, user["sub"])


@router.delete("/saved-jobs/{job_id}", status_code=204)
def unsave_job(
    job_id: str,
    user: dict = Depends(require_role("user")),
    conn=Depends(get_db),
):
    if not db_module.is_valid_uuid(job_id):
        raise HTTPException(status_code=400, detail={"error_code": error_codes.PROFILE_JOB_ID_INVALID_UUID, "message": f"job_id '{job_id}' không đúng định dạng UUID.", "params": {"value": job_id}})

    deleted = db_module.delete_saved_job(conn, ss_user_id=user["sub"], job_id=job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail={"error_code": error_codes.PROFILE_JOB_NOT_SAVED, "message": "Job này chưa được lưu"})
    conn.commit()
    return None
