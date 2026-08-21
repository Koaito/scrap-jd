"""
Conflict Detector — so khớp từng dòng đã validate (+ đã company-resolve
nếu là Job/Contact) với record hiện có trong DB, theo rule RIÊNG từng
entity (xem requirements.md Requirement 3 + design.md).

conflict_status có thể là 1 trong 4 giá trị (mở rộng so với thiết kế gốc
2 giá trị conflict/no_conflict, theo quyết định "cảnh báo record
inactive" đã chốt):
  - "no_conflict"              : dòng mới hoàn toàn, sẽ tạo record mới.
  - "conflict"                 : trùng với record ĐANG active — staff
                                  chọn Skip/Update/Create như thiết kế gốc.
  - "conflict_inactive"        : trùng với record đã bị soft-delete/CLOSED
                                  — cần cảnh báo riêng, hỏi "có chắc muốn
                                  ghi đè + kích hoạt lại?" trước khi cho
                                  chọn Update.
  - "pending_company_resolution": (chỉ Job/Contact) company_name trong
                                  file chưa resolve được company_id chắc
                                  chắn (nhiều gợi ý tương tự, cần staff tự
                                  chọn) — CHƯA thể detect conflict thật sự
                                  cho tới khi staff chọn xong company_id ở
                                  bước preview, conflict detection cho
                                  dòng này được làm LẠI lúc confirm (xem
                                  import_executor.py).
"""

from typing import Optional

import psycopg2.extras


def detect_company_conflict(conn, company_name: str, tax_id: Optional[str]) -> dict:
    """Company: match theo tax_id OR company_name — nếu CÙNG 1 record
    khớp cả 2 tiêu chí thì tính là 1 conflict duy nhất (Requirement 3.4).
    Match CẢ company đã is_active=false (giả định B: cảnh báo riêng thay
    vì âm thầm bỏ qua)."""
    company_name = (company_name or "").strip()
    tax_id = (tax_id or "").strip() or None

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        matched_by_tax = None
        if tax_id:
            cur.execute("SELECT * FROM companies WHERE tax_id = %s", (tax_id,))
            matched_by_tax = cur.fetchone()

        cur.execute(
            "SELECT * FROM companies WHERE lower(company_name) = lower(%s)",
            (company_name,),
        )
        matched_by_name = cur.fetchone()

    existing = matched_by_tax or matched_by_name
    # Nếu match cả 2 tiêu chí nhưng KHÁC record -> vẫn chỉ báo 1 conflict,
    # ưu tiên record match theo tax_id (định danh đáng tin hơn tên).
    if existing is None:
        return {"conflict_status": "no_conflict"}

    status = "conflict" if existing["is_active"] else "conflict_inactive"
    return {"conflict_status": status, "existing_record": dict(existing)}


def detect_job_conflict(conn, company_name: str, job_title: str, deadline) -> dict:
    """Job: match (company_name AND job_title AND deadline). Match CẢ
    job không OPEN (EXPIRED/CLOSED) — giả định B: cảnh báo riêng thay vì
    âm thầm tạo job trùng mới khi job cũ đã đóng."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT jp.*, c.company_name
            FROM job_postings jp
            JOIN companies c ON c.company_id = jp.company_id
            WHERE lower(c.company_name) = lower(%s)
              AND lower(jp.job_title) = lower(%s)
              AND jp.deadline = %s
            LIMIT 1
            """,
            (company_name, job_title, deadline),
        )
        row = cur.fetchone()

    if row is None:
        return {"conflict_status": "no_conflict"}

    status = "conflict" if row["job_status"] == "OPEN" else "conflict_inactive"
    return {"conflict_status": status, "existing_record": dict(row)}


def detect_contact_conflict(conn, company_id: str, contact_name: str, work_email: str) -> dict:
    """Contact: match (company_id AND contact_name AND email). Match CẢ
    contact đã is_active=false (giả định B)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM company_contacts
            WHERE company_id = %s
              AND lower(contact_name) = lower(%s)
              AND lower(work_email) = lower(%s)
            LIMIT 1
            """,
            (company_id, contact_name, work_email),
        )
        row = cur.fetchone()

    if row is None:
        return {"conflict_status": "no_conflict"}

    status = "conflict" if row["is_active"] else "conflict_inactive"
    return {"conflict_status": status, "existing_record": dict(row)}
