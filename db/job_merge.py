"""
db.job_merge — phần chạy SQL của lệnh gộp job trùng (`python main.py merge-duplicates`, xem
merge_duplicates.py, Phần 3b). Logic chọn job giữ / hợp nhất trường / kế hoạch chuyển dữ liệu
con là hàm THUẦN ở merge_duplicates.py; ở đây chỉ lấy dữ liệu.

HIỆN CÓ: phần ĐỌC (list_merge_job_details). Phần ghi (gộp một nhóm trong transaction) thuộc nửa
sau của 3b, sẽ thêm vào file này.
"""

# Cột của job_postings cần để lập kế hoạch gộp. Khác db.list_duplicate_job_rows (3a): lấy đủ các
# khối cần hợp nhất (lương, hạn, trạng thái, level + dấu, ghi chú), không lấy tên công ty/tỉnh.
_JOB_COLUMNS = (
    "job_id", "company_id", "job_title", "level_id", "level_code", "level_source",
    "level_rule_version", "level_signals", "province_id", "currency", "salary_min", "salary_max",
    "salary_type", "salary_period", "deadline", "job_status", "ss_team_notes", "source_url",
    "created_at", "has_editor", "has_notes",
)


def list_merge_job_details(conn, job_ids: list) -> dict:
    """{job_id (str): dict} cho các job trong `job_ids`, mỗi dict gồm:

      - _JOB_COLUMNS (has_editor = updated_by IS NOT NULL; has_notes = ss_team_notes không rỗng);
      - logs:         [{log_id, source_url}]            từ job_sources_log;
      - saved:        [{saved_job_id, ss_user_id}]      từ saved_jobs;
      - applications: [{application_id, ss_user_id, has_cv}] từ job_applications;
      - links:        [{link_id, contact_id, n_interactions}] từ job_contact_links;
      - n_applications / n_saved / n_contact_links: số phần tử của ba danh sách trên (cùng tên
        với db.list_duplicate_job_rows để dùng chung luật "dữ liệu cần bảo vệ").

    Mọi id trả về dạng str. job_id không tồn tại thì vắng mặt trong kết quả. CHỈ ĐỌC; đóng
    transaction đọc trước khi trả."""
    ids = [str(j) for j in dict.fromkeys(job_ids)]
    if not ids:
        return {}
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT jp.job_id, jp.company_id, jp.job_title, jp.level_id, l.level_code,
                   jp.level_source, jp.level_rule_version, jp.level_signals, jp.province_id,
                   jp.currency, jp.salary_min, jp.salary_max, jp.salary_type::text,
                   jp.salary_period::text, jp.deadline, jp.job_status::text, jp.ss_team_notes,
                   jp.source_url, jp.created_at,
                   (jp.updated_by IS NOT NULL) AS has_editor,
                   (COALESCE(btrim(jp.ss_team_notes), '') <> '') AS has_notes
            FROM job_postings jp
            LEFT JOIN levels l ON l.level_id = jp.level_id
            WHERE jp.job_id = ANY(%s::uuid[])
            """,
            (ids,),
        )
        for r in cur.fetchall():
            row = dict(zip(_JOB_COLUMNS, r))
            row["job_id"] = str(row["job_id"])
            row["company_id"] = str(row["company_id"])
            row.update(logs=[], saved=[], applications=[], links=[])
            out[row["job_id"]] = row

        cur.execute(
            "SELECT job_id, log_id, source_url FROM job_sources_log "
            "WHERE job_id = ANY(%s::uuid[]) ORDER BY collected_date, log_id",
            (ids,),
        )
        for job_id, log_id, url in cur.fetchall():
            if str(job_id) in out:
                out[str(job_id)]["logs"].append({"log_id": str(log_id), "source_url": url})

        cur.execute(
            "SELECT job_id, saved_job_id, ss_user_id FROM saved_jobs "
            "WHERE job_id = ANY(%s::uuid[]) ORDER BY created_at, saved_job_id",
            (ids,),
        )
        for job_id, saved_id, user_id in cur.fetchall():
            if str(job_id) in out:
                out[str(job_id)]["saved"].append({"saved_job_id": str(saved_id), "ss_user_id": str(user_id)})

        cur.execute(
            "SELECT job_id, application_id, ss_user_id, (cv_url IS NOT NULL) FROM job_applications "
            "WHERE job_id = ANY(%s::uuid[]) ORDER BY applied_at, application_id",
            (ids,),
        )
        for job_id, app_id, user_id, has_cv in cur.fetchall():
            if str(job_id) in out:
                out[str(job_id)]["applications"].append(
                    {"application_id": str(app_id), "ss_user_id": str(user_id), "has_cv": bool(has_cv)})

        cur.execute(
            """
            SELECT l.job_id, l.link_id, l.contact_id,
                   (SELECT count(*) FROM job_contact_interactions i WHERE i.link_id = l.link_id)
            FROM job_contact_links l
            WHERE l.job_id = ANY(%s::uuid[]) ORDER BY l.created_at, l.link_id
            """,
            (ids,),
        )
        for job_id, link_id, contact_id, n_inter in cur.fetchall():
            if str(job_id) in out:
                out[str(job_id)]["links"].append(
                    {"link_id": str(link_id), "contact_id": str(contact_id), "n_interactions": n_inter})
    conn.rollback()
    for row in out.values():
        row["n_applications"] = len(row["applications"])
        row["n_saved"] = len(row["saved"])
        row["n_contact_links"] = len(row["links"])
    return out
