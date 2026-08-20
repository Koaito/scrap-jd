"""
Router GET/PATCH cho audit_logs — lịch sử thao tác ss_team/admin trên
JD, company, HR contact (08/2026, xem db.py mục "AUDIT LOGS" +
sql/migration_add_audit_logs.sql cho toàn bộ lý do thiết kế).

CHỈ 1 route GET, phân biệt "log tự động" / "log thủ công" bằng query
param `view` — KHÔNG phải 2 route/2 bảng riêng (xem docstring
list_audit_logs bên dưới).

TOÀN BỘ route yêu cầu require_role("ss_team") — cùng lý do với
/companies/{id}/contacts (dữ liệu nội bộ team, 'user'/học viên không
có khái niệm này).
"""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

import db as db_module
from api.deps import get_db, require_role
from api.schemas import AuditLogNoteUpdate, AuditLogOut, PaginatedAuditLogs

router = APIRouter(prefix="/audit-logs", tags=["audit-logs"])

_VALID_ACTION_TYPES = {
    "CREATE_JOB", "UPDATE_JOB", "DELETE_JOB",
    "CREATE_COMPANY", "UPDATE_COMPANY", "DELETE_COMPANY",
    "CREATE_CONTACT", "UPDATE_CONTACT", "DELETE_CONTACT", "ASSIGN_CONTACT",
}
_VALID_ENTITY_TYPES = {"JOB", "COMPANY", "CONTACT"}


@router.get("", response_model=PaginatedAuditLogs)
def list_audit_logs(
    view: Literal["auto", "manual"] = Query(
        "auto",
        description="'auto' = TẤT CẢ thao tác (không note). 'manual' = chỉ tập "
                    "con action nhạy cảm (sửa/xoá JD, sửa/xoá company, mọi thao "
                    "tác HR contact), kèm cột note. Đây là 2 CÁCH LỌC trên CÙNG "
                    "1 bảng dữ liệu — 'manual' luôn là tập con của 'auto', "
                    "KHÔNG phải dữ liệu tách biệt.",
    ),
    entity_type: Optional[str] = Query(None, description="JOB | COMPANY | CONTACT"),
    company_id: Optional[str] = Query(None, description="Lọc mọi hoạt động (JD + HR contact) liên quan 1 công ty cụ thể"),
    actor_id: Optional[str] = Query(None, description="Lọc log do 1 thành viên ss_team/admin cụ thể thực hiện"),
    action_type: Optional[str] = Query(
        None,
        description="CREATE_JOB | UPDATE_JOB | DELETE_JOB | CREATE_COMPANY | "
                    "UPDATE_COMPANY | DELETE_COMPANY | CREATE_CONTACT | "
                    "UPDATE_CONTACT | DELETE_CONTACT | ASSIGN_CONTACT",
    ),
    pending_note: Optional[bool] = Query(
        None,
        description="true = CHỈ log đang chờ note (note_required=true, note "
                    "còn trống) — dùng cho badge nhắc nhở. Chỉ có ý nghĩa khi "
                    "view=manual (view=auto luôn bỏ qua tham số này vì log tự "
                    "động không có khái niệm note).",
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_role("ss_team")),
    conn=Depends(get_db),
):
    if entity_type is not None and entity_type not in _VALID_ENTITY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"entity_type '{entity_type}' không hợp lệ — có sẵn: {sorted(_VALID_ENTITY_TYPES)}",
        )
    if action_type is not None and action_type not in _VALID_ACTION_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"action_type '{action_type}' không hợp lệ — có sẵn: {sorted(_VALID_ACTION_TYPES)}",
        )
    if company_id is not None and not db_module.is_valid_uuid(company_id):
        raise HTTPException(status_code=400, detail=f"company_id '{company_id}' không đúng định dạng UUID.")
    if actor_id is not None and not db_module.is_valid_uuid(actor_id):
        raise HTTPException(status_code=400, detail=f"actor_id '{actor_id}' không đúng định dạng UUID.")

    rows, total = db_module.list_audit_logs(
        conn,
        manual_only=(view == "manual"),
        entity_type=entity_type,
        company_id=company_id,
        actor_id=actor_id,
        action_type=action_type,
        pending_note=pending_note if view == "manual" else None,
        limit=limit,
        offset=offset,
    )
    return PaginatedAuditLogs(total=total, limit=limit, offset=offset, items=rows)


@router.patch("/{log_id}/note", response_model=AuditLogOut)
def update_note(
    log_id: str,
    payload: AuditLogNoteUpdate,
    user: dict = Depends(require_role("ss_team")),
    conn=Depends(get_db),
):
    """Bổ sung/sửa note của 1 log đã tồn tại.

    QUYỀN: CHỈ actor_id GỐC của log (người thực hiện thao tác đó) mới
    được sửa — 403 nếu người gọi khác actor_id, kể cả admin (quyết định
    thiết kế đã chốt: note phản ánh đúng lời giải thích của CHÍNH người
    làm, không cho người khác viết hộ/sửa hộ để tránh việc note bị viết
    lại theo hướng khác ý người thực hiện thao tác gốc).

    Route này KHÔNG dùng để "thêm note lần đầu" cho log bắt buộc
    (note_required=true) — log đó ĐÃ CÓ note ngay lúc tạo (chặn cứng ở
    route ghi tương ứng), route này chỉ SỬA LẠI câu chữ nếu cần. Vẫn
    chặn set về rỗng cho log bắt buộc (409 nếu cố tình xoá) — dù
    AuditLogNoteUpdate.note đã có min_length=1 nên trường hợp gửi chuỗi
    rỗng hẳn đã bị Pydantic chặn ở tầng validate, check ở đây chỉ để an
    toàn kép."""
    if not db_module.is_valid_uuid(log_id):
        raise HTTPException(status_code=400, detail=f"log_id '{log_id}' không đúng định dạng UUID.")

    log = db_module.get_audit_log_by_id(conn, log_id)
    if log is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy log")

    if str(log.get("actor_id")) != str(user["sub"]):
        raise HTTPException(
            status_code=403,
            detail="Chỉ người đã thực hiện thao tác này mới được sửa note của log này.",
        )

    if log["note_required"] and not payload.note.strip():
        raise HTTPException(
            status_code=409,
            detail="Log này bắt buộc phải có note — không thể xoá trống.",
        )

    db_module.update_audit_log_note(conn, log_id, payload.note, note_updated_by=user["sub"])
    conn.commit()

    return db_module.get_audit_log_by_id(conn, log_id)
