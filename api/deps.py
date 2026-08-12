"""
Dependency injection dùng chung cho các router.

get_db(): mở 1 connection Postgres MỚI cho mỗi request, đóng lại khi
request xong (kể cả khi lỗi, nhờ try/finally) — đơn giản, đúng với quy
mô hiện tại (dashboard nội bộ team, không phải traffic công khai lớn).

Route handler khai báo bằng `def` (KHÔNG phải `async def`) — FastAPI tự
chạy các route `def` thường trong threadpool riêng, nên psycopg2 (thư
viện đồng bộ/blocking) vẫn chạy an toàn, không chặn event loop chính.
Không cần đổi sang asyncpg/psycopg3-async ở giai đoạn này.

NÂNG CẤP SAU (chỉ làm khi thật sự cần, đừng làm sớm):
  - Traffic cao -> đổi sang connection pool (psycopg2.pool hoặc
    SQLAlchemy engine với pool_size) thay vì mở/đóng connection mỗi
    request.
"""

from typing import Iterator

import db as db_module


def get_db() -> Iterator:
    conn = db_module.get_connection()
    try:
        yield conn
    finally:
        conn.close()
