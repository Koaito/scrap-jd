"""
Export Query — lấy TOÀN BỘ record (không phân trang, khác list_jobs()/
list_companies() dùng cho UI listing vốn giới hạn limit<=200) theo đúng
scope từng entity (Requirement 1.2/1.3/1.4), format sẵn đúng cột +
kiểu dữ liệu cho file export (Requirement 11.7: date ISO 8601,
Requirement 11.9: boolean "true"/"false").
"""

import psycopg2.extras

from api.services.entity_specs import get_spec


def query_jobs_for_export(conn) -> list[dict]:
    """Requirement 1.2: CHỈ job status OPEN."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT jp.job_id, c.company_name, jp.job_title, jp.matching_industry,
                   l.level_code, p.province_name, jp.work_type, jp.currency,
                   jp.salary_min, jp.salary_max, jp.salary_type, jp.salary_period,
                   jp.deadline, jp.job_status, jp.ss_team_notes,
                   jp.created_at, jp.updated_at
            FROM job_postings jp
            JOIN companies c ON c.company_id = jp.company_id
            LEFT JOIN levels l ON l.level_id = jp.level_id
            LEFT JOIN provinces p ON p.province_id = jp.province_id
            WHERE jp.job_status = 'OPEN'
            ORDER BY jp.created_at DESC
            """
        )
        rows = cur.fetchall()
    return _format_rows(rows, "job")


def query_companies_for_export(conn) -> list[dict]:
    """Requirement 1.3: toàn bộ company (kể cả đã soft-delete — is_active
    có mặt trong export_columns để người xem file tự biết trạng thái)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT c.company_id, c.company_name, c.tax_id, c.website, c.industry,
                   c.company_size, c.address, p.province_name, c.fanpage_url,
                   c.linkedin_url, c.partnership_potential, c.is_active,
                   c.created_at, c.updated_at
            FROM companies c
            LEFT JOIN provinces p ON p.province_id = c.province_id
            ORDER BY c.created_at DESC
            """
        )
        rows = cur.fetchall()
    return _format_rows(rows, "company")


def query_contacts_for_export(conn) -> list[dict]:
    """Requirement 1.4: toàn bộ contact (kể cả đã soft-delete)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT cc.contact_id, c.company_name, cc.contact_name, cc.job_title,
                   cc.work_email, cc.social_link, cc.phone_number, cc.found_source,
                   cc.contact_status, cc.is_active, cc.created_at, cc.updated_at
            FROM company_contacts cc
            JOIN companies c ON c.company_id = cc.company_id
            ORDER BY cc.created_at DESC
            """
        )
        rows = cur.fetchall()
    return _format_rows(rows, "contact")


def _format_rows(rows: list[dict], entity_type: str) -> list[dict]:
    spec = get_spec(entity_type)
    out = []
    for row in rows:
        formatted = {}
        for col in spec.export_columns:
            val = row.get(col)
            if val is None:
                formatted[col] = None
            elif isinstance(val, bool):
                formatted[col] = "true" if val else "false"
            elif hasattr(val, "isoformat"):
                formatted[col] = val.isoformat()
            else:
                formatted[col] = val
        out.append(formatted)
    return out


QUERY_FUNCS = {
    "job": query_jobs_for_export,
    "company": query_companies_for_export,
    "contact": query_contacts_for_export,
}
