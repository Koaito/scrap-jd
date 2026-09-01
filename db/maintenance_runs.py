"""
db.maintenance_runs — lưu bền trạng thái + lịch sử từng lượt chạy 1
trong 5 job bảo trì dữ liệu (backfill_company_profiles.py,
enrich_company_profile_from_website.py, enrich_company_web_info.py,
get_company_fb_linkedin_link.py, check_expired_source_jobs.py) vào bảng
maintenance_runs (08/2026, xem sql/migration_add_maintenance_runs.sql).

ĐỐI XỨNG db/crawl_runs.py — cùng pattern tự commit() ngay trong từng
hàm (không có 1 request/response bao quanh để router commit hộ, vì
api/maintenance_runner.py::execute() chạy NỀN qua BackgroundTasks).
KHÁC crawl_runs.py ở chỗ khoá theo job_type (không phải source) và dùng
params/stats JSONB thay cho cột riêng — xem docstring migration để biết
lý do.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Optional

import psycopg2.extras

logger = logging.getLogger(__name__)


class ActiveMaintenanceRunExistsError(Exception):
    """job_type này đang có 1 lượt 'queued'/'running' chưa xong — raise
    ở create_run() TRƯỚC KHI insert, để router trả 409 rõ ràng, đối
    xứng ActiveCrawlExistsError ở db/crawl_runs.py.

    LỚP CHẶN CHÍNH; UNIQUE INDEX ở DB là LỚP CHẶN THỨ 2 (phòng race
    condition 2 request cùng job_type gần như đồng thời) — router cần
    bắt CẢ 2 loại lỗi (xem api/routers/maintenance.py)."""


def create_run(conn, *, job_type: str, params: dict,
                triggered_by: Optional[str]) -> str:
    """Tạo 1 dòng maintenance_runs mới, status='queued', trả về run_id
    (str). Tự commit ngay — gọi TRƯỚC KHI add background task, đối xứng
    db.crawl_runs.create_run()."""
    with conn.cursor() as cur:
        # SELECT thường (không lock) chỉ để trả lỗi SỚM, thân thiện hơn
        # cho trường hợp thông thường — UNIQUE INDEX ở DB đã là lớp
        # chặn cuối đảm bảo tính đúng đắn dù có race condition (cùng lý
        # do db.crawl_runs.create_run()).
        cur.execute(
            "SELECT run_id FROM maintenance_runs "
            "WHERE job_type = %s AND status IN ('queued', 'running') "
            "LIMIT 1",
            (job_type,),
        )
        existing = cur.fetchone()
        if existing is not None:
            raise ActiveMaintenanceRunExistsError(
                f"Việc '{job_type}' đang có 1 lượt chạy chưa xong "
                f"(run_id={existing[0]}) — đợi lượt đó chạy xong trước."
            )

        cur.execute(
            """
            INSERT INTO maintenance_runs (job_type, params, triggered_by)
            VALUES (%s, %s, %s)
            RETURNING run_id
            """,
            (job_type, json.dumps(params, ensure_ascii=False), triggered_by),
        )
        run_id = str(cur.fetchone()[0])
    conn.commit()
    return run_id


def append_log(conn, run_id: str, level: str, message: str) -> None:
    """Thêm 1 dòng log live cho run_id — gọi từ logging.Handler gắn tạm
    thời trong execute() (xem api/maintenance_runner.py::_RunLogHandler),
    đối xứng db.crawl_runs.append_log()."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO maintenance_run_logs (run_id, level, message) VALUES (%s, %s, %s)",
            (run_id, level, message),
        )
    conn.commit()


def get_logs(conn, run_id: str, after_id: int = 0, limit: int = 500):
    """Trả list[dict] các dòng log có id > after_id, sắp CŨ -> MỚI —
    dùng cho GET /maintenance/{run_id}/logs?after_id=N, đối xứng
    db.crawl_runs.get_logs()."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, level, message, created_at FROM maintenance_run_logs "
            "WHERE run_id = %s AND id > %s ORDER BY id ASC LIMIT %s",
            (run_id, after_id, limit),
        )
        return cur.fetchall()


def get_logs_batch(conn, run_after_ids: dict, limit: int = 500) -> dict:
    """Trả {run_id: list[dict]} — log MỚI (id > after_id riêng của TỪNG
    run_id) cho NHIỀU run_id trong 1 lần gọi, gộp bằng 1 query duy nhất
    (UNION ALL qua VALUES) thay vì N query riêng lẻ — dùng cho GET
    /maintenance/logs-batch (09/2026, xem lịch sử trao đổi "gộp 5
    request logs.json thành 1"), nơi 5 khung "Log live" (mỗi job_type
    tab Bảo trì) trước đây gọi get_logs() RIÊNG mỗi khung, cùng chu kỳ
    2s -> 5 round-trip HTTP/DB liên tục dù server chỉ cần trả lời 1
    request.

    `run_after_ids`: dict {run_id: after_id} — mỗi run_id có after_id
    RIÊNG (không dùng chung 1 giá trị) vì mỗi khung log tự poll độc lập
    theo tiến độ đọc của chính nó (data-log-after-id riêng ở
    _maintenance_tab.html), không đồng bộ giữa các job_type.

    Không dùng `WHERE run_id = ANY(%s) AND id > %s` (1 after_id chung)
    vì sẽ SAI khi 2 job_type có after_id khác nhau — union theo từng
    cặp (run_id, after_id) riêng lẻ mới đúng ngữ nghĩa "log mới của
    RUN NÀY tính từ chỗ RUN NÀY đã đọc tới"."""
    if not run_after_ids:
        return {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # VALUES (%s, %s), (%s, %s), ... — mỗi cặp (run_id, after_id)
        # 1 dòng, LATERAL join để limit riêng theo TỪNG run_id (không
        # để 1 run_id log nhiều đè hết limit chung của round-trip).
        values_sql = ", ".join(["(%s, %s::bigint)"] * len(run_after_ids))
        params: list = []
        for rid, after_id in run_after_ids.items():
            params.extend([rid, after_id])
        params.append(limit)

        cur.execute(
            f"""
            SELECT v.run_id, l.id, l.level, l.message, l.created_at
            FROM (VALUES {values_sql}) AS v(run_id, after_id)
            JOIN LATERAL (
                SELECT id, level, message, created_at
                FROM maintenance_run_logs
                WHERE run_id = v.run_id AND id > v.after_id
                ORDER BY id ASC
                LIMIT %s
            ) l ON true
            ORDER BY v.run_id, l.id ASC
            """,
            params,
        )
        rows = cur.fetchall()

    result: dict = {rid: [] for rid in run_after_ids}
    for row in rows:
        result[row["run_id"]].append(
            {"id": row["id"], "level": row["level"], "message": row["message"], "created_at": row["created_at"]}
        )
    return result


def mark_running(conn, run_id: str) -> None:
    """Đổi status 'queued' -> 'running' — gọi ngay khi execute() bắt
    đầu gọi hàm run() thật."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE maintenance_runs SET status = 'running' WHERE run_id = %s",
            (run_id,),
        )
    conn.commit()


def mark_done(conn, run_id: str, stats: dict) -> None:
    """Đổi status -> 'done', điền stats + finished_at — gọi khi run()
    trả về thành công."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE maintenance_runs
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
    """Đổi status -> 'error', điền error + finished_at — gọi khi run()
    raise exception."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE maintenance_runs
            SET status = 'error', error = %s, finished_at = %s
            WHERE run_id = %s
            """,
            (error, datetime.now(timezone.utc), run_id),
        )
    conn.commit()


_MAINTENANCE_RUN_SELECT_COLUMNS = """
        mr.run_id, mr.job_type, mr.params, mr.status, mr.stats, mr.error,
        mr.triggered_by, u.full_name AS triggered_by_name,
        mr.started_at, mr.finished_at
"""

_MAINTENANCE_RUN_FROM_JOINS = """
    FROM maintenance_runs mr
    LEFT JOIN app_users u ON u.ss_user_id = mr.triggered_by
"""


def get_run(conn, run_id: str) -> Optional[dict]:
    """Trả 1 dict maintenance_runs đầy đủ hoặc None — dùng cho GET
    /maintenance/{run_id} (poll tiến độ)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_MAINTENANCE_RUN_SELECT_COLUMNS} {_MAINTENANCE_RUN_FROM_JOINS} "
            f"WHERE mr.run_id = %s",
            (run_id,),
        )
        return cur.fetchone()


def list_runs(conn, *, job_type: Optional[str] = None,
              status: Optional[str] = None,
              triggered_by: Optional[str] = None,
              limit: int = 50, offset: int = 0):
    """Trả (list[dict], total) — dùng cho GET /maintenance (trang lịch
    sử), sắp mới nhất trước — đối xứng db.crawl_runs.list_runs()."""
    conditions = []
    params: list = []

    if job_type:
        conditions.append("mr.job_type = %s")
        params.append(job_type)
    if status:
        conditions.append("mr.status = %s")
        params.append(status)
    if triggered_by:
        conditions.append("mr.triggered_by = %s")
        params.append(triggered_by)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT count(*) AS total {_MAINTENANCE_RUN_FROM_JOINS} {where_clause}",
            params,
        )
        total = cur.fetchone()["total"]

        cur.execute(
            f"SELECT {_MAINTENANCE_RUN_SELECT_COLUMNS} {_MAINTENANCE_RUN_FROM_JOINS} {where_clause} "
            f"ORDER BY mr.started_at DESC LIMIT %s OFFSET %s",
            params + [limit, offset],
        )
        rows = cur.fetchall()

    return rows, total


def get_latest_run_per_job_type(conn) -> dict:
    """Trả {job_type: dict|None} — 1 dict maintenance_runs GẦN NHẤT
    theo started_at cho MỖI job_type (bất kể status), hoặc None nếu
    job_type đó chưa từng chạy lần nào. Dùng cho khung "Log live" ở
    trang web (mỗi card job_type tự có khung log riêng), đối xứng ý
    tưởng db.crawl_runs.get_latest_run() nhưng trả đủ 5 job_type 1 lần
    thay vì chỉ 1 dòng — tránh 5 lần gọi API riêng lúc load trang."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (mr.job_type)
                {_MAINTENANCE_RUN_SELECT_COLUMNS}
            {_MAINTENANCE_RUN_FROM_JOINS}
            ORDER BY mr.job_type, mr.started_at DESC
            """
        )
        rows = cur.fetchall()
    return {row["job_type"]: row for row in rows}


def has_active_run(conn, job_type: str) -> bool:
    """True nếu job_type này đang có 1 lượt 'queued'/'running' — đối
    xứng db.crawl_runs.has_active_run()."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM maintenance_runs "
            "WHERE job_type = %s AND status IN ('queued', 'running') LIMIT 1",
            (job_type,),
        )
        return cur.fetchone() is not None


def reconcile_orphaned_runs(conn) -> int:
    """Đánh dấu 'error' MỌI dòng đang 'queued'/'running' — gọi ĐÚNG 1
    LẦN lúc app khởi động (api/app.py::lifespan, TRƯỚC khi nhận request
    nào), đối xứng db.crawl_runs.reconcile_orphaned_runs() — cùng lý do
    an toàn (1 process, BackgroundTasks không sống sót qua restart)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE maintenance_runs
            SET status = 'error',
                error = 'Server khởi động lại giữa chừng lượt chạy này (process cũ đã dừng, tự động đánh dấu lỗi để giải phóng job_type).',
                finished_at = now()
            WHERE status IN ('queued', 'running')
            RETURNING run_id
            """
        )
        count = cur.rowcount
    conn.commit()
    return count


def reconcile_stale_runs(conn, timeout_minutes: int) -> int:
    """Đánh dấu 'error' các dòng 'queued'/'running' đã quá
    `timeout_minutes` kể từ started_at mà chưa đổi trạng thái — gọi
    ĐỊNH KỲ qua APScheduler (api/services/maintenance_watchdog.py), đối
    xứng db.crawl_runs.reconcile_stale_runs()."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE maintenance_runs
            SET status = 'error',
                error = %s,
                finished_at = now()
            WHERE status IN ('queued', 'running')
              AND started_at < now() - (%s || ' minutes')::interval
            RETURNING run_id
            """,
            (
                f"Lượt chạy treo quá {timeout_minutes} phút không cập nhật "
                f"trạng thái, tự động đánh dấu lỗi để giải phóng job_type — có "
                f"thể do process bị treo/kill giữa chừng.",
                timeout_minutes,
            ),
        )
        count = cur.rowcount
    conn.commit()
    return count
