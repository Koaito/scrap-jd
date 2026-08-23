"""
db.contacts — tách từ db.py (God module) theo domain, xem README/kế hoạch refactor.
"""

import logging
from typing import Optional

import psycopg2.extras
import psycopg2

logger = logging.getLogger(__name__)


def list_company_contacts(conn, company_id: str, *, include_inactive: bool = False):
    """Danh sách contact của 1 company. include_inactive=True để xem lại
    contact đã soft-delete (xem lịch sử liên hệ cũ) — mặc định False,
    chỉ trả contact đang active."""
    query = (
        "SELECT * FROM company_contacts WHERE company_id = %s"
        + ("" if include_inactive else " AND is_active = true")
        + " ORDER BY created_at DESC"
    )
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, (company_id,))
        return cur.fetchall()


def list_all_contacts(
    conn,
    *,
    include_inactive: bool = False,
    contact_status: Optional[str] = None,
    company_id: Optional[str] = None,
    search: Optional[str] = None,
    created_by: Optional[str] = None,
    assigned_ss_user: Optional[str] = None,
):
    """Danh sách contact GỘP TẤT CẢ công ty (khác list_company_contacts()
    chỉ trả theo 1 company_id) — dùng cho trang "Danh sách contact" tổng
    hợp (GET /contacts). JOIN sang companies để trả kèm company_name vì
    company_contacts không tự có tên công ty.

    Filter đều optional, kết hợp AND với nhau:
    - contact_status: khớp đúng 1 trong 4 giá trị enum
    - company_id: chỉ contact thuộc đúng công ty này
    - search: khớp theo contact_name (ILIKE, không phân biệt hoa/thường,
      khớp 1 phần) — company_name lọc riêng qua company_id vì chọn theo
      dropdown công ty chính xác hơn search text tự do.
    - created_by: contact do 1 thành viên ss_team/admin cụ thể TỰ THÊM
      (khác assigned_ss_user bên dưới — 1 người có thể thêm contact rồi
      giao cho người khác phụ trách).
    - assigned_ss_user: contact đang được GIAO cho 1 thành viên cụ thể
      phụ trách (xem migration_add_assigned_ss_user.sql) — độc lập với
      created_by, có thể khác người tạo.
    """
    query = (
        "SELECT cc.*, c.company_name "
        "FROM company_contacts cc "
        "JOIN companies c ON c.company_id = cc.company_id "
        "WHERE 1=1"
    )
    params: list = []

    if not include_inactive:
        query += " AND cc.is_active = true"
    if contact_status:
        query += " AND cc.contact_status = %s"
        params.append(contact_status)
    if company_id:
        query += " AND cc.company_id = %s"
        params.append(company_id)
    if search:
        query += " AND cc.contact_name ILIKE %s"
        params.append(f"%{search}%")
    if created_by:
        query += " AND cc.created_by = %s"
        params.append(created_by)
    if assigned_ss_user:
        query += " AND cc.assigned_ss_user = %s"
        params.append(assigned_ss_user)

    query += " ORDER BY cc.created_at DESC"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        return cur.fetchall()


def get_company_contact_by_id(conn, contact_id: str):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM company_contacts WHERE contact_id = %s", (contact_id,))
        return cur.fetchone()


def create_company_contact(conn, *, company_id: str, contact_name: str,
                            job_title: Optional[str] = None, work_email: Optional[str] = None,
                            social_link: Optional[str] = None, phone_number: Optional[str] = None,
                            found_source: Optional[str] = None,
                            assigned_ss_user: Optional[str] = None,
                            created_by: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO company_contacts
                (company_id, contact_name, job_title, work_email, social_link,
                 phone_number, found_source, collected_date, assigned_ss_user,
                 created_by, updated_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_DATE, %s, %s, %s)
            RETURNING contact_id
            """,
            (company_id, contact_name, job_title, work_email, social_link,
             phone_number, found_source, assigned_ss_user, created_by, created_by),
        )
        return str(cur.fetchone()[0])


def update_company_contact(conn, contact_id: str, *, contact_name: Optional[str] = None,
                            job_title: Optional[str] = None, work_email: Optional[str] = None,
                            social_link: Optional[str] = None, phone_number: Optional[str] = None,
                            found_source: Optional[str] = None,
                            contact_status: Optional[str] = None,
                            last_contacted_date=None, updated_by: str) -> bool:
    """Chỉ field truyền vào (khác None) mới bị ghi đè — giống pattern
    update_job()/update_company_profile() đã có, tránh phải gửi lại
    toàn bộ object mỗi lần PATCH.

    found_source (BUG FIX 08/2026): thiếu hẳn khỏi hàm này từ đầu —
    "Nguồn tìm thấy" chỉ ghi được lúc create_company_contact(), PATCH
    sửa contact luôn bỏ qua field này dù router/schema có nhận."""
    fields, values = [], []
    for col, val in [
        ("contact_name", contact_name), ("job_title", job_title),
        ("work_email", work_email), ("social_link", social_link),
        ("phone_number", phone_number), ("found_source", found_source),
        ("contact_status", contact_status),
        ("last_contacted_date", last_contacted_date),
    ]:
        if val is not None:
            fields.append(f"{col} = %s")
            values.append(val)

    if not fields:
        return True  # không có gì để cập nhật, coi như thành công

    fields.append("updated_by = %s")
    values.append(updated_by)
    fields.append("updated_at = now()")
    values.append(contact_id)

    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE company_contacts SET {', '.join(fields)} WHERE contact_id = %s",
            values,
        )
        return cur.rowcount > 0


def assign_company_contact(conn, contact_id: str, *, assigned_ss_user: Optional[str],
                            updated_by: str) -> bool:
    """Gán (hoặc BỎ gán, khi assigned_ss_user=None) người phụ trách 1
    contact — tách route riêng khỏi update_company_contact() (xem
    api/routers/contacts.py::assign_contact) vì pattern "chỉ field !=
    None mới ghi đè" của update_company_contact() không phân biệt được
    "không gửi field" với "cố ý set về NULL để bỏ gán" — ở đây
    assigned_ss_user LUÔN được ghi (kể cả None), không có nhánh bỏ qua.

    Validate assigned_ss_user phải là ss_user_id hợp lệ, role ss_team
    hoặc admin (không gán nhầm cho role 'user' — học viên không có khái
    niệm "phụ trách" contact) nằm ở route, KHÔNG ở đây — hàm này chỉ lo
    ghi giá trị đã được validate."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE company_contacts SET assigned_ss_user = %s, updated_by = %s, "
            "updated_at = now() WHERE contact_id = %s",
            (assigned_ss_user, updated_by, contact_id),
        )
        return cur.rowcount > 0


def soft_delete_company_contact(conn, contact_id: str, updated_by: str) -> bool:
    """Xoá MỀM — is_active=false, KHÔNG DELETE thật (xem
    sql/migration_add_role_hierarchy.sql mục 2 để hiểu lý do giữ lịch
    sử). GET mặc định sẽ không còn thấy contact này nữa."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE company_contacts SET is_active = false, updated_by = %s, "
            "updated_at = now() WHERE contact_id = %s",
            (updated_by, contact_id),
        )
        return cur.rowcount > 0


class ContactHasLinksError(Exception):
    """Contact đang có job_contact_links (đã từng gắn với job cụ thể,
    vd qua job_contact_interactions ghi log liên hệ) — không cho hard
    delete để tránh vỡ FK (job_contact_links.contact_id KHÔNG có ON
    DELETE CASCADE) hoặc mất lịch sử liên hệ theo từng job nếu cố
    cascade xuống. Nơi gọi (router) bắt exception này để trả lỗi rõ
    ràng cho staff, thay vì để lộ ra IntegrityError thô từ Postgres."""


def hard_delete_company_contact(conn, contact_id: str) -> bool:
    """Xoá THẬT — chỉ dùng làm bước 2 sau khi contact đã soft-delete
    (is_active=false), theo đúng thiết kế 2 bước đã quyết định (xem
    lịch sử trao đổi 08/2026): staff xoá mềm trước để xác nhận, xoá
    cứng chỉ để dọn hẳn contact rác/trùng/nhập nhầm không còn cần giữ
    lịch sử nữa — KHÔNG dùng để xoá nhanh 1 bước như soft-delete.

    Chặn (raise ContactHasLinksError) nếu contact còn job_contact_links
    — nghĩa là đã từng được gắn với ít nhất 1 job cụ thể (có thể kèm
    log tương tác ở job_contact_interactions) — xoá thật sẽ mất lịch sử
    liên hệ theo job đó, hoặc vỡ FK nếu Postgres chặn. Trường hợp này
    contact vẫn ở trạng thái xoá mềm (không đổi gì), staff cần tự xử lý
    job_contact_links liên quan trước nếu thực sự muốn xoá hẳn.

    KHÔNG tự kiểm tra is_active — nơi gọi (router) chịu trách nhiệm xác
    nhận contact đã soft-delete trước khi gọi hàm này (theo đúng luồng
    2 bước qua UI), hàm này chỉ lo phần DELETE + kiểm tra FK.

    Trả True nếu có xoá (rowcount > 0), False nếu contact_id không tồn
    tại (đã bị xoá trước đó / gõ sai id)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM job_contact_links WHERE contact_id = %s LIMIT 1",
            (contact_id,),
        )
        if cur.fetchone() is not None:
            raise ContactHasLinksError(
                f"Contact {contact_id} đang có liên kết với job (job_contact_links) — "
                "không thể xoá cứng, sẽ mất lịch sử liên hệ theo job đó."
            )
        cur.execute(
            "DELETE FROM company_contacts WHERE contact_id = %s",
            (contact_id,),
        )
        return cur.rowcount > 0
