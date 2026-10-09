"""
db.job_duplicates — phần chạy SQL (CHỈ ĐỌC) của báo cáo job nghi trùng
(`python main.py report-duplicates`, xem scrapjd/cli/duplicate_report.py, Phần 3a). Logic phân
loại/đề xuất là hàm THUẦN ở scrapjd/cli/duplicate_report.py; ở đây chỉ lấy dữ liệu.

Khác v_duplicate_job_candidates (gom theo dedup_key = công ty + tiêu đề chuẩn hoá + tỉnh): ở
đây gom rộng hơn, theo (company_id, tiêu đề chuẩn hoá), để thấy thêm các cặp khác tỉnh (nhiều
khả năng là tin của từng chi nhánh, tầng "province" của báo cáo). Mỗi dòng mang dedup_key để
báo cáo đối chiếu số nhóm với view. Công thức chuẩn hoá tiêu đề giống hệt hàm job_dedup_key của
DB (lower + gộp khoảng trắng), tính ngay trong SQL để không lệch do khác locale giữa Postgres
và Python.
"""

# Cùng biểu thức với job_dedup_key() (và generate_job_hash()) trong sql/schema.sql.
_NORM_TITLE_SQL = "lower(regexp_replace(trim({col}), '\\s+', ' ', 'g'))"

_COLUMNS = (
    "job_id", "company_id", "company_name", "company_active", "job_title", "norm_title",
    "level_code", "province_id", "province_name", "job_status", "created_at", "deadline",
    "salary_min", "salary_max", "source_url", "dedup_key", "has_editor", "has_notes",
    "log_urls", "n_applications", "n_saved", "n_contact_links",
)


def list_duplicate_job_rows(conn) -> list:
    """Mọi job nằm trong một nhóm nghi trùng (>= 2 job cùng company_id và cùng tiêu đề
    chuẩn hoá), MỌI trạng thái (OPEN/CLOSED), mỗi job một dict gồm _COLUMNS:

      - norm_title: tiêu đề chuẩn hoá (khoá nhóm, cùng công thức với dedup_key);
      - dedup_key: khoá chống trùng của job (job_postings.dedup_key);
      - has_editor: từng có người sửa (updated_by IS NOT NULL);
      - has_notes: có ghi chú ss_team_notes không rỗng;
      - log_urls: các source_url của job trong job_sources_log (list, có thể rỗng);
      - n_applications / n_saved / n_contact_links: số đơn ứng tuyển, lượt lưu, liên kết
        liên hệ đang trỏ vào job (dữ liệu con mà việc gộp sau này phải chuyển đi).

    Sắp theo công ty, rồi cũ nhất trước. Không ghi gì; đóng transaction đọc trước khi trả."""
    norm_jp = _NORM_TITLE_SQL.format(col="jp.job_title")
    norm_plain = _NORM_TITLE_SQL.format(col="job_title")
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT jp.job_id, jp.company_id, c.company_name, c.is_active, jp.job_title,
                   {norm_jp} AS norm_title,
                   l.level_code, jp.province_id, p.province_name, jp.job_status::text,
                   jp.created_at, jp.deadline, jp.salary_min, jp.salary_max,
                   jp.source_url, jp.dedup_key,
                   (jp.updated_by IS NOT NULL) AS has_editor,
                   (COALESCE(btrim(jp.ss_team_notes), '') <> '') AS has_notes,
                   (SELECT array_agg(DISTINCT s.source_url) FROM job_sources_log s
                     WHERE s.job_id = jp.job_id AND s.source_url IS NOT NULL) AS log_urls,
                   (SELECT count(*) FROM job_applications a WHERE a.job_id = jp.job_id) AS n_applications,
                   (SELECT count(*) FROM saved_jobs sj WHERE sj.job_id = jp.job_id) AS n_saved,
                   (SELECT count(*) FROM job_contact_links cl WHERE cl.job_id = jp.job_id) AS n_contact_links
            FROM job_postings jp
            JOIN companies c ON c.company_id = jp.company_id
            LEFT JOIN levels l ON l.level_id = jp.level_id
            LEFT JOIN provinces p ON p.province_id = jp.province_id
            WHERE (jp.company_id, {norm_jp}) IN (
                SELECT company_id, {norm_plain}
                FROM job_postings
                GROUP BY company_id, {norm_plain}
                HAVING count(*) > 1
            )
            ORDER BY jp.company_id, jp.created_at, jp.job_id
            """
        )
        rows = []
        for r in cur.fetchall():
            row = dict(zip(_COLUMNS, r))
            row["job_id"] = str(row["job_id"])
            row["company_id"] = str(row["company_id"])
            row["log_urls"] = list(row["log_urls"] or [])
            rows.append(row)
    conn.rollback()
    return rows

