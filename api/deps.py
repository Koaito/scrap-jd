"""
Dependency injection dùng chung cho các router.

get_db(): MƯỢN 1 connection từ connection pool (db.get_pooled_connection(),
xem db.py mục "CONNECTION POOL") cho mỗi request, TRẢ LẠI pool khi
request xong (kể cả khi lỗi, nhờ try/finally) — đổi từ mở/đóng connection
thật mỗi request (08/2026, xem lịch sử trao đổi) sang mượn/trả connection
đã mở sẵn, giảm round-trip TCP/TLS khi nhiều người dùng dashboard cùng
lúc. Pool được khởi tạo 1 lần lúc app khởi động (api/app.py, startup
event gọi db.init_pool()) — get_db() chỉ mượn/trả, không tự khởi tạo.

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
Không cần đổi sang asyncpg/psycopg3-async ở giai đoạn này. Vì nhiều
thread có thể mượn/trả connection đồng thời, pool dùng
ThreadedConnectionPool (không phải SimpleConnectionPool) — xem db.py.
"""

from typing import Iterator

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

import db as db_module
from api import security


def get_db() -> Iterator:
    conn = db_module.get_pooled_connection()
    try:
        yield conn
    finally:
        db_module.release_connection(conn)


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
