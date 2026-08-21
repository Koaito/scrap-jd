"""
Auth đơn giản bằng 1 API key tĩnh, đủ dùng cho quy mô hiện tại (team 2
người, dashboard nội bộ) — KHÔNG phải OAuth2/JWT, không phân quyền theo
user. Đọc key thật từ biến môi trường API_KEY (xem .env.example).

CÁCH DÙNG (client gọi API phải gửi kèm 1 trong 2 cách):
  - Header: X-API-Key: <API_KEY>
  - Query string (chỉ để tiện test nhanh trên trình duyệt/Swagger, KHÔNG
    khuyến khích dùng ở frontend thật vì query string dễ lọt vào log
    server/proxy): ?api_key=<API_KEY>

Áp dụng cho MỌI endpoint (kể cả GET) — đăng ký ở app.py bằng
`dependencies=[Depends(require_api_key)]` cấp độ toàn app, không cần
thêm dependency riêng lẻ vào từng router.

LƯU Ý QUAN TRỌNG về thứ tự nạp .env: module này tự gọi load_dotenv()
ngay khi được import — KHÔNG dựa vào việc config.py (import gián tiếp
qua api.routers -> db) đã chạy trước hay chưa. Lý do: app.py import
`api.auth` TRƯỚC `api.routers`, nên nếu auth.py không tự nạp .env,
os.getenv("API_KEY") sẽ đọc phải giá trị rỗng (chạy trước khi
config.py kịp gọi load_dotenv()), dẫn đến lỗi "Server chưa cấu hình
API_KEY" dù .env có đủ giá trị.

NÂNG CẤP SAU (chỉ làm khi thật sự cần, đừng làm sớm):
  - Nhiều thành viên cần biết "ai gọi" -> đổi API_KEY (1 giá trị) thành
    danh sách nhiều key (vd API_KEYS="key1:tên1,key2:tên2" trong .env),
    map ngược lại tên người gọi để log/audit.
  - Cần phân quyền (vd chỉ admin được POST /crawl) -> OAuth2 + bảng
    app_users đã có sẵn trong schema.
"""

import os
import secrets

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader, APIKeyQuery

try:
    from dotenv import load_dotenv
    load_dotenv()  # đọc .env nếu có, không lỗi nếu không có — xem docstring
except ImportError:
    pass

_API_KEY = os.getenv("API_KEY", "")

_header_scheme = APIKeyHeader(name="X-API-Key", auto_error=False)
_query_scheme = APIKeyQuery(name="api_key", auto_error=False)


def require_api_key(
    header_key: str = Security(_header_scheme),
    query_key: str = Security(_query_scheme),
) -> None:
    if not _API_KEY:
        # An toàn theo hướng "fail closed": nếu quên cấu hình API_KEY
        # trong .env, chặn hết thay vì âm thầm cho qua (mở toang API).
        raise HTTPException(
            status_code=500,
            detail="Server chưa cấu hình API_KEY — xem .env.example.",
        )

    supplied = header_key or query_key
    if not supplied or not secrets.compare_digest(supplied, _API_KEY):
        raise HTTPException(
            status_code=401,
            detail="Thiếu hoặc sai API key. Gửi kèm header 'X-API-Key'.",
        )
