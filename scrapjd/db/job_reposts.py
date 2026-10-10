"""
db.job_reposts — phần chạy SQL (CHỈ ĐỌC) của báo cáo đo tỷ lệ gộp nhầm
(`python main.py report-reposts`, xem scrapjd/cli/repost_report.py, A5). Logic so độ giống / phân loại là hàm
THUẦN ở scrapjd/cli/repost_report.py; ở đây chỉ lấy dữ liệu.

Hai nguồn dữ liệu:
  - job_sources_log: mọi URL từng thấy của một job. Job có >= 2 dòng log là job đã nhận thêm tin
    ngoài tin đầu tiên (tin đăng lại do pipeline nối vào qua link_repost_source, tin cùng mã job
    nhưng đổi tiêu đề, hoặc tin do merge-duplicates chuyển sang). raw_jd_content của từng dòng là
    bằng chứng gốc để so xem các tin đó có thật sự cùng một vị trí không.
  - audit_logs MERGE_JOB: biết log nào do merge-duplicates chuyển từ job phụ sang job giữ
    (snapshot.job_sources_log.moved là danh sách log_id), và có bao nhiêu log bị bỏ vì trùng khoá
    (snapshot.job_sources_log.dropped; nguyên dòng đã mất raw_jd_content nên không so được).

Không dùng dedup_key (A3): báo cáo phải đo đúng thứ đã xảy ra, không phụ thuộc cách khoá được
định nghĩa lại sau này.
"""

from scrapjd.db.pg_types import Conn

_LOG_COLUMNS = (
    "job_id", "company_id", "company_name", "job_title", "level_code", "province_name",
    "job_status", "job_created_at", "log_id", "source_name", "source_url", "collected_date",
    "raw_jd_content",
)


def list_multi_source_job_logs(conn: Conn) -> list:
    """Mọi dòng job_sources_log của các job có >= 2 dòng log, mỗi dòng một dict gồm _LOG_COLUMNS
    (job_id, company_id, log_id là str). Sắp theo công ty, job, rồi tin cũ nhất trước
    (collected_date, log_id) — thứ tự này xác định nên báo cáo chạy lại cho cùng kết quả.

    raw_jd_content có thể None/rỗng (job cũ, hoặc chưa fetch được chi tiết). CHỈ ĐỌC; đóng
    transaction đọc trước khi trả."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT jp.job_id, jp.company_id, c.company_name, jp.job_title, lv.level_code,
                   pr.province_name, jp.job_status::text, jp.created_at,
                   s.log_id, s.source_name, s.source_url, s.collected_date, s.raw_jd_content
            FROM job_sources_log s
            JOIN job_postings jp ON jp.job_id = s.job_id
            JOIN companies c ON c.company_id = jp.company_id
            LEFT JOIN levels lv ON lv.level_id = jp.level_id
            LEFT JOIN provinces pr ON pr.province_id = jp.province_id
            WHERE s.job_id IN (SELECT job_id FROM job_sources_log GROUP BY job_id HAVING count(*) > 1)
            ORDER BY c.company_name, jp.job_id, s.collected_date, s.log_id
            """
        )
        rows = []
        for r in cur.fetchall():
            row = dict(zip(_LOG_COLUMNS, r))
            for key in ("job_id", "company_id", "log_id"):
                row[key] = str(row[key])
            rows.append(row)
    conn.rollback()
    return rows


def list_merge_log_origins(conn: Conn) -> dict:
    """Dấu vết các lần merge-duplicates --apply, đọc từ audit_logs (action MERGE_JOB của job phụ,
    nhận ra bằng changes.merged_into). Trả dict:

      - moved:   {log_id (str): {"donor_job_id": str, "keeper_job_id": str, "merged_at": datetime}}
                 các dòng job_sources_log đã được chuyển từ job phụ sang job giữ;
      - dropped: số dòng job_sources_log bị bỏ vì trùng (job_id, source_url) với job giữ — chỉ còn
                 độ dài nội dung trong snapshot nên không so được, chỉ để báo cáo biết.

    So khớp action bằng ::text nên chạy được cả khi DB chưa có nhãn MERGE_JOB trong enum (khi đó
    không có dòng nào). CHỈ ĐỌC; đóng transaction đọc trước khi trả."""
    moved: dict = {}
    dropped = 0
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.entity_id, a.changes->>'merged_into', a.created_at,
                   CASE WHEN jsonb_typeof(a.changes #> '{snapshot,job_sources_log,moved}') = 'array'
                        THEN a.changes #> '{snapshot,job_sources_log,moved}' ELSE '[]'::jsonb END,
                   CASE WHEN jsonb_typeof(a.changes #> '{snapshot,job_sources_log,dropped}') = 'array'
                        THEN jsonb_array_length(a.changes #> '{snapshot,job_sources_log,dropped}') ELSE 0 END
            FROM audit_logs a
            WHERE a.action_type::text = 'MERGE_JOB' AND a.changes->>'merged_into' IS NOT NULL
            ORDER BY a.created_at, a.log_id
            """
        )
        for donor_id, keeper_id, merged_at, moved_ids, n_dropped in cur.fetchall():
            dropped += n_dropped
            for log_id in moved_ids:
                moved[str(log_id)] = {"donor_job_id": str(donor_id), "keeper_job_id": keeper_id,
                                      "merged_at": merged_at}
    conn.rollback()
    return {"moved": moved, "dropped": dropped}
