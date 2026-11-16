"""
db.backfill_progress — tiến độ của các script vá dữ liệu (scripts/backfill/*) lưu trong bảng `backfill_progress`
(migration 0046), thay cho file `.backfill_*.done` cục bộ (E11).

Mỗi dòng nói "script X đã xử lý xong mục Y" (hiện Y là job_id, lưu dạng chuỗi). Script đọc tập "đã xong" để loại
bớt khỏi danh sách cần xử lý, và đánh dấu từng mục ngay khi xong, nên ngắt giữa chừng rồi chạy lại ở máy nào
cũng tiếp được đúng chỗ.

Không hàm nào ở đây tự commit: người gọi quyết định ranh giới transaction (cùng quy ước các module db khác,
ví dụ mark_source_detail_checked).
"""

from collections.abc import Iterable

from scrapjd.db.pg_types import Conn

__all__ = [
    "load_backfill_done",
    "mark_backfill_done",
    "mark_backfill_done_many",
    "reset_backfill_progress",
]


def load_backfill_done(conn: Conn, script_name: str) -> set[str]:
    """Mọi item_id script này đã xong. Đóng transaction đọc trước khi trả về (script gọi hàm này ở đầu lượt chạy,
    trước khi mở transaction ghi đầu tiên)."""
    with conn.cursor() as cur:
        cur.execute("SELECT item_id FROM backfill_progress WHERE script_name = %s", (script_name,))
        done = {row[0] for row in cur.fetchall()}
    conn.rollback()
    return done


def mark_backfill_done(conn: Conn, script_name: str, item_id: str) -> None:
    """Đánh dấu một mục đã xong. Đánh dấu lại mục đã có là không làm gì (giữ nguyên done_at đầu tiên).
    Không tự commit."""
    mark_backfill_done_many(conn, script_name, [item_id])


def mark_backfill_done_many(conn: Conn, script_name: str, item_ids: Iterable[str]) -> int:
    """Đánh dấu nhiều mục một lượt, trả số dòng MỚI thêm (mục đã có không tính). Dùng khi nhập file `.done` cũ.
    Không tự commit."""
    ids = sorted({str(i) for i in item_ids})
    if not ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO backfill_progress (script_name, item_id) "
            "SELECT %s, unnest(%s::text[]) ON CONFLICT (script_name, item_id) DO NOTHING",
            (script_name, ids),
        )
        return cur.rowcount


def reset_backfill_progress(conn: Conn, script_name: str) -> int:
    """Xoá toàn bộ tiến độ của script này (cho script chạy lại từ đầu), trả số dòng đã xoá. Không tự commit."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM backfill_progress WHERE script_name = %s", (script_name,))
        return cur.rowcount
