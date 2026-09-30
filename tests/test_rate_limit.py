"""
Test cho api/rate_limit.py::get_client_ip() / get_user_id_or_ip() — bản
sửa Phần 1 mục 3.14 của plan migrate Next.js: rate limit theo IP phải
đếm theo IP NGƯỜI DÙNG CUỐI (do Next.js khai báo qua header
X-Client-IP), không phải IP egress dùng chung của server Next.js.

Điểm an toàn cần giữ: header X-Client-IP CHỈ được tin khi request mang
X-API-Key hợp lệ. Nếu tin vô điều kiện, ai cũng tự đổi được khoá đếm
của mình bằng cách gửi mỗi request 1 IP giả -> rate limit vô dụng.

Request giả dựng bằng make_fake_request() (conftest.py, client cố định
127.0.0.1); API_KEY được patch thẳng vào api.auth._API_KEY (biến được
đọc lúc gọi hàm, không cache).
"""
import uuid

import pytest
from conftest import make_fake_request
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from api import security
from api.rate_limit import get_client_ip, get_user_id_or_ip

API_KEY = "test-api-key-not-a-real-secret"
DIRECT_IP = "127.0.0.1"  # IP kết nối trực tiếp trong make_fake_request()


@pytest.fixture(autouse=True)
def _configured_api_key(monkeypatch):
    monkeypatch.setattr("api.auth._API_KEY", API_KEY)


def _req(client_ip=None, api_key=API_KEY, **extra):
    headers = dict(extra)
    if client_ip is not None:
        headers["X-Client-IP"] = client_ip
    if api_key is not None:
        headers["X-API-Key"] = api_key
    return make_fake_request(headers)


class TestGetClientIp:
    def test_trusts_header_with_valid_api_key(self):
        assert get_client_ip(_req("203.0.113.7")) == "203.0.113.7"

    def test_strips_whitespace(self):
        assert get_client_ip(_req("  203.0.113.7 ")) == "203.0.113.7"

    def test_ipv6_is_normalized(self):
        assert get_client_ip(_req("2001:0DB8:0:0:0:0:0:1")) == "2001:db8::1"

    def test_ipv4_mapped_ipv6_becomes_ipv4(self):
        assert get_client_ip(_req("::ffff:203.0.113.7")) == "203.0.113.7"

    def test_ignores_header_without_api_key(self):
        """Kẻ gọi thẳng (không biết API_KEY) không tự khai IP được."""
        assert get_client_ip(_req("203.0.113.7", api_key=None)) == DIRECT_IP

    def test_ignores_header_with_wrong_api_key(self):
        assert get_client_ip(_req("203.0.113.7", api_key="wrong")) == DIRECT_IP

    def test_ignores_header_when_server_has_no_api_key(self, monkeypatch):
        """API_KEY chưa cấu hình + client gửi key rỗng: không được coi
        là 'khớp'."""
        monkeypatch.setattr("api.auth._API_KEY", "")
        assert get_client_ip(_req("203.0.113.7", api_key="")) == DIRECT_IP

    @pytest.mark.parametrize(
        "garbage",
        ["not-an-ip", "999.1.1.1", "203.0.113.7, 10.0.0.1", "1.2.3.4:8080", "a" * 500],
    )
    def test_invalid_ip_falls_back_to_direct_address(self, garbage):
        assert get_client_ip(_req(garbage)) == DIRECT_IP

    def test_missing_header_falls_back_to_direct_address(self):
        assert get_client_ip(_req(None)) == DIRECT_IP

    def test_empty_header_falls_back_to_direct_address(self):
        assert get_client_ip(_req("")) == DIRECT_IP


class TestGetUserIdOrIp:
    def test_valid_bearer_token_keys_by_user(self):
        user_id = str(uuid.uuid4())
        token = security.create_access_token(
            ss_user_id=user_id, role="ss_team", email="a@b.c", session_id=str(uuid.uuid4())
        )
        request = _req("203.0.113.7", Authorization=f"Bearer {token}")
        assert get_user_id_or_ip(request) == f"user:{user_id}"

    def test_without_token_uses_client_ip(self):
        assert get_user_id_or_ip(_req("203.0.113.7")) == "203.0.113.7"

    def test_invalid_token_uses_client_ip(self):
        request = _req("203.0.113.7", Authorization="Bearer not-a-jwt")
        assert get_user_id_or_ip(request) == "203.0.113.7"


class TestEndToEndLimit:
    """2 người dùng cuối khác nhau đi qua CÙNG 1 frontend (cùng API key)
    phải có 2 hạn mức riêng — đúng vấn đề mục 3.14 mô tả."""

    @pytest.fixture
    def client(self):
        limiter = Limiter(key_func=get_client_ip)
        app = FastAPI()
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
        app.add_middleware(SlowAPIMiddleware)

        @app.get("/ping")
        @limiter.limit("2/minute")
        def ping(request: Request):
            return {"ok": True}

        return TestClient(app)

    def _get(self, client, ip, key=API_KEY):
        headers = {"X-API-Key": key}
        if ip:
            headers["X-Client-IP"] = ip
        return client.get("/ping", headers=headers).status_code

    def test_each_end_user_has_own_bucket(self, client):
        assert [self._get(client, "203.0.113.1") for _ in range(3)] == [200, 200, 429]
        # Người dùng khác qua cùng frontend không bị ảnh hưởng.
        assert self._get(client, "203.0.113.2") == 200

    def test_spoofed_ips_without_valid_key_share_one_bucket(self, client):
        """Đổi IP giả mỗi request nhưng không có key đúng -> vẫn bị đếm
        chung theo IP kết nối thật, không né được giới hạn."""
        codes = [self._get(client, f"198.51.100.{i}", key="wrong") for i in range(3)]
        assert codes == [200, 200, 429]
