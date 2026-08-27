"""
Facade gộp lại router auth — file auth.py gốc (08/2026) từng gộp 738
dòng/14 endpoint của 4 concern khác nhau (session, quản trị user, đăng
ký công khai) trong 1 file, khó theo dõi mỗi khi sửa. Đã tách theo
domain thành 3 file, giống pattern re-export của db/__init__.py sau
khi tách db.py 2619 dòng — code cũ gọi `from api.routers import auth`
rồi dùng `auth.router` / `auth.public_router` (app.py, xem
app.include_router) KHÔNG cần đổi gì:

  - auth_session.py   : login / refresh / logout / me / change-password
                         (tự quản lý phiên của CHÍNH mình)
  - auth_users.py      : tạo / liệt kê / đổi role / khoá-mở khoá user
                         khác (admin, ss_team)
  - auth_registration.py : register / verify-email / resend-verification
                         / forgot-password / reset-password (public,
                         không cần X-API-Key — xem docstring file đó)

Muốn sửa logic 1 route cụ thể, vào thẳng file con tương ứng ở trên —
file này chỉ ghép router, không chứa logic.
"""

from fastapi import APIRouter

from api.routers.auth_session import router as _session_router
from api.routers.auth_users import router as _users_router
from api.routers.auth_registration import public_router

# router: mọi route vẫn nằm SAU lớp API_KEY (app.py include kèm
# dependencies=[Depends(require_api_key)]) — JWT là lớp thứ 2 xác định
# "user thật nào" bên trong. Gộp 2 router con bằng include_router
# KHÔNG khai lại prefix ở đây (mỗi router con đã tự có prefix="/auth")
# để tránh bị nhân đôi thành "/auth/auth/...".
router = APIRouter()
router.include_router(_session_router)
router.include_router(_users_router)

# public_router: re-export thẳng từ auth_registration.py — 5 route
# đăng ký/xác thực email/quên mật khẩu công khai, KHÔNG cần X-API-Key
# (app.py include KHÔNG kèm dependencies). Xem docstring
# auth_registration.py để biết lý do (link verify-email bấm từ email).

__all__ = ["router", "public_router"]
