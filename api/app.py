"""
Entry point FastAPI — lớp API layer bọc ngoài codebase crawler hiện có.

CHẠY:
    uvicorn api.app:app --reload --port 8000

Sau khi chạy, xem tài liệu tự sinh tại (CẦN set ENABLE_DOCS=true trong
.env trước — mặc định TẮT, xem mục "GIỚI HẠN ĐÃ BIẾT" bên dưới):
    http://localhost:8000/docs       (Swagger UI, có thể bấm thử luôn)
    http://localhost:8000/redoc      (ReDoc, dạng đọc)

Đây là process HOÀN TOÀN TÁCH BIỆT với main.py (CLI crawl hiện có) —
cả hai cùng nói chuyện với 1 Postgres, có thể chạy song song, không
xung đột. main.py KHÔNG bị sửa gì trong đợt thêm API layer này.

AUTH + CORS (xem thêm api/auth.py và .env.example):
  - MỌI endpoint THẬT SỰ trả dữ liệu (kể cả /health) yêu cầu header
    'X-API-Key' đúng giá trị biến môi trường API_KEY — đăng ký theo
    TỪNG router bằng `dependencies=[Depends(require_api_key)]` ở
    include_router() (và trực tiếp trên @app.get("/health")).
  - NGOẠI LỆ (08/2026, sửa bug link xác thực email luôn 401): 3 route
    công khai trong api/routers/auth.py — POST /auth/register, GET
    /auth/verify-email, POST /auth/resend-verification — nằm ở
    `auth.public_router`, include KHÔNG kèm X-API-Key. Lý do: GET
    /auth/verify-email được người dùng bấm thẳng từ email, trình duyệt
    không thể tự gắn header X-API-Key vào request đó — nếu vẫn bắt
    buộc key, link xác thực sẽ luôn 401 (đã gặp thực tế). Trước
    08/2026, KHÔNG có ngoại lệ này (dependencies đăng ký 1 lần cấp app
    cho MỌI route) — đây chính là bug đã sửa.
  - CORS chỉ mở cho domain liệt kê trong biến môi trường
    ALLOWED_ORIGINS (phân tách bằng dấu phẩy) — KHÔNG còn "*". Đổi
    domain (vd frontend deploy Vercel preview URL mới) chỉ cần sửa
    .env, không cần sửa code.

GIỚI HẠN ĐÃ BIẾT — /docs, /redoc, /openapi.json KHÔNG đi qua
`dependencies=` cấp app (08/2026, xác nhận bằng thực nghiệm): đây là
hành vi riêng của FastAPI — 3 route này được tự sinh bằng
`self.add_route()` (route Starlette thuần), KHÔNG phải "path operation"
kiểu FastAPI thường nên KHÔNG chạy qua dependency injection, bất kể
`dependencies=` khai báo ở đâu. Hệ quả: ai cũng xem được CẤU TRÚC API
(tên endpoint, tham số) dù không có key — KHÔNG lộ dữ liệu thật (mọi
route dữ liệu vẫn đòi key bình thường, đã test).

XỬ LÝ: mặc định TẮT HẲN 3 route này (docs_url/redoc_url/openapi_url =
None) — cùng nguyên tắc "fail closed" như API_KEY/ALLOWED_ORIGINS rỗng.
Cần xem Swagger lúc dev local -> set ENABLE_DOCS=true trong .env. KHÔNG
khuyến khích bật ENABLE_DOCS=true trên môi trường public (Render) trừ
khi đang cần debug tạm thời.
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from apscheduler.schedulers.background import BackgroundScheduler

import db as db_module
from api.auth import require_api_key
from api.rate_limit import limiter
from api.routers import auth, companies, contacts, crawl, jobs, me, meta, audit_logs, import_export
from api.services.preview_cleanup import CLEANUP_INTERVAL_MINUTES, run_cleanup_once
from api.services.crawl_watchdog import run_crawl_watchdog_once
from config import CRAWL_WATCHDOG_INTERVAL_MINUTES

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

# Fail-closed giống API_KEY/ALLOWED_ORIGINS: không set / set giá trị
# khác "true" -> TẮT docs công khai. Chỉ bật khi thật sự cần xem Swagger
# (dev local), KHÔNG khuyến khích bật trên môi trường public lâu dài.
_docs_enabled = os.getenv("ENABLE_DOCS", "").strip().lower() == "true"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Khởi tạo connection pool (db.py) 1 LẦN lúc app khởi động, đóng lại
    khi app tắt — thay cho mở/đóng connection Postgres thật ở MỖI
    request (08/2026, xem db.py mục "CONNECTION POOL"). Dùng lifespan
    (khuyến nghị hiện tại của FastAPI) thay vì @app.on_event("startup"/
    "shutdown") đã deprecated. Đóng pool lúc shutdown tránh connection
    bị bỏ "treo" phía Postgres khi Render restart/deploy lại server."""
    db_module.init_pool()

    # 08/2026 (xem sql/migration_add_crawl_runs.sql +
    # api/services/crawl_watchdog.py): reconcile CÁC LƯỢT CRAWL MỒ CÔI
    # từ lần chạy process TRƯỚC (còn kẹt 'queued'/'running' vì process
    # đó dừng đột ngột) — PHẢI chạy TRƯỚC yield (trước khi nhận request
    # nào), để không có cửa sổ thời gian nào UNIQUE INDEX
    # idx_crawl_runs_one_active_per_source còn bị "khoá" bởi dữ liệu cũ
    # trong lúc server mới đã bắt đầu nhận request.
    _startup_conn = db_module.get_pooled_connection()
    try:
        _orphaned = db_module.reconcile_orphaned_crawl_runs(_startup_conn)
        if _orphaned:
            logging.getLogger(__name__).warning(
                "Khởi động: đã reconcile %d lượt crawl mồ côi từ lần chạy trước.",
                _orphaned,
            )
    finally:
        db_module.release_connection(_startup_conn)

    # Cleanup task định kỳ cho import_previews hết hạn (Requirement 9)
    # — BackgroundScheduler chạy TRONG process này (không cần service
    # ngoài kiểu cron/Celery riêng), đủ cho quy mô hiện tại (1 instance,
    # xem README.md mục "Trạng thái" + cùng lưu ý về scale ngang như
    # api/rate_limit.py: nếu sau này chạy nhiều instance/worker, mỗi
    # process sẽ tự chạy cleanup riêng — KHÔNG sai (DELETE ... WHERE
    # expires_at < now() là idempotent, chạy trùng nhiều lần không hại
    # gì), chỉ hơi thừa công, không cần sửa gì thêm nếu scale sau này).
    scheduler = BackgroundScheduler()
    scheduler.add_job(run_cleanup_once, "interval", minutes=CLEANUP_INTERVAL_MINUTES)
    # Watchdog crawl treo (08/2026) — DÙNG CHUNG scheduler này, không
    # tạo thêm process/thread riêng. Đây là LỚP DỰ PHÒNG THỨ 2, bổ sung
    # cho reconcile_orphaned_crawl_runs() ở trên (chỉ bắt được lúc
    # server RESTART) — watchdog này bắt thêm trường hợp process không
    # restart nhưng 1 task bị treo giữa chừng (xem docstring
    # api/services/crawl_watchdog.py).
    scheduler.add_job(run_crawl_watchdog_once, "interval", minutes=CRAWL_WATCHDOG_INTERVAL_MINUTES)
    scheduler.start()

    yield

    scheduler.shutdown(wait=False)
    db_module.close_pool()


app = FastAPI(
    title="SCRAP JD API",
    description="API layer cho crawler job TopCV/VietnamWorks — team Student Success.",
    version="0.1.0",
    # KHÔNG còn dependencies=[Depends(require_api_key)] ở cấp app (khác
    # bản trước 08/2026) — X-API-Key giờ đăng ký RIÊNG cho từng router
    # bên dưới (include_router(..., dependencies=[...])) để có thể loại
    # trừ auth.public_router (xem docstring đầu file + api/routers/auth.py).
    lifespan=lifespan,
    # /docs, /redoc, /openapi.json KHÔNG đi qua dependencies= ở trên (xem
    # docstring đầu file) -> mặc định TẮT HẲN (fail-closed), chỉ bật khi
    # ENABLE_DOCS=true trong .env (dùng lúc dev local).
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

# Rate limiting (xem docstring đầy đủ ở api/rate_limit.py) — chỉ áp
# dụng thật sự cho 4 route trong auth.public_router có gắn decorator
# @limiter.limit(...) (api/routers/auth.py), KHÔNG tự động áp cho mọi
# route. 3 dòng dưới đây là phần "lắp" bắt buộc của slowapi:
#   - app.state.limiter: nơi decorator @limiter.limit(...) tra cứu
#     ngược lại limiter instance lúc request tới.
#   - exception_handler: bắt RateLimitExceeded, trả về response 429
#     kèm header Retry-After (hành vi mặc định của slowapi).
#   - SlowAPIMiddleware: BẮT BUỘC phải có, thiếu middleware này thì
#     decorator @limiter.limit(...) không chạy dù đã khai báo state +
#     exception handler ở trên (lỗi hay gặp khi tích hợp slowapi).
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# CORS: chỉ cho phép domain khai báo trong ALLOWED_ORIGINS (.env) gọi API
# này từ trình duyệt. Ví dụ .env:
#   ALLOWED_ORIGINS=https://ss-dashboard.vercel.app,http://localhost:3000
# Không set / để trống -> KHÔNG cho domain nào gọi từ trình duyệt (an
# toàn theo hướng "fail closed", giống cách xử lý thiếu API_KEY).
_allowed_origins = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Security headers cơ bản — thêm 08/2026 (rà soát bảo mật), áp dụng cho
# MỌI response kể cả lỗi/redirect. Chi phí gần như 0 (chỉ set vài
# header), nên thêm dù API này chủ yếu trả JSON thuần (không tự render
# HTML, trừ 2 route redirect ở api/routers/auth.py) — phòng hờ vẫn tốt
# hơn không có, và một số proxy/CDN phía trước (Render) không tự thêm
# các header này giúp.
#   - X-Content-Type-Options: nosniff — chặn trình duyệt tự đoán
#     content-type khác Content-Type server khai báo (chống 1 số kiểu
#     tấn công MIME-sniffing).
#   - X-Frame-Options: DENY — chặn nhúng response của domain này vào
#     <iframe> ở site khác (chống clickjacking) — không có lý do gì API
#     này cần bị nhúng iframe.
#   - Referrer-Policy: strict-origin-when-cross-origin — không rò rỉ
#     full URL (có thể chứa token trong query string, vd link verify-
#     email/reset-password) ra header Referer khi trình duyệt điều
#     hướng sang domain khác.
#   - Strict-Transport-Security (HSTS) — báo trình duyệt LUÔN dùng HTTPS
#     cho domain này trong 2 năm tới, kể cả lần gõ http:// đầu tiên. AN
#     TOÀN để bật vì Render luôn phục vụ qua HTTPS (không có luồng HTTP
#     thật nào bị "khoá" bởi header này) — nếu sau này tự host lại trên
#     hạ tầng KHÔNG luôn có HTTPS thì cần bỏ header này trước, nếu không
#     người dùng cũ (đã có header) sẽ không truy cập được qua HTTP nữa.
@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response

# X-API-Key bắt buộc cho MỌI router dữ liệu thật (kể cả auth.router —
# login/refresh/logout/me/change-password/users, những route CẦN biết
# client là frontend nội bộ trước khi xử lý tiếp JWT bên trong).
_require_key = [Depends(require_api_key)]
app.include_router(jobs.router, dependencies=_require_key)
app.include_router(companies.router, dependencies=_require_key)
app.include_router(contacts.router, dependencies=_require_key)
app.include_router(contacts.all_contacts_router, dependencies=_require_key)
app.include_router(crawl.router, dependencies=_require_key)
app.include_router(meta.router, dependencies=_require_key)
app.include_router(auth.router, dependencies=_require_key)
app.include_router(me.router, dependencies=_require_key)
app.include_router(audit_logs.router, dependencies=_require_key)
app.include_router(import_export.router, dependencies=_require_key)

# auth.public_router: register/verify-email/resend-verification — CỐ Ý
# KHÔNG kèm dependencies=_require_key (xem docstring đầu file).
app.include_router(auth.public_router)


@app.get("/health", tags=["meta"], dependencies=_require_key)
def health_check():
    """Kiểm tra server sống. LƯU Ý: endpoint này CŨNG yêu cầu API key
    (khai báo trực tiếp qua dependencies= ở decorator này, vì /health
    định nghĩa thẳng trên `app`, không qua include_router()) — nếu dùng
    cho load balancer/uptime monitor bên ngoài, monitor đó cần được cấp
    API_KEY để gọi được. Không động vào DB (health check nên nhanh,
    không phụ thuộc DB down)."""
    return {"status": "ok"}
