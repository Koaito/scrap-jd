"""
Giới hạn số job nền (crawl + maintenance) được phép chạy ĐỒNG THỜI trên
toàn hệ thống — thêm 08/2026 để chặn lỗi
"psycopg2.OperationalError: ... EMAXCONNSESSION ... max clients reached
in session mode - max clients are limited to pool_size: 15" từ Supabase
khi người dùng bấm chạy nhiều loại job khác nhau CÙNG LÚC (vd "Tra cứu
web" + "Tìm Facebook/LinkedIn" + "Vận hành dữ liệu" cùng 1 lúc, xem lịch
sử trao đổi "lỗi 500 khi chạy nhiều script cùng lúc").

TRƯỚC ĐÂY: db.ActiveMaintenanceRunExistsError/db.ActiveCrawlExistsError
chỉ chặn trùng CÙNG 1 job_type/source đang chạy (UNIQUE INDEX ở tầng
DB, xem sql/migration_add_maintenance_runs.sql và
sql/migration_add_crawl_runs.sql) — KHÔNG chặn việc chạy KHÁC LOẠI job
song song (vd vừa "Tra cứu web" vừa "Tìm Facebook" vừa "Vận hành" cùng
lúc), nên tổng connection Postgres mà các job nền mở ra có thể cộng dồn
vượt giới hạn Supabase.

VÌ SAO GLOBAL_JOB_LIMIT mặc định = 2: mỗi job nền đang chạy giữ 2
connection Postgres NGOÀI POOL cùng lúc — 1 cho execute()/_execute_one()
(chạy job thật), 1 riêng cho _RunLogHandler (ghi log live) — cả 2 CỐ Ý
dùng db.get_connection() (không qua pool, xem docstring db/connection.py
và mục CONNECTION POOL ở đầu api/maintenance_runner.py +
api/crawl_runner.py). Vậy tối đa 2 job * 2 connection = 4 connection
cho job nền tại 1 thời điểm, cộng với pool API (DB_POOL_MIN/DB_POOL_MAX,
xem config.py — đã hạ default xuống 8 cùng đợt sửa lỗi này) vẫn nằm
trong giới hạn 15 connection của gói Supabase hiện tại. Tăng số này lên
sẽ cần hạ DB_POOL_MAX tương ứng để tổng không vượt 15 (hoặc chuyển hẳn
sang Transaction Pooler port 6543 — xem .env.example).

CHỈ dùng threading.Semaphore (in-memory, KHÔNG phân tán giữa nhiều
process) vì Render hiện deploy 1 instance/1 process (xem
api/rate_limit.py, README.md mục "Trạng thái") — nếu sau này scale
ngang (nhiều instance) hoặc bật `uvicorn --workers > 1`, Semaphore này
sẽ KHÔNG còn tác dụng (mỗi process giữ bản đếm riêng, không đồng bộ với
nhau, y hệt giới hạn đã ghi ở api/rate_limit.py cho slowapi) và PHẢI đổi
sang cơ chế khóa dùng chung, ví dụ Postgres advisory lock
(pg_try_advisory_lock — không cần thêm service ngoài như Redis, đúng
tinh thần "free-tier, rẻ, an toàn trước" của project) thay vì Semaphore
ở đây.

Đặt ở module RIÊNG (không khai báo lặp lại Semaphore() ở từng file) để
api/maintenance_runner.py và api/crawl_runner.py dùng CHUNG đúng 1 giới
hạn tổng — nếu mỗi file tự tạo Semaphore(2) riêng, tổng job nền chạy
song song thực tế sẽ lên tới 4 (2 maintenance + 2 crawl độc lập nhau)
thay vì đúng 2 như mong muốn.
"""

import os
import threading

# Số job nền (maintenance + crawl CỘNG CHUNG 1 giới hạn) được phép chạy
# đồng thời trên toàn hệ thống — đọc từ env GLOBAL_JOB_LIMIT nếu cần
# chỉnh mà không sửa code. Mặc định 2, xem lý do chọn số này ở docstring
# module phía trên (đối chiếu với DB_POOL_MAX + giới hạn Supabase).
GLOBAL_JOB_LIMIT = int(os.getenv("GLOBAL_JOB_LIMIT", "2"))

# Dùng CHUNG 1 instance duy nhất — import biến này thẳng vào
# maintenance_runner.py và crawl_runner.py, KHÔNG tự tạo Semaphore()
# mới ở đó (xem lý do ở docstring module).
GLOBAL_JOB_SEMAPHORE = threading.Semaphore(GLOBAL_JOB_LIMIT)
