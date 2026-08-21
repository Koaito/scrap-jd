"""
Dependency injection dùng chung cho các router.

get_db(): MƯỢN 1 connection từ connection pool (db.get_pooled_connection(),
xem db.py mục "CONNECTION POOL") cho mỗi request, TRẢ LẠI pool khi
request xong (kể cả khi lỗi, nhờ try/finally) — đổi từ mở/đóng connection
thật mỗi request (08/2026, xem lịch sử trao đổi) sang mượn/trả connection
đã mở sẵn, giảm round-trip TCP/TLS khi nhiều người dùng dashboard cùng
lúc. Pool được khởi tạo 1 lần lúc app khởi động (api/app.py, startup
event gọi db.init_pool()) — get_db() chỉ mượn/trả, không tự khởi tạo.

get_current_user()/require_role()/require_admin() (require_role thêm
08/2026, xem sql/migration_add_role_hierarchy.sql): lớp đăng nhập TỪNG
NGƯỜI qua JWT — KHÁC api/auth.py (API_KEY tĩnh, đã đăng ký cấp app,
chặn TRƯỚC khi request chạm tới đây). 2 lớp xếp CHỒNG lên nhau:
  1. API_KEY (api/auth.py, dependencies=[] cấp app trong app.py) — xác
     nhận "client này là frontend của chúng ta", áp dụng MỌI request.
  2. JWT (get_current_user() dưới đây) — xác nhận "user THẬT nào đang
     gọi", chỉ áp dụng cho route nào khai báo Depends(get_current_user)
     hoặc Depends(require_role(...)) rõ ràng (không đăng ký cấp app, vì
     nhiều route như GET /jobs vẫn nên dùng được chỉ với API_KEY, không
     bắt buộc đăng nhập cá nhân).

08/2026: `detail` của 2 lỗi 401 trong get_current_user() đổi từ string
thuần sang dict có thêm `error_code` (`missing_auth_header` khi không
gửi header Authorization, `token_expired` khi có gửi nhưng token
invalid/hết hạn) — giữ nguyên `message` = text cũ. Lý do: FE nhận
status_code=401 y hệt nhau giữa lỗi API_KEY sai (api/auth.py) và lỗi
JWT ở đây, không có cách phân biệt để tự quyết định retry/redirect
đúng chỗ (API_KEY sai -> lỗi cấu hình, không tự sửa được; token hết
hạn -> gọi POST /auth/refresh; thiếu header -> yêu cầu đăng nhập lại).

3 role phân cấp (0 'user' < 1 'ss_team' < 2 'admin', xem ROLE_HIERARCHY)
thay cho 2 role cũ ('admin'/'member') — require_admin giờ chỉ là alias
của require_role("admin"), giữ để không phải sửa lại mọi chỗ đã dùng
tên cũ.

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
    conn=Depends(get_db),
) -> dict:
    """Verify JWT access token trong header `Authorization: Bearer
    <token>`, trả payload (dict có 'sub'=ss_user_id, 'role', 'email') —
    trước 08/2026 CHỈ đọc chữ ký JWT, không query DB (đúng lợi thế JWT:
    verify nhanh). Vì không query DB, route KHÔNG tự biết tài khoản có
    bị is_active=False/xoá sau khi token đã phát hành hay không — chấp
    nhận đánh đổi này vì access token sống ngắn (30 phút, xem
    security.ACCESS_TOKEN_EXPIRE_MINUTES).

    08/2026 (single-session, xem sql/migration_add_single_session.sql):
    THÊM 1 lượt query DB mỗi request (tra theo primary key ss_user_id,
    rất rẻ) để so khớp claim "sid" trong token với
    app_users.active_session_id hiện tại — cần thiết vì mục tiêu là
    CHẶN NGAY LẬP TỨC khi có phiên mới login/token bị thu hồi (đổi mật
    khẩu, logout), không chấp nhận cửa sổ chồng lấn tới 30 phút như
    trước (JWT thuần chữ ký không tự "chết" giữa chừng được). Đây là
    điểm khác duy nhất so với thiết kế "verify không cần DB" ban đầu —
    đánh đổi có chủ đích để enforce single-session THỰC SỰ, không chỉ ở
    tầng refresh token."""
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error_code": "missing_auth_header",
                "message": "Thiếu header 'Authorization: Bearer <access_token>'.",
            },
        )
    payload = security.decode_access_token(credentials.credentials)
    if payload is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error_code": "token_expired",
                "message": "Access token không hợp lệ hoặc đã hết hạn — dùng "
                            "refresh token qua POST /auth/refresh để lấy token mới.",
            },
        )

    user_row = db_module.get_user_by_id(conn, payload["sub"])
    if user_row is None or user_row.get("active_session_id") is None:
        raise HTTPException(
            status_code=401,
            detail={
                "error_code": "session_revoked",
                "message": "Phiên đăng nhập này không còn hiệu lực — đăng "
                            "nhập lại.",
            },
        )
    if str(user_row["active_session_id"]) != payload.get("sid"):
        # session_id trong token KHÁC session_id hiện đang active trong
        # DB -> tài khoản này vừa đăng nhập ở nơi khác (login() luôn
        # sinh session_id mới), phiên hiện tại bị thay thế.
        raise HTTPException(
            status_code=401,
            detail={
                "error_code": "session_replaced",
                "message": "Tài khoản này vừa đăng nhập ở một nơi khác — "
                            "phiên đăng nhập hiện tại đã bị đăng xuất. Mỗi "
                            "tài khoản chỉ dùng được ở 1 nơi tại 1 thời điểm.",
            },
        )
    return payload


# Phân cấp role (08/2026, xem sql/migration_add_role_hierarchy.sql) —
# số càng lớn càng nhiều quyền. 'user': chỉ xem/lọc job. 'ss_team': CRUD
# job/company/contact + xem danh sách tài khoản. 'admin': + trigger
# crawl + tạo/đổi role user khác. So sánh THEO BẬC (>=) chứ không so
# khớp đúng 1 chuỗi — admin tự động thoả mọi route yêu cầu ss_team trở
# xuống, không cần liệt kê admin riêng ở từng nơi.
ROLE_HIERARCHY = {"user": 0, "ss_team": 1, "admin": 2}


def require_role(min_role: str):
    """Trả về 1 dependency FastAPI chặn nếu role của user (lấy từ JWT,
    xem get_current_user) thấp hơn min_role theo ROLE_HIERARCHY. Dùng
    kiểu Depends(require_role("ss_team")) ngay trong khai báo route,
    tương tự require_admin() cũ nhưng tổng quát cho cả 3 bậc thay vì chỉ
    biết mỗi 'admin'.

    role không có trong ROLE_HIERARCHY (dữ liệu hỏng/token cũ trước khi
    đổi tên 'member' -> 'ss_team') bị coi như bậc thấp nhất có thể
    (-1, thấp hơn cả 'user') — an toàn theo hướng TỪ CHỐI thay vì lỡ cho
    qua nhầm."""
    required_level = ROLE_HIERARCHY[min_role]

    def dependency(user: dict = Depends(get_current_user)) -> dict:
        user_level = ROLE_HIERARCHY.get(user.get("role"), -1)
        if user_level < required_level:
            raise HTTPException(
                status_code=403,
                detail=f"Cần quyền tối thiểu '{min_role}' để thực hiện thao tác này.",
            )
        return user

    return dependency


# Giữ lại tên cũ làm alias — tránh phải sửa lại crawl.py/auth.py cùng
# lúc với việc đổi tên; cả 2 đều hiểu 'admin' là bậc cao nhất.
require_admin = require_role("admin")
