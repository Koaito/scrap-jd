"""
db.listing_state — MỘT nơi duy nhất ghi trạng thái của listing (một dòng job_sources_log = một URL tin
đăng) (C1, nửa 2/2; cột do sql/0043_add_listing_state_job_sources_log.sql thêm).

MÔ HÌNH (đã duyệt 08/10/2026, xem KE_HOACH_BACKEND_DATABASE.md mục 4): listing giữ trạng thái, job là giá
trị tổng hợp từ các listing của nó. Hệ quả ở tầng ghi, mọi hàm dưới đây thực thi đúng các luật sau:

  1. Listing mới do crawl hoặc nhập tay: OPEN, trừ khi job đang CLOSED thì listing sinh ra đã CLOSED với
     đúng closed_reason của job (staff / unknown / merged: tin đăng lại không mở được job; expired_auto:
     job được mở lại ngay sau đó bởi reopen_listing_for_repost, hoặc giữ CLOSED nếu hạn mới đã qua).
  2. Job chuyển sang CLOSED: mọi listing chưa CLOSED của job thành CLOSED, cùng closed_reason với job
     (close_job_listings). Listing đã CLOSED trước đó giữ lý do và giờ đóng của chính nó.
  3. Nhân viên mở lại job CLOSED: các listing CLOSED vì 'staff' về OPEN, và listing hiện hành (URL trùng
     job_postings.source_url) về OPEN dù lý do đóng là gì, để job OPEN không bao giờ có 0 listing OPEN do
     chính thao tác mở lại. Listing đóng vì lý do khác (expired_auto, merged) giữ nguyên
     (reopen_job_listings). Listing hiện hành chết thật thì check_expired_source_jobs đóng lại ở lượt sau.
  4. Pipeline mở lại job vì tin đăng lại: listing của URL mới về OPEN kèm hạn mới
     (reopen_listing_for_repost); các listing cũ giữ nguyên.

CHƯA ĐỔI CHỖ ĐỌC: không đoạn code nào đọc các cột này để ra quyết định (đó là C2 và C3). Mọi hàm ở đây chỉ
ghi, không tự commit (đúng quy ước lớp db: nơi gọi chịu trách nhiệm).

Toàn bộ SQL ghi listing_status / closed_reason / closed_at nằm ở file này, để CHECK
chk_job_sources_log_closed_state (CLOSED thì có lý do, không CLOSED thì không có lý do và giờ đóng) chỉ có
một chỗ cần giữ đúng.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

LISTING_OPEN = "OPEN"
LISTING_CLOSED = "CLOSED"
LISTING_UNKNOWN = "UNKNOWN"

# Cách xử lý khi URL đã có trong job_sources_log. Chuỗi SQL cố định (không nhận từ bên ngoài).
CONFLICT_JOB_AND_URL = "job_and_url"   # ON CONFLICT (job_id, source_url): giữ đúng hành vi cũ của insert_job
CONFLICT_URL = "url"                   # ON CONFLICT (source_url): giữ đúng hành vi cũ của link_repost_source
_CONFLICT_SQL = {
    CONFLICT_JOB_AND_URL: "ON CONFLICT (job_id, source_url) DO NOTHING",
    CONFLICT_URL: "ON CONFLICT (source_url) DO NOTHING",
}


def initial_listing_state(job_status: str, job_closed_reason: Optional[str]) -> tuple:
    """(listing_status, closed_reason) cho listing MỚI của một job đang ở trạng thái cho trước (luật 1).
    Hàm thuần, không đụng DB. Job CLOSED mà thiếu closed_reason (không xảy ra với dữ liệu hợp lệ vì
    chk_job_postings_closed_state) thì dùng 'unknown' thay vì làm vỡ CHECK của listing."""
    if job_status == "CLOSED":
        return LISTING_CLOSED, job_closed_reason or "unknown"
    return LISTING_OPEN, None


def insert_listing(conn, *, job_id: str, source_name: str, source_url: str,
                   salary_raw_text: str = "", raw_jd_content: str = "",
                   detail_fetched: bool = False, deadline=None,
                   on_conflict: str = CONFLICT_URL) -> bool:
    """Ghi một listing mới cho job ĐÃ CÓ; trạng thái suy ra từ job lúc này (luật 1). Trả True nếu vừa
    thêm dòng, False nếu URL đã có (ON CONFLICT DO NOTHING). Raise LookupError nếu job không tồn tại.

    first_seen_at và last_seen_at lấy mặc định now() của cột (cùng một giá trị, nên last_seen_at >=
    first_seen_at). detail_fetched=True ghi luôn detail_checked_at = now(): fetch trang chi tiết vừa thành
    công thì tin chắc chắn còn ở nguồn.

    Đọc job bằng FOR SHARE rồi mới chèn: nếu nhân viên đang đóng job trong một transaction khác thì lệnh
    đọc chờ tới khi họ commit, nên đọc ra đúng trạng thái đã đóng; ngược lại thao tác đóng job (update_job)
    chờ listing vừa chèn commit xong rồi mới đóng nốt nó. Không có kẽ hở \"job CLOSED mà listing mới OPEN\"."""
    if on_conflict not in _CONFLICT_SQL:
        raise ValueError(f"on_conflict không hợp lệ: {on_conflict!r}")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT job_status::text, closed_reason FROM job_postings WHERE job_id = %s FOR SHARE",
            (job_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"job {job_id} không tồn tại, không ghi được listing {source_url}")
        status, reason = initial_listing_state(row[0], row[1])
        cur.execute(
            f"""
            INSERT INTO job_sources_log (job_id, source_name, source_url, salary_raw_content,
                                         raw_jd_content, detail_checked_at,
                                         listing_status, deadline, closed_reason, closed_at)
            VALUES (%s, %s, %s, %s, %s, CASE WHEN %s THEN now() END,
                    %s, %s, %s, CASE WHEN %s = 'CLOSED' THEN now() END)
            {_CONFLICT_SQL[on_conflict]}
            """,
            (job_id, source_name, source_url, salary_raw_text, raw_jd_content or None,
             detail_fetched, status, deadline, reason, status),
        )
        return cur.rowcount > 0


def mark_listing_detail_checked(conn, source_url: str, *, deadline=None) -> None:
    """Fetch THÀNH CÔNG trang chi tiết của URL này: ghi detail_checked_at (như trước C1) và last_seen_at.
    `deadline` (nếu có) là hạn đọc được từ chính trang đó, ghi vào listing; None thì giữ hạn cũ (cùng quy
    tắc \"không xoá dữ liệu cũ khi lượt sau không lấy được\" của update_job_fields). Không đổi
    listing_status: một lần fetch chưa đủ để kết luận lại trạng thái. Không đụng job_postings."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE job_sources_log
               SET detail_checked_at = now(),
                   last_seen_at = GREATEST(last_seen_at, now()),
                   deadline = COALESCE(%s, deadline)
             WHERE source_url = %s
            """,
            (deadline, source_url),
        )


def mark_listing_seen(conn, source_url: str) -> bool:
    """Ghi last_seen_at = now() cho URL vừa được xác nhận còn ở nguồn (HTTP 2xx của
    check_expired_source_jobs). Không đổi listing_status. Trả True nếu có dòng được ghi."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE job_sources_log SET last_seen_at = GREATEST(last_seen_at, now()) WHERE source_url = %s",
            (source_url,),
        )
        return cur.rowcount > 0


def close_job_listings(conn, job_id: str) -> int:
    """Luật 2. Gọi SAU khi job đã được ghi CLOSED trong cùng transaction: mọi listing chưa CLOSED của job
    thành CLOSED với closed_reason và closed_at của job. Job không CLOSED thì không làm gì. Idempotent.
    Trả số listing vừa đóng."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE job_sources_log l
               SET listing_status = 'CLOSED', closed_reason = j.closed_reason,
                   closed_at = COALESCE(j.closed_at, now())
              FROM job_postings j
             WHERE j.job_id = l.job_id AND l.job_id = %s
               AND j.job_status = 'CLOSED' AND l.listing_status <> 'CLOSED'
            """,
            (job_id,),
        )
        return cur.rowcount


def reopen_job_listings(conn, job_id: str) -> int:
    """Luật 3. Gọi SAU khi nhân viên mở lại job (job đã được ghi OPEN trong cùng transaction): các listing
    chưa OPEN mà đóng vì 'staff', cùng listing hiện hành (URL trùng job_postings.source_url), về OPEN và
    xoá closed_reason/closed_at. Job không OPEN thì không làm gì. Idempotent. Trả số listing vừa mở."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE job_sources_log l
               SET listing_status = 'OPEN', closed_reason = NULL, closed_at = NULL
              FROM job_postings j
             WHERE j.job_id = l.job_id AND l.job_id = %s
               AND j.job_status = 'OPEN' AND l.listing_status <> 'OPEN'
               AND (l.closed_reason = 'staff' OR l.source_url = j.source_url)
            """,
            (job_id,),
        )
        return cur.rowcount


def reopen_listing_for_repost(conn, job_id: str, source_url: str, deadline) -> bool:
    """Luật 4. Pipeline vừa mở lại job vì tin đăng lại tại `source_url`: listing của URL đó về OPEN, hạn =
    hạn của tin mới (NULL nếu tin không ghi hạn), last_seen_at = now(). Listing khác của job không đổi.
    Trả False nếu job chưa có listing cho URL đó (nơi gọi bình thường đã ghi listing ngay trước)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE job_sources_log
               SET listing_status = 'OPEN', closed_reason = NULL, closed_at = NULL,
                   deadline = %s, last_seen_at = GREATEST(last_seen_at, now())
             WHERE job_id = %s AND source_url = %s
            """,
            (deadline, job_id, source_url),
        )
        found = cur.rowcount > 0
    if not found:
        logger.warning("Mở lại job %s nhưng chưa có listing cho %s, không có gì để mở.", job_id, source_url)
    return found


def set_current_listing_deadline(conn, job_id: str, deadline) -> int:
    """Nhân viên sửa hoặc xoá hạn của job (PATCH, import): ghi cùng hạn đó vào listing hiện hành (URL trùng
    job_postings.source_url), vì job.deadline vốn là hạn của URL hiện hành. Deadline NULL = xoá hạn. Chỉ ghi
    một listing, không đụng listing khác. Trả số dòng ghi (0 nếu job không có listing hiện hành)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE job_sources_log l SET deadline = %s
              FROM job_postings j
             WHERE j.job_id = l.job_id AND l.job_id = %s AND l.source_url = j.source_url
            """,
            (deadline, job_id),
        )
        return cur.rowcount


def job_is_closed_locked(conn, job_id: str) -> bool:
    """Job đang CLOSED? Khoá dòng job (FOR UPDATE) để trạng thái đọc ra không đổi tới hết transaction.
    update_job gọi trước khi ghi job_status = 'OPEN' để biết đây có phải lần MỞ LẠI (CLOSED -> OPEN) hay
    chỉ là form gửi lại OPEN: chỉ lần mở lại mới mở listing (luật 3). Job không tồn tại thì False."""
    with conn.cursor() as cur:
        cur.execute("SELECT job_status::text FROM job_postings WHERE job_id = %s FOR UPDATE", (job_id,))
        row = cur.fetchone()
    return row is not None and row[0] == "CLOSED"


def sync_listings_after_job_update(conn, job_id: str, *, job_status: Optional[str], was_closed: bool,
                                   deadline_changed: bool, deadline=None) -> None:
    """Đưa listing theo kịp một lần db.update_job() vừa ghi job (nhân viên sửa tay, import,
    check_expired_source_jobs). Gọi SAU câu UPDATE job_postings, cùng transaction.
      - job_status = 'CLOSED': đóng mọi listing (luật 2);
      - job_status = 'OPEN' và was_closed (đúng lần mở lại): luật 3;
      - hạn được sửa hoặc xoá (`deadline` None khi xoá): ghi cùng hạn vào listing hiện hành."""
    if job_status == "CLOSED":
        close_job_listings(conn, job_id)
    elif job_status == "OPEN" and was_closed:
        reopen_job_listings(conn, job_id)
    if deadline_changed:
        set_current_listing_deadline(conn, job_id, deadline)
