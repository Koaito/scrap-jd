"""
db.jobs — tách từ db.py (God module) theo domain.
"""

import json
import logging
from typing import Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)


def get_open_jobs_with_source_url(conn):
    """Lấy job đang OPEN và có source_url (job crawl — job nhập tay
    KHÔNG có source_url nên tự động bị loại, không có gì để re-check).
    Dùng cho check_expired_source_jobs.py — script re-check job còn OPEN
    trong DB có còn tồn tại thật ở nguồn (TopCV/VietnamWorks) hay không.

    KHÔNG lấy job đã EXPIRED/CLOSED — không cần re-check job vốn đã
    không còn hiệu lực từ trước.

    Trả về list[(job_id, job_title, source_url, deadline)]."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT job_id, job_title, source_url, deadline
            FROM job_postings
            WHERE job_status = 'OPEN'
              AND source_url IS NOT NULL AND source_url != ''
            ORDER BY created_at
            """
        )
        return cur.fetchall()


def probe_needs_enrichment(probe) -> bool:
    """probe = kết quả find_company_probe() (company_id, website, industry,
    company_size, address) hoặc None. Trả True nếu nên gọi
    fetch_company_profile() — tức là công ty chưa từng thấy, hoặc đã thấy
    nhưng còn thiếu field nào đó trong bộ mô tả công ty.

    ĐỔI (08/2026, thêm VietnamWorks): TRƯỚC ĐÂY dùng (tax_id, website) để
    quyết định — với TopCV thì work vì công ty luôn CÓ CƠ HỘI lấy được
    tax_id (trang company profile TopCV hiển thị "Mã số thuế"). Nhưng
    VietnamWorks KHÔNG BAO GIỜ hiển thị mã số thuế công ty (đã xác nhận
    08/2026) -> nếu vẫn dùng tax_id, mọi công ty crawl từ VietnamWorks sẽ
    có tax_id RỖNG VĨNH VIỄN -> hàm này LUÔN trả True -> pipeline.py gọi
    lại fetch_company_profile() ở MỌI LẦN CRAWL cho MỌI công ty VNW, dù
    đã có đủ dữ liệu từ trước -> tốn request thừa vô hạn, ngược hẳn mục
    đích thiết kế ban đầu ("chỉ crawl công ty 1 lần").

    Giờ đổi sang: coi công ty là "đã đủ" khi đã có TẤT CẢ 4 field lấy được
    qua fetch_company_profile() (website, industry, company_size, address)
    — không quan tâm tax_id nữa (nguồn nào có thì companies.tax_id vẫn
    được lưu qua get_or_create_company_by_profile()/update_company_profile()
    như cũ, chỉ là KHÔNG dùng nó để quyết định có cần crawl lại hay không).

    Tác dụng phụ có lợi: đồng thời sửa luôn phần "công ty enrich dở dang"
    — trước đây nếu 1 lần crawl chỉ lấy được website nhưng thiếu industry
    (vd TopCV đổi label tạm thời), công ty coi như "đủ" mãi mãi vì đã có
    website; giờ sẽ tiếp tục được thử vá lại industry/company_size/address
    ở các lần crawl sau, cho tới khi đủ cả 4 field."""
    if probe is None:
        return True
    _, website, industry, company_size, address = probe
    return not (website and industry and company_size and address)


def job_exists_by_source_url(conn, source_url: str) -> bool:
    """Chống trùng theo link JD gốc — nếu link này đã crawl rồi thì bỏ qua,
    tránh insert lại job giống hệt mỗi lần chạy crawler."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM job_sources_log WHERE source_url = %s LIMIT 1",
            (source_url,),
        )
        return cur.fetchone() is not None


def get_job_probe_by_source_url(conn, source_url: str):
    """Tra cứu nhanh 1 job đã có theo source_url — trả về
    (job_id, work_type, deadline, parsed_content) hoặc None nếu job này
    chưa từng crawl.

    Dùng để quyết định có cần fetch_job_full_detail() + update lại job CŨ
    hay không (job cũ có thể được crawl từ TRƯỚC khi tính năng work_type/
    deadline/parsed_content tồn tại, nên còn thiếu các field này)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT jp.job_id, jp.work_type, jp.deadline, jp.parsed_content
            FROM job_postings jp
            JOIN job_sources_log jsl ON jsl.job_id = jp.job_id
            WHERE jsl.source_url = %s
            LIMIT 1
            """,
            (source_url,),
        )
        return cur.fetchone()


def job_needs_detail_enrichment(probe) -> bool:
    """probe = kết quả get_job_probe_by_source_url() (job_id, work_type,
    deadline, parsed_content) hoặc None. Trả True nếu nên gọi
    fetch_job_full_detail() — tức là job chưa từng thấy, hoặc đã thấy
    nhưng còn thiếu work_type/deadline/parsed_content (job cũ crawl từ
    trước khi có các tính năng này)."""
    if probe is None:
        return True
    _, work_type, deadline, parsed_content = probe
    return not work_type or not deadline or not parsed_content


def update_job_fields(conn, job_id: str, *, work_type: Optional[str] = None,
                       deadline=None, parsed_content: Optional[dict] = None) -> None:
    """Vá thêm work_type/deadline/parsed_content cho 1 job ĐÃ TỒN TẠI (chỉ
    ghi đè field nào có giá trị mới, không xóa dữ liệu cũ nếu lần crawl
    sau không lấy được field đó) — dùng cho job cũ crawl từ trước khi có
    các cột này."""
    updates = []
    values = []
    if work_type:
        updates.append("work_type = %s")
        values.append(work_type)
    if deadline:
        updates.append("deadline = %s")
        values.append(deadline)
    if parsed_content:
        updates.append("parsed_content = %s")
        values.append(json.dumps(parsed_content, ensure_ascii=False))

    if not updates:
        return

    values.append(job_id)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE job_postings SET {', '.join(updates)} WHERE job_id = %s",
            values,
        )


def insert_job(conn, *, company_id: str, job_title: str, matching_industry: str,
                level_id: Optional[int], province_id: Optional[int],
                work_type: Optional[str], currency: str,
                salary_min: Optional[int], salary_max: Optional[int],
                salary_type: str, source_url: str, source_name: str,
                salary_raw_text: str = "", deadline=None,
                parsed_content: Optional[dict] = None,
                raw_jd_content: str = "",
                salary_period: str = "MONTH",
                created_by: Optional[str] = None) -> str:
    """Insert 1 job_postings + 1 job_sources_log tương ứng. content_hash được
    trigger Postgres tự tính (xem sql/schema.sql mục 5).

    parsed_content: dict {job_description, requirements, perks,
    required_skills} -> lưu vào job_postings.parsed_content (JSONB), dùng
    để tra cứu/lọc nhanh theo từng phần đã tách sẵn.
    raw_jd_content: text đã tách theo heading (KHÔNG phải HTML thô — HTML
    thô có nhiều rác kỹ thuật như SVG/class không có giá trị tra cứu lại)
    -> lưu vào job_sources_log.raw_jd_content, làm bằng chứng gốc để đối
    chiếu khi parsed_content bị lệch.
    salary_period: "MONTH" | "YEAR" — chu kỳ trả lương của salary_min/max
    (xem normalize.NormalizedSalary.salary_period + sql/migration_add_
    salary_period.sql). Mặc định "MONTH" khớp hành vi cũ."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO job_postings (
                company_id, job_title, matching_industry, level_id, province_id,
                work_type, currency, salary_min, salary_max, salary_type,
                salary_period, job_status, source_url, deadline, parsed_content,
                created_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN', %s, %s, %s, %s)
            RETURNING job_id
            """,
            (company_id, job_title, matching_industry, level_id, province_id,
             work_type, currency, salary_min, salary_max, salary_type, salary_period,
             source_url, deadline,
             json.dumps(parsed_content, ensure_ascii=False) if parsed_content else None,
             created_by),
        )
        job_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO job_sources_log (job_id, source_name, source_url,
                                          salary_raw_content, raw_jd_content)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (job_id, source_url) DO NOTHING
            """,
            (job_id, source_name, source_url, salary_raw_text, raw_jd_content or None),
        )
        return str(job_id)


def count_jobs(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM job_postings")
        return cur.fetchone()[0]


def find_manual_job_duplicate(conn, *, company_id: str, job_title: str,
                               level_id: Optional[int],
                               province_id: Optional[int]) -> Optional[str]:
    """Tìm job đã tồn tại TRÙNG (company_id, job_title, level_id,
    province_id) — CÙNG bộ khoá mà trigger Postgres generate_job_hash()
    dùng để tính content_hash (xem sql/schema.sql mục 5) — dùng để chống
    trùng khi POST /jobs bị gọi nhiều lần với data y hệt.

    TẠI SAO CẦN HÀM RIÊNG (không tái dùng job_exists_by_source_url() có
    sẵn): job crawl chống trùng theo source_url (link JD gốc, ổn định,
    duy nhất) — nhưng job NHẬP TAY qua create_manual_job() không có link
    gốc thật, source_url tự sinh NGẪU NHIÊN mỗi lần gọi
    ("manual://<uuid4-mới>") NÊN LUÔN LUÔN KHÁC NHAU -> cơ chế chống
    trùng theo source_url KHÔNG BAO GIỜ bắt được job nhập tay bị gửi lặp
    (vd người dùng bấm "Execute" trên Swagger nhiều lần, hoặc double-
    click nút Submit ở frontend sau này) -> mỗi lần bấm tạo 1 job_id mới
    dù nội dung y hệt (phát hiện qua test thật 08/2026).

    So khớp job_title không phân biệt hoa/thường + bỏ khoảng trắng thừa
    (giống cách content_hash chuẩn hoá) — level_id/province_id dùng
    IS NOT DISTINCT FROM để so khớp đúng cả trường hợp NULL (khác NULL
    != NULL thông thường của SQL, nếu dùng = thường sẽ luôn False khi 1
    trong 2 bên NULL, bỏ sót trường hợp cả 2 cùng thiếu level/province).

    Trả về job_id đã có (str) nếu tìm thấy trùng, None nếu chưa có."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT job_id FROM job_postings
            WHERE company_id = %s
              AND lower(trim(job_title)) = lower(trim(%s))
              AND level_id IS NOT DISTINCT FROM %s
              AND province_id IS NOT DISTINCT FROM %s
              AND job_status != 'CLOSED'
            LIMIT 1
            """,
            (company_id, job_title, level_id, province_id),
        )
        row = cur.fetchone()
        return str(row[0]) if row else None


def create_manual_job(conn, *, job_title: str, company_id: str,
                       matching_industry: str = "",
                       level_id: Optional[int] = None,
                       province_id: Optional[int] = None,
                       work_type: Optional[str] = None,
                       currency: str = "VNĐ",
                       salary_min: Optional[int] = None,
                       salary_max: Optional[int] = None,
                       salary_type: str = "NEGOTIABLE",
                       salary_period: str = "MONTH",
                       deadline=None,
                       parsed_content: Optional[dict] = None,
                       created_by: Optional[str] = None) -> str:
    """Tạo 1 job NHẬP TAY từ frontend (không qua crawl/adapter). Tái dùng
    thẳng insert_job() đã có sẵn cho pipeline crawl — cùng 1 hàm ghi, chỉ
    khác nguồn gọi tới, tránh viết trùng logic INSERT job_postings +
    job_sources_log.

    IDEMPOTENT (08/2026, vá bug trùng job — xem find_manual_job_duplicate()):
    kiểm tra trùng TRƯỚC khi insert — nếu đã có job cùng (company_id,
    job_title, level_id, province_id) VÀ CHƯA bị đóng (job_status !=
    'CLOSED'), trả về job_id ĐÃ CÓ đó thay vì tạo mới. An toàn khi bấm
    "Execute"/Submit nhiều lần với data y hệt (double-click, F5, gọi lại
    do timeout tưởng lỗi...). Chỉ bỏ qua job đã CLOSED khi so khớp — cho
    phép tạo lại 1 job y hệt title/company nếu job cũ đã bị đóng có chủ
    đích (không coi đó là "trùng ngoài ý muốn").

    KHÁC job crawl ở 2 điểm, để phân biệt rõ trong dữ liệu:
    - source_name = 'MANUAL' (thay vì 'TopCV'/'VietnamWorks').
    - source_url tự sinh dạng 'manual://<uuid>' — job nhập tay không có
      link JD gốc thật, nhưng job_sources_log.source_url là NOT NULL về
      mặt logic nghiệp vụ (dùng làm khoá chống trùng cho job crawl) nên
      cần 1 giá trị duy nhất thay vì để trống, tránh nhầm với chuỗi rỗng
      ở nơi khác trong code đang coi "" là chưa có giá trị.

    parsed_content (thêm 08/2026, xem lịch sử trao đổi): dict
    {job_description, requirements, perks, required_skills} — trước đây
    job nhập tay KHÔNG có chỗ lưu mô tả JD chi tiết (chỉ job crawl mới
    có), giờ mở field này cho cả 2 nguồn, dùng chung 1 cột JSONB
    job_postings.parsed_content, cùng cấu trúc pipeline crawl đang
    dùng (xem pipeline._build_parsed_content_and_raw()).

    salary_period (thêm 08/2026, xem sql/migration_add_salary_period.sql):
    "MONTH" | "YEAR" — mặc định "MONTH". Job nhập tay KHÔNG qua
    normalize_salary() (staff tự gõ salary_min/max sẵn số VNĐ/USD), nên
    KHÔNG tự suy luận được period từ text như job crawl — staff phải tự
    chọn đúng "YEAR" qua API nếu nhập lương năm, nếu không sẽ mặc định
    hiểu là lương/tháng (giữ nguyên hành vi trước khi có cột này)."""
    existing_job_id = find_manual_job_duplicate(
        conn, company_id=company_id, job_title=job_title,
        level_id=level_id, province_id=province_id,
    )
    if existing_job_id:
        logger.info(
            "POST /jobs trùng (company_id=%s, job_title=%r, level_id=%s, "
            "province_id=%s) -> trả về job đã có %s, KHÔNG tạo mới.",
            company_id, job_title, level_id, province_id, existing_job_id,
        )
        return existing_job_id

    import uuid
    source_url = f"manual://{uuid.uuid4()}"
    return insert_job(
        conn,
        company_id=company_id,
        job_title=job_title,
        matching_industry=matching_industry,
        level_id=level_id,
        province_id=province_id,
        work_type=work_type,
        currency=currency,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_type=salary_type,
        salary_period=salary_period,
        source_url=source_url,
        source_name="MANUAL",
        deadline=deadline,
        parsed_content=parsed_content,
        created_by=created_by,
    )


def update_job(conn, job_id: str, *, job_title: Optional[str] = None,
               matching_industry: Optional[str] = None,
               level_id: Optional[int] = None,
               province_id: Optional[int] = None,
               work_type: Optional[str] = None,
               currency: Optional[str] = None,
               salary_min: Optional[int] = None,
               salary_max: Optional[int] = None,
               salary_type: Optional[str] = None,
               salary_period: Optional[str] = None,
               deadline=None,
               job_status: Optional[str] = None,
               ss_team_notes: Optional[str] = None,
               parsed_content: Optional[dict] = None,
               updated_by: Optional[str] = None) -> bool:
    """Sửa TỰ DO các field của 1 job đã tồn tại — dùng cho PATCH /jobs/{id}
    phía frontend. KHÔNG phân biệt job crawl hay job nhập tay (team không
    cần phân quyền, mọi người dùng nội bộ ngang quyền — xem quyết định
    thiết kế trong API_README.md).

    Cũng là cách "xoá mềm" 1 job: gọi update_job(job_id, job_status='CLOSED')
    thay vì DELETE thật — job vẫn còn trong DB nên KHÔNG bị crawl lại tạo
    trùng ở lượt crawl sau (get_job_probe_by_source_url() vẫn thấy job
    này qua job_sources_log, không insert lại).

    salary_min/salary_max: CHO PHÉP truyền 0 (khác None) — vd người dùng
    muốn xoá lương cũ, sửa lại "Thoả thuận" (NEGOTIABLE, cả 2 đều None).
    Vì vậy dùng cờ has_* để phân biệt "không truyền field này" (giữ
    nguyên) với "truyền None có chủ đích" (xoá giá trị cũ) — khác các
    hàm update_* khác trong file này vốn coi giá trị rỗng/None là "bỏ
    qua", ở đây cần phân biệt rõ hơn vì lương là field có thể cố ý set
    về rỗng.

    parsed_content (thêm 08/2026): gửi dict {job_description, requirements,
    perks, required_skills} sẽ GHI ĐÈ TOÀN BỘ giá trị cũ (không merge
    từng key con — client tự gộp với giá trị cũ nếu chỉ muốn sửa 1 phần,
    lấy giá trị cũ qua GET /jobs/{id} trước khi PATCH).

    salary_period (thêm 08/2026): "MONTH" | "YEAR" — dùng pattern optional
    thường (bỏ qua nếu None) giống salary_type, KHÔNG dùng cờ has_* như
    salary_min/max, vì đây là enum chữ chứ không phải số — không có
    trường hợp "0 khác None" cần phân biệt ở đây.

    Trả False nếu job_id không tồn tại (không có gì để update), True nếu
    đã update thành công — route dùng giá trị này để trả 404 đúng lúc."""
    updates = []
    values = []

    if updated_by is not None:
        updates.append("updated_by = %s")
        values.append(updated_by)
    if job_title is not None:
        updates.append("job_title = %s")
        values.append(job_title)
    if matching_industry is not None:
        updates.append("matching_industry = %s")
        values.append(matching_industry)
    if level_id is not None:
        updates.append("level_id = %s")
        values.append(level_id)
    if province_id is not None:
        updates.append("province_id = %s")
        values.append(province_id)
    if work_type is not None:
        updates.append("work_type = %s")
        values.append(work_type)
    if currency is not None:
        updates.append("currency = %s")
        values.append(currency)
    if salary_min is not None:
        updates.append("salary_min = %s")
        values.append(salary_min)
    if salary_max is not None:
        updates.append("salary_max = %s")
        values.append(salary_max)
    if salary_type is not None:
        updates.append("salary_type = %s")
        values.append(salary_type)
    if salary_period is not None:
        updates.append("salary_period = %s")
        values.append(salary_period)
    if deadline is not None:
        updates.append("deadline = %s")
        values.append(deadline)
    if job_status is not None:
        updates.append("job_status = %s")
        values.append(job_status)
    if ss_team_notes is not None:
        updates.append("ss_team_notes = %s")
        values.append(ss_team_notes)
    if parsed_content is not None:
        updates.append("parsed_content = %s")
        values.append(json.dumps(parsed_content, ensure_ascii=False))

    if not updates:
        return job_exists_by_id(conn, job_id)

    values.append(job_id)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE job_postings SET {', '.join(updates)} WHERE job_id = %s",
            values,
        )
        return cur.rowcount > 0


def job_exists_by_id(conn, job_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM job_postings WHERE job_id = %s LIMIT 1", (job_id,))
        return cur.fetchone() is not None


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


def list_jobs(conn, *, industry: Optional[str] = None, province_name: Optional[str] = None,
              level_code: Optional[str] = None, work_type: Optional[str] = None,
              keyword: Optional[str] = None, job_status: Optional[str] = None,
              created_by: Optional[str] = None,
              limit: int = 50, offset: int = 0,
              include_content: bool = False):
    """Trả (list[dict] job, total_count) — dùng cho GET /jobs.

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

    limit/offset: phân trang chuẩn — FastAPI route validate limit tối
    đa (tránh client xin limit=999999 kéo sập DB), hàm này KHÔNG tự
    giới hạn, cứ tin tưởng giá trị truyền vào."""
    conditions = []
    params: list = []

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

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SELECT count(*) AS total FROM job_postings jp "
                    f"JOIN companies c ON c.company_id = jp.company_id "
                    f"LEFT JOIN levels l ON l.level_id = jp.level_id "
                    f"LEFT JOIN provinces p ON p.province_id = jp.province_id "
                    f"{where_clause}", params)
        total = cur.fetchone()["total"]

        # include_content=True: thêm jp.parsed_content vào SELECT list —
        # KHÔNG dùng chung _JOB_LIST_BASE_QUERY (hằng số dựng sẵn không
        # có cột này) mà chèn thêm cột ngay trước FROM, giữ nguyên mọi
        # cột khác + thứ tự JOIN/WHERE/ORDER BY như cũ.
        select_columns = (
            f"{_JOB_SELECT_COLUMNS}, jp.parsed_content" if include_content
            else _JOB_SELECT_COLUMNS
        )
        cur.execute(
            f"SELECT {select_columns} {_JOB_FROM_JOINS} {where_clause} "
            f"ORDER BY jp.created_at DESC LIMIT %s OFFSET %s",
            params + [limit, offset],
        )
        rows = cur.fetchall()

    return rows, total


# Field nào tính vào thống kê "thiếu dữ liệu" ở tab Tình trạng dữ liệu
# (blueprints/crawl_status.py bên mindx-jobs) — PHẢI khớp field/label/
# THỨ TỰ với JOB_HEALTH_FIELDS (crawler_client/jobs.py bên Flask).
# Trùng lặp CÓ CHỦ Ý (2 repo riêng biệt, không import chéo được). Sửa 1
# bên PHẢI sửa bên kia. Không đưa "salary" vào (khớp quyết định 08/2026
# bên Flask — "Thỏa thuận" là trạng thái hợp lệ, không phải thiếu).
_JOB_HEALTH_FIELDS = [
    ("skills", "Kỹ năng"),
    ("requirements", "Yêu cầu công việc"),
    ("benefits", "Phúc lợi"),
    ("description", "Mô tả công việc"),
    ("deadline", "Hạn nộp"),
]

# CTE dùng chung cho cả 4 khối của get_job_data_health() — tính sẵn 1
# lần/dòng job: company_name, source_name (subquery job_sources_log mới
# nhất, GIỐNG HỆT _JOB_SELECT_COLUMNS phía trên — KHÔNG dùng lại hằng
# đó trực tiếp vì hằng đó còn kèm nhiều cột khác không cần ở đây), và 5
# cờ boolean "thiếu field" khớp ĐÚNG logic _normalize_job() bên Flask:
#   skills thiếu <=> parsed_content->'required_skills' không phải mảng
#     JSONB thật (thiếu key/null/kiểu khác) HOẶC mảng rỗng — jsonb_typeof
#     kiểm tra kiểu TRƯỚC khi gọi jsonb_array_length (tránh lỗi nếu giá
#     trị không phải mảng — vd null literal hoặc kiểu khác do dữ liệu
#     hỏng).
#   requirements/benefits/description thiếu <=> ->>'key' NULL hoặc rỗng
#     (parsed_content->>'key' tự trả NULL nếu cả parsed_content lẫn key
#     đó không tồn tại — không cần check riêng "parsed_content IS NULL").
#   deadline thiếu <=> cột deadline NULL.
_JOB_FLAGS_CTE = """
    WITH job_flags AS (
        SELECT
            jp.job_id, jp.company_id, c.company_name, jp.job_title,
            jp.deadline, jp.job_status,
            (
                SELECT jsl.source_name FROM job_sources_log jsl
                WHERE jsl.job_id = jp.job_id
                ORDER BY jsl.collected_date DESC, jsl.log_id DESC
                LIMIT 1
            ) AS source_name,
            CASE
                WHEN jsonb_typeof(jp.parsed_content -> 'required_skills') IS DISTINCT FROM 'array' THEN true
                WHEN jsonb_array_length(jp.parsed_content -> 'required_skills') = 0 THEN true
                ELSE false
            END AS missing_skills,
            (jp.parsed_content ->> 'requirements' IS NULL OR jp.parsed_content ->> 'requirements' = '') AS missing_requirements,
            (jp.parsed_content ->> 'perks' IS NULL OR jp.parsed_content ->> 'perks' = '') AS missing_benefits,
            (jp.parsed_content ->> 'job_description' IS NULL OR jp.parsed_content ->> 'job_description' = '') AS missing_description,
            (jp.deadline IS NULL) AS missing_deadline
        FROM job_postings jp
        JOIN companies c ON c.company_id = jp.company_id
    )
"""


def _job_health_rows_from_counts(row) -> list:
    """Dựng list[{"field","label","missing","total","pct_missing"}] từ 1
    RealDictRow có cột total + missing_<field> — dùng chung cho tổng
    field health lẫn từng nhóm breakdown theo nguồn (job_health_by_source)."""
    total = row["total"]
    rows = []
    for field, label in _JOB_HEALTH_FIELDS:
        missing = row[f"missing_{field}"]
        pct_missing = round(missing / total * 100) if total else 0
        rows.append({
            "field": field, "label": label, "missing": missing,
            "total": total, "pct_missing": pct_missing,
        })
    return rows


def get_job_data_health(conn, today=None) -> dict:
    """Thay thế cho việc frontend (blueprints/crawl_status.py bên
    mindx-jobs) từng phải gọi list_all_jobs(include_content=True) — kéo
    TOÀN BỘ job + cột parsed_content (JSONB dài) của cả hệ thống về
    Flask, rồi tự đếm/group/tìm trùng bằng Python (job_field_health()/
    list_expired_open_jobs()/job_health_by_source()/
    find_duplicate_job_groups() ở crawler_client/jobs.py) — nặng nhất
    trong các route từng bị audit (thêm cả field JSONB dài so với case
    /companies cũ).

    Tính bằng SQL, dựa trên 1 CTE dùng chung (_JOB_FLAGS_CTE, xem
    docstring ở đó) cho cả 4 khối UI — mỗi khối 1 lượt SELECT riêng
    (KHÔNG gộp 1 câu duy nhất vì 4 khối cần GROUP BY/WHERE khác nhau,
    gộp sẽ phải CROSS JOIN nhiều CTE phụ, khó đọc hơn không giúp nhanh
    hơn đáng kể) — vẫn RẺ HƠN NHIỀU so với cách cũ vì KHÔNG BAO GIỜ
    serialize parsed_content (JSONB dài) qua network, Postgres chỉ trả
    về boolean/text ngắn đã tính sẵn.

    today: ngày để so sánh "đã hết hạn" — mặc định None -> tự lấy
    date.today() (giờ server chạy Postgres/API, KHÔNG có helper now_vn()
    ở backend này — khác helpers.now_vn() bên Flask). Lệch múi giờ chỉ
    ảnh hưởng job có deadline đúng NGÀY hiện tại, chấp nhận được cho 1
    trang thống kê tham khảo, không phải nghiệp vụ tính tiền/hạn chót
    pháp lý. Cho phép truyền vào để test dễ hơn.

    Trả dict:
      job_health_rows: list field health TỔNG (đúng thứ tự
        _JOB_HEALTH_FIELDS).
      job_health_total: tổng job.
      expired_open_jobs: list[{"id","position","company","deadline",
        "source"}] — OPEN + deadline < today, mới hết hạn gần đây nhất
        lên đầu (deadline DESC) — hữu ích hơn cũ ASC vì admin thường cần
        xử lý job MỚI hết hạn trước (job hết hạn lâu rồi ít khẩn cấp
        hơn, đã "cũ" sẵn trong nhận thức người xem).
      job_health_by_source: list[{"source","total","rows"}] — group
        theo source_name, "" hiển thị "Không rõ nguồn" (khớp hành vi cũ),
        sort total giảm dần.
      duplicate_job_groups: list[{"company","position","jobs":[...]}] —
        group theo (company_id, lower(trim(job_title))), CHỈ job OPEN,
        HAVING >= 2, sort số job trong nhóm giảm dần."""
    import datetime as _dt
    if today is None:
        today = _dt.date.today()

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # (a) Field health tổng trên toàn bộ job.
        cur.execute(
            _JOB_FLAGS_CTE + """
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE missing_skills) AS missing_skills,
                COUNT(*) FILTER (WHERE missing_requirements) AS missing_requirements,
                COUNT(*) FILTER (WHERE missing_benefits) AS missing_benefits,
                COUNT(*) FILTER (WHERE missing_description) AS missing_description,
                COUNT(*) FILTER (WHERE missing_deadline) AS missing_deadline
            FROM job_flags
            """
        )
        total_row = cur.fetchone()
        job_health_rows = _job_health_rows_from_counts(total_row)
        job_health_total = total_row["total"]

        # (b) Breakdown field health theo nguồn crawl.
        cur.execute(
            _JOB_FLAGS_CTE + """
            SELECT
                COALESCE(source_name, '') AS source_name,
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE missing_skills) AS missing_skills,
                COUNT(*) FILTER (WHERE missing_requirements) AS missing_requirements,
                COUNT(*) FILTER (WHERE missing_benefits) AS missing_benefits,
                COUNT(*) FILTER (WHERE missing_description) AS missing_description,
                COUNT(*) FILTER (WHERE missing_deadline) AS missing_deadline
            FROM job_flags
            GROUP BY COALESCE(source_name, '')
            ORDER BY total DESC
            """
        )
        source_rows = cur.fetchall()

        # (c) Job hết hạn (deadline < today) nhưng vẫn OPEN.
        cur.execute(
            _JOB_FLAGS_CTE + """
            SELECT job_id, job_title, company_name, deadline, source_name
            FROM job_flags
            WHERE job_status = 'OPEN' AND deadline IS NOT NULL AND deadline < %s
            ORDER BY deadline DESC
            """,
            (today,),
        )
        expired_rows = cur.fetchall()

        # (d) Job nghi trùng — cùng company + cùng vị trí (bỏ khoảng
        # trắng thừa, không phân biệt hoa/thường), đang OPEN, >= 2 job.
        # Dùng subquery riêng tính nhóm nào đạt HAVING >= 2 rồi JOIN lại
        # để lấy đủ chi tiết từng job trong nhóm đó (không thể vừa
        # GROUP BY vừa lấy chi tiết từng dòng trong 1 SELECT phẳng).
        cur.execute(
            _JOB_FLAGS_CTE + """
            SELECT jf.job_id, jf.job_title, jf.company_id, jf.company_name,
                   jf.deadline, jf.source_name,
                   lower(trim(jf.job_title)) AS position_key
            FROM job_flags jf
            JOIN (
                SELECT company_id, lower(trim(job_title)) AS position_key
                FROM job_flags
                WHERE job_status = 'OPEN' AND job_title IS NOT NULL AND trim(job_title) <> ''
                GROUP BY company_id, lower(trim(job_title))
                HAVING COUNT(*) >= 2
            ) dup ON dup.company_id = jf.company_id
                 AND dup.position_key = lower(trim(jf.job_title))
            WHERE jf.job_status = 'OPEN'
            ORDER BY jf.company_id, position_key, jf.job_title
            """
        )
        dup_rows = cur.fetchall()

    def _job_row_out(row) -> dict:
        return {
            "id": row["job_id"], "position": row["job_title"],
            "company": row["company_name"], "deadline": row["deadline"],
            "source": row["source_name"] or "",
        }

    job_health_by_source = [
        {
            "source": r["source_name"] or "Không rõ nguồn",
            "total": r["total"],
            "rows": _job_health_rows_from_counts(r),
        }
        for r in source_rows
    ]

    expired_open_jobs = [_job_row_out(r) for r in expired_rows]

    dup_groups: dict = {}
    for r in dup_rows:
        key = (r["company_id"], r["position_key"])
        dup_groups.setdefault(key, {
            "company": r["company_name"], "position": r["job_title"], "jobs": [],
        })["jobs"].append(_job_row_out(r))
    duplicate_job_groups = sorted(dup_groups.values(), key=lambda g: len(g["jobs"]), reverse=True)

    return {
        "job_health_rows": job_health_rows,
        "job_health_total": job_health_total,
        "expired_open_jobs": expired_open_jobs,
        "job_health_by_source": job_health_by_source,
        "duplicate_job_groups": duplicate_job_groups,
    }


def get_job_by_id(conn, job_id: str):
    """Trả 1 dict job đầy đủ (kèm parsed_content JSONB) hoặc None nếu
    không tìm thấy — dùng cho GET /jobs/{job_id}."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_JOB_SELECT_COLUMNS}, jp.parsed_content, jp.ss_team_notes "
            f"{_JOB_FROM_JOINS} "
            f"WHERE jp.job_id = %s",
            (job_id,),
        )
        return cur.fetchone()


def get_jobs_by_company_id(conn, company_id: str):
    """Trả list[dict] toàn bộ job đang mở của 1 công ty — dùng cho
    GET /companies/{company_id}/jobs (chi tiết công ty kèm job liên
    quan, tiện cho trang detail phía frontend)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"{_JOB_LIST_BASE_QUERY} WHERE jp.company_id = %s "
            f"ORDER BY jp.created_at DESC",
            (company_id,),
        )
        return cur.fetchall()
