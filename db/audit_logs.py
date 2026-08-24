"""
db.audit_logs — tách từ db.py (God module) theo domain.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)


ACTION_LOG_RULES: dict[str, dict] = {
    "CREATE_JOB":     {"is_manual_log": False, "note_required": False},
    "UPDATE_JOB":     {"is_manual_log": True,  "note_required": False},
    "DELETE_JOB":     {"is_manual_log": True,  "note_required": False},
    "CREATE_COMPANY": {"is_manual_log": False, "note_required": False},
    "UPDATE_COMPANY": {"is_manual_log": True,  "note_required": False},
    "DELETE_COMPANY": {"is_manual_log": True,  "note_required": True},
    "CREATE_CONTACT": {"is_manual_log": True,  "note_required": False},
    "UPDATE_CONTACT": {"is_manual_log": True,  "note_required": True},
    "DELETE_CONTACT": {"is_manual_log": True,  "note_required": True},
    "ASSIGN_CONTACT": {"is_manual_log": True,  "note_required": True},
    # Học viên ứng tuyển (upload CV) / huỷ ứng tuyển — xem
    # sql/migration_add_application_audit_log.sql. Cùng nhóm với
    # CREATE_JOB/CREATE_COMPANY: log tự động, không bắt buộc note.
    "APPLY_JOB":                {"is_manual_log": False, "note_required": False},
    "WITHDRAW_JOB_APPLICATION": {"is_manual_log": False, "note_required": False},
    # BUG FIX (08/2026): import_confirm() (api/routers/import_export.py)
    # gọi log_action(action_type="BULK_IMPORT_JOB"/"BULK_IMPORT_COMPANY"/
    # "BULK_IMPORT_CONTACT") nhưng 3 action_type này CHƯA TỪNG được đăng
    # ký ở đây -> log_action() luôn raise KeyError ngay khi tra
    # ACTION_LOG_RULES[action_type], SAU KHI execute_import() đã insert
    # thành công job/company/contact trong transaction -> router bắt
    # Exception rộng, conn.rollback() toàn bộ, trả 500 "Import failed due
    # to database error" — staff thấy lỗi 500 dù dữ liệu đúng, không lưu
    # được gì (đối chiếu log thật: KeyError: 'BULK_IMPORT_JOB' tại
    # db.py::log_action, gọi từ import_export.py::import_confirm dòng 231).
    #
    # is_manual_log=True: đúng bản chất, đây là thao tác staff chủ động
    # bấm "Xác nhận nhập dữ liệu" (không phải job tự động như CREATE_JOB
    # qua crawl/APPLY_JOB), cùng nhóm với UPDATE_JOB/CREATE_CONTACT.
    # note_required=True: khớp hành vi UI đã có sẵn — _dm_import.html bắt
    # buộc nhập "Ghi chú lần nhập" (textarea required, nút Xác nhận bị
    # disable tới khi có note) TRƯỚC KHI form có thể submit, nên tầng
    # log_action() enforce lại đúng ràng buộc đó thay vì mâu thuẫn với UI.
    "BULK_IMPORT_JOB":     {"is_manual_log": True, "note_required": True},
    "BULK_IMPORT_COMPANY": {"is_manual_log": True, "note_required": True},
    "BULK_IMPORT_CONTACT": {"is_manual_log": True, "note_required": True},
}


class NoteRequiredError(Exception):
    """Action thuộc nhóm bắt buộc note (xem ACTION_LOG_RULES) nhưng gọi
    log_action() không kèm note hoặc note rỗng — router PHẢI validate
    trước khi chạm tới thao tác chính (UPDATE/DELETE thật) để CHẶN CỨNG
    đúng yêu cầu (không note thì không cho lưu thao tác), KHÔNG để tới
    đây mới raise — raise ở đây chỉ là lớp phòng thủ THỨ 2 (giống CHECK
    constraint ở DB), phòng router nào quên validate."""


def diff_changed_fields(old_row: dict, payload_fields: dict) -> dict:
    """So sánh giá trị CŨ (old_row, lấy từ get_job_by_id()/get_company_by_id()/
    get_company_contact_by_id()) với các field THỰC SỰ có mặt trong request
    (payload_fields — dùng payload.model_dump(exclude_unset=True) ở router,
    KHÔNG phải toàn bộ payload, để không coi field không gửi là "đổi
    thành None"). Trả dict {field: {"old":..., "new":...}} CHỈ gồm field
    có giá trị thực sự khác nhau — field gửi lên nhưng trùng giá trị cũ
    (PATCH lại y hệt) KHÔNG được tính là 1 thay đổi.

    So sánh bằng str(...) 2 vế — old_row có thể chứa Decimal/date/UUID
    từ psycopg2 trong khi payload_fields là kiểu Python thuần từ
    Pydantic, so sánh trực tiếp (!=) dễ lệch kiểu dữ liệu dù giá trị
    hiển thị giống hệt nhau (vd Decimal('0') != 0 tuỳ context)."""
    changes = {}
    for field, new_val in payload_fields.items():
        old_val = old_row.get(field)
        if str(old_val) != str(new_val):
            changes[field] = {"old": old_val, "new": new_val}
    return changes


def log_action(conn, *, actor_id: Optional[str], action_type: str,
                entity_type: str, entity_id: str,
                entity_label: Optional[str] = None,
                company_id: Optional[str] = None,
                changes: Optional[dict] = None,
                note: Optional[str] = None) -> str:
    """Ghi 1 dòng audit_logs — gọi TRONG CÙNG transaction với thao tác
    chính (TRƯỚC conn.commit() của route, dùng CHUNG connection `conn`),
    KHÔNG tự commit ở đây — nếu thao tác chính rollback vì lỗi gì đó,
    log cũng phải rollback theo, không được tồn tại mồ côi mô tả 1 thao
    tác thực ra chưa xảy ra.

    is_manual_log/note_required tra từ ACTION_LOG_RULES theo action_type
    — raise KeyError rõ ràng nếu action_type gõ sai (lỗi lập trình, nên
    để crash thay vì âm thầm ghi sai luật).

    Raise NoteRequiredError nếu action_type thuộc nhóm bắt buộc mà note
    rỗng/None — ĐÂY LÀ LỚP CHẶN THỨ 2 (constraint CHECK ở DB là lớp
    thứ 3), lớp CHÍNH phải nằm ở router (trả 422 TRƯỚC KHI gọi
    UPDATE/DELETE thật trên bảng nghiệp vụ — xem api/routers/*.py) để
    không lỡ chạy nửa chừng thao tác chính rồi mới phát hiện thiếu note.

    Trả log_id (str) của dòng vừa tạo."""
    rules = ACTION_LOG_RULES[action_type]
    note = (note or "").strip() or None

    if rules["note_required"] and note is None:
        raise NoteRequiredError(
            f"Action '{action_type}' bắt buộc phải có note — router cần "
            f"validate và trả 422 TRƯỚC KHI gọi log_action()/thực hiện "
            f"thao tác chính."
        )

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_logs
                (actor_id, action_type, entity_type, entity_id, entity_label,
                 company_id, changes, is_manual_log, note_required, note,
                 note_updated_by, note_updated_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING log_id
            """,
            (
                actor_id, action_type, entity_type, entity_id, entity_label,
                company_id,
                json.dumps(changes, ensure_ascii=False, default=str) if changes else None,
                rules["is_manual_log"], rules["note_required"], note,
                actor_id if note is not None else None,
                datetime.now(timezone.utc) if note is not None else None,
            ),
        )
        return str(cur.fetchone()[0])


_AUDIT_LOG_SELECT_COLUMNS = """
        al.log_id, al.actor_id, u.full_name AS actor_name, al.action_type,
        al.entity_type, al.entity_id, al.entity_label, al.company_id,
        c.company_name, al.changes, al.is_manual_log, al.note_required,
        al.note, al.note_updated_by, al.note_updated_at, al.created_at
"""


_AUDIT_LOG_FROM_JOINS = """
    FROM audit_logs al
    LEFT JOIN app_users u ON u.ss_user_id = al.actor_id
    LEFT JOIN companies c ON c.company_id = al.company_id
"""


def list_audit_logs(conn, *, manual_only: bool = False,
                     entity_type: Optional[str] = None,
                     company_id: Optional[str] = None,
                     actor_id: Optional[str] = None,
                     action_type: Optional[str] = None,
                     pending_note: Optional[bool] = None,
                     limit: int = 50, offset: int = 0):
    """Trả (list[dict], total) — dùng cho GET /audit-logs.

    manual_only=True  -> view "log thủ công" (chỉ is_manual_log=true).
    manual_only=False -> view "log tự động" (TẤT CẢ dòng, kể cả những
    dòng cũng thuộc log thủ công — log thủ công LUÔN là tập con, không
    phải dữ liệu tách biệt).

    pending_note=True -> CHỈ trả dòng note_required=true VÀ note IS NULL
    (đang chờ ai đó điền) — dùng cho badge nhắc nhở ở UI, chỉ có ý
    nghĩa khi manual_only=True (log tự động không có khái niệm note)."""
    conditions = []
    params: list = []

    if manual_only:
        conditions.append("al.is_manual_log = true")
    if entity_type:
        conditions.append("al.entity_type = %s")
        params.append(entity_type)
    if company_id:
        conditions.append("al.company_id = %s")
        params.append(company_id)
    if actor_id:
        conditions.append("al.actor_id = %s")
        params.append(actor_id)
    if action_type:
        conditions.append("al.action_type = %s")
        params.append(action_type)
    if pending_note is True:
        conditions.append("al.note_required = true AND al.note IS NULL")
    elif pending_note is False:
        conditions.append("(al.note_required = false OR al.note IS NOT NULL)")

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT count(*) AS total {_AUDIT_LOG_FROM_JOINS} {where_clause}",
            params,
        )
        total = cur.fetchone()["total"]

        cur.execute(
            f"SELECT {_AUDIT_LOG_SELECT_COLUMNS} {_AUDIT_LOG_FROM_JOINS} {where_clause} "
            f"ORDER BY al.created_at DESC LIMIT %s OFFSET %s",
            params + [limit, offset],
        )
        rows = cur.fetchall()

    return rows, total


def get_audit_log_by_id(conn, log_id: str):
    """Trả 1 dict audit log đầy đủ hoặc None — dùng để kiểm tra quyền
    sửa note (so actor_id) trước khi PATCH /audit-logs/{log_id}/note."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_AUDIT_LOG_SELECT_COLUMNS} {_AUDIT_LOG_FROM_JOINS} "
            f"WHERE al.log_id = %s",
            (log_id,),
        )
        return cur.fetchone()


def update_audit_log_note(conn, log_id: str, note: str, note_updated_by: str) -> bool:
    """Sửa/bổ sung note của 1 log ĐÃ TỒN TẠI — dùng cho log thuộc nhóm
    note TUỲ CHỌN (note_required=false), nơi note có thể để trống lúc
    thao tác rồi bổ sung sau. QUYỀN "chỉ actor gốc mới được sửa" kiểm
    tra ở ROUTER (so log['actor_id'] với user hiện tại), KHÔNG ở đây —
    hàm này chỉ lo ghi giá trị đã được validate.

    note rỗng/khoảng trắng -> lưu NULL (cho phép "xoá" note cũ đã lỡ
    điền, TRỪ log có note_required=true — router phải chặn trường hợp
    đó TRƯỚC khi gọi hàm này, vì set NULL cho dòng note_required=true
    sẽ vi phạm CHECK constraint ở DB, raise lỗi rõ ràng thay vì âm thầm
    cho qua)."""
    note = (note or "").strip() or None
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE audit_logs SET note = %s, note_updated_by = %s, "
            "note_updated_at = now() WHERE log_id = %s",
            (note, note_updated_by, log_id),
        )
        return cur.rowcount > 0
