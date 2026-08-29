"""
Test cho middleware reject_oversized_request (api/app.py) — gate chặn
request quá lớn ở tầng app, thêm 08/2026 (xem việc_chưa_làm.txt mục
"Chưa có gate chặn gửi file quá lớn khi gửi CV").

Chỉ test HÀNH VI CỦA MIDDLEWARE (chặn dựa vào header Content-Length,
chạy TRƯỚC auth/route) — KHÔNG cần API_KEY/JWT thật, vì middleware này
cố ý đặt để chạy sớm hơn cả bước check X-API-Key (xem comment thứ tự
middleware trong api/app.py). Test dùng TestClient thật (không mock
app) để đảm bảo test đúng hành vi tích hợp của middleware trong app
thật, không phải gọi hàm middleware cô lập.

Chạy: pytest tests/test_request_size_limit.py -v
"""
import os

# api/app.py raise RuntimeError khi import nếu thiếu JWT_SECRET_KEY
# (xem api/security.py) — set trước khi import app, giống cách
# .github/workflows/test.yml set biến này cho toàn bộ suite.
os.environ.setdefault("JWT_SECRET_KEY", "test_secret_not_used_in_production")

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request as StarletteRequest

from api.app import app, reject_oversized_request
from config import MAX_REQUEST_BODY_BYTES

client = TestClient(app)


def _make_request(headers: dict) -> StarletteRequest:
    """Dựng Request thuần với header tuỳ ý — dùng để gọi thẳng middleware
    (KHÔNG qua TestClient/HTTP thật), tránh phụ thuộc việc httpx có tự
    gắn Content-Length hay không cho từng loại request."""
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http", "method": "GET", "path": "/health",
        "headers": raw_headers, "client": ("127.0.0.1", 12345), "query_string": b"",
    }
    return StarletteRequest(scope)


def test_oversized_content_length_rejected_with_413():
    """Content-Length vượt MAX_REQUEST_BODY_BYTES -> 413 ngay, không
    quan tâm route có tồn tại/cần auth hay không (middleware chạy
    TRƯỚC routing)."""
    oversized = MAX_REQUEST_BODY_BYTES + 1
    response = client.post(
        "/me/applications",
        headers={"content-length": str(oversized)},
        content=b"x",
    )
    assert response.status_code == 413
    assert "MB" in response.json()["detail"]


def test_content_length_exactly_at_limit_not_rejected_by_middleware():
    """Đúng bằng giới hạn (không vượt) -> middleware PHẢI cho qua (dùng
    strict >, không phải >=) — request sẽ bị chặn ở tầng khác (thiếu
    API key) chứ không phải 413."""
    response = client.post(
        "/me/applications",
        headers={"content-length": str(MAX_REQUEST_BODY_BYTES)},
        content=b"x",
    )
    assert response.status_code != 413


def test_small_request_not_rejected_by_middleware():
    """Request nhỏ bình thường -> không bao giờ dính 413 từ middleware
    này, dù thiếu API key (401) hay bất kỳ lỗi nào khác ở tầng sau."""
    response = client.get("/health")
    assert response.status_code != 413


@pytest.mark.asyncio
async def test_missing_content_length_passes_through_to_call_next():
    """Không có header Content-Length (vd chunked transfer-encoding) —
    middleware KHÔNG có đủ thông tin để kết luận, PHẢI gọi call_next()
    để tầng dưới tự xử lý tiếp (xem mục 'GIỚI HẠN ĐÃ BIẾT' ở comment
    middleware), không được tự ý chặn nhầm request hợp lệ."""
    request = _make_request({})
    sentinel = object()

    async def fake_call_next(_req):
        return sentinel

    result = await reject_oversized_request(request, fake_call_next)
    assert result is sentinel


@pytest.mark.asyncio
async def test_malformed_content_length_passes_through_to_call_next():
    """Content-Length không phải số hợp lệ -> không parse được, middleware
    PHẢI bỏ qua (không đoán mò) và gọi call_next() bình thường, thay vì
    crash 500 hoặc chặn nhầm."""
    request = _make_request({"content-length": "not-a-number"})
    sentinel = object()

    async def fake_call_next(_req):
        return sentinel

    result = await reject_oversized_request(request, fake_call_next)
    assert result is sentinel
