"""
db.email_templates — CRUD mẫu email liên hệ doanh nghiệp (thêm 08/2026,
xem sql/migration_add_email_templates.sql). Cùng pattern tách theo
domain như db/companies.py, db/contacts.py.

XOÁ HẲN (hard delete) — khác company_contacts/companies, bảng này KHÔNG
có is_active. Lịch sử ai xoá mẫu nào vẫn giữ được qua audit_logs (router
gọi log_action() TRƯỚC khi xoá — xem api/routers/email_templates.py).
"""

import logging
from typing import Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)


def _parse_pg_enum_array(raw) -> list[str]:
    """psycopg2 tự parse được mảng các kiểu built-in (int[], text[]...)
    nhưng KHÔNG tự parse mảng enum tự định nghĩa (vd contact_status_enum[]
    của cột recommended_for) — driver trả về nguyên chuỗi thô dạng Postgres
    array literal, ví dụ '{UNCONTACTED}' hoặc '{UNCONTACTED,RESPONDED}',
    thay vì list Python. Nếu không parse lại, Pydantic (EmailTemplateOut.
    recommended_for: list[str]) sẽ validate fail → FastAPI raise
    ResponseValidationError → 500.

    Hàm này convert chuỗi thô đó thành list[str] thật. Nếu input đã là
    list sẵn (vd psycopg2 phiên bản/khác cấu hình tự parse được, hoặc gọi
    lại hàm này 2 lần) thì trả nguyên, không xử lý gì thêm."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    s = str(raw).strip()
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1]
    if not s:
        return []
    return [item.strip().strip('"') for item in s.split(",") if item.strip()]


def list_email_templates(conn):
    """Toàn bộ mẫu, sắp theo display_order rồi tới created_at (mẫu mới
    thêm chưa chỉnh display_order sẽ rơi xuống cuối theo đúng thứ tự tạo,
    KHÔNG xáo trộn ngẫu nhiên)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM email_templates ORDER BY display_order ASC, created_at ASC"
        )
        rows = cur.fetchall()
        for row in rows:
            row["recommended_for"] = _parse_pg_enum_array(row.get("recommended_for"))
        return rows


def get_email_template_by_id(conn, template_id: str):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM email_templates WHERE template_id = %s", (template_id,))
        row = cur.fetchone()
        if row is not None:
            row["recommended_for"] = _parse_pg_enum_array(row.get("recommended_for"))
        return row


def create_email_template(
    conn, *, title: str, description: Optional[str], body: str,
    recommended_for: list[str], display_order: int, created_by: str,
) -> str:
    """Tạo mẫu mới. recommended_for là mảng contact_status_enum (có thể
    rỗng — không gợi ý riêng cho trạng thái nào, giống 2/6 mẫu gốc)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO email_templates "
            "(title, description, body, recommended_for, display_order, created_by, updated_by) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING template_id",
            (title, description, body, recommended_for, display_order, created_by, created_by),
        )
        return cur.fetchone()[0]


def patch_email_template(
    conn, template_id: str, *,
    title: Optional[str] = None,
    description: Optional[str] = None,
    body: Optional[str] = None,
    recommended_for: Optional[list[str]] = None,
    display_order: Optional[int] = None,
    updated_by: str,
) -> bool:
    """Sửa TỰ DO — chỉ field truyền vào (khác None) mới bị ghi đè, giống
    pattern patch_company_profile()/patch_contact(). description truyền
    "" (chuỗi rỗng, khác None) hợp lệ — xoá mô tả cũ, DB cho phép NULL
    lẫn "". Trả False nếu template_id không tồn tại."""
    fields, values = [], []
    if title is not None:
        fields.append("title = %s")
        values.append(title)
    if description is not None:
        fields.append("description = %s")
        values.append(description)
    if body is not None:
        fields.append("body = %s")
        values.append(body)
    if recommended_for is not None:
        fields.append("recommended_for = %s")
        values.append(recommended_for)
    if display_order is not None:
        fields.append("display_order = %s")
        values.append(display_order)

    if not fields:
        # Không field nào thực sự đổi (payload rỗng) — vẫn đếm là "có
        # sửa" ở mức router (updated_by/updated_at không đổi gì thêm),
        # nhưng ở tầng DB không cần chạm gì cả, tránh UPDATE vô nghĩa.
        return get_email_template_by_id(conn, template_id) is not None

    fields.append("updated_by = %s")
    values.append(updated_by)
    values.append(template_id)

    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE email_templates SET {', '.join(fields)} WHERE template_id = %s",
            tuple(values),
        )
        return cur.rowcount > 0


def delete_email_template(conn, template_id: str) -> bool:
    """XOÁ HẲN (hard delete) — theo đúng yêu cầu thiết kế, KHÔNG soft-
    delete như company_contacts. Trả False nếu template_id không tồn tại
    (router không log DELETE_EMAIL_TEMPLATE khi trả False, tránh log rác
    cho request xoá 1 id đã xoá/không tồn tại từ trước)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM email_templates WHERE template_id = %s", (template_id,))
        return cur.rowcount > 0
