"""
CRUD company_contacts (HR contact) — thêm 08/2026 (Phần 1 phân quyền).
Bảng đã có sẵn từ schema.sql gốc (dùng nội bộ qua db.merge_companies()
khi gộp company trùng), đây là lần đầu lộ ra API.

TOÀN BỘ route ở đây yêu cầu require_role("ss_team") — khác /jobs, /companies
(GET công khai cho mọi role đã đăng nhập), vì HR contact là thông tin
nhạy cảm (email/SĐT cá nhân của người liên hệ), 'user' KHÔNG được thấy
theo đúng thiết kế 3 role đã thống nhất (xem lịch sử trao đổi).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import db as db_module
from api.deps import ROLE_HIERARCHY, get_db, require_role
from api.rate_limit import get_user_id_or_ip, limiter
from api.schemas import (
    CompanyContactCreate,
    CompanyContactOut,
    CompanyContactWithCompanyOut,
    CompanyContactUpdate,
    ContactAssignUpdate,
    ContactDeleteRequest,
)

router = APIRouter(prefix="/companies/{company_id}/contacts", tags=["contacts"])

# Router riêng, KHÔNG có company_id trong prefix — cho GET /contacts (danh
# sách gộp mọi công ty). Tách router thay vì nhét route "" trần vào router
# trên vì FastAPI/APIRouter gắn prefix cố định cho toàn bộ router đó, không
# thể có 1 route trong cùng router "thoát" khỏi {company_id} trong path.
all_contacts_router = APIRouter(prefix="/contacts", tags=["contacts"])

_VALID_CONTACT_STATUS = {"UNCONTACTED", "EMAIL_SENT", "RESPONDED", "IN_PARTNERSHIP"}


def _validate_assignee(conn, assigned_ss_user: str) -> None:
    """Kiểm tra assigned_ss_user là 1 ss_user_id tồn tại thật VÀ có role
    ss_team hoặc admin — dùng chung cho create_contact() và
    assign_contact() bên dưới. Không cho gán contact cho role 'user'
    (học viên): 'phụ trách contact' là khái niệm nội bộ team SS, học
    viên không có quyền/khái niệm này trong hệ thống.

    Raise HTTPException 400 nếu không phải UUID hợp lệ, 404 nếu không
    tìm thấy user, 422 nếu tìm thấy nhưng role không đủ — 3 mã lỗi khác
    nhau để frontend phân biệt được nguyên nhân chính xác thay vì gộp
    chung 1 lỗi mơ hồ."""
    if not db_module.is_valid_uuid(assigned_ss_user):
        raise HTTPException(status_code=400, detail=f"assigned_ss_user '{assigned_ss_user}' không đúng định dạng UUID.")
    target_user = db_module.get_user_by_id(conn, assigned_ss_user)
    if target_user is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy tài khoản assigned_ss_user.")
    if ROLE_HIERARCHY.get(target_user.get("role"), -1) < ROLE_HIERARCHY["ss_team"]:
        raise HTTPException(
            status_code=422,
            detail="assigned_ss_user phải là tài khoản có role 'ss_team' hoặc 'admin' — "
                   "không thể giao contact cho tài khoản role 'user' (học viên).",
        )


@all_contacts_router.get("", response_model=list[CompanyContactWithCompanyOut])
@limiter.limit("60/minute", key_func=get_user_id_or_ip)
def list_all_contacts(
    request: Request,
    include_inactive: bool = Query(
        False, description="true = xem cả contact đã xoá mềm (lịch sử liên hệ cũ)"
    ),
    contact_status: str | None = Query(
        None, description="Lọc theo trạng thái: UNCONTACTED | EMAIL_SENT | RESPONDED | IN_PARTNERSHIP"
    ),
    company_id: str | None = Query(None, description="Lọc theo 1 công ty cụ thể"),
    search: str | None = Query(None, description="Tìm theo tên contact (khớp 1 phần, không phân biệt hoa/thường)"),
    created_by: str | None = Query(
        None, description="Lọc contact do 1 thành viên ss_team/admin cụ thể TỰ THÊM (ss_user_id)"
    ),
    assigned_ss_user: str | None = Query(
        None, description="Lọc contact đang được GIAO cho 1 thành viên cụ thể phụ trách (ss_user_id) — độc lập với created_by"
    ),
    user: dict = Depends(require_role("ss_team")),
    conn=Depends(get_db),
):
    """Danh sách contact GỘP TẤT CẢ công ty, kèm company_name — dùng cho
    trang "Danh sách contact" tổng hợp ở frontend. Cùng require_role
    ("ss_team") như mọi route contact khác trong file này vì đây vẫn là
    dữ liệu nhạy cảm (email/SĐT cá nhân).

    Rate limit 60/minute theo user_id (thêm 08/2026, key_func=
    get_user_id_or_ip giống /me/*) — không theo IP mặc định vì route
    này chỉ nội bộ ss_team, số lượng nhân viên ít nhưng có thể ngồi
    chung văn phòng/mạng, khoá theo IP sẽ khiến cả phòng share chung 1
    hạn mức."""
    if contact_status is not None and contact_status not in _VALID_CONTACT_STATUS:
        raise HTTPException(
            status_code=400,
            detail=f"contact_status '{contact_status}' không hợp lệ — "
                   f"có sẵn: {sorted(_VALID_CONTACT_STATUS)}",
        )
    if company_id is not None:
        if not db_module.is_valid_uuid(company_id):
            raise HTTPException(status_code=400, detail=f"company_id '{company_id}' không đúng định dạng UUID.")
        if db_module.get_company_by_id(conn, company_id) is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy công ty")
    if created_by is not None and not db_module.is_valid_uuid(created_by):
        raise HTTPException(status_code=400, detail=f"created_by '{created_by}' không đúng định dạng UUID.")
    if assigned_ss_user is not None and not db_module.is_valid_uuid(assigned_ss_user):
        raise HTTPException(status_code=400, detail=f"assigned_ss_user '{assigned_ss_user}' không đúng định dạng UUID.")

    return db_module.list_all_contacts(
        conn,
        include_inactive=include_inactive,
        contact_status=contact_status,
        company_id=company_id,
        search=search,
        created_by=created_by,
        assigned_ss_user=assigned_ss_user,
    )


@router.get("", response_model=list[CompanyContactOut])
@limiter.limit("60/minute", key_func=get_user_id_or_ip)
def list_contacts(
    request: Request,
    company_id: str,
    include_inactive: bool = Query(
        False, description="true = xem cả contact đã xoá mềm (lịch sử liên hệ cũ)"
    ),
    user: dict = Depends(require_role("ss_team")),
    conn=Depends(get_db),
):
    if not db_module.is_valid_uuid(company_id):
        raise HTTPException(status_code=400, detail=f"company_id '{company_id}' không đúng định dạng UUID.")
    if db_module.get_company_by_id(conn, company_id) is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy công ty")

    return db_module.list_company_contacts(conn, company_id, include_inactive=include_inactive)


@router.post("", response_model=CompanyContactOut, status_code=201)
def create_contact(
    company_id: str,
    payload: CompanyContactCreate,
    user: dict = Depends(require_role("ss_team")),
    conn=Depends(get_db),
):
    if not db_module.is_valid_uuid(company_id):
        raise HTTPException(status_code=400, detail=f"company_id '{company_id}' không đúng định dạng UUID.")
    if db_module.get_company_by_id(conn, company_id) is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy công ty")

    if payload.assigned_ss_user is not None:
        _validate_assignee(conn, payload.assigned_ss_user)

    contact_id = db_module.create_company_contact(
        conn,
        company_id=company_id,
        contact_name=payload.contact_name,
        job_title=payload.job_title,
        work_email=payload.work_email,
        social_link=payload.social_link,
        phone_number=payload.phone_number,
        found_source=payload.found_source,
        assigned_ss_user=payload.assigned_ss_user,
        created_by=user["sub"],
    )

    # CREATE_CONTACT — note TUỲ CHỌN (khác sửa/xoá/gán contact bên dưới),
    # xem db.ACTION_LOG_RULES. Vẫn thuộc log thủ công (is_manual_log=true)
    # vì đây là thao tác HR contact — "bất kỳ thao tác nào bên HR contact
    # đều phải nằm ở log thủ công".
    db_module.log_action(
        conn, actor_id=user["sub"], action_type="CREATE_CONTACT",
        entity_type="CONTACT", entity_id=contact_id,
        entity_label=payload.contact_name, company_id=company_id,
        note=payload.note,
    )

    conn.commit()

    return db_module.get_company_contact_by_id(conn, contact_id)


@router.patch("/{contact_id}", response_model=CompanyContactOut)
def update_contact(
    company_id: str,
    contact_id: str,
    payload: CompanyContactUpdate,
    user: dict = Depends(require_role("ss_team")),
    conn=Depends(get_db),
):
    if not db_module.is_valid_uuid(contact_id):
        raise HTTPException(status_code=400, detail=f"contact_id '{contact_id}' không đúng định dạng UUID.")

    existing = db_module.get_company_contact_by_id(conn, contact_id)
    if existing is None or str(existing["company_id"]) != company_id:
        raise HTTPException(status_code=404, detail="Không tìm thấy contact thuộc công ty này")

    if payload.contact_status is not None and payload.contact_status not in _VALID_CONTACT_STATUS:
        raise HTTPException(
            status_code=400,
            detail=f"contact_status '{payload.contact_status}' không hợp lệ — "
                   f"có sẵn: {sorted(_VALID_CONTACT_STATUS)}",
        )

    # CHẶN CỨNG: sửa HR contact bắt buộc note NẾU thực sự có field nào
    # đổi giá trị (xem db.ACTION_LOG_RULES, action UPDATE_CONTACT) —
    # kiểm tra TRƯỚC KHI gọi update_company_contact(), không cho thao
    # tác chính chạy nếu thiếu note.
    payload_fields = payload.model_dump(exclude_unset=True, exclude={"note"})
    changes = db_module.diff_changed_fields(existing, payload_fields) if payload_fields else {}
    if changes and not (payload.note or "").strip():
        raise HTTPException(
            status_code=422,
            detail="Sửa HR contact bắt buộc phải có 'note' giải thích lý do sửa "
                   "(field 'note' trong body) — các ss_team khác cần biết vì sao "
                   "thông tin contact này thay đổi.",
        )

    updated = db_module.update_company_contact(
        conn, contact_id,
        contact_name=payload.contact_name,
        job_title=payload.job_title,
        work_email=payload.work_email,
        social_link=payload.social_link,
        phone_number=payload.phone_number,
        contact_status=payload.contact_status,
        last_contacted_date=payload.last_contacted_date,
        updated_by=user["sub"],
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Không tìm thấy contact")

    if changes:
        db_module.log_action(
            conn, actor_id=user["sub"], action_type="UPDATE_CONTACT",
            entity_type="CONTACT", entity_id=contact_id,
            entity_label=existing["contact_name"], company_id=company_id,
            changes=changes, note=payload.note,
        )

    conn.commit()

    return db_module.get_company_contact_by_id(conn, contact_id)


@router.patch("/{contact_id}/assign", response_model=CompanyContactOut)
def assign_contact(
    company_id: str,
    contact_id: str,
    payload: ContactAssignUpdate,
    user: dict = Depends(require_role("ss_team")),
    conn=Depends(get_db),
):
    """Gán (hoặc bỏ gán, khi assigned_ss_user=null trong body) người
    phụ trách 1 contact — route RIÊNG khỏi PATCH /{contact_id} thường
    (xem docstring ContactAssignUpdate trong api/schemas.py để hiểu vì
    sao tách route thay vì gộp field vào CompanyContactUpdate: pattern
    "field != None mới ghi đè" của route update thường sẽ không cho
    phép bỏ gán về NULL một cách tường minh)."""
    if not db_module.is_valid_uuid(contact_id):
        raise HTTPException(status_code=400, detail=f"contact_id '{contact_id}' không đúng định dạng UUID.")

    existing = db_module.get_company_contact_by_id(conn, contact_id)
    if existing is None or str(existing["company_id"]) != company_id:
        raise HTTPException(status_code=404, detail="Không tìm thấy contact thuộc công ty này")

    if payload.assigned_ss_user is not None:
        _validate_assignee(conn, payload.assigned_ss_user)

    # So sánh str() vì existing["assigned_ss_user"] là UUID object (từ
    # psycopg2) còn payload.assigned_ss_user là str/None từ Pydantic.
    is_change = str(existing.get("assigned_ss_user")) != str(payload.assigned_ss_user)

    # CHẶN CỨNG: gán/đổi/bỏ gán contact bắt buộc note NẾU thực sự đổi
    # người phụ trách (xem db.ACTION_LOG_RULES, action ASSIGN_CONTACT).
    if is_change and not (payload.note or "").strip():
        raise HTTPException(
            status_code=422,
            detail="Gán/đổi/bỏ gán người phụ trách HR contact bắt buộc phải có "
                   "'note' giải thích lý do — các ss_team khác cần biết vì sao.",
        )

    db_module.assign_company_contact(
        conn, contact_id,
        assigned_ss_user=payload.assigned_ss_user,
        updated_by=user["sub"],
    )

    if is_change:
        db_module.log_action(
            conn, actor_id=user["sub"], action_type="ASSIGN_CONTACT",
            entity_type="CONTACT", entity_id=contact_id,
            entity_label=existing["contact_name"], company_id=company_id,
            changes={"assigned_ss_user": {
                "old": existing.get("assigned_ss_user"),
                "new": payload.assigned_ss_user,
            }},
            note=payload.note,
        )

    conn.commit()

    return db_module.get_company_contact_by_id(conn, contact_id)


@router.delete("/{contact_id}", status_code=204)
def delete_contact(
    company_id: str,
    contact_id: str,
    payload: ContactDeleteRequest,
    user: dict = Depends(require_role("ss_team")),
    conn=Depends(get_db),
):
    """Xoá MỀM (is_active=false) — KHÔNG xoá thật, giữ lịch sử liên hệ
    (xem sql/migration_add_role_hierarchy.sql). Gọi lại nhiều lần trên
    cùng 1 contact đã ẩn vẫn trả 204, không lỗi (nhưng KHÔNG ghi thêm
    log mới lần thứ 2 trở đi — xem is_change bên dưới).

    note BẮT BUỘC (thêm 08/2026, xem db.ACTION_LOG_RULES — action
    DELETE_CONTACT thuộc nhóm CHẶN CỨNG, khác mọi route DELETE khác
    trong API này) — thiếu note -> 422 ngay từ Pydantic
    (ContactDeleteRequest.note không có default), KHÔNG chạm tới DB."""
    if not db_module.is_valid_uuid(contact_id):
        raise HTTPException(status_code=400, detail=f"contact_id '{contact_id}' không đúng định dạng UUID.")

    existing = db_module.get_company_contact_by_id(conn, contact_id)
    if existing is None or str(existing["company_id"]) != company_id:
        raise HTTPException(status_code=404, detail="Không tìm thấy contact thuộc công ty này")

    is_change = existing["is_active"]
    db_module.soft_delete_company_contact(conn, contact_id, updated_by=user["sub"])

    if is_change:
        db_module.log_action(
            conn, actor_id=user["sub"], action_type="DELETE_CONTACT",
            entity_type="CONTACT", entity_id=contact_id,
            entity_label=existing["contact_name"], company_id=company_id,
            note=payload.note,
        )

    conn.commit()
    return None


@router.delete("/{contact_id}/hard", status_code=204)
def hard_delete_contact(
    company_id: str,
    contact_id: str,
    user: dict = Depends(require_role("ss_team")),
    conn=Depends(get_db),
):
    """Xoá THẬT — chỉ dùng làm bước 2, sau khi contact ĐÃ soft-delete
    (is_active=false) qua DELETE /{contact_id} ở trên (thiết kế 2 bước,
    xem lịch sử trao đổi 08/2026): staff xoá mềm trước để xác nhận,
    route này chỉ để dọn hẳn contact rác/trùng/nhập nhầm không còn cần
    giữ lịch sử — KHÔNG dùng để xoá nhanh 1 bước như DELETE thường.

    409 nếu contact CHƯA soft-delete (is_active vẫn true) — ép staff đi
    đúng luồng 2 bước qua UI, tránh xoá cứng nhầm 1 phát mất luôn dữ
    liệu không có đường lấy lại.

    409 nếu contact đang có job_contact_links (đã từng gắn với job cụ
    thể) — xoá thật sẽ mất lịch sử liên hệ theo job đó / vỡ FK, xem
    docstring ContactHasLinksError ở db.py. Trường hợp này contact vẫn
    giữ nguyên trạng thái xoá mềm sau khi gọi route này."""
    if not db_module.is_valid_uuid(contact_id):
        raise HTTPException(status_code=400, detail=f"contact_id '{contact_id}' không đúng định dạng UUID.")

    existing = db_module.get_company_contact_by_id(conn, contact_id)
    if existing is None or str(existing["company_id"]) != company_id:
        raise HTTPException(status_code=404, detail="Không tìm thấy contact thuộc công ty này")

    if existing["is_active"]:
        raise HTTPException(
            status_code=409,
            detail="Contact vẫn đang active — cần xoá mềm (DELETE /companies/{company_id}/contacts/{contact_id}) trước khi xoá cứng.",
        )

    try:
        db_module.hard_delete_company_contact(conn, contact_id)
    except db_module.ContactHasLinksError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    conn.commit()
    return None
