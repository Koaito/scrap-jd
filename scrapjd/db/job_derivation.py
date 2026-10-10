"""
db.job_derivation — SUY RA trạng thái, hạn và URL của job từ các listing của nó (C2, nửa 1/2).

MÔ HÌNH (xem scrapjd/db/listing_state.py và KE_HOACH_BACKEND_DATABASE.md mục 4): listing (một dòng
job_sources_log) giữ trạng thái, job là giá trị tổng hợp. Nửa 1/2 của C2 CHỈ ĐỊNH NGHĨA phép tổng hợp đó
và đo độ lệch so với giá trị đang lưu trong job_postings (scrapjd/cli/check_listing_derivation.py). Chưa có chỗ ghi
nào dùng nó: nửa 2/2 mới cho pipeline và update_job ghi giá trị suy ra vào job.

LUẬT (bạn duyệt 08/10/2026):
  - Trạng thái: OPEN nếu có listing OPEN. Không có OPEN mà còn listing UNKNOWN thì vẫn OPEN (chưa có
    bằng chứng đã chết, thà thiếu còn hơn sai). Mọi listing đều CLOSED thì CLOSED.
  - Hạn: hạn muộn nhất trong các listing OPEN; không có listing OPEN thì muộn nhất trong mọi listing. Không
    listing nào có hạn thì NULL.
  - source_url: URL của listing OPEN mới nhất (first_seen_at lớn nhất); không có OPEN thì listing thấy
    gần nhất (last_seen_at lớn nhất). Hòa thì xét tiếp theo thứ tự cố định để kết quả không phụ thuộc thứ tự
    dòng đọc ra.
  - closed_reason (chỉ có nghĩa khi CLOSED; luật này do mình đề xuất, bạn chưa duyệt): lý do của listing đóng
    muộn nhất (closed_at lớn nhất, NULL coi là cũ nhất); hòa thì lý do do người quyết định thắng:
    staff, merged, unknown, expired_auto.

Job chưa có listing nào thì KHÔNG suy ra được (trả None): đó là dữ liệu thiếu, không phải \"job CLOSED\".

Hàm thuần (derive_job_from_listings) có test riêng; phần SQL chỉ đọc.
"""

from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Optional
from scrapjd.db.pg_types import Conn

LISTING_OPEN = "OPEN"
LISTING_CLOSED = "CLOSED"
LISTING_UNKNOWN = "UNKNOWN"

# Thứ tự thắng khi hai listing đóng cùng một lúc: lý do do người quyết định trước lý do tự động.
_REASON_PRIORITY: dict[Optional[str], int] = {"staff": 0, "merged": 1, "unknown": 2, "expired_auto": 3}


@dataclass(frozen=True)
class DerivedJob:
    job_status: str                    # 'OPEN' | 'CLOSED'
    closed_reason: Optional[str]       # có giá trị khi và chỉ khi job_status == 'CLOSED'
    deadline: Optional[date]
    source_url: Optional[str]


def _latest(listings: list[dict], key: Callable[[dict], Any]) -> dict:
    """Phần tử có `key` lớn nhất; hòa thì lấy URL nhỏ nhất theo thứ tự chữ (xác định, không phụ thuộc thứ
    tự dòng đọc ra)."""
    best = max(key(l) for l in listings)
    tied = [l for l in listings if key(l) == best]
    return min(tied, key=lambda l: l.get("source_url") or "")


def _closed_sort_key(listing: dict) -> tuple:
    closed_at = listing.get("closed_at")
    priority = _REASON_PRIORITY.get(listing.get("closed_reason"), len(_REASON_PRIORITY))
    # max(): closed_at lớn hơn thắng (NULL = cũ nhất), hòa thì priority nhỏ hơn thắng
    return (closed_at is not None, closed_at.timestamp() if closed_at is not None else 0.0, -priority)


def derive_job_from_listings(listings: list) -> Optional[DerivedJob]:
    """Giá trị job suy ra từ danh sách listing (mỗi phần tử là dict có các khoá listing_status, deadline,
    first_seen_at, last_seen_at, closed_reason, closed_at, source_url). Danh sách rỗng thì trả None."""
    if not listings:
        return None
    open_ones = [l for l in listings if l["listing_status"] == LISTING_OPEN]
    unknown_ones = [l for l in listings if l["listing_status"] == LISTING_UNKNOWN]
    closed_ones = [l for l in listings if l["listing_status"] == LISTING_CLOSED]

    if open_ones or unknown_ones:
        status, reason = "OPEN", None
    else:
        status = "CLOSED"
        reason = _latest(closed_ones, _closed_sort_key).get("closed_reason") or "unknown"

    deadline_pool = open_ones or listings
    deadlines = [l["deadline"] for l in deadline_pool if l.get("deadline") is not None]
    deadline = max(deadlines) if deadlines else None

    if open_ones:
        url = _latest(open_ones, lambda l: (l["first_seen_at"], l["last_seen_at"])).get("source_url")
    else:
        url = _latest(listings, lambda l: (l["last_seen_at"], l["first_seen_at"])).get("source_url")

    return DerivedJob(job_status=status, closed_reason=reason, deadline=deadline, source_url=url)


# ---------------------------------------------------------------------- SQL (CHỈ ĐỌC)
_JOB_COLUMNS = ("job_id", "job_title", "company_name", "job_status", "closed_reason", "deadline", "source_url")
_LISTING_COLUMNS = ("job_id", "source_url", "listing_status", "deadline", "first_seen_at", "last_seen_at",
                    "closed_reason", "closed_at")


def list_jobs_with_listings(conn: Conn) -> list:
    """[{**cột job (_JOB_COLUMNS, job_id là str), "listings": [dict (_LISTING_COLUMNS)]}] cho MỌI job, kể cả
    job chưa có listing (listings = []). Sắp theo job_id; listing sắp theo first_seen_at, source_url. CHỈ
    ĐỌC; không lấy raw_jd_content (nặng, không cần); đóng transaction đọc trước khi trả."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT jp.job_id, jp.job_title, c.company_name, jp.job_status::text, jp.closed_reason,
                   jp.deadline, jp.source_url
              FROM job_postings jp JOIN companies c ON c.company_id = jp.company_id
             ORDER BY jp.job_id
            """
        )
        jobs = []
        for r in cur.fetchall():
            row = dict(zip(_JOB_COLUMNS, r))
            row["job_id"] = str(row["job_id"])
            row["listings"] = []
            jobs.append(row)
        by_id = {j["job_id"]: j for j in jobs}
        cur.execute(
            """
            SELECT job_id, source_url, listing_status, deadline, first_seen_at, last_seen_at,
                   closed_reason, closed_at
              FROM job_sources_log
             ORDER BY job_id, first_seen_at, source_url
            """
        )
        for r in cur.fetchall():
            row = dict(zip(_LISTING_COLUMNS, r))
            row["job_id"] = str(row["job_id"])
            job = by_id.get(row["job_id"])
            if job is not None:
                job["listings"].append(row)
    conn.rollback()
    return jobs


def list_checkable_listings(conn: Conn) -> list:
    """Listing mà check_expired_source_jobs còn phải kiểm tra (C3a): listing OPEN hoặc UNKNOWN của job đang
    OPEN. Listing CLOSED không cần kiểm lại; listing của job đã đóng cũng không (job CLOSED thì mọi listing
    đã CLOSED, xem db.listing_state). Gồm cả listing của job nhập tay (URL dạng manual://uuid): chúng có thể
    có hạn, và nhánh hạn không đụng tới mạng.

    Trả list[(job_id, job_title, source_url, deadline)], mỗi phần tử là MỘT listing, sắp theo job
    (created_at, job_id) rồi listing (first_seen_at, source_url) để các listing cùng job nằm liền nhau và
    thứ tự job theo created_at như check_expired_source_jobs vẫn xử lý. CHỈ ĐỌC, không commit hay
    rollback: nơi gọi tự commit từng job khi ghi."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT jp.job_id, jp.job_title, l.source_url, l.deadline
              FROM job_sources_log l
              JOIN job_postings jp ON jp.job_id = l.job_id
             WHERE jp.job_status = 'OPEN' AND l.listing_status IN ('OPEN', 'UNKNOWN')
             ORDER BY jp.created_at, jp.job_id, l.first_seen_at, l.source_url
            """
        )
        return cur.fetchall()
