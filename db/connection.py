"""
db.connection — tách từ db.py (God module) theo domain.
"""

import logging
import os
import time
import uuid as uuid_module
from typing import Optional

import psycopg2
import psycopg2.pool
from config import DB_CONFIG, DB_POOL_MAX, DB_POOL_MIN, DB_POOL_WAIT_TIMEOUT

logger = logging.getLogger(__name__)


def is_valid_uuid(value: Optional[str]) -> bool:
    """Kiểm tra `value` có đúng định dạng UUID không, TRƯỚC khi đưa vào
    query Postgres. BUG ĐÃ VÁ (08/2026, phát hiện qua test thật): nếu
    truyền thẳng 1 chuỗi sai định dạng UUID (vd người dùng quên thay thế
    placeholder mẫu như "<company_id_vừa_tạo_ở_bước_1>" bằng ID thật) vào
    cột kiểu UUID, psycopg2 raise lỗi KHÔNG được bắt (InvalidTextRepresentation)
    -> vọt thành 500 Internal Server Error mù mờ, không rõ nguyên nhân cho
    người gọi API. Validate trước bằng hàm này để trả 400 rõ ràng thay
    vì để Postgres tự raise lỗi giữa chừng request."""
    if not value:
        return False
    try:
        uuid_module.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def get_connection():
    """Mở 1 connection Postgres MỚI, ĐỘC LẬP với pool bên dưới — dùng cho
    CLI/script chạy 1 lần rồi thoát (main.py, enrich_company_web_info.py,
    get_company_fb_linkedin_link.py, api/crawl_runner.py chạy nền). Các
    nơi này mở/đóng đúng 1 lần mỗi lần chạy, tần suất thấp -> không cần
    pool, và code gọi conn.close() trực tiếp (không phải
    release_connection()) nên KHÔNG được đổi hàm này sang lấy từ pool
    (nếu đổi, conn.close() ở những nơi đó sẽ đóng vật lý connection mà
    không trả "chỗ" lại cho pool, làm pool rò rỉ dần tới khi hết
    maxconn).

    Muốn dùng pool (traffic lặp lại nhiều lần/giây, như API layer) ->
    dùng get_pooled_connection() + release_connection() bên dưới."""
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn


_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None


def init_pool(minconn: int = DB_POOL_MIN, maxconn: int = DB_POOL_MAX) -> None:
    """Khởi tạo pool 1 LẦN — gọi trong FastAPI startup event
    (api/app.py). Gọi lại khi pool đã tồn tại là no-op (an toàn nếu lỡ
    gọi 2 lần, vd test hoặc reload).

    minconn/maxconn: đọc từ config.py (DB_POOL_MIN/DB_POOL_MAX, đọc từ
    env DB_POOL_MIN/DB_POOL_MAX) — CÂN NHẮC set maxconn thấp hơn giới
    hạn connection Postgres phía Render/Supabase cho phép (managed
    Postgres tier free thường giới hạn thấp, vd 20-60 connection), để
    tránh pool "xin" nhiều hơn DB cho phép -> lỗi connect khi pool cố
    mở connection thứ maxconn."""
    global _pool
    if _pool is not None:
        logger.warning("init_pool() gọi lại khi pool đã tồn tại — bỏ qua.")
        return
    _pool = psycopg2.pool.ThreadedConnectionPool(minconn, maxconn, **DB_CONFIG)
    logger.info("Đã khởi tạo connection pool (minconn=%s, maxconn=%s).", minconn, maxconn)


def get_pooled_connection():
    """Mượn 1 connection từ pool — dùng trong api/deps.py:get_db().
    PHẢI trả lại bằng release_connection() (KHÔNG gọi conn.close()
    trực tiếp, xem lý do trong docstring get_connection() ở trên).

    Raise lỗi rõ ràng nếu gọi trước khi init_pool() chạy (lỗi cấu hình
    ở api/app.py, không nên xảy ra khi chạy qua uvicorn bình thường)
    thay vì để AttributeError mù mờ (None.getconn()).

    08/2026 (xem lịch sử trao đổi "connection pool exhausted" sau khi
    nâng DB_POOL_MAX) — BOUNDED-WAIT thay vì raise PoolError NGAY LẬP
    TỨC khi pool hết slot: polling nhiều tab/nhiều job_type card cùng
    lúc tạo BURST xin connection trong tích tắc (mili-giây), trong khi
    mỗi query mượn connection thường chỉ giữ vài chục ms rồi trả ngay
    (get_db() release() trong finally) — retry vài lần trong khoảng
    DB_POOL_WAIT_TIMEOUT giây thường đủ để "hứng" được slot vừa trả ra,
    thay vì 500 ngay cho request TỚI CHẬM HƠN 1 nhịp. Không dùng
    threading.Semaphore/Condition đồng bộ chính xác ở đây vì
    ThreadedConnectionPool không tự expose event "vừa có connection
    trả về" để chờ đúng nghĩa — poll bằng vòng lặp + sleep ngắn là cách
    đơn giản, đủ dùng cho quy mô hiện tại (không tốn CPU đáng kể vì
    tổng thời gian chờ tối đa vài giây, không phải vòng lặp bận rộn dài
    hạn).

    Vẫn raise psycopg2.pool.PoolError như cũ nếu HẾT hẳn
    DB_POOL_WAIT_TIMEOUT giây mà không có slot nào trống — nghĩa là
    NGHẼN THẬT (không phải burst thoáng qua), lúc đó 500 vẫn là phản hồi
    đúng (che giấu bằng cách chờ lâu hơn sẽ chỉ làm request TREO lâu vô
    ích thay vì báo lỗi rõ ràng cho FE retry)."""
    if _pool is None:
        raise RuntimeError(
            "Connection pool chưa được khởi tạo — init_pool() phải chạy "
            "trong FastAPI startup event trước khi có request nào chạm "
            "get_db(). Kiểm tra lại api/app.py."
        )
    deadline = time.monotonic() + DB_POOL_WAIT_TIMEOUT
    _retry_delay = 0.05  # 50ms — đủ ngắn để bắt kịp query trung bình vài
    # chục ms, KHÔNG quá ngắn tới mức vòng lặp retry chiếm CPU đáng kể.
    while True:
        try:
            conn = _pool.getconn()
            conn.autocommit = False
            return conn
        except psycopg2.pool.PoolError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_retry_delay)
            # Tăng dần delay (tối đa 300ms) — burst thật sự nặng (nghẽn
            # kéo dài, không phải chỉ 1 nhịp thoáng qua) không nên retry
            # dồn dập liên tục làm nặng thêm việc pool đang cạn.
            _retry_delay = min(_retry_delay * 1.5, 0.3)


def release_connection(conn) -> None:
    """Trả connection về pool — dùng thay cho conn.close() trong
    api/deps.py:get_db(). An toàn gọi cả khi pool chưa init (no-op),
    tránh lỗi kép nếu request lỗi ngay từ get_pooled_connection()."""
    if _pool is None:
        return
    _pool.putconn(conn)


def close_pool() -> None:
    """Đóng TOÀN BỘ connection trong pool — gọi trong FastAPI shutdown
    event (api/app.py), tránh connection bị bỏ "treo" (leak) phía
    Postgres khi Render restart/deploy lại server."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
        logger.info("Đã đóng connection pool.")


def apply_schema(conn, schema_path: str = "sql/schema.sql"):
    """Chạy file schema.sql (idempotent — có thể chạy lại nhiều lần an toàn)."""
    with open(schema_path, "r", encoding="utf-8") as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    logger.info("Đã áp dụng schema từ %s", schema_path)


# ---------------------------------------------------------------------------
# Migration tracking
# ---------------------------------------------------------------------------
# Hai cơ chế đi cùng nhau, không thay thế nhau:
#
# - sql/schema.sql là schema ĐẦY ĐỦ, mới nhất. `init-db` chạy file này để dựng
#   DB mới, rồi ghi nhận mọi migration_*.sql hiện có vào schema_migrations
#   (baseline_migrations) vì schema.sql đã chứa sẵn kết quả của chúng.
# - migration_*.sql + bảng schema_migrations dùng để nâng cấp DB đã có dữ liệu.
#   apply_migrations() chạy các file chưa có trong schema_migrations, mỗi file
#   một transaction.
#
# DB đã dựng từ trước khi có bảng schema_migrations (chưa biết file nào đã
# chạy) thì dùng baseline_migrations() một lần để ghi nhận trạng thái hiện tại,
# KHÔNG chạy lại SQL. Lý do: các migration cũ chạy theo thứ tự tên file, mà
# thứ tự đó không khớp thứ tự phụ thuộc thật (ví dụ crawl_batches cần kiểu
# enum do crawl_runs tạo; vài file còn viết theo tên bảng ss_team_members đã
# đổi thành app_users), nên chạy lại cả chuỗi trên DB đã ở trạng thái mới sẽ
# lỗi. Migration viết MỚI phải idempotent (IF NOT EXISTS...) và chỉ phụ thuộc
# trạng thái schema hiện tại.
_MIGRATIONS_DIR = "sql"


def _ensure_schema_migrations_table(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    conn.commit()


def _list_migration_files(migrations_dir: str = _MIGRATIONS_DIR) -> list:
    """Tên file migration_*.sql trong `migrations_dir`, SẮP XẾP THEO TÊN
    (không phải theo thời gian tạo file) — quy ước đặt tên hiện tại
    (migration_add_xxx.sql, migration_rename_xxx.sql...) không tự mang
    thứ tự thời gian, nhưng mọi migration đều độc lập/idempotent (xem
    docstring apply_migrations()) nên thứ tự chạy KHÔNG ảnh hưởng kết
    quả cuối — sort theo tên chỉ để có 1 thứ tự CỐ ĐỊNH, lặp lại được
    giữa các lần chạy, không phải để đảm bảo tính đúng đắn."""
    return sorted(
        f for f in os.listdir(migrations_dir)
        if f.startswith("migration_") and f.endswith(".sql")
    )


def list_pending_migrations(conn, migrations_dir: str = _MIGRATIONS_DIR) -> list:
    """Tên các file migration_*.sql CHƯA có trong schema_migrations của
    DB đang kết nối — dùng để kiểm tra TRƯỚC khi deploy (vd hiện cảnh
    báo/chặn nếu còn migration chưa chạy) mà không cần thật sự chạy gì."""
    _ensure_schema_migrations_table(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT filename FROM schema_migrations")
        applied = {row[0] for row in cur.fetchall()}
    return [f for f in _list_migration_files(migrations_dir) if f not in applied]


def apply_migrations(conn, migrations_dir: str = _MIGRATIONS_DIR) -> list:
    """Chạy MỌI migration_*.sql chưa được ghi log áp dụng cho DB đang kết
    nối, mỗi file trong 1 transaction riêng (lỗi ở file nào dừng lại ở
    đó — KHÔNG rollback các file trước đã chạy + ghi log thành công,
    KHÔNG chạy tiếp các file sau) rồi ghi vào bảng schema_migrations.
    Trả về danh sách filename VỪA áp dụng thành công (rỗng nếu DB đã
    theo kịp, không có gì để chạy)."""
    pending = list_pending_migrations(conn, migrations_dir)
    newly_applied = []
    for filename in pending:
        path = os.path.join(migrations_dir, filename)
        with open(path, "r", encoding="utf-8") as f:
            sql = f.read()
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s)",
                (filename,),
            )
        conn.commit()
        newly_applied.append(filename)
        logger.info("Đã áp dụng migration: %s", filename)
    return newly_applied


def baseline_migrations(conn, migrations_dir: str = _MIGRATIONS_DIR,
                        except_files=()) -> list:
    """Ghi vào schema_migrations mọi migration CHƯA có log, KHÔNG chạy SQL
    của chúng. Dùng khi DB đã ở trạng thái mới nhất nhưng bảng
    schema_migrations còn thiếu/trống (xem khối chú thích "Migration
    tracking" phía trên).

    `except_files`: các migration để nguyên ở trạng thái chưa áp dụng, để lần
    `apply_migrations()` sau chạy thật (ví dụ file tạo một bảng DB chưa có).

    Trả về danh sách filename vừa được ghi nhận."""
    skip = set(except_files)
    marked = [
        f for f in list_pending_migrations(conn, migrations_dir) if f not in skip
    ]
    with conn.cursor() as cur:
        for filename in marked:
            cur.execute(
                "INSERT INTO schema_migrations (filename) VALUES (%s) "
                "ON CONFLICT DO NOTHING",
                (filename,),
            )
    conn.commit()
    return marked
