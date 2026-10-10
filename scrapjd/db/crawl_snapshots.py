"""
db.crawl_snapshots — lưu/đọc snapshot HTML/JSON gốc của lượt crawl (đợt 3,
10/2026, xem scrapjd/snapshots.py + sql/migration_add_crawl_snapshots.sql).

Cùng pattern scrapjd/db/crawl_runs.py: mỗi hàm tự commit() ngay vì execute() chạy nền,
không có request/response bao quanh để commit hộ.
"""

import gzip
import logging
from typing import Iterable, Optional

import psycopg2.extras

from scrapjd.config import SNAPSHOT_RETENTION_DAYS
from scrapjd.db.pg_types import Conn, fetch_optional_row

logger = logging.getLogger(__name__)


def save_snapshots(conn: Conn, run_id: str, source: str, items: Iterable,
                   retention_days: int = SNAPSHOT_RETENTION_DAYS) -> int:
    """Lưu các Snapshot (xem scrapjd/snapshots.py) của 1 lượt crawl, rồi xoá bản ghi
    cũ hơn retention_days. Trả số bản ghi đã lưu.

    Không bao giờ raise: lưu snapshot chỉ phục vụ debug, lỗi ở đây (migration
    chưa chạy, DB tạm mất kết nối...) KHÔNG được làm đổi kết quả của lượt
    crawl. Lỗi được log + rollback rồi trả 0."""
    items = list(items)
    if not items:
        return 0
    try:
        with conn.cursor() as cur:
            for it in items:
                raw = it.body.encode("utf-8", errors="replace")
                cur.execute(
                    """
                    INSERT INTO crawl_snapshots
                        (run_id, source, kind, reason, url, content_gz, raw_bytes, truncated)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (run_id, source, it.kind, it.reason, it.url,
                     psycopg2.Binary(gzip.compress(raw)), len(raw), it.truncated),
                )
            cur.execute(
                "DELETE FROM crawl_snapshots "
                "WHERE created_at < now() - make_interval(days => %s)",
                (retention_days,),
            )
        conn.commit()
        return len(items)
    except Exception:  # noqa: BLE001 - snapshot chỉ để debug, không được làm hỏng crawl
        logger.exception("Không lưu được %d snapshot của run %s", len(items), run_id)
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0


def list_snapshots(conn: Conn, *, run_id: Optional[str] = None,
                   source: Optional[str] = None, limit: int = 50) -> list:
    """Liệt kê snapshot (KHÔNG kèm nội dung), mới nhất trước — dùng cho CLI
    `main.py snapshots` để chọn id cần xuất."""
    conditions, params = [], []
    if run_id:
        conditions.append("run_id = %s")
        params.append(run_id)
    if source:
        conditions.append("source = %s")
        params.append(source)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT id, run_id, source, kind, reason, url, raw_bytes, truncated, created_at "
            f"FROM crawl_snapshots {where} ORDER BY created_at DESC, id DESC LIMIT %s",
            params + [limit],
        )
        return cur.fetchall()


def get_snapshot(conn: Conn, snapshot_id: int) -> Optional[dict]:
    """Đọc 1 snapshot kèm nội dung đã giải nén ("body": str), hoặc None."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, run_id, source, kind, reason, url, content_gz, raw_bytes, "
            "truncated, created_at FROM crawl_snapshots WHERE id = %s",
            (snapshot_id,),
        )
        row = fetch_optional_row(cur)
    if row is None:
        return None
    row = dict(row)
    row["body"] = gzip.decompress(bytes(row.pop("content_gz"))).decode("utf-8", errors="replace")
    return row
