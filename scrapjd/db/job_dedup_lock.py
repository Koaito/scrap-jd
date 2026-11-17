"""
db.job_dedup_lock — khoá advisory chống trùng khi nhiều nơi ghi job chạy song song (A4). Tách từ
scrapjd/db/jobs.py (đã chạm ngưỡng 700 dòng, xem tests/test_db_facade.py). Tên hàm giữ nguyên và vẫn gọi được
qua `db.lock_job_dedup_key`, `db.JobDedupLockTimeout`...

Nơi dùng: pipeline._process_job (crawl), db.create_manual_job và route POST /jobs (nhập tay).
"""

from typing import Optional

from psycopg2 import errors as pg_errors
from scrapjd.db.pg_types import Conn, fetch_scalar

# Thời gian tối đa chờ khoá chống trùng (mili giây). Mỗi job crawl chỉ giữ khoá vài chục mili giây
# (tra rồi insert rồi commit), nên chờ lâu hơn vậy nghĩa là có chuyện bất thường (một kết nối đứng
# treo giữa transaction). Hết thời gian thì báo JobDedupLockTimeout thay vì chờ vô hạn.
JOB_DEDUP_LOCK_TIMEOUT_MS = 10_000

# Tiền tố trộn vào khoá advisory để không đụng các khoá advisory khác (nếu sau này có).
_JOB_DEDUP_LOCK_PREFIX = "job_dedup:"


class JobDedupLockTimeout(Exception):
    """Chờ khoá chống trùng quá JOB_DEDUP_LOCK_TIMEOUT_MS. Transaction của nơi gọi đã bị Postgres
    đánh dấu lỗi: PHẢI rollback trước khi dùng kết nối tiếp (pipeline đã làm vậy ở vòng lặp job)."""


def lock_job_dedup_key(conn: Conn, *, company_id: str, job_title: str, province_id: Optional[int],
                       timeout_ms: int = JOB_DEDUP_LOCK_TIMEOUT_MS) -> None:
    """Giành khoá advisory CẤP TRANSACTION theo khoá chống trùng (job_postings.dedup_key = công ty +
    tiêu đề chuẩn hoá + tỉnh, xem sql/0039_add_job_dedup_key.sql) để bước "tra trùng rồi insert" của
    hai nơi ghi KHÔNG chen vào nhau được (A4).

    VẤN ĐỀ: hai lượt crawl khác nguồn (TopCV, VietnamWorks, CareerViet) chạy song song, hoặc crawl
    chạy lúc có người nhập tay, có thể cùng tra thấy "chưa có job này" rồi cùng insert, ra hai job
    cùng dedup_key. DB không có UNIQUE trên dedup_key (dữ liệu thật có nhóm trùng, và khác level là
    hợp lệ khi nhập tay) nên không có gì chặn.

    CÁCH CHẶN: gọi hàm này TRƯỚC câu tra trùng, trong cùng transaction với câu insert. Bên đến sau
    đứng chờ tới khi bên trước commit/rollback (khoá tự nhả), rồi tra lại và thấy job vừa tạo (mức
    cô lập mặc định READ COMMITTED: mỗi câu lệnh thấy dữ liệu đã commit mới nhất). Hai khoá chống
    trùng khác nhau KHÔNG chặn nhau. Dùng pg_advisory_xact_lock (không phải khoá phiên) nên an toàn
    với connection pooler của Supabase: khoá gắn với transaction, không rò sang client khác.

    Chỉ có tác dụng khi kết nối KHÔNG ở chế độ autocommit (khi đó transaction kết thúc ngay sau câu
    lệnh và khoá nhả liền); gặp autocommit thì raise RuntimeError để lỗi này lộ ra thay vì âm thầm
    vô tác dụng.

    Chờ quá timeout_ms thì raise JobDedupLockTimeout. lock_timeout chỉ đổi cho riêng câu giành khoá
    rồi trả về giá trị cũ, nên các câu lệnh sau trong transaction không bị ảnh hưởng.

    Khoá dựa trên dedup_key tính bằng job_dedup_key() của DB nên cùng một công thức với find_repost_
    candidate / find_manual_job_duplicate: tiêu đề lệch hoa/thường hay khoảng trắng vẫn chung một khoá.
    Không đóng transaction và không commit."""
    if getattr(conn, "autocommit", False) is True:
        raise RuntimeError("lock_job_dedup_key cần kết nối không autocommit: khoá cấp transaction "
                           "sẽ nhả ngay sau câu lệnh nên không bảo vệ được gì.")
    with conn.cursor() as cur:
        cur.execute("SELECT current_setting('lock_timeout')")
        previous = fetch_scalar(cur)
        cur.execute("SELECT set_config('lock_timeout', %s, true)", (f"{int(timeout_ms)}ms",))
        try:
            cur.execute(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended(%s || job_dedup_key(%s::uuid, %s::text, %s::int), 0))",
                (_JOB_DEDUP_LOCK_PREFIX, company_id, job_title, province_id),
            )
        except pg_errors.LockNotAvailable as exc:
            raise JobDedupLockTimeout(
                f"Chờ quá {timeout_ms} ms để giành khoá chống trùng job '{job_title}'") from exc
        cur.execute("SELECT set_config('lock_timeout', %s, true)", (previous,))


def lock_dedup_key_for_job_edit(conn: Conn, job_id: str, *, job_title: Optional[str] = None,
                                province_id: Optional[int] = None, clear_province: bool = False,
                                timeout_ms: int = JOB_DEDUP_LOCK_TIMEOUT_MS) -> None:
    """E4: giành khoá chống trùng của khoá MỚI trước khi update_job đổi tiêu đề hoặc tỉnh của job.

    VẤN ĐỀ: trigger set_job_dedup_key tính lại dedup_key ở mọi UPDATE, nên sửa job_title/province_id là
    đổi khoá chống trùng. crawl (pipeline) và nhập tay (create_manual_job) đều giành khoá advisory theo
    khoá đó rồi mới "tra trùng, rồi insert"; đường sửa không giành thì nó chen vào giữa hai bước ấy: crawl
    tra thấy "chưa có", nhân viên đổi một job khác sang đúng khoá đó, rồi crawl insert, ra hai job cùng khoá.
    Chốt chặn duy nhất trước đây là UNIQUE(source_url), không liên quan tới khoá này.

    CÁCH CHẶN: giành khoá của khoá MỚI (cùng công thức job_dedup_key() của DB) rồi giữ tới commit/rollback của
    nơi gọi. Bên crawl đến sau chờ, rồi tra lại và thấy job vừa sửa. Nếu crawl đến trước thì nhân viên chờ rồi
    vẫn sửa được: khoá chỉ xếp hàng, KHÔNG từ chối (không có UNIQUE trên dedup_key, và nhân viên được quyền
    cố ý để hai job cùng khoá, báo cáo trùng sẽ thấy).

    Chỉ khoá khi khoá THẬT SỰ đổi: form gửi lại đúng tiêu đề/tỉnh đang có, hay chỉ sửa field khác, thì không
    khoá gì (tránh chờ nhau vô ích). Không khoá khoá CŨ: rời khỏi một khoá không thể tạo trùng.

    Thứ tự khoá: advisory trước, rồi dòng job (FOR NO KEY UPDATE), giống pipeline (advisory rồi mới sửa/gộp job)
    và quy ước "job trước, listing sau" của listing_state. Khoá khoá advisory dựa vào lần đọc dòng job KHÔNG
    khoá, nên sau đó đọc lại dòng job KHOÁ DÒNG và tính lại khoá. Thường hai lần khớp nhau, xong. Nếu công
    ty/tiêu đề/tỉnh của job đã bị đổi giữa hai lần đọc (vd gộp công ty) thì khoá vừa giành không còn đúng; lúc này
    dòng job đã bị ta giữ nên không ai đổi được nữa, ta giành thêm đúng khoá tính từ dòng đã khoá (một lần,
    không vòng lặp). Ca hiếm này giành advisory khi đã giữ dòng job (ngược thứ tự thường lệ): nếu một transaction
    khác đang giữ khoá đó và chờ dòng job thì Postgres báo deadlock cho một bên (lỗi rõ ràng, không ghi sai).

    job_id không tồn tại: không khoá gì, update_job sẽ tự trả False. Cùng điều kiện với lock_job_dedup_key:
    kết nối không autocommit; chờ quá timeout_ms thì JobDedupLockTimeout (nơi gọi phải rollback)."""
    if job_title is None and province_id is None and not clear_province:
        return
    first = _read_edit_target(conn, job_id, job_title, province_id, clear_province, lock_row=False)
    if first is None or not first[3]:
        return
    lock_job_dedup_key(conn, company_id=first[0], job_title=first[1], province_id=first[2],
                       timeout_ms=timeout_ms)
    locked = _read_edit_target(conn, job_id, job_title, province_id, clear_province, lock_row=True)
    if locked is None or locked[:3] == first[:3] or not locked[3]:
        return
    lock_job_dedup_key(conn, company_id=locked[0], job_title=locked[1], province_id=locked[2],
                       timeout_ms=timeout_ms)


def _read_edit_target(conn: Conn, job_id: str, job_title: Optional[str], province_id: Optional[int],
                      clear_province: bool,
                      *, lock_row: bool) -> Optional[tuple[str, str, Optional[int], bool]]:
    """(company_id, tiêu đề mới, tỉnh mới, khoá có đổi không) của job nếu áp thay đổi vào; None nếu không có job.
    lock_row=True đọc FOR NO KEY UPDATE (cùng mức với UPDATE job thường và insert_listing)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT company_id::text, job_title, province_id, dedup_key FROM job_postings "
            "WHERE job_id = %s" + (" FOR NO KEY UPDATE" if lock_row else ""), (job_id,))
        found = cur.fetchone()
        if found is None:
            return None
        company_id, cur_title, cur_province, cur_key = found
        new_title = job_title if job_title is not None else cur_title
        new_province = None if clear_province else (province_id if province_id is not None else cur_province)
        cur.execute("SELECT job_dedup_key(%s::uuid, %s::text, %s::int)", (company_id, new_title, new_province))
        new_key = fetch_scalar(cur)
    return company_id, new_title, new_province, new_key != cur_key
