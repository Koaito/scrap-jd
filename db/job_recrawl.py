"""
db.job_recrawl — ghi job do pipeline crawl lại (tách từ db/jobs.py, 10/2026):
ghi nhận nguồn phụ cho tin đăng lại, tìm job để coi là tin đăng lại và mở lại job
đã đóng (3c), dời hạn nộp, tìm job theo mã job trong URL, và cập nhật job đã có bằng dữ liệu vừa crawl. Tên hàm giữ nguyên và vẫn gọi
được qua `db.update_job_from_recrawl`, `db.link_repost_source`...
"""

import json
import logging
from datetime import date
from typing import Optional

from db.audit_logs import log_action
from db.job_levels import _derived_level_assignments
from db.listing_state import CONFLICT_URL, insert_listing, reopen_listing_for_repost

logger = logging.getLogger(__name__)

# closed_reason mà pipeline được phép TỰ mở lại khi thấy tin đăng lại. Chỉ job do check_expired_source_jobs
# đóng. 'staff' là nhân viên chủ động đóng, 'unknown' không rõ (coi như do nhân viên đóng), 'merged' dự
# phòng: cả ba giữ CLOSED.
AUTO_REOPEN_REASONS = frozenset({"expired_auto"})


def link_repost_source(conn, job_id: str, *, source_name: str, source_url: str,
                       raw_jd_content: str = "", salary_raw_text: str = "", deadline=None) -> bool:
    """Ghi 1 source_url mới vào job ĐÃ CÓ như một nguồn phụ (job_sources_log),
    không tạo job mới. Dùng khi pipeline nhận ra tin vừa crawl là đăng lại của
    job đã có (cùng công ty, tiêu đề chuẩn hoá và tỉnh, nhưng khác source_url; khoá này
    không gồm level, xem find_repost_candidate).

    Trước đây tin đăng lại bị bỏ mà KHÔNG ghi gì, nên lượt crawl sau URL đó vẫn
    "chưa từng thấy": fetch chi tiết, xử lý công ty rồi lại bỏ, lặp mãi. Có dòng
    log này thì get_job_probe_by_source_url() nhận ra URL, đi nhánh "job đã có"
    và không fetch lại nếu job đã đủ field.

    raw_jd_content được giữ làm bằng chứng gốc của tin đăng lại: nếu sau này
    khoá trùng được siết chặt hơn thì còn dữ liệu để xem lại tin nào từng bị
    gộp nhầm.

    Hàm này KHÔNG đụng job_postings.source_url. Cột đó không phải "nguồn gốc bất biến" của
    job: nó là URL mà check_expired_source_jobs kiểm tra còn sống hay không, lúc tạo bằng URL
    tin crawl đầu tiên và có thể bị ghi đè sau đó (reopen_job_for_repost khi mở lại job đóng;
    merge-duplicates khi job giữ lấy lại OPEN từ job phụ). Mọi URL từng thấy, kể cả URL cũ,
    nằm ở job_sources_log.

    Trạng thái của listing mới (C1, db.listing_state): OPEN, trừ khi job đang CLOSED thì listing sinh ra
    đã CLOSED với đúng closed_reason của job (tin đăng lại không mở được job đóng vì staff, unknown,
    merged; job đóng vì expired_auto được pipeline mở lại ngay sau bằng reopen_job_for_repost, khi đó
    listing này về OPEN). `deadline` là hạn của chính tin đăng lại này, ghi vào listing.

    Không tự commit (đúng quy ước của lớp db: nơi gọi chịu trách nhiệm).
    Trả True nếu vừa thêm dòng mới, False nếu source_url đã có.

    Một URL chỉ thuộc một job (UNIQUE (source_url), D2, sql/0041_unique_source_url_job_sources_log.sql)
    nên ON CONFLICT nhắm vào source_url chứ không còn (job_id, source_url). Pipeline đã tra URL đã biết
    trước khi tới đây (get_job_probe_by_source_url), nên URL đã nằm ở một job KHÁC chỉ xảy ra khi hai
    tiến trình crawl gặp cùng URL cùng lúc. Khi đó hàm KHÔNG ghi đè và KHÔNG báo lỗi, trả False và ghi
    cảnh báo để còn dấu vết."""
    inserted = insert_listing(
        conn, job_id=job_id, source_name=source_name, source_url=source_url,
        salary_raw_text=salary_raw_text, raw_jd_content=raw_jd_content,
        detail_fetched=True, deadline=deadline, on_conflict=CONFLICT_URL,
    )
    with conn.cursor() as cur:
        if not inserted:
            cur.execute("SELECT job_id FROM job_sources_log WHERE source_url = %s", (source_url,))
            row = cur.fetchone()
            if row is not None and str(row[0]) != str(job_id):
                logger.warning(
                    "URL %s đã thuộc job %s, không ghi thêm làm nguồn phụ của job %s.",
                    source_url, row[0], job_id,
                )
        return inserted


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


def find_repost_candidate(conn, *, company_id: str, job_title: str, province_id: Optional[int],
                          level_id: Optional[int] = None) -> Optional[dict]:
    """Tìm job đã có để coi tin vừa crawl là ĐĂNG LẠI của nó (Phần 3c). Tra theo khoá chống trùng
    job_postings.dedup_key (công ty + tiêu đề chuẩn hoá + tỉnh, A3, xem sql/0039_add_job_dedup_key.sql),
    cùng khoá mà find_manual_job_duplicate() và báo cáo job trùng dùng. Khác find_manual_job_duplicate()
    (POST /jobs nhập tay) ở hai điểm, đều rút ra từ dữ liệu job trùng thật (xem README, mục
    merge-duplicates):

      1. Xét CẢ job đã CLOSED. Trước đây điều kiện job_status != 'CLOSED' làm tin đăng
         lại của job đã hết hạn/đã đóng sinh ra job mới (khoảng 88% job trùng).
      2. KHÔNG xét level. Level suy từ số năm kinh nghiệm ở từng trang nên hai lần đăng của cùng
         một tin hay ra level khác nhau (khoảng 34% job trùng). Cái giá: hai vị trí cùng tên, cùng
         công ty, cùng tỉnh nhưng khác cấp sẽ bị coi là một. (Nhập tay thì level do nhân viên
         chọn nên được xét, xem find_manual_job_duplicate.)

    Tỉnh nằm trong khoá (tỉnh NULL là một giá trị riêng): tin khác tỉnh có thể là chi nhánh khác.

    Nếu có nhiều job khớp thì chọn: job OPEN trước, rồi job cùng level với tin mới, rồi job
    tạo gần nhất (kết quả xác định, không ngẫu nhiên). `level_id` chỉ dùng để xếp hạng.

    Trả dict {job_id, job_status, level_id, deadline, closed_reason} hoặc None. `closed_reason` đọc
    thẳng từ cột job_postings.closed_reason (A2): None khi job OPEN; khi CLOSED là 'staff' |
    'expired_auto' | 'merged' | 'unknown'. Nơi gọi chỉ tự mở lại khi closed_reason thuộc
    AUTO_REOPEN_REASONS (hiện chỉ 'expired_auto'). Trước A2 giá trị này suy từ audit_logs (thiếu bản
    ghi DELETE_JOB = coi là đóng tự động); nay không còn đọc audit_logs. Chỉ đọc, không đóng
    transaction."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT j.job_id, j.job_status::text, j.level_id, j.deadline, j.closed_reason
              FROM job_postings j
             WHERE j.dedup_key = job_dedup_key(%s::uuid, %s::text, %s::int)
             ORDER BY (j.job_status <> 'OPEN'),
                      (j.level_id IS NOT DISTINCT FROM %s) DESC,
                      j.created_at DESC, j.job_id
             LIMIT 1
            """,
            (company_id, job_title, province_id, level_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"job_id": str(row[0]), "job_status": row[1], "level_id": row[2],
            "deadline": row[3], "closed_reason": row[4]}


def reopen_job_for_repost(conn, job_id: str, *, source_url: str, deadline,
                          today: Optional[date] = None) -> bool:
    """Mở lại job CLOSED khi nhận ra tin vừa crawl là đăng lại của nó (Phần 3c, cách A):
    job_status -> OPEN, deadline -> hạn của tin mới, job_postings.source_url -> URL của
    tin mới. Phải đổi cả source_url vì check_expired_source_jobs kiểm tra theo
    job_postings.source_url: để URL cũ (đã chết) thì lượt kiểm tra sau sẽ đóng lại ngay.
    URL cũ vẫn còn trong job_sources_log. deadline NULL (tin mới không có hạn) thì ghi NULL,
    vì hạn cũ đã qua và sẽ làm job bị đóng lại theo deadline. closed_reason/closed_at được xoá
    (job OPEN không có lý do đóng).

    KHÔNG mở lại khi hạn của tin mới đã qua (deadline < today, today mặc định date.today()
    như check_expired_source_jobs), vì job sẽ bị đóng lại ngay lượt kiểm tra sau.

    Một câu UPDATE có điều kiện job_status = 'CLOSED' nên an toàn khi crawl chạy song song
    (chỉ một bên mở được, bên kia nhận False) và không đụng job đang OPEN. Việc loại job do
    nhân viên chủ động đóng (closed_reason, xem find_repost_candidate) là việc của nơi gọi.

    Listing của source_url mới cũng về OPEN kèm hạn mới (db.listing_state.reopen_listing_for_repost);
    listing cũ giữ nguyên (URL cũ đã chết nên vẫn CLOSED).

    Mở lại được thì ghi audit REOPEN_JOB (actor NULL, changes = giá trị cũ/mới của job_status,
    deadline, source_url, closed_reason) CÙNG transaction, nên không có lần mở lại nào thiếu log.
    Không tự commit. Trả True nếu có dòng được mở lại."""
    if deadline is not None and deadline < (today or date.today()):
        return False
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH old AS (
                SELECT job_id, deadline, source_url, closed_reason
                  FROM job_postings
                 WHERE job_id = %s AND job_status = 'CLOSED'
                   FOR UPDATE
            )
            UPDATE job_postings j
               SET job_status = 'OPEN', deadline = %s, source_url = %s,
                   closed_reason = NULL, closed_at = NULL
              FROM old
             WHERE j.job_id = old.job_id
            RETURNING j.job_title, j.company_id, old.deadline, old.source_url, old.closed_reason
            """,
            (job_id, deadline, source_url),
        )
        row = cur.fetchone()
    if row is None:
        return False
    title, company_id, old_deadline, old_url, old_reason = row
    # Listing của URL mới (pipeline vừa ghi nó CLOSED theo job) về OPEN kèm hạn mới (C1, luật 4).
    reopen_listing_for_repost(conn, job_id, source_url, deadline)
    log_action(
        conn, actor_id=None, action_type="REOPEN_JOB", entity_type="JOB", entity_id=job_id,
        entity_label=title, company_id=str(company_id),
        changes={
            "job_status": {"old": "CLOSED", "new": "OPEN"},
            "deadline": {"old": old_deadline, "new": deadline},
            "source_url": {"old": old_url, "new": source_url},
            "closed_reason": {"old": old_reason, "new": None},
        },
    )
    return True


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
