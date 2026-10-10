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
