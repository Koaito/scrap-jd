"""
db.crawl_batches — "crawl nhiều category liên tục" (08/2026, xem
docstring sql/migration_add_crawl_batches.sql).

Cùng pattern scrapjd/db/crawl_runs.py: mỗi hàm tự lo transaction riêng (commit()
ngay trong hàm) — lý do giống hệt crawl_runs.py, các hàm này đều được
gọi từ scrapjd/api/crawl_runner.py::execute() (background task, không có
request/response bao quanh để commit hộ).

MODULE NÀY KHÔNG ĐỘNG TỚI CƠ CHẾ CỦA crawl_runs.py (heartbeat/log
live/UNIQUE INDEX một nguồn một lượt) — mỗi category trong batch vẫn là
1 dòng crawl_runs bình thường, được tạo qua db.crawl_runs.create_run()
(nhận thêm batch_id/batch_position qua tham số optional). Module này chỉ
lo phần MỚI: bảng crawl_batches (metadata chung) + logic "category kế
tiếp là gì".
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg2.extras

from scrapjd.db.crawl_runs import create_run
from scrapjd.db.pg_types import Conn, Row, fetch_all_rows, fetch_one_row, fetch_scalar

logger = logging.getLogger(__name__)


def create_batch(conn: Conn, *, source: str, categories: list[str], pages: int,
                  max_jobs: Optional[int], triggered_by: Optional[str]) -> str:
    """Tạo 1 dòng crawl_batches mới (status='running'), trả về batch_id
    (str). Gọi TRƯỚC KHI tạo run đầu tiên (xem
    scrapjd/api/crawl_runner.py::start_batch()) — batch phải tồn tại trước để
    run đầu có chỗ trỏ batch_id vào."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO crawl_batches (source, categories, pages, max_jobs, triggered_by)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING batch_id
            """,
            (
                source,
                json.dumps(categories, ensure_ascii=False),
                pages, max_jobs, triggered_by,
            ),
        )
        batch_id = str(fetch_scalar(cur))
    conn.commit()
    return batch_id


def advance_batch(conn: Conn, batch_id: str, finished_position: int) -> Optional[str]:
    """Gọi từ scrapjd/api/crawl_runner.py::execute() NGAY SAU KHI 1 run trong
    batch đổi xong trạng thái 'done'/'error' (cả 2 đều tính là "category
    này đã xong", batch KHÔNG dừng lại nếu 1 category lỗi — giống vòng
    for bash chạy hết các lệnh dù 1 lệnh giữa chừng lỗi).

    - Còn category kế tiếp trong batch.categories -> tạo 1 dòng
      crawl_runs mới cho category đó (batch_position = finished_position + 1),
      trả về run_id mới để execute() gọi tiếp (tự nối đuôi, không cần
      BackgroundTasks/router can thiệp gì thêm).
    - Hết category (finished_position là category CUỐI) -> mark_done(),
      trả về None (execute() dừng, không gọi tiếp).
    - batch_id không tồn tại (lẽ ra không xảy ra, phòng thủ) -> trả None.

    KHÔNG bọc try/except ActiveCrawlExistsError ở đây (để nguyên cho
    caller bắt) — execute() là nơi quyết định phải làm gì nếu tạo run kế
    tiếp thất bại (vd đánh dấu batch 'error' thay vì để 'running' treo
    mãi), xem docstring execute()."""
    batch = get_batch(conn, batch_id)
    if batch is None:
        logger.error("advance_batch() gọi với batch_id không tồn tại: %s", batch_id)
        return None

    if batch["status"] != "running":
        # Batch đã bị đóng từ ngoài (close_orphaned_batches() sau khi watchdog
        # coi run treo là lỗi) mà task cũ lại tỉnh dậy và gọi tới đây: không
        # đẻ thêm run cho batch đã khép lại, nếu không lịch sử sẽ có batch
        # 'error'/'done' mà vẫn đang có run chạy.
        logger.warning(
            "advance_batch(): batch %s đã ở trạng thái '%s', không tạo category kế tiếp.",
            batch_id, batch["status"],
        )
        return None

    categories = batch["categories"]
    next_position = finished_position + 1
    if next_position >= len(categories):
        mark_done(conn, batch_id)
        return None

    return create_run(
        conn, source=batch["source"], category=categories[next_position],
        pages=batch["pages"], max_jobs=batch["max_jobs"],
        triggered_by=batch["triggered_by"],
        batch_id=batch_id, batch_position=next_position,
    )


def mark_done(conn: Conn, batch_id: str) -> None:
    """Đổi status -> 'done', điền finished_at — gọi khi advance_batch()
    xác định category cuối cùng đã xong (KHÔNG liên quan category đó
    done hay error — batch 'done' nghĩa là "đã chạy hết danh sách",
    không phải "mọi category đều thành công")."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE crawl_batches SET status = 'done', finished_at = %s WHERE batch_id = %s",
            (datetime.now(timezone.utc), batch_id),
        )
    conn.commit()


def mark_error(conn: Conn, batch_id: str, error: str) -> None:
    """Đổi status -> 'error', điền error + finished_at — gọi khi
    execute() không tự tạo được run kế tiếp giữa chừng batch (vd
    ActiveCrawlExistsError bất ngờ do ai đó crawl tay đúng lúc source
    này vừa rảnh). KHÁC mark_done(): batch dừng SỚM, chưa chạy hết
    categories còn lại — xem docstring cột status trong migration."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE crawl_batches
            SET status = 'error', error = %s, finished_at = %s
            WHERE batch_id = %s
            """,
            (error, datetime.now(timezone.utc), batch_id),
        )
    conn.commit()


def delete_batch(conn: Conn, batch_id: str) -> None:
    """Xoá 1 batch CHƯA có run con nào (ON DELETE CASCADE ở crawl_runs.batch_id
    sẽ xoá cả run con nếu có, nên chỉ gọi khi biết chắc batch vừa tạo mà
    run đầu thất bại — xem scrapjd/api/crawl_runner.py::start_batch()).
    Ngoài trường hợp đó dùng close_orphaned_batches(), không xoá lịch sử."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM crawl_batches WHERE batch_id = %s", (batch_id,))
    conn.commit()


def close_orphaned_batches(conn: Conn, *, grace_minutes: int = 0) -> int:
    """Đóng mọi batch 'running' KHÔNG còn run nào 'queued'/'running' — tức
    không còn gì đang chạy để tự nối tiếp category kế. Trả về số batch đã đóng.

    Batch chỉ được nối tiếp bởi chính task đang chạy run của nó
    (crawl_runner._run_batch_locked). Task đó chết thì không ai đóng batch:
    reconcile_orphaned_runs() / reconcile_stale_runs() chỉ sửa crawl_runs, nên
    batch kẹt 'running' mãi (8 batch trong dữ liệu 11/2026). Trạng thái được
    suy ra từ số run đã tạo so với danh sách category, theo đúng định nghĩa
    ở sql/migration_add_crawl_batches.sql ('done' = đã chạy hết danh sách, dù
    category nào lỗi; 'error' = dừng sớm):

    - số run >= số category -> 'done' (run cuối bị chết cũng tính: category đó
      đã được chạy, không còn gì để nối tiếp).
    - ít hơn -> 'error', ghi rõ đã tạo bao nhiêu run trên tổng số category.

    finished_at lấy mốc kết thúc run con cuối (rơi về created_at nếu batch
    không có run nào), không phải now(), để lịch sử cũ không bị đổi thành
    ngày dọn.

    grace_minutes: chỉ đóng batch mà hoạt động cuối (run con kết thúc cuối
    cùng, hoặc created_at) đã cách hơn ngần này phút. Cần > 0 khi gọi định kỳ
    (watchdog): giữa lúc một run xong và lúc advance_batch() tạo run kế tiếp
    batch cũng không có run nào đang chạy trong chốc lát, không được đóng
    nhầm. Đặt 0 CHỈ khi gọi lúc khởi động, SAU reconcile_orphaned_runs() (lúc
    đó chắc chắn không còn task nào đang nối tiếp, cùng lý do ở docstring của
    hàm đó)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH agg AS (
                SELECT cb.batch_id,
                       jsonb_array_length(cb.categories)               AS total,
                       count(cr.run_id)                                AS n_runs,
                       count(cr.run_id) FILTER (
                           WHERE cr.status IN ('queued', 'running'))   AS n_active,
                       COALESCE(max(cr.finished_at), cb.created_at)    AS last_activity
                FROM crawl_batches cb
                LEFT JOIN crawl_runs cr ON cr.batch_id = cb.batch_id
                WHERE cb.status = 'running'
                GROUP BY cb.batch_id, cb.categories, cb.created_at
            )
            UPDATE crawl_batches cb
            SET status = (CASE WHEN a.n_runs >= a.total THEN 'done' ELSE 'error' END)::crawl_status_enum,
                error = CASE
                    WHEN a.n_runs >= a.total THEN NULL
                    ELSE format(
                        'Batch dừng giữa chừng: chỉ tạo được %%s/%%s category (process bị '
                        'dừng hoặc run bị treo), các category còn lại không được chạy.',
                        a.n_runs, a.total)
                END,
                finished_at = a.last_activity
            FROM agg a
            WHERE cb.batch_id = a.batch_id
              AND a.n_active = 0
              AND a.last_activity <= now() - make_interval(mins => %s)
            RETURNING cb.batch_id
            """,
            (grace_minutes,),
        )
        count = cur.rowcount
    conn.commit()
    return count


_BATCH_SELECT_COLUMNS = """
        cb.batch_id, cb.source, cb.categories, cb.pages, cb.max_jobs,
        cb.status, cb.error, cb.triggered_by,
        u.full_name AS triggered_by_name,
        cb.created_at, cb.finished_at
"""

_BATCH_FROM_JOINS = """
    FROM crawl_batches cb
    LEFT JOIN app_users u ON u.ss_user_id = cb.triggered_by
"""


def get_batch(conn: Conn, batch_id: str) -> Optional[Row]:
    """Trả 1 dict crawl_batches (KHÔNG kèm items — dùng nội bộ trong
    advance_batch(), nhẹ hơn get_batch_with_items() cho router) hoặc
    None nếu không tồn tại."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_BATCH_SELECT_COLUMNS} {_BATCH_FROM_JOINS} WHERE cb.batch_id = %s",
            (batch_id,),
        )
        return cur.fetchone()


def get_batch_with_items(conn: Conn, batch_id: str) -> Optional[Row]:
    """Trả 1 dict crawl_batches KÈM "items" (list các crawl_runs con,
    sắp theo batch_position tăng dần) + "total"/"completed" (số category
    đã done/error, KHÔNG tính category đang 'queued'/'running') — dùng
    cho GET /crawl/batch/{batch_id}, để frontend gộp hiển thị tiến độ
    tổng kiểu "2/6 category xong" thay vì phải tự đếm từ list run rời
    rạc."""
    batch = get_batch(conn, batch_id)
    if batch is None:
        return None

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                cr.run_id, cr.source, cr.category, cr.pages, cr.max_jobs,
                cr.status, cr.stats, cr.error, cr.triggered_by,
                cr.started_at, cr.finished_at, cr.progress,
                cr.batch_id, cr.batch_position
            FROM crawl_runs cr
            WHERE cr.batch_id = %s
            ORDER BY cr.batch_position ASC
            """,
            (batch_id,),
        )
        items = cur.fetchall()

    batch["items"] = items
    batch["total"] = len(batch["categories"])
    batch["completed"] = sum(1 for it in items if it["status"] in ("done", "error"))
    return batch


def list_batches(conn: Conn, *, source: Optional[str] = None,
                  status: Optional[str] = None,
                  triggered_by: Optional[str] = None,
                  limit: int = 50, offset: int = 0) -> tuple[list[Row], int]:
    """Trả (list[dict], total) — dùng cho GET /crawl/batch (lịch sử
    batch, đối xứng GET /crawl cho run đơn lẻ). Cùng shape
    (total/limit/offset/items) với list_crawl_runs()."""
    conditions = []
    params: list[Any] = []

    if source:
        conditions.append("cb.source = %s")
        params.append(source)
    if status:
        conditions.append("cb.status = %s")
        params.append(status)
    if triggered_by:
        conditions.append("cb.triggered_by = %s")
        params.append(triggered_by)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT count(*) AS total {_BATCH_FROM_JOINS} {where_clause}",
            params,
        )
        total = fetch_one_row(cur)["total"]

        cur.execute(
            f"SELECT {_BATCH_SELECT_COLUMNS} {_BATCH_FROM_JOINS} {where_clause} "
            f"ORDER BY cb.created_at DESC LIMIT %s OFFSET %s",
            params + [limit, offset],
        )
        rows = fetch_all_rows(cur)

    return rows, total
