"""
db.job_sync — ĐƯA JOB THEO KỊP LISTING (C2, nửa 2/2): sau mỗi lần ghi listing, ghi lại job_status,
closed_reason, deadline, source_url của job cho bằng giá trị db.job_derivation suy ra từ các listing.

Đây là chỗ \"job là giá trị tổng hợp\" thành hiện thực ở tầng ghi. Các đường ghi trực tiếp vào job (ca đặc
biệt của reopen_job_for_repost, extend_job_deadline, update_job) vẫn còn, tạm thời, để C4 gỡ khi đã đủ bằng
chứng (lệnh check-listing-derivation --strict); hàm này chạy SAU chúng nên giá trị cuối cùng luôn là giá trị
suy ra, và là no-op khi hai bên đã khớp.

QUY ƯỚC
  - Chỉ ghi các cột LỆCH, một câu UPDATE, và chỉ khi có lệch (không lệch thì không ghi gì, kể cả không khoá
    ghi thêm ngoài việc khoá dòng job để đọc).
  - KHÔNG làm nhảy updated_at: đây là việc giữ cho khớp, không phải người sửa job (cùng cách db.job_merge
    dùng cờ app.skip_updated_at, ở đây khôi phục cờ về giá trị cũ ngay sau câu UPDATE để không rò sang các
    câu sau trong cùng transaction, vd update_job ngay sau đó).
  - Job chưa có listing nào thì không làm gì (không suy ra được, xem derive_job_from_listings).
  - Khoá dòng job bằng FOR NO KEY UPDATE, cùng mức khoá với insert_listing và UPDATE job thường, để một
    transaction vừa chèn listing vừa đồng bộ không phải \"nâng khoá\" (nâng từ FOR SHARE lên FOR UPDATE giữa
    hai transaction là công thức deadlock).
  - Không commit (đúng quy ước lớp db). Trả {cột: (giá trị cũ, giá trị mới)} của các cột đã đổi; trạng thái
    đổi thì ghi log INFO để thấy khi nào đường ghi trực tiếp và listing bất đồng.
"""

import logging

from db.job_derivation import _LISTING_COLUMNS, DerivedJob, derive_job_from_listings

logger = logging.getLogger(__name__)

SKIP_UPDATED_AT_SETTING = "app.skip_updated_at"


def diff_job_from_derived(stored: dict, derived: DerivedJob, *, defer_deadline: bool = False) -> dict:
    """{cột: (giá trị đang lưu, giá trị suy ra)} của các cột job LỆCH so với giá trị suy ra. Hàm thuần, không
    DB. `stored` cần các khoá job_status, closed_reason, deadline, source_url. Đây là MỘT nơi duy nhất quyết
    định cột nào phải ghi: sync_job_from_listings ghi theo nó, và kế hoạch gộp job (merge_duplicates, C3c)
    dùng chính nó để dự đoán những gì lúc gộp thật sẽ ghi.

    closed_reason chỉ có mặt khi job suy ra là CLOSED (job OPEN thì trigger trg_set_job_closed_state tự xoá lý
    do). defer_deadline=True bỏ qua hạn (xem followup_reasons của sync_job_from_listings)."""
    changes: dict = {}
    if stored["job_status"] != derived.job_status:
        changes["job_status"] = (stored["job_status"], derived.job_status)
    if derived.job_status == "CLOSED" and (
            "job_status" in changes or stored["closed_reason"] != derived.closed_reason):
        changes["closed_reason"] = (stored["closed_reason"], derived.closed_reason)
    if stored["deadline"] != derived.deadline and not defer_deadline:
        changes["deadline"] = (stored["deadline"], derived.deadline)
    if stored["source_url"] != derived.source_url:
        changes["source_url"] = (stored["source_url"], derived.source_url)
    return changes


def sync_job_from_listings(conn, job_id: str, *, followup_reasons=None) -> dict:
    """Xem docstring module. Trả {} nếu không có gì đổi hoặc job không tồn tại hoặc chưa có listing.

    followup_reasons (chỉ link_repost_source truyền, TẠM THỜI tới C4): tập closed_reason được pipeline mở lại
    tự động (AUTO_REOPEN_REASONS). Pipeline luôn gọi tiếp một trong hai hàm cũ sau link_repost_source:
      - job đang OPEN: extend_job_deadline. Hàm này là nơi dời hạn và đếm repost_deadline_extended, và pipeline
        không gọi nó cho job do nhân viên sửa tay, nên khi có tham số này đồng bộ KHÔNG đụng hạn của job OPEN
        (nhân viên đã chỉnh tay thì tin đăng lại không kéo hạn của họ);
      - job CLOSED vì một lý do trong tập: reopen_job_for_repost. Hàm này đọc giá trị cũ để ghi audit
        REOPEN_JOB, nên đồng bộ KHÔNG đụng gì của job đó; reopen_job_for_repost tự đồng bộ ngay sau khi mở được,
        còn khi từ chối mở lại (hạn tin mới đã qua) thì pipeline gọi sync_job_from_listings trực tiếp.
    Giá trị cuối cùng vẫn khớp listing vì hạn và URL mà hai hàm kia ghi chính là của listing mới. C4 bỏ hai hàm
    đó và bỏ tham số này."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT job_status::text, closed_reason, deadline, source_url FROM job_postings "
            "WHERE job_id = %s FOR NO KEY UPDATE",
            (job_id,),
        )
        row = cur.fetchone()
        if row is None:
            return {}
        stored = dict(zip(("job_status", "closed_reason", "deadline", "source_url"), row))
        if (followup_reasons is not None and stored["job_status"] == "CLOSED"
                and stored["closed_reason"] in followup_reasons):
            return {}
        cur.execute(
            "SELECT job_id, source_url, listing_status, deadline, first_seen_at, last_seen_at, "
            "closed_reason, closed_at FROM job_sources_log WHERE job_id = %s",
            (job_id,),
        )
        listings = [dict(zip(_LISTING_COLUMNS, r)) for r in cur.fetchall()]
        derived = derive_job_from_listings(listings)
        if derived is None:
            return {}

        defer = followup_reasons is not None and stored["job_status"] == "OPEN"
        changes = diff_job_from_derived(stored, derived, defer_deadline=defer)
        if not changes:
            return {}

        cur.execute("SELECT current_setting(%s, true)", (SKIP_UPDATED_AT_SETTING,))
        previous = cur.fetchone()[0] or ""
        assignments = ", ".join(f"{col} = %s" for col in changes)
        values = [new for _, new in changes.values()] + [job_id]
        cur.execute("SELECT set_config(%s, 'on', true)", (SKIP_UPDATED_AT_SETTING,))
        try:
            cur.execute(f"UPDATE job_postings SET {assignments} WHERE job_id = %s", values)
        finally:
            cur.execute("SELECT set_config(%s, %s, true)", (SKIP_UPDATED_AT_SETTING, previous))
    if "job_status" in changes:
        logger.info("Job %s: trạng thái %s -> %s theo các listing.", job_id, *changes["job_status"])
    return changes
