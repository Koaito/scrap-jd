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


def find_duplicate_contacts(
    conn,
    *,
    company_id: str,
    work_email: Optional[str],
    social_link: Optional[str],
    phone_number: Optional[str],
    exclude_contact_id: Optional[str] = None,
) -> list[dict]:
    """Match MỜ (khác hẳn detect_contact_conflict ở trên vốn match cứng
    company_id + contact_name + email cho lần build preview đầu tiên) —
    dùng cho tính năng "cảnh báo trùng ngay khi staff sửa field lỗi tại
    chỗ" (xem preview_manager.apply_field_fix()). Quyết định thiết kế đã
    chốt qua trao đổi với staff:

    - Tiêu chí: CÙNG company_id, VÀ khớp ít nhất 1/3 trong
      (work_email, social_link, phone_number) — so khớp case-insensitive,
      đã strip khoảng trắng thừa. contact_status KHÔNG tính vào điểm
      match (chỉ là trạng thái làm việc, không phải định danh liên hệ).
    - match_score = số cột khớp / 3 (0.33 / 0.67 / 1.0) — điểm càng cao
      càng chắc là cùng 1 người, để FE hiển thị mức độ tin cậy cho staff
      tự quyết định thay vì chặn cứng.
    - Trả list (không phải 1 record) vì lý thuyết có thể khớp nhiều
      contact khác nhau cùng lúc (vd trùng phone với người A, trùng email
      với người B) — caller (preview_manager) tự chọn record match_score
      cao nhất nếu chỉ cần 1.
    - exclude_contact_id: dùng khi sửa 1 contact ĐANG tồn tại trong DB
      (không phải import) để không tự-match với chính nó — hiện tại
      import flow chưa cần (contact trong file luôn là "chưa tồn tại
      trong DB" cho tới khi staff bấm Update), giữ tham số optional để
      tái dùng cho mục đích khác sau này (vd form sửa contact trực tiếp).
    """
    company_id = (company_id or "").strip() or None
    work_email = (work_email or "").strip() or None
    social_link = (social_link or "").strip() or None
    phone_number = (phone_number or "").strip() or None

    if not company_id or not any([work_email, social_link, phone_number]):
        # Không đủ cơ sở để so khớp mờ — thiếu company_id (không biết so
        # trong phạm vi công ty nào) hoặc cả 3 cột định danh đều rỗng.
        return []

    where_clauses = ["company_id = %s"]
    params: list = [company_id]

    match_clauses = []
    if work_email:
        match_clauses.append("lower(trim(work_email)) = lower(%s)")
        params.append(work_email)
    if social_link:
        match_clauses.append("lower(trim(social_link)) = lower(%s)")
        params.append(social_link)
    if phone_number:
        match_clauses.append("trim(phone_number) = trim(%s)")
        params.append(phone_number)

    where_clauses.append("(" + " OR ".join(match_clauses) + ")")

    if exclude_contact_id:
        where_clauses.append("contact_id != %s")
        params.append(exclude_contact_id)

    query = f"SELECT * FROM company_contacts WHERE {' AND '.join(where_clauses)}"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()

    results = []
    for row in rows:
        matched_fields = []
        if work_email and row.get("work_email") and row["work_email"].strip().lower() == work_email.lower():
            matched_fields.append("work_email")
        if social_link and row.get("social_link") and row["social_link"].strip().lower() == social_link.lower():
            matched_fields.append("social_link")
        if phone_number and row.get("phone_number") and row["phone_number"].strip() == phone_number.strip():
            matched_fields.append("phone_number")

        if not matched_fields:
            # Không nên xảy ra (query đã lọc theo đúng OR ở trên) — giữ
            # lại như lớp phòng thủ, bỏ qua record không giải thích được
            # thay vì báo match_score sai.
            continue

        results.append({
            "existing_record": dict(row),
            "matched_fields": matched_fields,
            "match_score": round(len(matched_fields) / 3, 2),
        })

    results.sort(key=lambda r: r["match_score"], reverse=True)
    return results
