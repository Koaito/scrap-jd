"""
Entry point FastAPI — lớp API layer bọc ngoài codebase crawler hiện có.

CHẠY:
    uvicorn api.app:app --reload --port 8000

Sau khi chạy, xem tài liệu tự sinh tại:
    http://localhost:8000/docs       (Swagger UI, có thể bấm thử luôn)
    http://localhost:8000/redoc      (ReDoc, dạng đọc)

Đây là process HOÀN TOÀN TÁCH BIỆT với main.py (CLI crawl hiện có) —
cả hai cùng nói chuyện với 1 Postgres, có thể chạy song song, không
xung đột. main.py KHÔNG bị sửa gì trong đợt thêm API layer này.

AUTH + CORS (xem thêm api/auth.py và .env.example):
  - MỌI endpoint (kể cả /health) yêu cầu header 'X-API-Key' đúng giá
    trị biến môi trường API_KEY — đăng ký 1 lần ở cấp app bằng
    `dependencies=[Depends(require_api_key)]`, không cần sửa từng
    router riêng lẻ.
  - CORS chỉ mở cho domain liệt kê trong biến môi trường
    ALLOWED_ORIGINS (phân tách bằng dấu phẩy) — KHÔNG còn "*". Đổi
    domain (vd frontend deploy Vercel preview URL mới) chỉ cần sửa
    .env, không cần sửa code.
"""

import logging
import os

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.auth import require_api_key
from api.routers import companies, crawl, jobs, meta

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)

app = FastAPI(
    title="SCRAP JD API",
    description="API layer cho crawler job TopCV/VietnamWorks — team Student Success.",
    version="0.1.0",
    dependencies=[Depends(require_api_key)],
)

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

app.include_router(jobs.router)
app.include_router(companies.router)
app.include_router(crawl.router)
app.include_router(meta.router)


@app.get("/health", tags=["meta"])
def health_check():
    """Kiểm tra server sống. LƯU Ý: endpoint này CŨNG yêu cầu API key
    (đăng ký ở cấp app) — nếu dùng cho load balancer/uptime monitor bên
    ngoài, monitor đó cần được cấp API_KEY để gọi được. Không động vào
    DB (health check nên nhanh, không phụ thuộc DB down)."""
    return {"status": "ok"}
