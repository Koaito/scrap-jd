"""
db.job_queries — truy vấn ĐỌC danh sách / chi tiết job cho API (tách từ
scrapjd/db/jobs.py, 10/2026). Chỉ SELECT, không ghi. Ghi job (insert/update/
tạo tay) nằm ở scrapjd/db/jobs.py; thống kê "tình trạng dữ liệu" ở scrapjd/db/job_health.py.
Tên hàm giữ nguyên và vẫn gọi được qua `db.list_jobs`, `db.get_job_by_id`...
"""

from typing import Any, Optional

import psycopg2.extras
from scrapjd.db.pg_types import Conn, Row, fetch_all_rows, fetch_one_row, fetch_optional_row


_JOB_SELECT_COLUMNS = """
        jp.job_id, jp.job_title, jp.matching_industry, jp.work_type,
        jp.currency, jp.salary_min, jp.salary_max, jp.salary_type, jp.salary_period,
        jp.deadline, jp.job_status, jp.source_url, jp.created_at, jp.updated_at,
        jp.created_by, jp.updated_by,
        c.company_id, c.company_name,
        l.level_code,
        p.province_name,
        (
            SELECT jsl.source_name FROM job_sources_log jsl
            WHERE jsl.job_id = jp.job_id
            ORDER BY jsl.collected_date DESC, jsl.log_id DESC
            LIMIT 1
        ) AS source_name
"""


_JOB_FROM_JOINS = """
    FROM job_postings jp
    JOIN companies c ON c.company_id = jp.company_id
    LEFT JOIN levels l ON l.level_id = jp.level_id
    LEFT JOIN provinces p ON p.province_id = jp.province_id
"""


_JOB_LIST_BASE_QUERY = f"SELECT {_JOB_SELECT_COLUMNS} {_JOB_FROM_JOINS}"


def list_jobs(conn: Conn, *, industry: Optional[str] = None, province_name: Optional[str] = None,
              level_code: Optional[str] = None, work_type: Optional[str] = None,
              keyword: Optional[str] = None, job_status: Optional[str] = None,
              created_by: Optional[str] = None, ids: Optional[list[str]] = None,
              limit: int = 50, offset: int = 0,
              cursor: Optional[tuple[Any, Any]] = None,
              include_content: bool = False) -> tuple[list[Row], int, Optional[tuple[Any, Any]]]:
    """Trả (list[dict] job, total_count, next_cursor) — dùng cho GET
    /jobs. `next_cursor` là tuple (created_at, job_id) của dòng cuối
    cùng trong batch vừa trả, hoặc None nếu đây là dòng cuối — tầng API
    (scrapjd/api/routers/jobs.py) chịu trách nhiệm encode/decode tuple này
    thành chuỗi opaque, hàm này chỉ làm việc với tuple thô.

    Mọi filter đều optional, bỏ qua field nào = None. `keyword` so khớp
    kiểu ILIKE trên job_title (không phân biệt hoa/thường, không cần
    khớp chính xác) — đủ dùng cho ô tìm kiếm đơn giản, KHÔNG phải full-
    text search (nếu sau này cần search nhanh trên dữ liệu lớn, nên
    thêm GIN index + to_tsvector riêng, không sửa hàm này vội).

    created_by: lọc job do 1 thành viên ss_team/admin cụ thể tự nhập
    tay (xem sql/migration_add_audit_columns.sql) — dùng cho trang
    "theo dõi hoạt động" nội bộ (08/2026), KHÔNG khớp job crawl tự động
    (created_by luôn NULL với job crawl, nên filter này không bao giờ
    trả về job crawl dù truyền UUID nào).

    include_content: mặc định False — GIỮ NGUYÊN hành vi cũ, KHÔNG select
    cột parsed_content (JSONB, có thể dài ~2000 ký tự/job) để list nhẹ
    payload cho phần lớn use-case (route public GET /jobs, kể cả trang
    tuyển dụng công khai, đa số chỉ cần tên/lương/company, không cần mô
    tả đầy đủ). Truyền True khi CẦN đủ nội dung (job_description/
    requirements/perks/required_skills) ngay ở list, thay vì phải gọi
    riêng get_job_by_id() cho từng job — vd tab "Tình trạng dữ liệu"
    (08/2026, xem thảo luận: trước đây tab này gọi list_jobs() mặc định
    và báo sai 100% job thiếu nội dung, dù dữ liệu có đủ trong DB, chỉ
    vì list không trả cột này).

    limit/offset: phân trang chuẩn (chế độ "Trang X/Y" ở index.html) —
    FastAPI route validate limit tối đa (tránh client xin limit=999999
    kéo sập DB), hàm này KHÔNG tự giới hạn, cứ tin tưởng giá trị
    truyền vào.

    cursor: keyset pagination (thêm 09/2026, chế độ "cuộn vô hạn" ở
    index.html, xem lịch sử trao đổi "2 chế độ phân trang + toggle") —
    tuple (created_at, job_id) của dòng CUỐI CÙNG client đã nhận ở lần
    gọi trước, hoặc None nếu đây là lần gọi đầu tiên. Khi có giá trị,
    HÀM NÀY BỎ QUA `offset` (tầng API phải tự đảm bảo không truyền cả
    2 cùng lúc — xem validate ở scrapjd/api/routers/jobs.py, không validate lại
    ở đây để giữ hàm DB thuần, không biết về HTTP 422).

    Vì sao cần job_id làm khóa phụ: nếu chỉ ORDER BY created_at DESC,
    2 job cùng crawl 1 batch có thể trùng created_at tới micro-giây,
    khiến thứ tự giữa chúng không xác định — offset-pagination cũ
    "sống được" với rủi ro này (chỉ lệch/lặp hiếm khi có insert xen
    giữa lúc đang phân trang), nhưng cursor-pagination BẮT BUỘC thứ tự
    tuyệt đối ổn định (so sánh tuple (created_at, job_id) < cursor),
    nên thêm `jp.job_id DESC` làm khóa phụ luôn cho CẢ 2 chế độ — không
    đổi kết quả nhìn thấy được ở chế độ offset (chỉ phá tie 1 cách
    quyết định thay vì tùy Postgres), an toàn giữ nguyên.

    ids (thêm khi migrate Next.js, Phần 5 mục 10 của plan): lọc đúng 1
    tập job_id cho trước, dùng `jp.job_id = ANY(%s)` — gom N lần gọi
    GET /jobs/{id} riêng lẻ (vd trang "Job đã lưu", chỉ có sẵn danh
    sách job_id từ GET /me/saved-jobs, không có nội dung job kèm theo)
    thành đúng 1 query. KẾT HỢP ĐƯỢC với mọi filter khác ở trên (AND
    chung, không phải OR/thay thế) — vd `ids=...&status=OPEN` vẫn hợp
    lệ, lọc trong đúng tập id đó theo status. Không tự ép limit theo
    len(ids) — caller (scrapjd/api/routers/jobs.py) tự chịu trách nhiệm truyền
    limit đủ lớn nếu muốn lấy đủ toàn bộ id đã liệt kê trong 1 lần gọi,
    hàm này giữ nguyên hành vi limit/offset/cursor như filter khác."""
    conditions = []
    params: list[Any] = []

    if industry:
        conditions.append("jp.matching_industry = %s")
        params.append(industry)
    if province_name:
        conditions.append("p.province_name = %s")
        params.append(province_name)
    if level_code:
        conditions.append("l.level_code = %s")
        params.append(level_code)
    if work_type:
        conditions.append("jp.work_type = %s")
        params.append(work_type)
    if job_status:
        conditions.append("jp.job_status = %s")
        params.append(job_status)
    if keyword:
        conditions.append("jp.job_title ILIKE %s")
        params.append(f"%{keyword}%")
    if created_by:
        conditions.append("jp.created_by = %s")
        params.append(created_by)
    if ids:
        conditions.append("jp.job_id = ANY(%s::uuid[])")
        params.append(ids)

    # cursor riêng biệt với các filter khác — luôn ANDed thêm vào SAU
    # (không ảnh hưởng câu COUNT(*) ở dưới, vì COUNT vẫn cần đếm ĐÚNG
    # tổng số dòng khớp filter, không phụ thuộc client đang cuộn tới
    # đâu).
    list_conditions = list(conditions)
    list_params = list(params)
    if cursor is not None:
        list_conditions.append("(jp.created_at, jp.job_id) < (%s, %s)")
        list_params.extend([cursor[0], cursor[1]])

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    list_where_clause = f"WHERE {' AND '.join(list_conditions)}" if list_conditions else ""

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SELECT count(*) AS total FROM job_postings jp "
                    f"JOIN companies c ON c.company_id = jp.company_id "
                    f"LEFT JOIN levels l ON l.level_id = jp.level_id "
                    f"LEFT JOIN provinces p ON p.province_id = jp.province_id "
                    f"{where_clause}", params)
        total = fetch_one_row(cur)["total"]

        # include_content=True: thêm jp.parsed_content vào SELECT list —
        # KHÔNG dùng chung _JOB_LIST_BASE_QUERY (hằng số dựng sẵn không
        # có cột này) mà chèn thêm cột ngay trước FROM, giữ nguyên mọi
        # cột khác + thứ tự JOIN/WHERE/ORDER BY như cũ.
        select_columns = (
            f"{_JOB_SELECT_COLUMNS}, jp.parsed_content" if include_content
            else _JOB_SELECT_COLUMNS
        )
        if cursor is not None:
            cur.execute(
                f"SELECT {select_columns} {_JOB_FROM_JOINS} {list_where_clause} "
                f"ORDER BY jp.created_at DESC, jp.job_id DESC LIMIT %s",
                list_params + [limit],
            )
        else:
            cur.execute(
                f"SELECT {select_columns} {_JOB_FROM_JOINS} {list_where_clause} "
                f"ORDER BY jp.created_at DESC, jp.job_id DESC LIMIT %s OFFSET %s",
                list_params + [limit, offset],
            )
        rows = fetch_all_rows(cur)

    next_cursor = None
    if len(rows) == limit:
        last = rows[-1]
        next_cursor = (last["created_at"], last["job_id"])

    return rows, total, next_cursor


def get_job_by_id(conn: Conn, job_id: str) -> Optional[Row]:
    """Trả 1 dict job đầy đủ (kèm parsed_content JSONB) hoặc None nếu
    không tìm thấy — dùng cho GET /jobs/{job_id}."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_JOB_SELECT_COLUMNS}, jp.parsed_content, jp.ss_team_notes "
            f"{_JOB_FROM_JOINS} "
            f"WHERE jp.job_id = %s",
            (job_id,),
        )
        return fetch_optional_row(cur)


def get_jobs_by_company_id(conn: Conn, company_id: str) -> list[Row]:
    """Trả list[dict] toàn bộ job đang mở của 1 công ty — dùng cho
    GET /companies/{company_id}/jobs (chi tiết công ty kèm job liên
    quan, tiện cho trang detail phía frontend)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"{_JOB_LIST_BASE_QUERY} WHERE jp.company_id = %s "
            f"ORDER BY jp.created_at DESC",
            (company_id,),
        )
        return fetch_all_rows(cur)
