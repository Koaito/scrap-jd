"""
Router quản trị tài khoản người dùng (admin/ss_team) — xem docstring
api/security.py và sql/migration_add_auth.sql để hiểu toàn bộ thiết kế
trước khi đọc file này.

Tách ra từ api/routers/auth.py (08/2026) — xem docstring auth.py
(facade) và auth_session.py để biết lý do tách 738 dòng/14 endpoint
thành 3 file theo domain. File này chứa các route "staff quản lý tài
khoản NGƯỜI KHÁC": tạo tài khoản, liệt kê, xem đơn ứng tuyển/job đã
lưu của 1 user, đổi role, khoá/mở khoá — khác auth_session.py (mỗi
người tự quản lý phiên/mật khẩu CỦA CHÍNH MÌNH).

Tất cả route ở đây đều yêu cầu ít nhất require_role("ss_team"), phần
lớn require_admin — không có route công khai nào.
"""

from fastapi import APIRouter, Depends, HTTPException

import db as db_module
from api import security
from api.deps import require_admin, require_role, get_db
from api.schemas import (
    JobApplicationOut, SavedJobOut, UserActiveStatusUpdate,
    UserCreateByAdmin, UserCreatedOut, UserOut, UserRoleUpdate,
)

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/users", response_model=UserCreatedOut, status_code=201)
def create_user(
    payload: UserCreateByAdmin,
    admin: dict = Depends(require_admin),
    conn=Depends(get_db),
):
    """CHỈ admin gọi được (require_admin). Mật khẩu TẠM được server tự
    sinh, trả về ĐÚNG 1 LẦN trong response này — admin tự đưa cho người
    dùng qua kênh khác (Slack/nói miệng), KHÔNG có luồng gửi email (xem
    README.md mục Auth). Tài khoản mới luôn must_change_password=True,
    bắt đổi mật khẩu ngay lần đăng nhập đầu."""
    if payload.role not in ("user", "ss_team", "admin"):
        raise HTTPException(
            status_code=400,
            detail="role phải là 1 trong: user, ss_team, admin.",
        )

    existing = db_module.get_user_by_email(conn, payload.email)
    if existing is not None:
        raise HTTPException(status_code=409, detail="Email này đã có tài khoản.")

    temp_password = security.generate_temp_password()
    ss_user_id = db_module.create_user(
        conn,
        full_name=payload.full_name,
        email=payload.email,
        password_hash=security.hash_password(temp_password),
        role=payload.role,
        must_change_password=True,
    )
    conn.commit()

    row = db_module.get_user_by_id(conn, ss_user_id)
    return {**row, "temp_password": temp_password}


@router.get("/users", response_model=list[UserOut])
def list_users(
    user: dict = Depends(require_role("ss_team")),
    conn=Depends(get_db),
):
    """Danh sách toàn bộ tài khoản (thêm 08/2026) — ss_team trở lên xem
    được (khác POST /auth/users tạo tài khoản, vẫn admin-only), dùng cho
    mục "xem danh sách tài khoản" trong dashboard ss_team đã thống nhất."""
    return db_module.list_users(conn)


@router.get("/users/{ss_user_id}/applications", response_model=list[JobApplicationOut])
def list_applications_of_user(
    ss_user_id: str,
    user: dict = Depends(require_role("ss_team")),
    conn=Depends(get_db),
):
    """Thêm 08/2026 — chiều "1 học viên đã ứng tuyển job nào", để bổ
    sung cho GET /jobs/{job_id}/applications (chiều ngược lại, "1 job
    có ai ứng tuyển") đã có sẵn — SS team/admin cần cả 2 chiều để theo
    dõi hoạt động ứng tuyển/lưu job của học viên. Tái dùng thẳng
    db.list_applications_for_user() — hàm này vốn dùng cho GET
    /me/applications (học viên xem đơn của CHÍNH MÌNH, ss_user_id lấy
    từ JWT); ở đây staff truyền ss_user_id của NGƯỜI KHÁC qua path,
    response_model JobApplicationOut giống hệt vì cùng là "xem job nào
    kèm job_title/job_status/company_name", không cần full_name/email
    của chính học viên đó (staff đã biết đang xem ai qua ss_user_id)."""
    if not db_module.is_valid_uuid(ss_user_id):
        raise HTTPException(status_code=400, detail=f"ss_user_id '{ss_user_id}' không đúng định dạng UUID.")
    if db_module.get_user_by_id(conn, ss_user_id) is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản.")

    return db_module.list_applications_for_user(conn, ss_user_id)


@router.get("/users/{ss_user_id}/saved-jobs", response_model=list[SavedJobOut])
def list_saved_jobs_of_user(
    ss_user_id: str,
    user: dict = Depends(require_role("ss_team")),
    conn=Depends(get_db),
):
    """Thêm 08/2026 — mirror ĐÚNG list_applications_of_user() ở trên
    nhưng cho chiều "lưu" thay vì "ứng tuyển": 1 học viên đã lưu
    (bookmark) job nào, để bổ sung cho GET /jobs/{job_id}/saved-jobs
    (chiều "1 job có ai lưu") — xem docstring 2 route đó và
    db.list_saved_jobs_for_job() để biết lý do đảo ngược quyết định
    "saved_jobs riêng tư 100%" ban đầu. Tái dùng thẳng
    db.list_saved_jobs_for_user() (vốn dùng cho GET /me/saved-jobs)."""
    if not db_module.is_valid_uuid(ss_user_id):
        raise HTTPException(status_code=400, detail=f"ss_user_id '{ss_user_id}' không đúng định dạng UUID.")
    if db_module.get_user_by_id(conn, ss_user_id) is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản.")

    return db_module.list_saved_jobs_for_user(conn, ss_user_id)


@router.patch("/users/{ss_user_id}/role", response_model=UserOut)
def update_user_role(
    ss_user_id: str,
    payload: UserRoleUpdate,
    admin: dict = Depends(require_admin),
    conn=Depends(get_db),
):
    """CHỈ admin gọi được. Đổi role của 1 user khác — CHẶN admin tự đổi
    role CHÍNH MÌNH (tránh tự khoá mình khỏi quyền admin do bấm nhầm;
    muốn đổi role của chính mình thì nhờ admin khác, hoặc sửa thẳng
    trong DB nếu là admin duy nhất — xem lịch sử trao đổi trước khi
    code phần này)."""
    if payload.role not in ("user", "ss_team", "admin"):
        raise HTTPException(
            status_code=400,
            detail="role phải là 1 trong: user, ss_team, admin.",
        )
    if ss_user_id == admin["sub"]:
        raise HTTPException(
            status_code=400,
            detail="Không thể tự đổi role của chính mình — nhờ admin "
                   "khác thực hiện thao tác này.",
        )

    updated = db_module.update_user_role(conn, ss_user_id, payload.role)
    if not updated:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản.")
    conn.commit()

    return db_module.get_user_by_id(conn, ss_user_id)


@router.patch("/users/{ss_user_id}/active-status", response_model=UserOut)
def update_user_active_status(
    ss_user_id: str,
    payload: UserActiveStatusUpdate,
    admin: dict = Depends(require_admin),
    conn=Depends(get_db),
):
    """CHỈ admin gọi được. Khoá/mở khoá VĨNH VIỄN 1 tài khoản khác —
    CHẶN admin tự khoá CHÍNH MÌNH (cùng lý do với update_user_role() ở
    trên — tránh tự khoá mình khỏi hệ thống do bấm nhầm, đặc biệt nguy
    hiểm hơn tự đổi role vì is_active=false chặn đăng nhập hoàn toàn,
    không có role nào cứu được).

    Dùng khi 1 người rời nhóm/vi phạm cần chặn đăng nhập ngay — KHÁC
    locked_until (khoá TẠM THỜI, tự hết hạn do sai mật khẩu liên tiếp,
    xem db.record_failed_login()). Vô hiệu hoá không revoke JWT access
    token đang có hiệu lực (tối đa 30 phút) — xem docstring
    db.update_user_active_status()."""
    if ss_user_id == admin["sub"]:
        raise HTTPException(
            status_code=400,
            detail="Không thể tự vô hiệu hoá/kích hoạt chính mình — nhờ "
                   "admin khác thực hiện thao tác này.",
        )

    updated = db_module.update_user_active_status(conn, ss_user_id, payload.is_active)
    if not updated:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản.")
    conn.commit()

    return db_module.get_user_by_id(conn, ss_user_id)
