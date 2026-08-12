"""
Dependency injection dùng chung cho các router.

get_db(): mở 1 connection Postgres MỚI cho mỗi request, đóng lại khi
request xong (kể cả khi lỗi, nhờ try/finally) — đơn giản, đúng với quy
mô hiện tại (dashboard nội bộ team, không phải traffic công khai lớn).

get_current_user()/require_admin() (thêm 08/2026): lớp đăng nhập TỪNG
NGƯỜI qua JWT — KHÁC api/auth.py (API_KEY tĩnh, đã đăng ký cấp app,
chặn TRƯỚC khi request chạm tới đây). 2 lớp xếp CHỒNG lên nhau:
  1. API_KEY (api/auth.py, dependencies=[] cấp app trong app.py) — xác
     nhận "client này là frontend của chúng ta", áp dụng MỌI request.
  2. JWT (get_current_user() dưới đây) — xác nhận "user THẬT nào đang
     gọi", chỉ áp dụng cho route nào khai báo Depends(get_current_user)
     hoặc Depends(require_admin) rõ ràng (không đăng ký cấp app, vì
     nhiều route như GET /jobs vẫn nên dùng được chỉ với API_KEY, không
     bắt buộc đăng nhập cá nhân).

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

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import db as db_module
from api import security


def get_db() -> Iterator:
    conn = db_module.get_connection()
    try:
        yield conn
    finally:
        conn.close()


# auto_error=False -> tự kiểm tra thiếu header để trả message rõ ràng
# hơn thay vì lỗi mặc định chung chung của FastAPI.
_bearer_scheme = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_bearer_scheme),
) -> dict:
    """Verify JWT access token trong header `Authorization: Bearer
    <token>`, trả payload (dict có 'sub'=ss_user_id, 'role', 'email') —
    CHỈ đọc từ chữ ký JWT, KHÔNG query DB (đúng lợi thế JWT: verify
    nhanh). Vì không query DB, route KHÔNG tự biết tài khoản có bị
    is_active=False/xoá sau khi token đã phát hành hay không — chấp
    nhận đánh đổi này vì access token sống ngắn (30 phút, xem
    security.ACCESS_TOKEN_EXPIRE_MINUTES); cần thu hồi ngay lập tức thì
    dùng revoke_all_refresh_tokens_for_user() để chặn user lấy access
    token MỚI, chờ token hiện tại tự hết hạn."""
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Thiếu header 'Authorization: Bearer <access_token>'.",
        )
    payload = security.decode_access_token(credentials.credentials)
    if payload is None:
        raise HTTPException(
            status_code=401,
            detail="Access token không hợp lệ hoặc đã hết hạn — dùng "
                   "refresh token qua POST /auth/refresh để lấy token mới.",
        )
    return payload


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """Như get_current_user(), thêm điều kiện role='admin'. Dùng cho
    route quản trị (vd POST /auth/users tạo tài khoản mới)."""
    if user.get("role") != "admin":
        raise HTTPException(
            status_code=403,
            detail="Chỉ admin mới có quyền thực hiện thao tác này.",
        )
    return user
