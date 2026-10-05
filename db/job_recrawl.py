"""
db.job_recrawl — ghi job do pipeline crawl lại (tách từ db/jobs.py, 10/2026):
ghi nhận nguồn phụ cho tin đăng lại, dời hạn nộp, tìm job theo mã job trong
URL, và cập nhật job đã có bằng dữ liệu vừa crawl. Tên hàm giữ nguyên và vẫn gọi
được qua `db.update_job_from_recrawl`, `db.link_repost_source`...
"""

import json
import logging
from typing import Optional

from db.job_levels import _derived_level_assignments

logger = logging.getLogger(__name__)


def link_repost_source(conn, job_id: str, *, source_name: str, source_url: str,
                       raw_jd_content: str = "", salary_raw_text: str = "") -> bool:
    """Ghi 1 source_url mới vào job ĐÃ CÓ như một nguồn phụ (job_sources_log),
    không tạo job mới. Dùng khi pipeline nhận ra tin vừa crawl là đăng lại của
    job đã có (cùng company/title/level/province nhưng khác source_url).

    Trước đây tin đăng lại bị bỏ mà KHÔNG ghi gì, nên lượt crawl sau URL đó vẫn
    "chưa từng thấy": fetch chi tiết, xử lý công ty rồi lại bỏ, lặp mãi. Có dòng
    log này thì get_job_probe_by_source_url() nhận ra URL, đi nhánh "job đã có"
    và không fetch lại nếu job đã đủ field.

    raw_jd_content được giữ làm bằng chứng gốc của tin đăng lại: nếu sau này
    khoá trùng được siết chặt hơn thì còn dữ liệu để xem lại tin nào từng bị
    gộp nhầm. job_postings.source_url (nguồn gốc của job) KHÔNG đổi.

    Không tự commit (đúng quy ước của lớp db: nơi gọi chịu trách nhiệm).
    Trả True nếu vừa thêm dòng mới, False nếu (job_id, source_url) đã có."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO job_sources_log (job_id, source_name, source_url,
                                          salary_raw_content, raw_jd_content,
                                          detail_checked_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (job_id, source_url) DO NOTHING
            """,
            (job_id, source_name, source_url, salary_raw_text, raw_jd_content or None),
        )
        return cur.rowcount > 0


def extend_job_deadline(conn, job_id: str, new_deadline) -> bool:
    """Dời deadline của job OPEN ra SAU (hoặc điền khi đang NULL), không bao
    giờ rút ngắn. Dùng khi pipeline nhận ra tin đăng lại có hạn nộp mới hơn:
    job cũ đã quá hạn (deadline < hôm nay nhưng vẫn OPEN, đang nằm trong danh
    sách "job hết hạn" của tab tình trạng dữ liệu) sẽ sống lại đúng với thực
    tế là nhà tuyển dụng vừa đăng lại.

    Một câu UPDATE có điều kiện nên không cần đọc deadline cũ trước và không
    có race giữa đọc-rồi-ghi. Job CLOSED không bị đụng (người dùng đã chủ động
    đóng). Không tự commit. Trả True nếu có dòng được cập nhật."""
    if new_deadline is None:
        return False
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE job_postings
               SET deadline = %s
             WHERE job_id = %s
               AND job_status = 'OPEN'
               AND (deadline IS NULL OR deadline < %s)
            """,
            (new_deadline, job_id, new_deadline),
        )
        return cur.rowcount > 0


def find_jobs_by_source_url_regex(conn, *, source_name: str, url_regex: str) -> list:
    """Các job đã có ÍT NHẤT MỘT nguồn (job_sources_log) của source_name với
    source_url khớp url_regex (regex POSIX của Postgres). Dùng để tìm job cùng
    mã số ở nguồn mà URL đổi theo tiêu đề (VietnamWorks: ...-<mã>-jv, nhà tuyển
    dụng sửa tiêu đề thì phần chữ của URL đổi còn mã giữ nguyên).

    Chỉ ĐỌC, không tự commit. Trả list[(job_id, job_title, job_status,
    updated_by, created_at)], cũ nhất trước (created_at, rồi job_id) để hai dòng
    cùng điểm thì pipeline chọn được dòng tạo sớm nhất. updated_by khác NULL nghĩa
    là đã có người trong team sửa tay. url_regex do adapter dựng từ mã số (chỉ
    chữ số), không bao giờ lấy từ người dùng."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT jp.job_id, jp.job_title, jp.job_status, jp.updated_by, jp.created_at
            FROM job_postings jp
            WHERE EXISTS (
                SELECT 1 FROM job_sources_log jsl
                WHERE jsl.job_id = jp.job_id
                  AND jsl.source_name = %s
                  AND jsl.source_url ~ %s
            )
            ORDER BY jp.created_at, jp.job_id
            """,
            (source_name, url_regex),
        )
        return cur.fetchall()


def update_job_from_recrawl(conn, job_id: str, *, job_title: str,
                             level_id: Optional[int] = None,
                             work_type: Optional[str] = None,
                             parsed_content: Optional[dict] = None,
                             salary: Optional[dict] = None,
                             level_source: Optional[str] = None,
                             level_rule_version: Optional[int] = None,
                             level_signals: Optional[dict] = None) -> bool:
    """Cập nhật 1 job ĐÃ CÓ bằng dữ liệu vừa crawl lại (tin đã đổi tiêu đề nên URL
    mới, xem pipeline._update_job_by_job_code). Ghi:
      - job_title: luôn ghi;
      - level_id / work_type / parsed_content: chỉ ghi khi có giá trị (None/rỗng
        = giữ nguyên, để lần crawl không lấy được field đó không xoá dữ liệu cũ).
        level_id đi kèm level_source/level_rule_version (căn cứ suy level, xem
        _check_level_stamp) và không bao giờ đè dòng level_source='manual';
        level_signals (tín hiệu thô đã đọc, normalize.build_level_signals) được ghi
        cùng level để tính lại sau này không cần tải lại trang;
      - salary: dict {currency, salary_min, salary_max, salary_type,
        salary_period}, ghi NGUYÊN BỘ khi truyền (salary_min/max None là NULL
        thật); None = giữ nguyên lương cũ.
    KHÔNG đụng công ty, tỉnh, ngành, deadline (deadline có extend_job_deadline
    riêng, không bao giờ rút ngắn).

    Chốt chặn nằm ngay trong câu UPDATE: chỉ ghi khi job còn OPEN và chưa có ai
    sửa tay (updated_by IS NULL), nên nếu trong lúc pipeline xử lý có người vừa
    sửa/đóng job thì không bị ghi đè (không có race giữa đọc rồi ghi). Không tự
    commit. Trả True nếu có dòng được cập nhật."""
    updates = ["job_title = %s"]
    values = [job_title]
    if level_id is not None:
        level_sets, level_values = _derived_level_assignments(
            level_id, level_source, level_rule_version, level_signals,
        )
        updates.extend(level_sets)
        values.extend(level_values)
    elif level_source is not None or level_signals is not None:
        raise ValueError("level_source / level_signals chỉ có nghĩa khi truyền level_id")
    if work_type:
        updates.append("work_type = %s")
        values.append(work_type)
    if parsed_content:
        updates.append("parsed_content = %s")
        values.append(json.dumps(parsed_content, ensure_ascii=False))
    if salary is not None:
        updates.extend([
            "currency = %s", "salary_min = %s", "salary_max = %s",
            "salary_type = %s", "salary_period = %s",
        ])
        values.extend([
            salary["currency"], salary["salary_min"], salary["salary_max"],
            salary["salary_type"], salary["salary_period"],
        ])

    values.append(job_id)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE job_postings SET {', '.join(updates)} "
            "WHERE job_id = %s AND job_status = 'OPEN' AND updated_by IS NULL",
            values,
        )
        return cur.rowcount > 0
