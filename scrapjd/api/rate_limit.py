"""
Rate limiting cho các route CÔNG KHAI (auth.public_router) — thêm
08/2026 vì trước đó KHÔNG có giới hạn nào ở tầng API cho 4 route dễ bị
spam nhất: POST /auth/register, /auth/resend-verification,
/auth/forgot-password, /auth/reset-password (đều không cần X-API-Key
lẫn JWT, ai cũng gọi được, xem docstring scrapjd/api/app.py).

Rủi ro nếu KHÔNG giới hạn: 1 script gọi lặp POST /auth/register có thể
tạo hàng loạt tài khoản rác (mỗi lần gửi kèm 1 email, tốn quota Resend
— free tier 3.000 email/tháng, xem .env.example); tương tự
resend-verification/forgot-password đều trigger gửi email thật, có thể
bị lợi dụng để "bomb" email tới 1 địa chỉ bất kỳ. reset-password không
gửi email nhưng vẫn giới hạn để chặn dò token hàng loạt (dù token 32
byte urlsafe gần như không thể đoán được, giới hạn ở đây là lớp phòng
thủ thêm, không phải lớp chính).

Dùng slowapi (wrapper của limits cho FastAPI/Starlette) — thư viện phổ
biến, nhẹ, không cần thêm service ngoài (Redis...) cho quy mô hiện tại.

QUAN TRỌNG — giới hạn đã biết: mặc định slowapi đếm request TRONG BỘ
NHỚ (in-memory), RIÊNG cho từng process. Deploy hiện tại trên Render là
1 instance/1 process (xem README.md mục "Trạng thái") nên không sao —
nhưng nếu sau này scale ngang (nhiều instance/worker) hoặc bật
--workers > 1 cho uvicorn, mỗi process sẽ đếm request ĐỘC LẬP (vd giới
hạn "5/hour" thực chất thành "5/hour x N process") — lúc đó cần đổi
sang storage dùng chung (Redis, xem storage_uri= của Limiter) mới đúng
nghĩa giới hạn toàn cục.

Key dùng để đếm: IP của người dùng cuối, lấy bằng get_client_ip() bên
dưới (xem docstring hàm đó) — ĐỦ cho quy mô hiện tại, dù có thể bị vượt
qua nếu tấn công qua nhiều IP (proxy/botnet).

09/2026 (Phần 1 mục 3.14 của plan migrate Next.js): trước đây key là
get_remote_address (request.client.host). Render forward đúng IP của
NGƯỜI GỌI TRỰC TIẾP tới Render — nhưng người gọi trực tiếp luôn là
server Next.js (Vercel), không phải trình duyệt, nên MỌI người dùng qua
frontend dùng chung 1 hạn mức (vd GET /companies, GET /jobs 60/phút cho
cả hệ thống, /auth/login 20/phút cho cả hệ thống). Sửa: Next.js gửi kèm
IP thật qua header X-Client-IP, backend chỉ tin header này khi request
có X-API-Key hợp lệ (xem get_client_ip).
"""

import ipaddress

from slowapi import Limiter
from slowapi.util import get_remote_address

from scrapjd.api import security
from scrapjd.api.auth import has_valid_api_key_header

# Header frontend dùng để khai báo IP thật của người dùng cuối. Đổi tên
# ở đây thì phải đổi cả lib/client-ip.ts phía Next.js.
CLIENT_IP_HEADER = "x-client-ip"


def get_client_ip(request) -> str:
    """IP dùng làm khoá đếm rate limit.

    Tin header X-Client-IP CHỈ KHI request mang X-API-Key hợp lệ (chỉ
    frontend chính thức cầm API_KEY, và nó tự lấy IP từ header do
    Vercel gán — không phải giá trị trình duyệt tự khai). Không có API
    key hợp lệ (gọi thẳng, Swagger, kẻ tấn công không biết key), header
    không phải IP hợp lệ, hoặc thiếu header -> rơi về
    get_remote_address như cũ. Không bao giờ raise: lỗi ở đây sẽ thành
    500 cho mọi route có rate limit.

    IPv4 viết dạng IPv6 (::ffff:1.2.3.4) được đưa về IPv4 để cùng 1
    người không bị tách thành 2 khoá."""
    forwarded = request.headers.get(CLIENT_IP_HEADER, "").strip()
    if forwarded and has_valid_api_key_header(request):
        try:
            ip = ipaddress.ip_address(forwarded)
        except ValueError:
            return get_remote_address(request)
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        return str(ip)
    return get_remote_address(request)


limiter = Limiter(key_func=get_client_ip)


def get_user_id_or_ip(request) -> str:
    """Key function riêng cho route ĐÃ đăng nhập (vd /me/applications,
    /me/saved-jobs) — thêm 08/2026 cùng đợt rate-limit /auth/login.

    Dùng ss_user_id (từ JWT) thay vì địa chỉ IP như limiter mặc định ở
    trên, vì các route này luôn có Authorization header hợp lệ (đã qua
    require_role() mới tới được decorator) — khoá theo user_id công bằng
    hơn: nhiều học viên chung 1 mạng (KTX, wifi trường) sẽ không bị tính
    chung 1 hạn mức IP và vô tình đụng trần của nhau.

    Tự rơi về IP nếu vì lý do gì đó không đọc được token hợp lệ (không
    nên xảy ra trong thực tế vì Depends(require_role(...)) đã chạy trước,
    nhưng slowapi tính rate limit trước khi vào body hàm nên cứ phòng hờ
    thay vì để lỗi 500) — an toàn hơn là bỏ giới hạn hoàn toàn."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:]
        payload = security.decode_access_token(token)
        if payload and payload.get("sub"):
            return f"user:{payload['sub']}"
    return get_client_ip(request)
