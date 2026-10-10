"""
db.company_analytics — tín hiệu "Tiềm năng hợp tác" và thống kê "tình trạng
dữ liệu" của công ty (tách từ scrapjd/db/companies.py, 10/2026). Chỉ SELECT.
"""

from typing import Optional

import psycopg2.extras
from scrapjd.db.pg_types import Conn, fetch_one_row


# Level "mới ra trường" — khớp level_group 'Entry Level' (xem
# sql/schema.sql INSERT INTO levels: Intern/Fresher/Junior). Group theo
# level_group thay vì hard-code 3 level_code ở đây để khớp đúng 1 nguồn
# sự thật với cột levels.level_group trong DB (khác
# potential_score._ENTRY_LEVELS bên Flask — nơi đó hard-code lại vì
# không có sẵn cột level_group trong job đã _normalize_job(), xem
# docstring bên đó).
_ENTRY_LEVEL_GROUP = "Entry Level"

# Ngành MindX đào tạo — PHẢI khớp constants.INDUSTRIES bên Flask
# (mindx-jobs/constants.py). Trùng lặp CÓ CHỦ Ý (không import chéo 2
# repo riêng biệt được) — job.matching_industry là free text do crawl/
# nhập tay ghi vào (không FK), nên so khớp bằng IN (...) giống hệt cách
# potential_score.suggest_partnership_potential() làm bên Flask
# (`j.get("industry") in INDUSTRIES`). Sửa 1 bên PHẢI sửa bên kia.
_TARGET_INDUSTRIES = (
    "Code", "Data Analysis", "Data Engineer", "Data Scientist",
    "Business Analysis", "UI/UX Design",
)

# Trạng thái contact tính là "đã từng phản hồi" — khớp
# potential_score._RESPONDED_STATUSES bên Flask.
_RESPONDED_CONTACT_STATUSES = ("RESPONDED", "IN_PARTNERSHIP")


def get_partnership_signals(conn: Conn, company_ids: Optional[list] = None) -> dict:
    """Tính sẵn (bằng SQL GROUP BY, KHÔNG kéo full job/contact object về
    Python) 2 tín hiệu cần cho gợi ý "Tiềm năng hợp tác"
    (potential_score.suggest_partnership_potential() bên Flask) mà
    PHẢI join qua job_postings/company_contacts mới biết được:
      - has_open_entry_job: công ty có job OPEN, level Intern/Fresher/
        Junior không.
      - matches_target_industry: công ty có job thuộc đúng nhóm ngành
        MindX đào tạo không (bất kể job đó OPEN hay CLOSED — giữ đúng
        hành vi cũ của suggest_partnership_potential(), hàm đó không
        lọc theo status_raw cho tiêu chí này).
      - has_responded: công ty có contact status RESPONDED/
        IN_PARTNERSHIP không.

    KHÔNG tính is_hn_hcm/has_company_size ở đây — 2 field đó đã có sẵn
    trực tiếp trên companies (province_name, company_size), nơi gọi
    (route /companies bên Flask) tính thẳng từ CompanyOut đã có, không
    cần round-trip riêng.

    company_ids: optional — nếu truyền vào (list company_id), CHỈ tính
    cho các công ty đó (WHERE c.company_id = ANY(%s), tận dụng PK index)
    thay vì toàn bộ DB — dùng khi trang /companies chỉ cần tín hiệu cho
    đúng 1 trang (per_page công ty) đang hiển thị, không phải mọi công
    ty trong hệ thống. None = tính cho TẤT CẢ công ty (vd nếu sau này
    cần dùng ở nơi không có sẵn danh sách company_id).

    Trả dict {company_id: {"has_open_entry_job": bool,
    "matches_target_industry": bool, "has_responded": bool}} — company
    không có job/contact nào khớp tiêu chí nào đơn giản KHÔNG xuất hiện
    trong dict (coi như False cả 3, nơi gọi tự .get(id, default) khi
    build gợi ý)."""
    company_filter = ""
    base_params: list = []
    if company_ids is not None:
        if not company_ids:
            return {}
        company_filter = "AND jp.company_id = ANY(%s::uuid[])"
        base_params = [list(company_ids)]

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Job signals: 1 lượt GROUP BY company_id, dùng bool_or() thay
        # vì kéo từng dòng job về đếm bằng Python — Postgres tự lo phần
        # "có ít nhất 1 job khớp điều kiện" hiệu quả hơn nhiều so với
        # SELECT * rồi loop ở tầng ứng dụng (đây chính là thứ
        # list_all_jobs() cũ đang làm, xem lịch sử trao đổi).
        cur.execute(
            f"""
            SELECT
                jp.company_id,
                bool_or(jp.job_status = 'OPEN' AND l.level_group = %s) AS has_open_entry_job,
                bool_or(jp.matching_industry = ANY(%s)) AS matches_target_industry
            FROM job_postings jp
            LEFT JOIN levels l ON l.level_id = jp.level_id
            WHERE 1=1 {company_filter}
            GROUP BY jp.company_id
            """,
            [_ENTRY_LEVEL_GROUP, list(_TARGET_INDUSTRIES)] + base_params,
        )
        job_rows = cur.fetchall()

        contact_filter = ""
        contact_params: list = []
        if company_ids is not None:
            contact_filter = "AND cc.company_id = ANY(%s::uuid[])"
            contact_params = [list(company_ids)]

        cur.execute(
            f"""
            SELECT cc.company_id, bool_or(cc.contact_status = ANY(%s::contact_status_enum[])) AS has_responded
            FROM company_contacts cc
            WHERE cc.is_active = true {contact_filter}
            GROUP BY cc.company_id
            """,
            [list(_RESPONDED_CONTACT_STATUSES)] + contact_params,
        )
        contact_rows = cur.fetchall()

    signals: dict = {}
    for row in job_rows:
        signals[row["company_id"]] = {
            "has_open_entry_job": bool(row["has_open_entry_job"]),
            "matches_target_industry": bool(row["matches_target_industry"]),
            "has_responded": False,
        }
    for row in contact_rows:
        entry = signals.setdefault(
            row["company_id"],
            {"has_open_entry_job": False, "matches_target_industry": False, "has_responded": False},
        )
        entry["has_responded"] = bool(row["has_responded"])

    return signals


# Field nào tính vào thống kê "thiếu dữ liệu" ở tab Tình trạng dữ liệu
# (blueprints/crawl_status.py bên mindx-jobs) — PHẢI khớp field/label/
# THỨ TỰ với COMPANY_HEALTH_FIELDS (crawler_client/companies.py bên
# Flask). Trùng lặp CÓ CHỦ Ý — cùng lý do với _TARGET_INDUSTRIES ở trên
# (2 repo riêng biệt, không import chéo được). Sửa 1 bên PHẢI sửa bên
# kia. company_id không đưa vào vì luôn có sẵn, không có ý nghĩa thống
# kê (giống comment gốc bên Flask).
_COMPANY_HEALTH_FIELDS = [
    ("tax_id", "Mã số thuế"),
    ("website", "Website"),
    ("industry", "Ngành"),
    ("address", "Địa chỉ"),
    ("company_size", "Quy mô"),
    ("fanpage", "Fanpage"),
    ("linkedin_company", "LinkedIn"),
]


def get_company_data_health(conn: Conn) -> dict:
    """Thay thế cho việc frontend (blueprints/crawl_status.py bên
    mindx-jobs) từng phải gọi list_all_companies() + list_all_contacts()
    (kéo TOÀN BỘ company/contact về Flask rồi tự đếm field rỗng bằng
    Python — count_missing_fields()/count_companies_without_contact() ở
    crawler_client/) chỉ để vẽ 2 khối "Company thiếu dữ liệu theo từng
    trường" + "Company chưa có contact" ở tab Tình trạng dữ liệu.

    Tính bằng 1 lượt SQL duy nhất — COUNT(*) FILTER(...) cho từng field
    trong _COMPANY_HEALTH_FIELDS + NOT EXISTS subquery cho "chưa có
    contact active" — CHỈ trên company đang active (is_active=true,
    khớp hành vi cũ: list_all_companies() mặc định KHÔNG trả company đã
    xoá mềm). Chi phí không tăng theo tổng số company/contact serialize
    qua network nữa — Postgres tự đếm, có index PK trên company_contacts
    .company_id cho subquery NOT EXISTS.

    "Thiếu" = cột NULL hoặc chuỗi rỗng — khớp cách _normalize_company()
    bên Flask quy None -> "" rồi count_missing_fields() chỉ check falsy.

    Trả dict:
      company_health_rows: list[{"field","label","missing","total",
        "pct_missing"}] — ĐÚNG THỨ TỰ _COMPANY_HEALTH_FIELDS.
      company_health_total: tổng company active.
      company_no_contact_missing / company_no_contact_total: số company
        active chưa có contact active nào / tổng company active (2 số
        này CÙNG company_health_total nếu không lọc gì thêm, tách riêng
        để khớp đúng tên biến _status_tab_context() cũ đang dùng)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE c.tax_id IS NULL OR c.tax_id = '') AS missing_tax_id,
                COUNT(*) FILTER (WHERE c.website IS NULL OR c.website = '') AS missing_website,
                COUNT(*) FILTER (WHERE c.industry IS NULL OR c.industry = '') AS missing_industry,
                COUNT(*) FILTER (WHERE c.address IS NULL OR c.address = '') AS missing_address,
                COUNT(*) FILTER (WHERE c.company_size IS NULL OR c.company_size = '') AS missing_company_size,
                COUNT(*) FILTER (WHERE c.fanpage_url IS NULL OR c.fanpage_url = '') AS missing_fanpage,
                COUNT(*) FILTER (WHERE c.linkedin_url IS NULL OR c.linkedin_url = '') AS missing_linkedin_company,
                COUNT(*) FILTER (
                    WHERE NOT EXISTS (
                        SELECT 1 FROM company_contacts cc
                        WHERE cc.company_id = c.company_id AND cc.is_active = true
                    )
                ) AS missing_contact
            FROM companies c
            WHERE c.is_active = true
            """
        )
        row = fetch_one_row(cur)

    total = row["total"]
    company_health_rows = []
    for field, label in _COMPANY_HEALTH_FIELDS:
        missing = row[f"missing_{field}"]
        pct_missing = round(missing / total * 100) if total else 0
        company_health_rows.append({
            "field": field, "label": label, "missing": missing,
            "total": total, "pct_missing": pct_missing,
        })

    return {
        "company_health_rows": company_health_rows,
        "company_health_total": total,
        "company_no_contact_missing": row["missing_contact"],
        "company_no_contact_total": total,
    }
