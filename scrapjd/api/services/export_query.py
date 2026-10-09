"""
Export Query — lấy record theo ĐÚNG bộ lọc staff chọn ở bước preview
(status/khoảng ngày/company/limit N-mới-nhất — xem ExportFilters), khác
list_jobs()/list_companies() dùng cho UI listing vốn giới hạn limit<=200
theo phân trang khác hẳn. Format sẵn đúng cột + kiểu dữ liệu cho file
export (Requirement 11.7: date ISO 8601, Requirement 11.9: boolean
"true"/"false").

Thêm 08/2026 (export có filter + preview trước khi tải): TRƯỚC ĐÂY 3 hàm
query_*_for_export() luôn lấy TOÀN BỘ record, không filter gì (job còn
hard-code WHERE job_status='OPEN', company/contact lấy cả record đã
soft-delete) — staff không lọc được theo trạng thái/khoảng ngày/công ty,
và không xem trước được sẽ xuất ra bao nhiêu dòng, dữ liệu gì trước khi
tải file thật. Giờ mọi filter đi qua ĐÚNG 1 nguồn (ExportFilters +
_build_where), dùng CHUNG cho cả preview (trả tổng số dòng + N dòng mẫu)
lẫn export thật (trả full file) — đảm bảo "preview thấy gì thì tải đúng
cái đó", tránh lệch như 2 nơi định nghĩa filter riêng rẽ.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional

import psycopg2.extras

from scrapjd.api.services.entity_specs import get_spec


# Việt Nam dùng UTC+7 quanh năm (không có giờ mùa hè từ 1975) nên dùng offset cố
# định, không cần ZoneInfo/gói tzdata trên máy chạy. Dùng CHUNG cho hai việc cần khớp nhau:
# bộ lọc from_date/to_date (vn_day_range) và giờ hiện trong file export (_format_export_value).
_VN_TZ = timezone(timedelta(hours=7))


def vn_day_range(
    from_date: Optional[date], to_date: Optional[date]
) -> tuple[Optional[datetime], Optional[datetime]]:
    """Đổi khoảng NGÀY VIỆT NAM [from_date, to_date] (gồm cả hai đầu) thành khoảng thời điểm
    nửa mở [start, end) để so với cột TIMESTAMPTZ: start = 00:00 giờ VN của from_date,
    end = 00:00 giờ VN của NGÀY SAU to_date. Mỗi đầu là None nếu tham số tương ứng là None.

    Vì sao nửa mở thay vì `<= to_date`: so cột thời điểm với một date là so với 00:00 của ngày đó,
    nên `col <= to_date` loại gần hết ngày cuối (chỉ giữ dòng tạo đúng 00:00:00). Còn `col <
    to_date + 1 ngày` thì gồm đủ 24 giờ, và không cần đoán độ chính xác của cột (giây, micro giây).
    Cùng cách db.stats đang tính ranh giới tháng.

    Ranh giới theo giờ VN (không phải UTC) để khớp giờ hiện trong file và ngày staff nhìn thấy.
    Kết quả là datetime CÓ múi giờ, nên so sánh đúng bất kể TimeZone của session.

    to_date là ngày lớn nhất Python biểu diễn được (9999-12-31) thì không có ngày sau để làm
    đầu mút: coi như không giới hạn trên (đúng ý nghĩa «đến hết»)."""
    start = datetime.combine(from_date, time.min, tzinfo=_VN_TZ) if from_date is not None else None
    end = None
    if to_date is not None and to_date < date.max:
        end = datetime.combine(to_date + timedelta(days=1), time.min, tzinfo=_VN_TZ)
    return start, end


@dataclass
class ExportFilters:
    """Bộ lọc export — TẤT CẢ optional, kết hợp AND với nhau. Không set
    filter nào (mọi field None) = lấy toàn bộ, giữ đúng hành vi cũ.

    status: giá trị enum của status field riêng từng entity (job_status /
        contact_status) — validate ở router theo entity_specs trước khi
        tới đây, hàm ở đây không tự re-validate.
    is_active: riêng company/contact (job không có cột is_active) — lọc
        record còn hoạt động / đã soft-delete. None = lấy cả 2.
    company_id: riêng job/contact (company export theo company_id chính
        nó, không filter theo company khác).
    date_field: "created_at" | "updated_at" — cột áp from_date/to_date.
    from_date/to_date: khoảng NGÀY VIỆT NAM (UTC+7), gồm cả hai đầu, theo date_field.
        Một dòng tạo lúc 01:00 sáng 08/10 giờ VN (= 18:00 07/10 UTC) thuộc ngày 08/10,
        đúng như giờ hiện trong file. Xem vn_day_range().
    limit: lấy N dòng ĐẦU sau khi đã áp mọi filter khác + đã ORDER BY
        created_at DESC — dùng cho trường hợp "không lọc gì, chỉ cần N
        job/company/contact mới nhất". None = không giới hạn.
    """
    status: Optional[str] = None
    is_active: Optional[bool] = None
    company_id: Optional[str] = None
    date_field: str = "created_at"
    from_date: Optional[date] = None
    to_date: Optional[date] = None
    limit: Optional[int] = None


def _build_where(
    filters: ExportFilters,
    *,
    status_column: Optional[str],
    has_is_active: bool,
    company_column: Optional[str],
    date_table_alias: str,
) -> tuple[str, list]:
    """Build "WHERE ..." (chuỗi rỗng nếu không filter nào) + params theo
    ĐÚNG thứ tự %s xuất hiện trong chuỗi — dùng chung cho preview lẫn
    export thật của 1 entity, gọi bởi query_*_for_export()/
    count_rows_for_export() bên dưới.

    status_column/company_column: tên cột thật ĐÃ gồm alias bảng (vd
        "jp.job_status") — None nếu entity đó không có cột tương ứng
        (company không filter theo company_id).
    date_table_alias: alias bảng chứa created_at/updated_at (vd "jp",
        "c", "cc") — filters.date_field chỉ nhận đúng 1 trong 2 tên cột
        cố định (kiểm ở router bằng schema) nên ghép thẳng, không có
        nguy cơ SQL injection.
    """
    clauses: list[str] = []
    params: list[Any] = []

    if filters.status and status_column:
        clauses.append(f"{status_column} = %s")
        params.append(filters.status)

    if filters.is_active is not None and has_is_active:
        clauses.append("is_active = %s")
        params.append(filters.is_active)

    if filters.company_id and company_column:
        clauses.append(f"{company_column} = %s")
        params.append(filters.company_id)

    # Khoảng ngày VN -> khoảng thời điểm nửa mở [start, end), xem vn_day_range(). Cột trần (không bọc
    # hàm) nên dùng được index trên created_at/updated_at nếu sau này thêm.
    date_col = f"{date_table_alias}.{filters.date_field}"
    start, end = vn_day_range(filters.from_date, filters.to_date)
    if start is not None:
        clauses.append(f"{date_col} >= %s")
        params.append(start)
    if end is not None:
        clauses.append(f"{date_col} < %s")
        params.append(end)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


def query_jobs_for_export(conn, filters: Optional[ExportFilters] = None) -> list[dict]:
    """Không truyền filters (hoặc mọi field None) = lấy toàn bộ job mọi
    trạng thái — TRƯỚC ĐÂY hard-code WHERE job_status='OPEN', giờ 'OPEN'
    chỉ còn là 1 lựa chọn filters.status, KHÔNG còn mặc định ẩn (staff tự
    chọn trạng thái muốn xuất ở bước preview, mặc định FE nên để trống
    = tất cả, xem router)."""
    filters = filters or ExportFilters()
    where, params = _build_where(
        filters,
        status_column="jp.job_status",
        has_is_active=False,
        company_column="jp.company_id",
        date_table_alias="jp",
    )
    limit_sql = ""
    if filters.limit is not None:
        limit_sql = "LIMIT %s"
        params.append(filters.limit)

    query = f"""
        SELECT jp.job_id, c.company_name, jp.job_title, jp.matching_industry,
               l.level_code, p.province_name, jp.work_type, jp.currency,
               jp.salary_min, jp.salary_max, jp.salary_type, jp.salary_period,
               jp.deadline, jp.job_status, jp.ss_team_notes,
               jp.created_at, jp.updated_at
        FROM job_postings jp
        JOIN companies c ON c.company_id = jp.company_id
        LEFT JOIN levels l ON l.level_id = jp.level_id
        LEFT JOIN provinces p ON p.province_id = jp.province_id
        {where}
        ORDER BY jp.created_at DESC
        {limit_sql}
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return _format_rows(rows, "job")


def query_companies_for_export(conn, filters: Optional[ExportFilters] = None) -> list[dict]:
    """Không filter is_active = lấy cả company đã soft-delete (giữ đúng
    hành vi cũ) — is_active có mặt trong export_columns để người xem
    file tự biết trạng thái; staff có thể tự lọc is_active=true ở bước
    preview nếu chỉ muốn company đang hoạt động."""
    filters = filters or ExportFilters()
    where, params = _build_where(
        filters,
        status_column=None,
        has_is_active=True,
        company_column=None,
        date_table_alias="c",
    )
    limit_sql = ""
    if filters.limit is not None:
        limit_sql = "LIMIT %s"
        params.append(filters.limit)

    query = f"""
        SELECT c.company_id, c.company_name, c.tax_id, c.website, c.industry,
               c.company_size, c.address, p.province_name, c.fanpage_url,
               c.linkedin_url, c.partnership_potential, c.is_active,
               c.created_at, c.updated_at
        FROM companies c
        LEFT JOIN provinces p ON p.province_id = c.province_id
        {where}
        ORDER BY c.created_at DESC
        {limit_sql}
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return _format_rows(rows, "company")


def query_contacts_for_export(conn, filters: Optional[ExportFilters] = None) -> list[dict]:
    """Không filter is_active = lấy cả contact đã soft-delete (giữ đúng
    hành vi cũ)."""
    filters = filters or ExportFilters()
    where, params = _build_where(
        filters,
        status_column="cc.contact_status",
        has_is_active=True,
        company_column="cc.company_id",
        date_table_alias="cc",
    )
    limit_sql = ""
    if filters.limit is not None:
        limit_sql = "LIMIT %s"
        params.append(filters.limit)

    query = f"""
        SELECT cc.contact_id, c.company_name, cc.contact_name, cc.job_title,
               cc.work_email, cc.social_link, cc.phone_number, cc.found_source,
               cc.contact_status, cc.is_active, cc.created_at, cc.updated_at
        FROM company_contacts cc
        JOIN companies c ON c.company_id = cc.company_id
        {where}
        ORDER BY cc.created_at DESC
        {limit_sql}
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return _format_rows(rows, "contact")


_EXPORT_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def _format_export_value(val):
    """Giá trị 1 ô trong file export (đã loại None và bool).

    Thời điểm (datetime) xuất theo GIỜ VIỆT NAM, dạng "YYYY-MM-DD HH:MM:SS", đọc
    thẳng được trong Excel. Trước đây xuất chuỗi isoformat thô, tức giờ UTC không
    kèm nhãn, nhân viên ở VN dễ đọc nhầm thành giờ VN (lệch 7 tiếng).

    Cột created_at/updated_at là TIMESTAMPTZ (migration 0042, D3) nên psycopg2 trả datetime có
    múi giờ, quy đổi sang giờ VN. Datetime naive (DB chưa chạy 0042, cột còn là TIMESTAMP lưu
    theo UTC) vẫn được coi là UTC, nên hàm đúng ở cả hai trạng thái.
    Giá trị kiểu date thuần (vd deadline) giữ nguyên isoformat, vì không có giờ
    để quy đổi."""
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return val.astimezone(_VN_TZ).strftime(_EXPORT_DATETIME_FORMAT)
    if hasattr(val, "isoformat"):
        return val.isoformat()
    return val


def _format_rows(rows: list[dict], entity_type: str) -> list[dict]:
    spec = get_spec(entity_type)
    out = []
    for row in rows:
        formatted: dict[str, Any] = {}
        for col in spec.export_columns:
            val = row.get(col)
            if val is None:
                formatted[col] = None
            elif isinstance(val, bool):
                formatted[col] = "true" if val else "false"
            else:
                formatted[col] = _format_export_value(val)
        out.append(formatted)
    return out


# entity_type -> (bảng chính + alias, cột status, có is_active không,
# cột company_id, alias bảng chứa created_at/updated_at) — dùng bởi
# count_rows_for_export() để đếm TỔNG số dòng khớp filter cho bước
# preview (đếm bằng COUNT(*) riêng, không SELECT hết cột rồi len()).
_COUNT_TABLE_SPECS = {
    "job": ("job_postings jp", "jp.job_status", False, "jp.company_id", "jp"),
    "company": ("companies c", None, True, None, "c"),
    "contact": ("company_contacts cc", "cc.contact_status", True, "cc.company_id", "cc"),
}


def count_rows_for_export(conn, entity_type: str, filters: ExportFilters) -> int:
    """Đếm tổng số dòng khớp filters (KHÔNG áp filters.limit — limit là
    "lấy N dòng đầu SAU khi lọc", nên tổng số dòng thực tế lọc được vẫn
    cần hiện đúng cho staff biết, dù cuối cùng chỉ tải N dòng)."""
    table, status_col, has_is_active, company_col, alias = _COUNT_TABLE_SPECS[entity_type]
    where, params = _build_where(
        filters,
        status_column=status_col,
        has_is_active=has_is_active,
        company_column=company_col,
        date_table_alias=alias,
    )
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {table} {where}", params)
        return cur.fetchone()[0]


QUERY_FUNCS = {
    "job": query_jobs_for_export,
    "company": query_companies_for_export,
    "contact": query_contacts_for_export,
}
