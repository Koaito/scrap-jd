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


def update_progress(conn, run_id: str, progress: dict) -> None:
    """Ghi ĐÈ (không cộng dồn) snapshot tiến độ mới nhất — gọi liên tục
    (mỗi trang fetch xong 1 lần) trong lúc execute() đang chạy pipeline
    thật, xem docstring migration_add_crawl_progress_logs.sql.

    progress: dict gọn kiểu {"page": int, "fetched": int,
    "inserted": int, "last_update": iso str} — KHÔNG có shape cố định
    bắt buộc ở tầng DB (JSONB), pipeline.py là nơi quyết định đúng các
    key này, xem docstring run_pipeline() tham số on_progress.

    Tự commit ngay (cùng lý do các hàm mark_*() khác trong module này
    — execute() chạy nền, không có request/response bao quanh để commit
    hộ) nhưng dùng connection RIÊNG với connection đang chạy
    run_pipeline() sẽ KHÔNG áp dụng ở đây — cùng 1 conn, chỉ là 1
    UPDATE + commit() độc lập, không mở transaction lồng nhau."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE crawl_runs SET progress = %s WHERE run_id = %s",
            (json.dumps(progress, ensure_ascii=False, default=str), run_id),
        )
    conn.commit()


def append_log(conn, run_id: str, level: str, message: str) -> None:
    """Thêm 1 dòng log live cho run_id — gọi từ logging.Handler gắn tạm
    thời trong execute() (xem api/crawl_runner.py::_RunLogHandler), bắt
    MỌI log do pipeline.py/adapters/*.py phát ra qua logger chuẩn
    (logging.getLogger(__name__)) trong lúc lượt crawl này đang chạy —
    không cần sửa từng file logger.info() rải rác thành 2 lời gọi.

    Tự commit ngay mỗi dòng (chấp nhận nhiều round-trip DB nhỏ, đổi lấy
    log không bị mất nếu process bị kill giữa chừng — khớp tinh thần
    "ghi ngay, không gom batch" của cả module này)."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO crawl_run_logs (run_id, level, message) VALUES (%s, %s, %s)",
            (run_id, level, message),
        )
    conn.commit()


def get_logs(conn, run_id: str, after_id: int = 0, limit: int = 500):
    """Trả list[dict] các dòng log có id > after_id, sắp CŨ -> MỚI (đúng
    thứ tự đọc như terminal thật) — dùng cho GET
    /crawl/{run_id}/logs?after_id=N (poll tăng dần, xem docstring index
    idx_crawl_run_logs_run_id_id).

    limit: chặn trần 1 lần trả về (phòng client poll sau khi bỏ lỡ rất
    lâu, hoặc lượt crawl log quá nhiều dòng) — client tự gọi lại với
    after_id mới nếu còn thiếu, không cần backend trả hết 1 lần."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT id, level, message, created_at FROM crawl_run_logs "
            "WHERE run_id = %s AND id > %s ORDER BY id ASC LIMIT %s",
            (run_id, after_id, limit),
        )
        return cur.fetchall()


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
        cr.started_at, cr.finished_at, cr.progress
"""

_CRAWL_RUN_FROM_JOINS = """
    FROM crawl_runs cr
    LEFT JOIN app_users u ON u.ss_user_id = cr.triggered_by
"""


def get_latest_run(conn) -> Optional[dict]:
    """Trả 1 dict crawl_runs GẦN NHẤT theo started_at (bất kể status —
    queued/running/done/error đều tính), hoặc None nếu chưa từng crawl
    lần nào — dùng cho GET /crawl/latest-log-run (08/2026, xem lịch sử
    trao đổi "khung Log live luôn hiện cố định trên trang").

    Khác get_run()/list_runs() (đều lọc theo run_id/điều kiện cụ thể):
    hàm này KHÔNG có tham số lọc — luôn trả đúng 1 dòng mới nhất, để
    frontend luôn có 1 run_id để load log khi mở trang /crawl, kể cả
    lúc không có lượt nào đang chạy (hiện log của lượt gần nhất đã
    xong/lỗi, thay vì để khung log trống)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_CRAWL_RUN_SELECT_COLUMNS} {_CRAWL_RUN_FROM_JOINS} "
            f"ORDER BY cr.started_at DESC LIMIT 1",
        )
        return cur.fetchone()


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


def reconcile_orphaned_runs(conn) -> int:
    """Đánh dấu 'error' MỌI dòng đang 'queued'/'running' — gọi ĐÚNG 1
    LẦN lúc app khởi động (api/app.py::lifespan, TRƯỚC khi nhận request
    nào), KHÔNG gọi ở nơi khác.

    LÝ DO AN TOÀN GỌI VÔ ĐIỀU KIỆN: kiến trúc hiện tại là 1 process
    (không Celery/RQ), BackgroundTasks của FastAPI SỐNG CÙNG VÒNG ĐỜI
    process — khi process cũ dừng (deploy mới, Render sleep dậy...),
    MỌI background task đang chạy dở cũng biến mất theo, không có gì
    "tiếp tục chạy" ở process mới cả. Nên bất kỳ dòng nào còn
    'queued'/'running' tại thời điểm process MỚI khởi động chắc chắn là
    mồ côi (orphaned) từ 1 lần chạy trước đã chết dở — không tồn tại
    trường hợp dòng đó vẫn đang thực sự được xử lý bởi 1 process khác.

    Trả về số dòng đã reconcile (dùng để log, không bắt buộc xử lý)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE crawl_runs
            SET status = 'error',
                error = 'Server khởi động lại giữa chừng lượt crawl này (process cũ đã dừng, tự động đánh dấu lỗi để giải phóng nguồn).',
                finished_at = now()
            WHERE status IN ('queued', 'running')
            RETURNING run_id
            """
        )
        count = cur.rowcount
    conn.commit()
    return count


def reconcile_stale_runs(conn, timeout_minutes: int) -> int:
    """Đánh dấu 'error' các dòng 'queued'/'running' đã quá `timeout_minutes`
    kể từ started_at MÀ CHƯA đổi trạng thái — gọi ĐỊNH KỲ qua APScheduler
    (api/services/crawl_watchdog.py), KHÁC reconcile_orphaned_runs()
    (chỉ gọi 1 lần lúc khởi động).

    Bắt trường hợp reconcile_orphaned_runs() KHÔNG bắt được: process
    KHÔNG restart nhưng riêng 1 background task bị TREO (vd network
    treo vô hạn không timeout, thread bị deadlock) — process vẫn sống,
    vẫn nhận request bình thường, nên "lúc khởi động" không xảy ra để
    reconcile_orphaned_runs() có cơ hội chạy lại.

    timeout_minutes: xem CRAWL_STALE_TIMEOUT_MINUTES (config.py) — PHẢI
    đủ lớn hơn thời gian 1 lượt crawl HỢP LỆ có thể chạy (worst case
    max_jobs=1000), nếu không sẽ tự huỷ nhầm crawl vẫn đang chạy bình
    thường."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE crawl_runs
            SET status = 'error',
                error = %s,
                finished_at = now()
            WHERE status IN ('queued', 'running')
              AND started_at < now() - (%s || ' minutes')::interval
            RETURNING run_id
            """,
            (
                f"Lượt crawl treo quá {timeout_minutes} phút không cập nhật "
                f"trạng thái, tự động đánh dấu lỗi để giải phóng nguồn — có "
                f"thể do process bị treo/kill giữa chừng.",
                timeout_minutes,
            ),
        )
        count = cur.rowcount
    conn.commit()
    return count

