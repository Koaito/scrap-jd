"""
Router CRUD mẫu email liên hệ doanh nghiệp — thêm 08/2026, xem
sql/migration_add_email_templates.sql + lịch sử trao đổi "chia phần
danh sách contact thành 2 phần: danh sách (như hiện tại) + quản lý mẫu
email, giống bên export/import".

Trước đây 6 mẫu này hardcode CỨNG trong public/app.js phía frontend,
KHÔNG có trong DB — router này thay thế bằng CRUD thật, dữ liệu persist.

TOÀN BỘ route yêu cầu require_role("ss_team") — cả 'ss_team' và 'admin'
đều được thêm/sửa/xoá (quyết định thiết kế đã chốt: không phân biệt
riêng admin cho tính năng này, khác trang "Crawl dữ liệu" dùng
admin_required ở phía frontend).

XOÁ HẲN (hard delete, KHÔNG soft-delete) — theo đúng yêu cầu đã chốt,
khác /companies, /companies/{id}/contacts. Lịch sử ai xoá mẫu nào vẫn
giữ được qua audit_logs vì log_action() luôn gọi TRƯỚC delete_email_template()
trong cùng transaction.

note: CREATE không bắt buộc, UPDATE/DELETE bắt buộc (xem
db.ACTION_LOG_RULES action CREATE_EMAIL_TEMPLATE/UPDATE_EMAIL_TEMPLATE/
DELETE_EMAIL_TEMPLATE) — enforce ở đây (422 TRƯỚC KHI chạm DB), giống
hệt pattern PATCH/DELETE /companies/{id}/contacts/{id}.
"""

import db as db_module
from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_db, require_role
from api.schemas import (
    EmailTemplateCreate,
    EmailTemplateDeleteRequest,
    EmailTemplateOut,
    EmailTemplateUpdate,
    PlaceholderHelpOut,
)

router = APIRouter(prefix="/email-templates", tags=["email-templates"])


@router.get("", response_model=list[EmailTemplateOut])
def list_email_templates(
    conn=Depends(get_db),
    user: dict = Depends(require_role("ss_team")),
):
    return db_module.list_email_templates(conn)


@router.get("/placeholder-help", response_model=PlaceholderHelpOut)
def get_placeholder_help(
    user: dict = Depends(require_role("ss_team")),
):
    """Bảng chú giải 5 placeholder cố định ({{TEN_CONG_TY}}, {{TEN_STAFF}}...)
    để hiển thị trong UI thêm/sửa mẫu — theo đúng yêu cầu đã chốt: giữ
    nguyên placeholder, chỉ thêm ghi chú hướng dẫn cách điền cho đúng.

    Đặt TRƯỚC /{template_id} bên dưới trong file (FastAPI khớp route
    theo thứ tự khai báo) — nếu đặt sau, "placeholder-help" sẽ bị route
    /{template_id} nuốt mất, hiểu nhầm thành template_id='placeholder-help'
    rồi trả 400 (không phải UUID hợp lệ) thay vì đúng bảng chú giải này."""
    return PlaceholderHelpOut()


@router.get("/{template_id}", response_model=EmailTemplateOut)
def get_email_template(
    template_id: str,
    conn=Depends(get_db),
    user: dict = Depends(require_role("ss_team")),
):
    if not db_module.is_valid_uuid(template_id):
        raise HTTPException(status_code=400, detail=f"template_id '{template_id}' không đúng định dạng UUID.")
    row = db_module.get_email_template_by_id(conn, template_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy mẫu email")
    return row


@router.post("", response_model=EmailTemplateOut, status_code=201)
def create_email_template(
    payload: EmailTemplateCreate,
    conn=Depends(get_db),
    user: dict = Depends(require_role("ss_team")),
):
    template_id = db_module.create_email_template(
        conn,
        title=payload.title,
        description=payload.description,
        body=payload.body,
        recommended_for=payload.recommended_for,
        display_order=payload.display_order,
        created_by=user["sub"],
    )

    # CREATE_EMAIL_TEMPLATE không thuộc nhóm bắt buộc note (xem
    # db.ACTION_LOG_RULES) — note optional, giống CREATE_CONTACT.
    db_module.log_action(
        conn, actor_id=user["sub"], action_type="CREATE_EMAIL_TEMPLATE",
        entity_type="EMAIL_TEMPLATE", entity_id=template_id,
        entity_label=payload.title, note=payload.note,
    )
    conn.commit()

    return db_module.get_email_template_by_id(conn, template_id)


@router.patch("/{template_id}", response_model=EmailTemplateOut)
def patch_email_template(
    template_id: str,
    payload: EmailTemplateUpdate,
    conn=Depends(get_db),
    user: dict = Depends(require_role("ss_team")),
):
    if not db_module.is_valid_uuid(template_id):
        raise HTTPException(status_code=400, detail=f"template_id '{template_id}' không đúng định dạng UUID.")

    existing = db_module.get_email_template_by_id(conn, template_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy mẫu email")

    # CHẶN CỨNG: sửa mẫu email bắt buộc note NẾU thực sự có field nào
    # đổi giá trị (xem db.ACTION_LOG_RULES, action UPDATE_EMAIL_TEMPLATE)
    # — kiểm tra TRƯỚC KHI gọi patch_email_template(), không cho thao
    # tác chính chạy nếu thiếu note. Patch rỗng/trùng giá trị cũ thì
    # note không bắt buộc (chưa có gì để giải thích lý do sửa).
    payload_fields = payload.model_dump(exclude_unset=True, exclude={"note"})
    changes = db_module.diff_changed_fields(existing, payload_fields) if payload_fields else {}
    if changes and not (payload.note or "").strip():
        raise HTTPException(
            status_code=422,
            detail="Sửa mẫu email bắt buộc phải có 'note' giải thích lý do sửa "
                   "(field 'note' trong body) — các ss_team khác cần biết vì sao "
                   "nội dung mẫu này thay đổi.",
        )

    updated = db_module.patch_email_template(
        conn, template_id,
        title=payload.title,
        description=payload.description,
        body=payload.body,
        recommended_for=payload.recommended_for,
        display_order=payload.display_order,
        updated_by=user["sub"],
    )
    if not updated:
        raise HTTPException(status_code=404, detail="Không tìm thấy mẫu email")

    if changes:
        db_module.log_action(
            conn, actor_id=user["sub"], action_type="UPDATE_EMAIL_TEMPLATE",
            entity_type="EMAIL_TEMPLATE", entity_id=template_id,
            entity_label=existing["title"], changes=changes, note=payload.note,
        )

    conn.commit()

    return db_module.get_email_template_by_id(conn, template_id)


@router.delete("/{template_id}", status_code=204)
def delete_email_template(
    template_id: str,
    payload: EmailTemplateDeleteRequest,
    conn=Depends(get_db),
    user: dict = Depends(require_role("ss_team")),
):
    """XOÁ HẲN (hard delete) — theo đúng yêu cầu đã chốt, KHÔNG soft-
    delete như DELETE /companies hoặc /companies/{id}/contacts/{id}.

    note BẮT BUỘC — DELETE_EMAIL_TEMPLATE thuộc nhóm action bị CHẶN
    CỨNG nếu thiếu note (xem db.ACTION_LOG_RULES): thiếu note -> 422
    ngay từ Pydantic (EmailTemplateDeleteRequest.note không có default),
    KHÔNG chạm tới DB."""
    if not db_module.is_valid_uuid(template_id):
        raise HTTPException(status_code=400, detail=f"template_id '{template_id}' không đúng định dạng UUID.")

    existing = db_module.get_email_template_by_id(conn, template_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy mẫu email")

    # log_action() TRƯỚC delete thật — entity_id vẫn còn ý nghĩa trong
    # audit_logs dù row email_templates biến mất ngay sau đó (hard
    # delete), giống pattern DELETE_JOB/DELETE_COMPANY (xoá thật hoặc
    # xoá mềm, log vẫn giữ nguyên vẹn không phụ thuộc entity còn tồn
    # tại hay không).
    db_module.log_action(
        conn, actor_id=user["sub"], action_type="DELETE_EMAIL_TEMPLATE",
        entity_type="EMAIL_TEMPLATE", entity_id=template_id,
        entity_label=existing["title"], note=payload.note,
    )
    db_module.delete_email_template(conn, template_id)
    conn.commit()
    return None
