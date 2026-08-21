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
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile, File, Form

import db as db_module
from api import storage as cv_storage
from api.deps import get_db, require_role
from api.rate_limit import get_user_id_or_ip, limiter
from api.schemas import (
    JobApplicationOut,
    SavedJobCreate,
    SavedJobOut,
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
        raise HTTPException(status_code=400, detail=f"job_id '{job_id}' không đúng định dạng UUID.")
    
    job = db_module.get_job_by_id(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")
    if job["job_status"] != "OPEN":
        raise HTTPException(
            status_code=400,
            detail=f"Job đang ở trạng thái '{job['job_status']}', không thể ứng tuyển.",
        )

    # 1. Kiểm tra file PDF
    if not cv_file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Chỉ chấp nhận file CV định dạng .pdf.")
    
    file_bytes = cv_file.file.read()
    if len(file_bytes) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Dung lượng file CV tối đa là 5MB.")

    # 2. Tạo bản ghi ban đầu để lấy application_id
    try:
        application_id = db_module.create_job_application(
            conn, ss_user_id=user["sub"], job_id=job_id, note=note, cv_url=None,
        )
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        raise HTTPException(status_code=409, detail="Bạn đã ứng tuyển job này rồi.")

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
        raise HTTPException(status_code=500, detail=str(exc))

    conn.commit()

    applications = db_module.list_applications_for_user(conn, user["sub"])
    return next(a for a in applications if str(a["application_id"]) == application_id)


@router.get("/applications", response_model=list[JobApplicationOut])
def list_my_applications(
    user: dict = Depends(require_role("user")),
    conn=Depends(get_db),
):
    return db_module.list_applications_for_user(conn, user["sub"])


@router.get("/applications/{application_id}/cv-url")
def get_cv_signed_url(
    application_id: str,
    user: dict = Depends(require_role("ss_team")),  # Chỉ Staff / Admin mới có quyền lấy
    conn=Depends(get_db),
):
    """Staff lấy Signed URL để tải và xem CV học viên."""
    if not db_module.is_valid_uuid(application_id):
        raise HTTPException(status_code=400, detail="application_id không hợp lệ.")
    
    with conn.cursor() as cur:
        cur.execute("SELECT cv_url FROM job_applications WHERE application_id = %s", (application_id,))
        row = cur.fetchone()
    
    if not row or not row["cv_url"]:
        raise HTTPException(status_code=404, detail="Học viên chưa nộp CV cho đơn này.")
    
    signed_url = cv_storage.get_signed_url(row["cv_url"])
    if not signed_url:
        raise HTTPException(status_code=500, detail="Không thể tạo link tải file lúc này.")
    
    return {"signed_url": signed_url}


<<<<<<< HEAD
=======
@router.delete("/applications/{job_id}", status_code=204)
def withdraw_application(
    job_id: str,
    user: dict = Depends(require_role("user")),
    conn=Depends(get_db),
):
    """Huỷ ứng tuyển (thêm 08/2026, xem db.delete_job_application()) —
    học viên chỉ huỷ được đơn của CHÍNH mình (ss_user_id lấy từ JWT,
    không nhận qua path/body, giống mọi route khác trong file này).
    Huỷ xong có thể POST /me/applications lại nếu muốn ứng tuyển lại."""
    if not db_module.is_valid_uuid(job_id):
        raise HTTPException(status_code=400, detail=f"job_id '{job_id}' không đúng định dạng UUID.")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT cv_url FROM job_applications WHERE ss_user_id = %s AND job_id = %s",
            (user["sub"], job_id),
        )
        row = cur.fetchone()

    deleted = db_module.delete_job_application(conn, ss_user_id=user["sub"], job_id=job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Bạn chưa ứng tuyển job này.")
    conn.commit()

    # Dọn dẹp file PDF trên storage
    if row and row.get("cv_url"):
        cv_storage.delete_cv(row["cv_url"])

>>>>>>> 7d96241e0ac0b305d464549c4c8db02d84509e1e
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
        raise HTTPException(status_code=400, detail=f"job_id '{payload.job_id}' không đúng định dạng UUID.")
    if db_module.get_job_by_id(conn, payload.job_id) is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy job")

    try:
        saved_job_id = db_module.create_saved_job(conn, ss_user_id=user["sub"], job_id=payload.job_id)
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        raise HTTPException(status_code=409, detail="Job này đã được lưu rồi")
    conn.commit()

    saved = db_module.list_saved_jobs_for_user(conn, user["sub"])
    return next(s for s in saved if str(s["saved_job_id"]) == saved_job_id)


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
        raise HTTPException(status_code=400, detail=f"job_id '{job_id}' không đúng định dạng UUID.")

    deleted = db_module.delete_saved_job(conn, ss_user_id=user["sub"], job_id=job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Job này chưa được lưu")
    conn.commit()
    return None
