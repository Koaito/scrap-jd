"""
db.connection — tách từ db.py (God module) theo domain.
"""

import logging
import os
import uuid as uuid_module
from typing import Optional

import psycopg2
import psycopg2.pool
from config import DB_CONFIG, DB_POOL_MAX, DB_POOL_MIN

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
    thay vì để AttributeError mù mờ (None.getconn())."""
    if _pool is None:
        raise RuntimeError(
            "Connection pool chưa được khởi tạo — init_pool() phải chạy "
            "trong FastAPI startup event trước khi có request nào chạm "
            "get_db(). Kiểm tra lại api/app.py."
        )
    conn = _pool.getconn()
    conn.autocommit = False
    return conn


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
# Migration tracking (thêm 08/2026)
# ---------------------------------------------------------------------------
# Trước đây sql/ có 29 file migration_*.sql rời rạc, KHÔNG có cơ chế nào
# ghi lại DB nào (dev/staging/prod) đã chạy file nào — schema.sql là bản
# "snapshot" phải tự tay cập nhật khớp sau mỗi migration, dễ lệch giữa
# các môi trường hoặc quên chạy 1 file khi deploy thủ công qua Render.
#
# apply_migrations() dưới đây KHÔNG thay thế schema.sql (vẫn giữ nguyên
# vai trò: setup DB MỚI TỪ ĐẦU qua `python main.py init-db`, xem
# apply_schema() ở trên) — mà giải quyết bài toán khác: DB ĐÃ CÓ SẴN dữ
# liệu (dev lâu năm/staging/prod), cần biết CHÍNH XÁC migration nào đã
# chạy, migration nào chưa, để deploy an toàn.
#
# An toàn để chạy `apply_migrations()` bất kỳ lúc nào, kể cả trên DB đã
# chạy tay 1 số migration trước đó KHÔNG qua cơ chế này: mọi file
# migration_*.sql trong repo đều viết idempotent (ADD COLUMN IF NOT
# EXISTS, CREATE ... IF NOT EXISTS, ON CONFLICT DO NOTHING...) — đã rà
# soát mẫu nhiều file xác nhận quy ước này. Vì vậy chạy lại 1 migration
# ĐÃ áp dụng trước đó (nhưng chưa từng được ghi log) vẫn AN TOÀN (no-op),
# chỉ hơi tốn công quét lại — apply_migrations() gọi 1 lần lúc adopt
# tính năng này sẽ tự "bắt kịp" (catch up) đúng trạng thái thật của DB
# đó, không cần thao tác thủ công nào thêm.
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
