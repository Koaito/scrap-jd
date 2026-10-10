"""
db.listing_state — MỘT nơi duy nhất ghi trạng thái của listing (một dòng job_sources_log = một URL tin
đăng) (C1, nửa 2/2; cột do sql/0043_add_listing_state_job_sources_log.sql thêm).

MÔ HÌNH (đã duyệt 08/10/2026, xem KE_HOACH_BACKEND_DATABASE.md mục 4): listing giữ trạng thái, job là giá
trị tổng hợp từ các listing của nó. Hệ quả ở tầng ghi, mọi hàm dưới đây thực thi đúng các luật sau:

  1. Listing mới do crawl hoặc nhập tay: OPEN, trừ khi job đang CLOSED. Job CLOSED vì staff / unknown / merged
     thì listing sinh ra đã CLOSED với đúng closed_reason của job (tin đăng lại không mở được job). Job CLOSED vì
     'expired_auto' (do check_expired_source_jobs đóng) thì tin đăng lại là bằng chứng job còn sống (C4): listing
     sinh ra OPEN kèm hạn của tin mới, trừ khi hạn đó đã qua thì listing sinh ra CLOSED 'expired_auto'. Job suy ra
     từ listing nên tự mở lại (db.job_sync, do link_repost_source gọi), không còn hàm mở lại riêng.
  2. Job chuyển sang CLOSED: mọi listing chưa CLOSED của job thành CLOSED, cùng closed_reason với job
     (close_job_listings). Listing đã CLOSED trước đó giữ lý do và giờ đóng của chính nó.
  3. Nhân viên mở lại job CLOSED: các listing CLOSED vì 'staff' về OPEN, và listing hiện hành (URL trùng
     job_postings.source_url) về OPEN dù lý do đóng là gì, để job OPEN không bao giờ có 0 listing OPEN do
     chính thao tác mở lại. Listing đóng vì lý do khác (expired_auto, merged) giữ nguyên
     (reopen_job_listings). Listing hiện hành chết thật thì check_expired_source_jobs đóng lại ở lượt sau.
  4. (Đã gộp vào luật 1 ở C4: listing của tin đăng lại sinh ra OPEN kèm hạn mới, không còn bước mở listing riêng.)
  5. Hạn của listing đã qua: listing OPEN hoặc UNKNOWN đóng với lý do 'expired_auto' (close_expired_listings,
     C3a). Job chỉ đóng khi hết listing sống, nhờ sync_job_from_listings của nơi gọi.
  6. URL của listing trả HTTP 404/410: đúng listing đó đóng 'expired_auto' (close_listing_dead, C3b). HTTP 2xx
     chỉ ghi last_seen_at (mark_listing_seen), KHÔNG đổi UNKNOWN thành OPEN (bạn chốt 09/10).

CHƯA ĐỔI CHỖ ĐỌC: không đoạn code nào đọc các cột này để ra quyết định (đó là C2 và C3). Mọi hàm ở đây chỉ
ghi, không tự commit (đúng quy ước lớp db: nơi gọi chịu trách nhiệm).

Toàn bộ SQL ghi listing_status / closed_reason / closed_at nằm ở file này, để CHECK
chk_job_sources_log_closed_state (CLOSED thì có lý do, không CLOSED thì không có lý do và giờ đóng) chỉ có
một chỗ cần giữ đúng.
"""

import logging
from datetime import date
from typing import Optional

from scrapjd.db.job_sync import sync_job_from_listings
from scrapjd.db.pg_types import Conn

logger = logging.getLogger(__name__)

LISTING_OPEN = "OPEN"
LISTING_CLOSED = "CLOSED"
LISTING_UNKNOWN = "UNKNOWN"

# closed_reason mà tin đăng lại được phép mở lại job (luật 1). Chỉ job do check_expired_source_jobs đóng. 'staff' là
# nhân viên chủ động đóng, 'unknown' không rõ (coi như do nhân viên đóng), 'merged' dự phòng: cả ba giữ CLOSED.
AUTO_REOPEN_REASONS = frozenset({"expired_auto"})

# Cách xử lý khi URL đã có trong job_sources_log. Chuỗi SQL cố định (không nhận từ bên ngoài).
CONFLICT_NONE = "none"                 # INSERT thường: URL đã có thì raise UniqueViolation (insert_job)
CONFLICT_URL = "url"                   # ON CONFLICT (source_url) DO NOTHING (link_repost_source)
_CONFLICT_SQL = {
    CONFLICT_NONE: "",
    CONFLICT_URL: "ON CONFLICT (source_url) DO NOTHING",
}


def initial_listing_state(job_status: str, job_closed_reason: Optional[str], *, deadline=None,
                          today: Optional[date] = None) -> tuple:
    """(listing_status, closed_reason) cho listing MỚI của một job đang ở trạng thái cho trước (luật 1), `deadline`
    là hạn của chính tin mới. Hàm thuần, không đụng DB. Job CLOSED mà thiếu closed_reason (không xảy ra với dữ liệu
    hợp lệ vì chk_job_postings_closed_state) thì dùng 'unknown' thay vì làm vỡ CHECK của listing.

    Job CLOSED vì lý do trong AUTO_REOPEN_REASONS: listing OPEN nếu tin mới không có hạn hoặc hạn chưa qua (hạn
    đúng bằng hôm nay vẫn còn hiệu lực, giống check_expired_source_jobs: chỉ đóng khi deadline < hôm nay), còn lại
    CLOSED 'expired_auto' vì job sẽ bị đóng lại ngay ở lượt kiểm tra sau. `today` mặc định date.today()."""
    if job_status != "CLOSED":
        return LISTING_OPEN, None
    if job_closed_reason in AUTO_REOPEN_REASONS:
        if deadline is not None and deadline < (today or date.today()):
            return LISTING_CLOSED, job_closed_reason
        return LISTING_OPEN, None
    return LISTING_CLOSED, job_closed_reason or "unknown"


def insert_listing(conn: Conn, *, job_id: str, source_name: str, source_url: str,
                   salary_raw_text: str = "", raw_jd_content: str = "",
                   detail_fetched: bool = False, deadline=None,
                   on_conflict: str = CONFLICT_URL, today: Optional[date] = None) -> bool:
    """Ghi một listing mới cho job ĐÃ CÓ; trạng thái suy ra từ job lúc này và hạn của tin mới (luật 1; `today` chỉ
    để test, mặc định date.today()). Job CLOSED vì expired_auto mà tin mới còn hạn thì listing sinh ra OPEN trong khi
    job còn CLOSED: nơi gọi PHẢI gọi sync_job_from_listings ngay sau trong cùng transaction (link_repost_source làm
    việc đó). Trả True nếu vừa
    thêm dòng, False nếu URL đã có (chỉ với CONFLICT_URL; CONFLICT_NONE thì raise UniqueViolation). Raise LookupError nếu job không tồn tại.

    first_seen_at và last_seen_at lấy mặc định now() của cột (cùng một giá trị, nên last_seen_at >=
    first_seen_at). detail_fetched=True ghi luôn detail_checked_at = now(): fetch trang chi tiết vừa thành
    công thì tin chắc chắn còn ở nguồn.

    Đọc job bằng FOR NO KEY UPDATE rồi mới chèn: nếu nhân viên đang đóng job trong một transaction khác thì
    lệnh đọc chờ tới khi họ commit, nên đọc ra đúng trạng thái đã đóng; ngược lại thao tác đóng job
    (update_job) chờ listing vừa chèn commit xong rồi mới đóng nốt nó. Không có kẽ hở \"job CLOSED mà listing
    mới OPEN\". Cùng mức khoá với sync_job_from_listings và UPDATE job thường nên không có chuyện nâng khoá
    giữa hai transaction (deadlock)."""
    if on_conflict not in _CONFLICT_SQL:
        raise ValueError(f"on_conflict không hợp lệ: {on_conflict!r}")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT job_status::text, closed_reason FROM job_postings WHERE job_id = %s FOR NO KEY UPDATE",
            (job_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise LookupError(f"job {job_id} không tồn tại, không ghi được listing {source_url}")
        status, reason = initial_listing_state(row[0], row[1], deadline=deadline, today=today)
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


def mark_listing_detail_checked(conn: Conn, source_url: str, *, deadline=None) -> None:
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
            RETURNING job_id
            """,
            (deadline, source_url),
        )
        row = cur.fetchone()
    if row is not None:
        sync_job_from_listings(conn, str(row[0]))      # hạn listing đổi thì hạn job suy ra có thể đổi theo


def mark_listing_seen(conn: Conn, source_url: str) -> bool:
    """Ghi last_seen_at = now() cho URL vừa được xác nhận còn ở nguồn (HTTP 2xx của
    check_expired_source_jobs). Không đổi listing_status. Trả True nếu có dòng được ghi."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE job_sources_log SET last_seen_at = GREATEST(last_seen_at, now()) WHERE source_url = %s "
            "RETURNING job_id",
            (source_url,),
        )
        row = cur.fetchone()
    if row is None:
        return False
    sync_job_from_listings(conn, str(row[0]))          # job chưa có listing OPEN thì URL suy ra theo last_seen_at
    return True


def close_job_listings(conn: Conn, job_id: str) -> int:
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


def reopen_job_listings(conn: Conn, job_id: str) -> int:
    """Luật 3. Gọi SAU khi nhân viên mở lại job (job đã được ghi OPEN trong cùng transaction): các listing
    chưa OPEN mà đóng vì 'staff', cùng listing hiện hành (URL trùng job_postings.source_url), về OPEN và
    xoá closed_reason/closed_at; nếu vậy mà job vẫn không có listing nào OPEN hoặc UNKNOWN thì mở listing
    thấy gần nhất. Job không OPEN thì không làm gì. Idempotent. Trả số listing vừa mở."""
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
        opened = cur.rowcount
        # Dữ liệu cũ có thể không còn listing nào trùng job.source_url (vd URL đã đổi): nhân viên đã quyết định
        # mở job thì phải có ít nhất một listing còn sống, nếu không job suy ra sẽ bị đóng lại ngay. Mở listing
        # thấy gần nhất.
        cur.execute(
            """
            UPDATE job_sources_log SET listing_status = 'OPEN', closed_reason = NULL, closed_at = NULL
             WHERE log_id = (SELECT l.log_id FROM job_sources_log l
                              WHERE l.job_id = %s ORDER BY l.last_seen_at DESC, l.source_url LIMIT 1)
               AND listing_status = 'CLOSED'
               AND EXISTS (SELECT 1 FROM job_postings WHERE job_id = %s AND job_status = 'OPEN')
               AND NOT EXISTS (SELECT 1 FROM job_sources_log
                                WHERE job_id = %s AND listing_status IN ('OPEN', 'UNKNOWN'))
            """,
            (job_id, job_id, job_id),
        )
        return opened + cur.rowcount


def set_job_listings_deadline(conn: Conn, job_id: str, deadline) -> int:
    """Nhân viên sửa hoặc xoá hạn của job (PATCH, import): ghi cùng hạn đó vào MỌI listing OPEN của job (bạn
    duyệt 08/10: nhân viên gõ hạn nào thì hạn job đúng là hạn đó, kể cả khi job có nhiều listing OPEN hạn khác
    nhau). Job không có listing OPEN thì ghi vào mọi listing, đúng \"vùng\" mà derive_job_from_listings lấy
    hạn khi không có OPEN. Deadline None = xoá hạn. Trả số dòng ghi."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE job_sources_log SET deadline = %s
             WHERE job_id = %s
               AND (listing_status = 'OPEN'
                    OR NOT EXISTS (SELECT 1 FROM job_sources_log o
                                    WHERE o.job_id = %s AND o.listing_status = 'OPEN'))
            """,
            (deadline, job_id, job_id),
        )
        return cur.rowcount


def close_expired_listings(conn: Conn, job_id: str, today) -> int:
    """Luật 5 (C3a). Đóng mọi listing OPEN hoặc UNKNOWN của job mà hạn đã qua (`deadline` < today) với lý do
    'expired_auto' và closed_at = now(). Listing không có hạn thì không bao giờ bị đóng ở đây. Trả số listing
    vừa đóng.

    Điều kiện hạn được kiểm lại NGAY TRONG câu UPDATE (không tin danh sách đọc từ trước): giữa lúc đọc và lúc
    ghi, tin đăng lại hoặc nhân viên có thể đã dời hạn, khi đó listing không bị đóng. Khoá dòng job trước
    (FOR NO KEY UPDATE, cùng mức với insert_listing và sync_job_from_listings) để không giành nhau với
    thao tác đóng hoặc mở job đồng thời.

    KHÔNG tự đồng bộ job: nơi gọi gọi sync_job_from_listings sau đó (job chỉ CLOSED khi hết listing sống).
    Không commit."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM job_postings WHERE job_id = %s FOR NO KEY UPDATE", (job_id,))
        if cur.fetchone() is None:
            return 0
        cur.execute(
            """
            UPDATE job_sources_log
               SET listing_status = 'CLOSED', closed_reason = 'expired_auto', closed_at = now()
             WHERE job_id = %s AND listing_status IN ('OPEN', 'UNKNOWN')
               AND deadline IS NOT NULL AND deadline < %s
            """,
            (job_id, today),
        )
        return cur.rowcount


def close_listing_dead(conn: Conn, job_id: str, source_url: str) -> bool:
    """Luật 6 (C3b). Nguồn xác nhận URL này đã chết (HTTP 404/410): listing của URL đó, đang OPEN hoặc UNKNOWN,
    đóng với lý do 'expired_auto' và closed_at = now(). Chỉ đóng ĐÚNG listing đó, listing khác của job giữ
    nguyên. Trả True nếu vừa đóng; False nếu listing không tồn tại, không thuộc job này, hoặc đã CLOSED rồi
    (idempotent, và không ghi đè lý do hay giờ đóng cũ).

    Khoá dòng job trước (FOR NO KEY UPDATE, như close_expired_listings). KHÔNG tự đồng bộ job: nơi gọi gọi
    sync_job_from_listings sau đó (job chỉ CLOSED khi hết listing sống). Không commit."""
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM job_postings WHERE job_id = %s FOR NO KEY UPDATE", (job_id,))
        if cur.fetchone() is None:
            return False
        cur.execute(
            """
            UPDATE job_sources_log
               SET listing_status = 'CLOSED', closed_reason = 'expired_auto', closed_at = now()
             WHERE job_id = %s AND source_url = %s AND listing_status IN ('OPEN', 'UNKNOWN')
            """,
            (job_id, source_url),
        )
        return cur.rowcount > 0


def job_is_closed_locked(conn: Conn, job_id: str) -> bool:
    """Job đang CLOSED? Khoá dòng job (FOR NO KEY UPDATE) để trạng thái đọc ra không đổi tới hết transaction.
    update_job gọi trước khi ghi job_status = 'OPEN' để biết đây có phải lần MỞ LẠI (CLOSED -> OPEN) hay
    chỉ là form gửi lại OPEN: chỉ lần mở lại mới mở listing (luật 3). Job không tồn tại thì False."""
    with conn.cursor() as cur:
        cur.execute("SELECT job_status::text FROM job_postings WHERE job_id = %s FOR NO KEY UPDATE", (job_id,))
        row = cur.fetchone()
    return row is not None and row[0] == "CLOSED"


def sync_listings_after_job_update(conn: Conn, job_id: str, *, job_status: Optional[str], was_closed: bool,
                                   deadline_changed: bool, deadline=None) -> None:
    """Đưa listing rồi job theo kịp một lần db.update_job() vừa ghi job (nhân viên sửa tay, import,
    check_expired_source_jobs). Gọi SAU câu UPDATE job_postings, cùng transaction.
      - job_status = 'CLOSED': đóng mọi listing (luật 2);
      - job_status = 'OPEN' và was_closed (đúng lần mở lại): luật 3;
      - hạn được sửa hoặc xoá (`deadline` None khi xoá): ghi vào mọi listing OPEN (set_job_listings_deadline);
      - cuối cùng đồng bộ job theo listing (C2): giá trị job thành giá trị suy ra. Không có thay đổi trạng
        thái hay hạn thì không đụng gì (sửa tên, lương... không tốn thêm câu nào)."""
    if job_status == "CLOSED":
        close_job_listings(conn, job_id)
    elif job_status == "OPEN" and was_closed:
        reopen_job_listings(conn, job_id)
    if deadline_changed:
        set_job_listings_deadline(conn, job_id, deadline)
    if job_status is not None or deadline_changed:
        sync_job_from_listings(conn, job_id)
