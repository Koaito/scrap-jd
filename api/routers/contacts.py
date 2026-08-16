"""
CRUD company_contacts (HR contact) — thêm 08/2026 (Phần 1 phân quyền).
Bảng đã có sẵn từ schema.sql gốc (dùng nội bộ qua db.merge_companies()
khi gộp company trùng), đây là lần đầu lộ ra API.

TOÀN BỘ route ở đây yêu cầu require_role("ss_team") — khác /jobs, /companies
(GET công khai cho mọi role đã đăng nhập), vì HR contact là thông tin
nhạy cảm (email/SĐT cá nhân của người liên hệ), 'user' KHÔNG được thấy
theo đúng thiết kế 3 role đã thống nhất (xem lịch sử trao đổi).
"""

from fastapi import APIRouter, Depends, HTTPException, Query

import db as db_module
from api.deps import get_db, require_role
from api.schemas import CompanyContactCreate, CompanyContactOut, CompanyContactUpdate

router = APIRouter(prefix="/companies/{company_id}/contacts", tags=["contacts"])

_VALID_CONTACT_STATUS = {"UNCONTACTED", "EMAIL_SENT", "RESPONDED", "IN_PARTNERSHIP"}


@router.get("", response_model=list[CompanyContactOut])
def list_contacts(
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

    contact_id = db_module.create_company_contact(
        conn,
        company_id=company_id,
        contact_name=payload.contact_name,
        job_title=payload.job_title,
        work_email=payload.work_email,
        social_link=payload.social_link,
        phone_number=payload.phone_number,
        found_source=payload.found_source,
        created_by=user["sub"],
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
    conn.commit()

    return db_module.get_company_contact_by_id(conn, contact_id)


@router.delete("/{contact_id}", status_code=204)
def delete_contact(
    company_id: str,
    contact_id: str,
    user: dict = Depends(require_role("ss_team")),
    conn=Depends(get_db),
):
    """Xoá MỀM (is_active=false) — KHÔNG xoá thật, giữ lịch sử liên hệ
    (xem sql/migration_add_role_hierarchy.sql). Gọi lại nhiều lần trên
    cùng 1 contact đã ẩn vẫn trả 204, không lỗi."""
    if not db_module.is_valid_uuid(contact_id):
        raise HTTPException(status_code=400, detail=f"contact_id '{contact_id}' không đúng định dạng UUID.")

    existing = db_module.get_company_contact_by_id(conn, contact_id)
    if existing is None or str(existing["company_id"]) != company_id:
        raise HTTPException(status_code=404, detail="Không tìm thấy contact thuộc công ty này")

    db_module.soft_delete_company_contact(conn, contact_id, updated_by=user["sub"])
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
