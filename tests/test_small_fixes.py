"""
Mục 8 — các sửa nhỏ, không cần DB thật (trừ ca ILIKE ở cuối, tự bỏ qua nếu thiếu TEST_DATABASE_URL):

  - escape `%`, `_`, `\\` khi tìm kiếm ILIKE (scrapjd/db/sql_like.py);
  - upload CV kiểm chữ ký `%PDF-` và đọc có trần;
  - `?api_key=` mặc định tắt, bật lại bằng ALLOW_QUERY_API_KEY=true;
  - /healthz công khai, có chạm DB, không lộ chi tiết lỗi.
"""

import io
import os
import sys
import uuid
from unittest.mock import MagicMock, patch
from urllib.parse import urlparse

import pytest
from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scrapjd.api import auth, error_codes
from scrapjd.api.routers import me as me_router
from scrapjd.db.sql_like import contains_pattern, escape_like

from conftest import make_fake_request

API_KEY = "test-key-123"


# ---------- escape_like ----------

@pytest.mark.parametrize(
    "raw, escaped",
    [
        ("abc", "abc"),
        ("100%", "100\\%"),
        ("a_b", "a\\_b"),
        ("a\\b", "a\\\\b"),  # backslash phải escape trước, không bị escape hai lần
        ("%_\\", "\\%\\_\\\\"),
        ("", ""),
    ],
)
def test_escape_like(raw, escaped):
    assert escape_like(raw) == escaped


def test_contains_pattern_bao_quanh_bang_percent():
    assert contains_pattern("50%_off") == "%50\\%\\_off%"


# ---------- upload CV ----------

def _apply(content: bytes, filename="cv.pdf"):
    conn = MagicMock()
    user = {"sub": str(uuid.uuid4()), "role": "user"}
    with patch.object(me_router.db_module, "is_valid_uuid", return_value=True), \
         patch.object(me_router.db_module, "get_job_by_id", return_value={"job_status": "OPEN"}), \
         patch.object(me_router.db_module, "create_job_application", return_value=str(uuid.uuid4())) as create, \
         patch.object(me_router.db_module, "set_application_cv_url"), \
         patch.object(me_router.cv_storage, "upload_cv", return_value="cv-files/x.pdf") as upload:
        try:
            me_router.apply_to_job(
                request=make_fake_request(), job_id=str(uuid.uuid4()), note=None,
                cv_file=UploadFile(file=io.BytesIO(content), filename=filename),
                user=user, conn=conn,
            )
            err = None
        except HTTPException as exc:
            err = exc
        except Exception:
            # Ca hợp lệ đi tiếp tới phần dựng kết quả (ngoài phạm vi test này): chỉ cần biết
            # đã qua kiểm tra file, tức đã gọi tạo bản ghi và upload.
            err = None
    return err, create, upload


def test_cv_html_doi_duoi_pdf_bi_tu_choi():
    err, create, upload = _apply(b"<html><script>alert(1)</script></html>")
    assert err is not None and err.status_code == 400
    assert err.detail["error_code"] == error_codes.PROFILE_CV_FORMAT_INVALID
    create.assert_not_called()
    upload.assert_not_called()


def test_cv_dung_pdf_qua_kiem_tra():
    err, create, upload = _apply(b"%PDF-1.7\n...")
    assert err is None
    create.assert_called_once()
    upload.assert_called_once()


def test_cv_qua_5mb_bi_tu_choi_truoc_khi_kiem_chu_ky():
    err, create, _ = _apply(b"%PDF-" + b"0" * (5 * 1024 * 1024))
    assert err is not None
    assert err.detail["error_code"] == error_codes.PROFILE_CV_FILE_TOO_LARGE
    create.assert_not_called()


def test_cv_dung_5mb_khong_bi_chan():
    err, create, _ = _apply(b"%PDF-" + b"0" * (5 * 1024 * 1024 - 5))
    assert err is None
    create.assert_called_once()


def test_cv_sai_duoi_van_bi_tu_choi():
    err, _, _ = _apply(b"%PDF-1.4", filename="cv.exe")
    assert err.detail["error_code"] == error_codes.PROFILE_CV_FORMAT_INVALID


# ---------- ?api_key= ----------

@pytest.fixture
def _key(monkeypatch):
    monkeypatch.setattr(auth, "_API_KEY", API_KEY)
    monkeypatch.delenv("ALLOW_QUERY_API_KEY", raising=False)


def test_query_key_mac_dinh_bi_tu_choi(_key):
    with pytest.raises(HTTPException) as exc:
        auth.require_api_key(header_key=None, query_key=API_KEY)
    assert exc.value.status_code == 401


def test_query_key_duoc_khi_bat_co(_key, monkeypatch):
    monkeypatch.setenv("ALLOW_QUERY_API_KEY", "true")
    assert auth.require_api_key(header_key=None, query_key=API_KEY) is None


def test_header_key_luon_dung_duoc(_key):
    assert auth.require_api_key(header_key=API_KEY, query_key=None) is None


def test_header_sai_khong_duoc_cuu_boi_query_khi_tat(_key):
    with pytest.raises(HTTPException):
        auth.require_api_key(header_key="sai", query_key=API_KEY)


# ---------- /healthz ----------

@pytest.fixture(scope="module")
def client():
    from scrapjd.api.app import app
    return TestClient(app)


def _conn_ok():
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = (1,)
    return conn


def test_healthz_cong_khai_khong_can_api_key(client):
    conn = _conn_ok()
    with patch("scrapjd.api.app.db_module.get_pooled_connection", return_value=conn), \
         patch("scrapjd.api.app.db_module.release_connection") as release:
        r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    release.assert_called_once_with(conn)


def test_healthz_503_khi_truy_van_loi_va_khong_lo_chi_tiet(client):
    conn = MagicMock()
    conn.cursor.side_effect = RuntimeError("password authentication failed for user secret_user")
    with patch("scrapjd.api.app.db_module.get_pooled_connection", return_value=conn), \
         patch("scrapjd.api.app.db_module.release_connection") as release:
        r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json() == {"status": "degraded"}
    assert "secret_user" not in r.text
    release.assert_called_once_with(conn)


def test_healthz_503_khi_khong_lay_duoc_connection(client):
    with patch("scrapjd.api.app.db_module.get_pooled_connection", side_effect=RuntimeError("pool het")):
        r = client.get("/healthz")
    assert r.status_code == 503
    assert r.json() == {"status": "degraded"}


def test_healthz_khong_vao_openapi(client):
    from scrapjd.api.app import app
    assert "/healthz" not in app.openapi()["paths"]


# ---------- ILIKE trên Postgres thật ----------

_TEST_DB = os.environ.get("TEST_DATABASE_URL")


@pytest.mark.skipif(not _TEST_DB, reason="Không đặt TEST_DATABASE_URL")
def test_ilike_khop_dung_ky_tu_dac_biet_tren_postgres():
    import psycopg2

    if "test" not in urlparse(_TEST_DB).path.lower():
        pytest.fail("Từ chối chạy: tên database không chứa 'test'")
    conn = psycopg2.connect(_TEST_DB)
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE TEMP TABLE t(title text)")
            cur.executemany(
                "INSERT INTO t VALUES (%s)",
                [("Sale 100% off",), ("Sale 100 off",), ("data_engineer",), ("dataXengineer",), ("a\\b",), ("ab",)],
            )

            def find(kw):
                cur.execute("SELECT title FROM t WHERE title ILIKE %s ORDER BY 1", (contains_pattern(kw),))
                return [r[0] for r in cur.fetchall()]

            assert find("%") == ["Sale 100% off"]
            assert find("_") == ["data_engineer"]
            assert find("a\\b") == ["a\\b"]  # không lỗi "LIKE pattern must not end with escape character"
            assert find("\\") == ["a\\b"]
            assert find("DATA_ENGINEER") == ["data_engineer"]  # vẫn không phân biệt hoa/thường
    finally:
        conn.rollback()
        conn.close()
