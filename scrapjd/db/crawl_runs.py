"""
db.crawl_runs — lưu bền trạng thái + lịch sử từng lượt crawl vào bảng
crawl_runs (08/2026, THAY THẾ _RUNS dict RAM cũ trong
scrapjd/api/crawl_runner.py — xem sql/migration_add_crawl_runs.sql để biết đầy
đủ lý do thiết kế).

Cùng pattern scrapjd/db/audit_logs.py: mỗi hàm ở đây tự lo transaction của
riêng nó (commit() ngay trong hàm, KHÁC các hàm insert_job()/log_action()
ở module khác vốn để router tự commit cùng lúc với thao tác chính) —
lý do khác biệt: scrapjd/api/crawl_runner.py::execute() chạy NỀN (background
task), không có 1 request/response bao quanh để router commit hộ, nên
mỗi lần đổi trạng thái (queued -> running -> done/error) PHẢI tự commit
ngay, để nếu process bị kill giữa chừng (deploy mới đè lên lúc đang
crawl) thì trạng thái đã ghi trước đó vẫn không bị mất/rollback.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

import psycopg2.extras
from scrapjd.db.pg_types import Conn, Row, fetch_all_rows, fetch_one_row, fetch_scalar

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
    scrapjd/api/routers/crawl.py)."""


def create_run(conn: Conn, *, source: str, category: str, pages: int,
                max_jobs: Optional[int], triggered_by: Optional[str],
                batch_id: Optional[str] = None,
                batch_position: Optional[int] = None) -> str:
    """Tạo 1 dòng crawl_runs mới, status='queued', trả về run_id (str).

    Tự commit ngay (xem docstring module) — gọi TRƯỚC KHI add background
    task, để chắc chắn dòng đã nằm trong DB trước khi execute() (chạy
    nền, có thể bắt đầu gần như ngay lập tức) cố tìm lại nó.

    batch_id/batch_position (08/2026, xem docstring
    sql/migration_add_crawl_batches.sql): CHỈ truyền khi run này là 1
    category trong 1 batch "crawl nhiều category liên tục" — mặc định
    None nghĩa là run đơn lẻ như trước giờ, KHÔNG đổi hành vi gì cho
    mọi lời gọi cũ (POST /crawl đơn lẻ) đang không truyền 2 tham số
    này."""
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
            INSERT INTO crawl_runs (source, category, pages, max_jobs, triggered_by, batch_id, batch_position)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING run_id
            """,
            (source, category, pages, max_jobs, triggered_by, batch_id, batch_position),
        )
        run_id = str(fetch_scalar(cur))
    conn.commit()
    return run_id


def update_progress(conn: Conn, run_id: str, progress: dict[str, Any]) -> None:
    """Ghi ĐÈ (không cộng dồn) snapshot tiến độ mới nhất — gọi liên tục
    (mỗi trang fetch xong 1 lần) trong lúc execute() đang chạy pipeline
    thật, xem docstring migration_add_crawl_progress_logs.sql.

    progress: dict gọn kiểu {"page": int, "fetched": int,
    "inserted": int, "last_update": iso str} — KHÔNG có shape cố định
    bắt buộc ở tầng DB (JSONB), scrapjd/pipeline.py là nơi quyết định đúng các
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


def append_log(conn: Conn, run_id: str, level: str, message: str) -> None:
    """Thêm 1 dòng log live cho run_id — gọi từ logging.Handler gắn tạm
    thời trong execute() (xem scrapjd/api/run_log.py::capture_run_logs), bắt
    MỌI log do scrapjd/pipeline.py/adapters/*.py phát ra qua logger chuẩn
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


def get_logs(conn: Conn, run_id: str, after_id: int = 0, limit: int = 500) -> list[Row]:
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
        return fetch_all_rows(cur)


def mark_running(conn: Conn, run_id: str) -> None:
    """Đổi status 'queued' -> 'running' — gọi ngay khi execute() bắt đầu
    chạy pipeline thật (TRƯỚC khi gọi run_pipeline(), có thể mất vài
    phút), để GET /crawl/{run_id} poll thấy đúng trạng thái thay vì kẹt
    ở 'queued' suốt lúc đang crawl.

    Đồng thời ghi heartbeat ĐẦU TIÊN (progress.last_update = bây giờ) trong
    CÙNG câu UPDATE (10/2026). Watchdog (reconcile_stale_runs) tính "treo"
    theo progress.last_update; nếu để trống tới lần on_progress đầu tiên,
    1 run phải xếp hàng lâu ở 'queued' (chờ GLOBAL_JOB_SEMAPHORE) rồi mới
    chạy sẽ bị so với started_at là mốc từ lúc tạo, và bị đánh dấu 'error'
    ngay lượt quét kế tiếp dù vừa mới bắt đầu chạy. Gộp vào 1 câu UPDATE để
    không có khoảng hở giữa 'running' và heartbeat đầu."""
    initial_progress = {
        "fetched": 0,
        "inserted": 0,
        "last_update": datetime.now(timezone.utc).isoformat(),
    }
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE crawl_runs SET status = 'running', progress = %s WHERE run_id = %s",
            (json.dumps(initial_progress), run_id),
        )
    conn.commit()


def mark_done(conn: Conn, run_id: str, stats: dict[str, Any]) -> None:
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


def mark_error(conn: Conn, run_id: str, error: str, stats: Optional[dict[str, Any]] = None) -> None:
    """Đổi status -> 'error', điền error + finished_at — gọi khi
    run_pipeline() raise exception, hoặc source không có adapter đăng ký
    (lỗi xảy ra TRƯỚC khi kịp mark_running(), vẫn hợp lệ đi thẳng từ
    'queued' -> 'error').

    stats (đợt 3, 10/2026): thống kê TẠM THỜI tới lúc lỗi — dùng khi bị chặn
    giữa chừng để không mất số job đã lưu, kèm "blocked": True (xem
    get_recent_blocked_run). None -> không đụng cột stats như trước."""
    with conn.cursor() as cur:
        if stats is None:
            cur.execute(
                """
                UPDATE crawl_runs
                SET status = 'error', error = %s, finished_at = %s
                WHERE run_id = %s
                """,
                (error, datetime.now(timezone.utc), run_id),
            )
        else:
            cur.execute(
                """
                UPDATE crawl_runs
                SET status = 'error', error = %s, finished_at = %s, stats = %s
                WHERE run_id = %s
                """,
                (error, datetime.now(timezone.utc),
                 json.dumps(stats, ensure_ascii=False, default=str), run_id),
            )
    conn.commit()


def get_recent_blocked_run(conn: Conn, source: str, within_minutes: int) -> Optional[dict[str, Any]]:
    """Lượt crawl GẦN NHẤT của `source` bị chặn (status='error' và
    stats.blocked = true) trong `within_minutes` phút qua, hoặc None. Dùng để
    cảnh báo trong log lúc bắt đầu lượt mới — KHÔNG dùng để chặn bấm chạy."""
    if within_minutes <= 0:
        return None
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT run_id, finished_at FROM crawl_runs
            WHERE source = %s AND status = 'error'
              AND stats->>'blocked' = 'true'
              AND finished_at > now() - make_interval(mins => %s)
            ORDER BY finished_at DESC LIMIT 1
            """,
            (source, within_minutes),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return {"run_id": str(row[0]), "finished_at": row[1]}


_CRAWL_RUN_SELECT_COLUMNS = """
        cr.run_id, cr.source, cr.category, cr.pages, cr.max_jobs,
        cr.status, cr.stats, cr.error, cr.triggered_by,
        u.full_name AS triggered_by_name,
        cr.started_at, cr.finished_at, cr.progress,
        cr.batch_id, cr.batch_position
"""

_CRAWL_RUN_FROM_JOINS = """
    FROM crawl_runs cr
    LEFT JOIN app_users u ON u.ss_user_id = cr.triggered_by
"""


def get_latest_run(conn: Conn) -> Optional[Row]:
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


def get_run(conn: Conn, run_id: str) -> Optional[Row]:
    """Trả 1 dict crawl_runs đầy đủ hoặc None — dùng cho GET
    /crawl/{run_id} (poll tiến độ)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_CRAWL_RUN_SELECT_COLUMNS} {_CRAWL_RUN_FROM_JOINS} "
            f"WHERE cr.run_id = %s",
            (run_id,),
        )
        return cur.fetchone()


def list_runs(conn: Conn, *, source: Optional[str] = None,
              status: Optional[str] = None,
              triggered_by: Optional[str] = None,
              limit: int = 50, offset: int = 0) -> tuple[list[Row], int]:
    """Trả (list[dict], total) — dùng cho GET /crawl (trang "Lịch sử
    crawl"), sắp mới nhất trước. Cùng shape (total/limit/offset/items)
    với list_audit_logs() để frontend dùng chung 1 kiểu phân trang."""
    conditions = []
    params: list[Any] = []

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
        total = fetch_one_row(cur)["total"]

        cur.execute(
            f"SELECT {_CRAWL_RUN_SELECT_COLUMNS} {_CRAWL_RUN_FROM_JOINS} {where_clause} "
            f"ORDER BY cr.started_at DESC LIMIT %s OFFSET %s",
            params + [limit, offset],
        )
        rows = fetch_all_rows(cur)

    return rows, total


def has_active_run(conn: Conn, source: str) -> bool:
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


def reconcile_orphaned_runs(conn: Conn) -> int:
    """Đánh dấu 'error' MỌI dòng đang 'queued'/'running' — gọi ĐÚNG 1
    LẦN lúc app khởi động (scrapjd/api/app.py::lifespan, TRƯỚC khi nhận request
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


# Mốc "lần cuối có tiến độ" của 1 dòng crawl_runs: progress.last_update do
# scrapjd/api/crawl_runner.py ghi sau mỗi job. Regex chặn giá trị không phải ISO
# datetime (dữ liệu lỗi/ghi tay) trả NULL thay vì để ép kiểu ::timestamptz
# raise lỗi làm hỏng cả câu UPDATE của watchdog — khi đó COALESCE rơi về
# started_at.
_LAST_PROGRESS_AT_SQL = r"""
    CASE
        WHEN progress->>'last_update' ~
             '^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$'
        THEN (progress->>'last_update')::timestamptz
    END
"""


def reconcile_stale_runs(conn: Conn, timeout_minutes: int, *,
                         no_progress_minutes: Optional[int] = None) -> int:
    """Đánh dấu 'error' các lượt crawl bị TREO — gọi ĐỊNH KỲ qua APScheduler
    (scrapjd/api/services/crawl_watchdog.py), KHÁC reconcile_orphaned_runs() (chỉ
    gọi 1 lần lúc khởi động). Trả về tổng số dòng đã đánh dấu.

    Bắt trường hợp reconcile_orphaned_runs() KHÔNG bắt được: process
    KHÔNG restart nhưng riêng 1 background task bị TREO (vd network
    treo vô hạn không timeout, thread bị deadlock) — process vẫn sống,
    vẫn nhận request bình thường, nên "lúc khởi động" không xảy ra để
    reconcile_orphaned_runs() có cơ hội chạy lại.

    2 quy tắc khác nhau theo status (đổi 10/2026 — trước đây MỌI dòng đều
    so với started_at với 1 ngưỡng cố định):

    - 'running': treo nếu KHÔNG có tiến độ mới trong `no_progress_minutes`
      kể từ progress.last_update (heartbeat sau mỗi job, xem
      scrapjd/api/crawl_runner.py), rơi về started_at nếu chưa có heartbeat nào
      (dòng cũ trước khi có cột progress). Tính theo tiến độ thay vì tổng
      thời gian chạy vì thời gian 1 lượt hợp lệ rất khó ước lượng (max_jobs
      lớn x delay 5-16s/request có thể vượt 2-3 giờ) — ngưỡng theo tổng
      thời gian hoặc huỷ nhầm lượt đang chạy đều, hoặc phải đặt quá lớn nên
      treo thật vẫn giữ khoá nguồn rất lâu.
    - 'queued': so với started_at (mốc tạo dòng) với `timeout_minutes`.
      Không có heartbeat để dựa vào, và 1 run có thể xếp hàng hợp lệ chờ
      GLOBAL_JOB_SEMAPHORE (xem scrapjd/api/concurrency.py) trong lúc job khác chạy.
      Vì vậy ngưỡng này phải LỚN (xem CRAWL_STALE_TIMEOUT_MINUTES).

    no_progress_minutes=None -> dùng timeout_minutes cho cả 2 status."""
    running_limit = timeout_minutes if no_progress_minutes is None else no_progress_minutes
    total = 0
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE crawl_runs
            SET status = 'error',
                error = %s,
                finished_at = now()
            WHERE status = 'running'
              AND COALESCE({_LAST_PROGRESS_AT_SQL}, started_at)
                  < now() - make_interval(mins => %s)
            RETURNING run_id
            """,
            (
                f"Lượt crawl không có tiến độ mới trong {running_limit} phút, "
                f"tự động đánh dấu lỗi để giải phóng nguồn — có thể do process "
                f"bị treo/kill giữa chừng.",
                running_limit,
            ),
        )
        total += cur.rowcount

        cur.execute(
            """
            UPDATE crawl_runs
            SET status = 'error',
                error = %s,
                finished_at = now()
            WHERE status = 'queued'
              AND started_at < now() - make_interval(mins => %s)
            RETURNING run_id
            """,
            (
                f"Lượt crawl nằm ở trạng thái chờ quá {timeout_minutes} phút "
                f"mà chưa bắt đầu chạy, tự động đánh dấu lỗi để giải phóng "
                f"nguồn — có thể do background task bị mất.",
                timeout_minutes,
            ),
        )
        total += cur.rowcount
    conn.commit()
    return total
