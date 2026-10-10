"""
db.company_queries — truy vấn ĐỌC danh sách / chi tiết công ty cho API
(tách từ scrapjd/db/companies.py, 10/2026). Chỉ SELECT, không ghi.
"""

from typing import Optional

import psycopg2.extras
from scrapjd.db.pg_types import Conn, Row, fetch_all_rows, fetch_one_row, fetch_optional_row


_COMPANY_SELECT_COLUMNS = """
        c.company_id, c.company_name, c.tax_id, c.website, c.industry,
        c.company_size, c.address, c.fanpage_url, c.linkedin_url,
        c.partnership_potential, c.is_active,
        c.created_at, c.updated_at, c.created_by, c.updated_by,
        p.province_name
"""


_COMPANY_FROM_JOINS = """
    FROM companies c
    LEFT JOIN provinces p ON p.province_id = c.province_id
"""


_COMPANY_LIST_BASE_QUERY = f"SELECT {_COMPANY_SELECT_COLUMNS} {_COMPANY_FROM_JOINS}"


def list_companies(conn: Conn, *, keyword: Optional[str] = None,
                    has_social: Optional[bool] = None,
                    province_name: Optional[str] = None,
                    created_by: Optional[str] = None,
                    include_inactive: bool = False,
                    limit: int = 50, offset: int = 0) -> tuple[list[Row], int]:
    """Trả (list[dict] company, total_count) — dùng cho GET /companies.

    has_social=True  -> chỉ công ty đã có fanpage_url HOẶC linkedin_url.
    has_social=False -> chỉ công ty còn thiếu CẢ HAI (tập ứng viên cho
    scrapjd/maintenance/get_company_fb_linkedin_link.py) — tiện cho dashboard theo dõi tiến
    độ enrich mà không cần chạy script tay để biết còn bao nhiêu.

    created_by: lọc công ty do 1 thành viên ss_team/admin cụ thể tự
    thêm tay (xem sql/migration_add_audit_columns.sql) — dùng cho trang
    "theo dõi hoạt động" nội bộ (08/2026). Công ty tạo qua crawl pipeline
    có created_by NULL, không khớp filter này với bất kỳ UUID nào.

    include_inactive (08/2026, xem sql/migration_add_company_soft_delete.sql):
    False (mặc định) -> chỉ trả company is_active=true, giống pattern
    company_contacts. True -> xem cả company đã xoá mềm (vd trang xem
    lại lịch sử/audit log cần hiển thị tên company dù đã bị xoá)."""
    conditions = []
    params: list = []

    if keyword:
        conditions.append("c.company_name ILIKE %s")
        params.append(f"%{keyword}%")
    if province_name:
        conditions.append("p.province_name = %s")
        params.append(province_name)
    if has_social is True:
        conditions.append("(c.fanpage_url IS NOT NULL OR c.linkedin_url IS NOT NULL)")
    elif has_social is False:
        conditions.append("(c.fanpage_url IS NULL AND c.linkedin_url IS NULL)")
    if created_by:
        conditions.append("c.created_by = %s")
        params.append(created_by)
    if not include_inactive:
        conditions.append("c.is_active = true")

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT count(*) AS total FROM companies c "
            f"LEFT JOIN provinces p ON p.province_id = c.province_id {where_clause}",
            params,
        )
        total = fetch_one_row(cur)["total"]

        cur.execute(
            f"{_COMPANY_LIST_BASE_QUERY} {where_clause} "
            f"ORDER BY c.created_at DESC LIMIT %s OFFSET %s",
            params + [limit, offset],
        )
        rows = fetch_all_rows(cur)

    return rows, total


def get_company_by_id(conn: Conn, company_id: str) -> Optional[Row]:
    """Trả 1 dict company đầy đủ hoặc None — dùng cho GET
    /companies/{company_id}. KHÔNG còn products_services (08/2026, xem
    sql/migration_drop_products_services.sql)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_COMPANY_SELECT_COLUMNS} "
            f"{_COMPANY_FROM_JOINS} "
            f"WHERE c.company_id = %s",
            (company_id,),
        )
        return fetch_optional_row(cur)
