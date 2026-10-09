"""
db.job_health — thống kê "tình trạng dữ liệu" của job cho tab Tình trạng
dữ liệu (tách từ scrapjd/db/jobs.py, 10/2026). Chỉ SELECT, không ghi.
"""

import psycopg2.extras


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
