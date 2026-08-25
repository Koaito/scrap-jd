"""
db.crawl_runs — lưu bền trạng thái + lịch sử từng lượt crawl vào bảng
crawl_runs (08/2026, THAY THẾ _RUNS dict RAM cũ trong
api/crawl_runner.py — xem sql/migration_add_crawl_runs.sql để biết đầy
đủ lý do thiết kế).

Cùng pattern db/audit_logs.py: mỗi hàm ở đây tự lo transaction của
riêng nó (commit() ngay trong hàm, KHÁC các hàm insert_job()/log_action()
ở module khác vốn để router tự commit cùng lúc với thao tác chính) —
lý do khác biệt: api/crawl_runner.py::execute() chạy NỀN (background
task), không có 1 request/response bao quanh để router commit hộ, nên
mỗi lần đổi trạng thái (queued -> running -> done/error) PHẢI tự commit
ngay, để nếu process bị kill giữa chừng (deploy mới đè lên lúc đang
crawl) thì trạng thái đã ghi trước đó vẫn không bị mất/rollback.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import psycopg2.extras

logger = logging.getLogger(__name__)


class ActiveCrawlExistsError(Exception):
    """Nguồn (source) này đang có 1 lượt crawl 'queued'/'running' chưa
    xong — raise ở create_run() TRƯỚC KHI insert, để router trả 409 rõ
    ràng ("nguồn X đang crawl, đợi xong đã") thay vì để UNIQUE INDEX
    idx_crawl_runs_one_active_per_source ở DB raise IntegrityError mù mờ.

    Đây là LỚP CHẶN CHÍNH; UNIQUE INDEX ở DB là LỚP CHẶN THỨ 2 (phòng
    race condition 2 request cùng nguồn lọt qua SELECT check gần như
    đồng thời) — nếu race condition đó xảy ra, insert thứ 2 vẫn sẽ raise
    psycopg2.errors.UniqueViolation, router cần bắt CẢ 2 loại lỗi (xem
    api/routers/crawl.py)."""


def create_run(conn, *, source: str, category: str, pages: int,
                max_jobs: Optional[int], triggered_by: Optional[str]) -> str:
    """Tạo 1 dòng crawl_runs mới, status='queued', trả về run_id (str).

    Tự commit ngay (xem docstring module) — gọi TRƯỚC KHI add background
    task, để chắc chắn dòng đã nằm trong DB trước khi execute() (chạy
    nền, có thể bắt đầu gần như ngay lập tức) cố tìm lại nó."""
    with conn.cursor() as cur:
        # SELECT ... FOR UPDATE không cần thiết ở đây: UNIQUE INDEX có
        # điều kiện ở DB đã là lớp chặn CUỐI đảm bảo tính đúng đắn dù có
        # race condition — SELECT thường (không lock) chỉ để trả lỗi
        # SỚM, THÂN THIỆN hơn cho trường hợp thông thường (không phải
        # race condition), không phải cơ chế chặn duy nhất.
        cur.execute(
            "SELECT run_id FROM crawl_runs "
            "WHERE source = %s AND status IN ('queued', 'running') "
            "LIMIT 1",
            (source,),
        )
        existing = cur.fetchone()
        if existing is not None:
            raise ActiveCrawlExistsError(
                f"Nguồn '{source}' đang có 1 lượt crawl chưa xong "
                f"(run_id={existing[0]}) — đợi lượt đó chạy xong trước."
            )

        cur.execute(
            """
            INSERT INTO crawl_runs (source, category, pages, max_jobs, triggered_by)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING run_id
            """,
            (source, category, pages, max_jobs, triggered_by),
        )
        run_id = str(cur.fetchone()[0])
    conn.commit()
    return run_id


def mark_running(conn, run_id: str) -> None:
    """Đổi status 'queued' -> 'running' — gọi ngay khi execute() bắt đầu
    chạy pipeline thật (TRƯỚC khi gọi run_pipeline(), có thể mất vài
    phút), để GET /crawl/{run_id} poll thấy đúng trạng thái thay vì kẹt
    ở 'queued' suốt lúc đang crawl."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE crawl_runs SET status = 'running' WHERE run_id = %s",
            (run_id,),
        )
    conn.commit()


def mark_done(conn, run_id: str, stats: dict) -> None:
    """Đổi status -> 'done', điền stats + finished_at — gọi khi
    run_pipeline() trả về thành công."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE crawl_runs
            SET status = 'done', stats = %s, finished_at = %s
            WHERE run_id = %s
            """,
            (
                json.dumps(stats, ensure_ascii=False, default=str),
                datetime.now(timezone.utc),
                run_id,
            ),
        )
    conn.commit()


def mark_error(conn, run_id: str, error: str) -> None:
    """Đổi status -> 'error', điền error + finished_at — gọi khi
    run_pipeline() raise exception, hoặc source không có adapter đăng ký
    (lỗi xảy ra TRƯỚC khi kịp mark_running(), vẫn hợp lệ đi thẳng từ
    'queued' -> 'error')."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE crawl_runs
            SET status = 'error', error = %s, finished_at = %s
            WHERE run_id = %s
            """,
            (error, datetime.now(timezone.utc), run_id),
        )
    conn.commit()


_CRAWL_RUN_SELECT_COLUMNS = """
        cr.run_id, cr.source, cr.category, cr.pages, cr.max_jobs,
        cr.status, cr.stats, cr.error, cr.triggered_by,
        u.full_name AS triggered_by_name,
        cr.started_at, cr.finished_at
"""

_CRAWL_RUN_FROM_JOINS = """
    FROM crawl_runs cr
    LEFT JOIN app_users u ON u.ss_user_id = cr.triggered_by
"""


def get_run(conn, run_id: str) -> Optional[dict]:
    """Trả 1 dict crawl_runs đầy đủ hoặc None — dùng cho GET
    /crawl/{run_id} (poll tiến độ)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_CRAWL_RUN_SELECT_COLUMNS} {_CRAWL_RUN_FROM_JOINS} "
            f"WHERE cr.run_id = %s",
            (run_id,),
        )
        return cur.fetchone()


def list_runs(conn, *, source: Optional[str] = None,
              status: Optional[str] = None,
              triggered_by: Optional[str] = None,
              limit: int = 50, offset: int = 0):
    """Trả (list[dict], total) — dùng cho GET /crawl (trang "Lịch sử
    crawl"), sắp mới nhất trước. Cùng shape (total/limit/offset/items)
    với list_audit_logs() để frontend dùng chung 1 kiểu phân trang."""
    conditions = []
    params: list = []

    if source:
        conditions.append("cr.source = %s")
        params.append(source)
    if status:
        conditions.append("cr.status = %s")
        params.append(status)
    if triggered_by:
        conditions.append("cr.triggered_by = %s")
        params.append(triggered_by)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT count(*) AS total {_CRAWL_RUN_FROM_JOINS} {where_clause}",
            params,
        )
        total = cur.fetchone()["total"]

        cur.execute(
            f"SELECT {_CRAWL_RUN_SELECT_COLUMNS} {_CRAWL_RUN_FROM_JOINS} {where_clause} "
            f"ORDER BY cr.started_at DESC LIMIT %s OFFSET %s",
            params + [limit, offset],
        )
        rows = cur.fetchall()

    return rows, total


def has_active_run(conn, source: str) -> bool:
    """True nếu source này đang có 1 lượt 'queued'/'running' — dùng ở
    router (GET /crawl/active hoặc validate trước khi hiện nút) nếu
    frontend cần hỏi TRƯỚC khi thử POST /crawl, thay vì đợi 409 trả
    về rồi mới biết. Không dùng nội bộ trong create_run() (hàm đó tự
    SELECT riêng, xem lý do trong docstring create_run)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM crawl_runs "
            "WHERE source = %s AND status IN ('queued', 'running') LIMIT 1",
            (source,),
        )
        return cur.fetchone() is not None
