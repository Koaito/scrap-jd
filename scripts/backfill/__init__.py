"""Script vá dữ liệu job đã lưu, có tiến độ lưu trong bảng `backfill_progress` (migration 0046), nên chạy lại ở máy nào cũng tiếp đúng chỗ."""

from typing import Callable

from scrapjd import db
from scrapjd.db.pg_types import Conn


def progress_marker(conn: Conn, script_name: str) -> Callable[[str], None]:
    """Hàm `on_done(job_id)` cho `run()` của các script backfill: ghi dấu "xong" vào DB và commit NGAY, để ngắt
    giữa chừng rồi chạy lại vẫn tiếp đúng chỗ. Ghi dấu lỗi thì rollback rồi ném lại; `run()` đếm đó là một lỗi
    của job, và job bị xử lý lại lần sau (việc vá idempotent), không mất dữ liệu."""

    def on_done(job_id: str) -> None:
        try:
            db.mark_backfill_done(conn, script_name, str(job_id))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return on_done
