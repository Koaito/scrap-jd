"""
Company Resolver — dùng cho import Job/Contact (2 entity import bằng
company_name dạng text, không bắt nhập company_id UUID tay — xem quyết
định thiết kế trong lịch sử trao đổi).

Thứ tự resolve cho 1 dòng (company_name, tax_id tuỳ có hay không):
  1. Có tax_id -> match TUYỆT ĐỐI theo tax_id (định danh thật, ổn định
     theo pháp luật VN) -> resolved ngay, KHÔNG cần gợi ý.
  2. Không có tax_id (hoặc tax_id không khớp ai) -> match TUYỆT ĐỐI theo
     company_name (case-insensitive, sau khi trim) -> resolved.
  3. Không match tuyệt đối -> trả về "needs_resolution" kèm danh sách
     gợi ý company có TÊN TƯƠNG TỰ (pg_trgm similarity), để staff tự
     chọn tay ở bước preview — CHẤP NHẬN tốn thời gian rà từng dòng đổi
     lấy độ chính xác, KHÔNG tự động chọn công ty gần giống nhất (rủi ro
     gộp nhầm 2 công ty khác nhau tên gần giống).
  4. Staff không chọn công ty nào trong danh sách gợi ý (hoặc chọn "Tạo
     công ty mới") -> tạo company mới lúc confirm (không tạo ngay lúc
     preview, tránh rác DB nếu staff huỷ giữa chừng không confirm).

Ngưỡng similarity 0.3 (thang 0-1 của pg_trgm) — kinh nghiệm chung: dưới
0.3 gần như không liên quan gì, trên 0.3 bắt đầu có tín hiệu tên gần
giống đáng xem xét. Không hạ thấp hơn để tránh trả về quá nhiều gợi ý
nhiễu không giúp ích cho staff.
"""

from dataclasses import dataclass
from typing import Optional

import psycopg2.extras

SIMILARITY_THRESHOLD = 0.3
MAX_SUGGESTIONS = 5


@dataclass
class CompanySuggestion:
    company_id: str
    company_name: str
    tax_id: Optional[str]
    is_active: bool
    similarity: float


@dataclass
class CompanyResolution:
    status: str  # "resolved" | "needs_resolution"
    company_id: Optional[str] = None
    company_is_active: Optional[bool] = None
    suggestions: list[CompanySuggestion] = None


def resolve_company(conn, company_name: str, tax_id: Optional[str] = None) -> CompanyResolution:
    company_name = (company_name or "").strip()
    tax_id = (tax_id or "").strip() or None

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if tax_id:
            cur.execute(
                "SELECT company_id, is_active FROM companies WHERE tax_id = %s",
                (tax_id,),
            )
            row = cur.fetchone()
            if row:
                return CompanyResolution(
                    status="resolved",
                    company_id=str(row["company_id"]),
                    company_is_active=row["is_active"],
                )

        cur.execute(
            "SELECT company_id, is_active FROM companies WHERE lower(company_name) = lower(%s)",
            (company_name,),
        )
        row = cur.fetchone()
        if row:
            return CompanyResolution(
                status="resolved",
                company_id=str(row["company_id"]),
                company_is_active=row["is_active"],
            )

    suggestions = suggest_companies(conn, company_name)
    return CompanyResolution(status="needs_resolution", suggestions=suggestions)


def get_company(conn, company_id: str) -> Optional[dict]:
    """Tra 1 company theo company_id — dùng cho resolve-company (staff tự
    chọn 1 công ty trong modal ở bước preview, xem preview_manager.py::
    resolve_company_selection()) khi cần biết TÊN THẬT của company vừa
    chọn (chứ không phải company_name gốc gõ trong file, có thể lệch —
    vd file ghi "FPT" nhưng staff chọn company "FPT Software") để detect
    conflict cho đúng (detect_job_conflict() match theo company_name dạng
    text, không dùng company_id trực tiếp — xem conflict_detector.py).

    Trả None nếu company_id không tồn tại (case hiếm: preview cũ, company
    vừa bị xoá giữa chừng) — caller tự quyết định xử lý (hiện tại:
    preview_manager raise ValueError, router trả 404)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM companies WHERE company_id = %s", (company_id,))
        row = cur.fetchone()
    return dict(row) if row else None


def suggest_companies(conn, company_name: str) -> list[CompanySuggestion]:
    """Gợi ý company có tên TƯƠNG TỰ company_name, dùng pg_trgm
    similarity() — cần extension pg_trgm + index gin_trgm_ops (xem
    sql/migration_add_import_export.sql). Trả cả company đã inactive
    (is_active=false) — staff cần THẤY để tự quyết định reactivate hay
    bỏ qua, ẩn đi sẽ khiến staff tưởng công ty chưa từng tồn tại rồi tạo
    trùng."""
    company_name = (company_name or "").strip()
    if not company_name:
        return []

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT company_id, company_name, tax_id, is_active,
                   similarity(company_name, %s) AS sim
            FROM companies
            WHERE similarity(company_name, %s) > %s
            ORDER BY sim DESC
            LIMIT %s
            """,
            (company_name, company_name, SIMILARITY_THRESHOLD, MAX_SUGGESTIONS),
        )
        rows = cur.fetchall()

    return [
        CompanySuggestion(
            company_id=str(r["company_id"]),
            company_name=r["company_name"],
            tax_id=r["tax_id"],
            is_active=r["is_active"],
            similarity=float(r["sim"]),
        )
        for r in rows
    ]
