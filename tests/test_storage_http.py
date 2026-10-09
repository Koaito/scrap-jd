"""
api/storage.py gọi Supabase Storage qua HTTP. Test này dựng một server HTTP
THẬT trên 127.0.0.1 (không mock thư viện HTTP) để kiểm tra đúng thứ đi qua
dây: method, đường dẫn, header, body nhị phân, JSON, xử lý mã lỗi, lỗi kết
nối và timeout. Nhờ vậy đổi thư viện HTTP (requests -> curl_cffi, 10/2026)
mà vẫn chắc hành vi không đổi.

Không cần Postgres hay mạng ngoài.
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import pytest

import scrapjd.api.storage as storage

# Có byte không hợp lệ UTF-8 để chắc body nhị phân không bị biến dạng.
PDF_BYTES = b"%PDF-1.4\n\xff\xfe\x00\x01binary\x80\x81" + bytes(range(256))


class _Handler(BaseHTTPRequestHandler):
    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        srv = self.server
        srv.seen.append({
            "method": self.command, "path": self.path,
            "headers": {k.lower(): v for k, v in self.headers.items()},
            "body": body,
        })
        if srv.delay:
            time.sleep(srv.delay)
        payload = srv.reply_body
        self.send_response(srv.reply_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_POST = do_DELETE = do_GET = _handle

    def log_message(self, *args):  # im lặng
        pass


class _QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        # Test timeout: client đóng kết nối trước khi server kịp trả lời ->
        # BrokenPipeError là bình thường, không in traceback ra output pytest.
        pass


@pytest.fixture
def server():
    srv = _QuietServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    srv.block_on_close = False
    srv.seen = []
    srv.reply_status = 200
    srv.reply_body = b"{}"
    srv.delay = 0
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture(autouse=True)
def configured(server):
    base = f"http://127.0.0.1:{server.server_address[1]}"
    with patch.object(storage, "SUPABASE_URL", base), \
         patch.object(storage, "SUPABASE_SERVICE_ROLE_KEY", "service-key"), \
         patch.object(storage, "SUPABASE_CV_BUCKET", "cv-files"):
        yield base


def _dead_url() -> str:
    """URL của cổng không ai nghe (kết nối bị từ chối)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------- upload_cv

def test_upload_sends_binary_body_and_headers(server):
    server.reply_status = 200
    path = storage.upload_cv(PDF_BYTES, "user-1", "app-9")

    assert path == "cv-files/user-1/app-9.pdf"
    (req,) = server.seen
    assert req["method"] == "POST"
    assert req["path"] == "/storage/v1/object/cv-files/user-1/app-9.pdf"
    assert req["body"] == PDF_BYTES
    h = req["headers"]
    assert h["authorization"] == "Bearer service-key"
    assert h["apikey"] == "service-key"
    assert h["content-type"] == "application/pdf"
    assert h["x-upsert"] == "true"


def test_upload_accepts_201(server):
    server.reply_status = 201
    assert storage.upload_cv(b"x", "u", "a") == "cv-files/u/a.pdf"


def test_upload_http_error_raises_runtime_error(server):
    server.reply_status = 500
    server.reply_body = b'{"message":"boom"}'
    with pytest.raises(RuntimeError, match="HTTP 500"):
        storage.upload_cv(b"x", "u", "a")


def test_upload_not_configured_raises_without_calling(server):
    with patch.object(storage, "SUPABASE_SERVICE_ROLE_KEY", ""):
        with pytest.raises(RuntimeError, match="SUPABASE"):
            storage.upload_cv(b"x", "u", "a")
    assert server.seen == []


def test_upload_connection_error_is_runtime_error():
    # api/routers/me.py chỉ bắt RuntimeError quanh upload_cv; lỗi mạng mà
    # lọt ra dạng exception của thư viện HTTP sẽ thành 500 không có
    # error_code và bỏ lại transaction dở. Phải được gói thành RuntimeError.
    with patch.object(storage, "SUPABASE_URL", _dead_url()):
        with pytest.raises(RuntimeError, match="storage"):
            storage.upload_cv(b"x", "u", "a")


def test_upload_timeout_is_runtime_error(server):
    server.delay = 1.5
    with patch.object(storage, "_TIMEOUT", 0.5):
        with pytest.raises(RuntimeError, match="storage"):
            storage.upload_cv(b"x", "u", "a")


# ------------------------------------------------------------ get_signed_url

def test_signed_url_prefixes_storage_path_and_sends_json(server):
    server.reply_body = json.dumps({"signedURL": "/object/sign/cv-files/u/a.pdf?token=t"}).encode()
    url = storage.get_signed_url("cv-files/u/a.pdf", expires_in=120)

    assert url == f"{storage.SUPABASE_URL}/storage/v1/object/sign/cv-files/u/a.pdf?token=t"
    (req,) = server.seen
    assert req["method"] == "POST"
    assert req["path"] == "/storage/v1/object/sign/cv-files/u/a.pdf"
    assert json.loads(req["body"]) == {"expiresIn": 120}
    assert req["headers"]["content-type"].startswith("application/json")
    assert req["headers"]["authorization"] == "Bearer service-key"


def test_signed_url_absolute_is_returned_as_is(server):
    server.reply_body = b'{"signedURL": "https://cdn.example/x?t=1"}'
    assert storage.get_signed_url("cv-files/u/a.pdf") == "https://cdn.example/x?t=1"


def test_signed_url_non_200_returns_none(server):
    server.reply_status = 404
    assert storage.get_signed_url("cv-files/u/a.pdf") is None


def test_signed_url_empty_field_returns_none(server):
    server.reply_body = b'{"signedURL": ""}'
    assert storage.get_signed_url("cv-files/u/a.pdf") is None


def test_signed_url_invalid_json_returns_none(server):
    server.reply_body = b"not json"
    assert storage.get_signed_url("cv-files/u/a.pdf") is None


def test_signed_url_connection_error_returns_none():
    with patch.object(storage, "SUPABASE_URL", _dead_url()):
        assert storage.get_signed_url("cv-files/u/a.pdf") is None


def test_signed_url_timeout_returns_none(server):
    server.delay = 1.5
    with patch.object(storage, "_TIMEOUT", 0.5):
        assert storage.get_signed_url("cv-files/u/a.pdf") is None


@pytest.mark.parametrize("bad", ["", None, "no-slash"])
def test_signed_url_invalid_path_makes_no_request(server, bad):
    assert storage.get_signed_url(bad) is None
    assert server.seen == []


# ----------------------------------------------------------------- delete_cv

def test_delete_sends_json_body(server):
    storage.delete_cv("cv-files/u/a.pdf")

    (req,) = server.seen
    assert req["method"] == "DELETE"
    assert req["path"] == "/storage/v1/object/cv-files"
    assert json.loads(req["body"]) == {"prefixes": ["u/a.pdf"]}
    assert req["headers"]["content-type"].startswith("application/json")
    assert req["headers"]["apikey"] == "service-key"


def test_delete_swallows_connection_error():
    with patch.object(storage, "SUPABASE_URL", _dead_url()):
        storage.delete_cv("cv-files/u/a.pdf")  # không được raise


def test_delete_swallows_timeout(server):
    server.delay = 1.5
    with patch.object(storage, "_TIMEOUT", 0.5):
        storage.delete_cv("cv-files/u/a.pdf")


@pytest.mark.parametrize("bad", ["", None, "no-slash"])
def test_delete_invalid_path_makes_no_request(server, bad):
    storage.delete_cv(bad)
    assert server.seen == []
