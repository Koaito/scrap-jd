"""
Rate limiting cho các route CÔNG KHAI (auth.public_router) — thêm
08/2026 vì trước đó KHÔNG có giới hạn nào ở tầng API cho 4 route dễ bị
spam nhất: POST /auth/register, /auth/resend-verification,
/auth/forgot-password, /auth/reset-password (đều không cần X-API-Key
lẫn JWT, ai cũng gọi được, xem docstring api/app.py).

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

Key dùng để đếm: địa chỉ IP request (get_remote_address, đọc
request.client.host) — ĐỦ cho quy mô hiện tại, dù có thể bị vượt qua
nếu tấn công qua nhiều IP (proxy/botnet) hoặc bị hiểu sai nếu app đứng
sau reverse proxy không forward đúng IP gốc (Render forward đúng IP
thật qua request.client.host, đã xác nhận không cần đọc thêm header
X-Forwarded-For thủ công).
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
