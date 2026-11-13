"""
Test snapshot OpenAPI (Mục 2c) — xem docstring scrapjd/api/openapi_snapshot.py để biết bối cảnh.

Test quan trọng nhất là `test_snapshot_khop_voi_app`: nếu ai đổi route/tham số/schema của API mà không sinh lại
`openapi.snapshot.json` thì CI đỏ, kèm danh sách endpoint/schema đã đổi và lệnh sinh lại. Nhờ vậy bản types.ts bên
frontend (sinh từ chính snapshot này) không lệch âm thầm so với backend.

Không cần DB, không cần mạng. Chạy: pytest tests/test_openapi_snapshot.py -v
"""
import json
import os

# scrapjd/api/app.py raise RuntimeError khi import nếu thiếu JWT_SECRET_KEY — giống các test khác (xem
# tests/test_request_size_limit.py): set trước khi import app.
os.environ.setdefault("JWT_SECRET_KEY", "test_secret_not_used_in_production")

import pytest

from scrapjd.api import openapi_snapshot as snap
from scrapjd.api.openapi_snapshot import build_schema, load_snapshot, main, render, summarize_diff


@pytest.fixture(scope="module")
def current_schema():
    return build_schema()


# ---------------------------------------------------------------------------
# Snapshot đã commit so với app
# ---------------------------------------------------------------------------

def test_snapshot_khop_voi_app(current_schema):
    assert snap.SNAPSHOT_PATH.exists(), (
        f"Chưa có {snap.SNAPSHOT_PATH.name}. Sinh bằng: {snap.SNAPSHOT_COMMAND}"
    )
    committed = load_snapshot()
    if committed != current_schema:
        summary = "\n".join(summarize_diff(committed, current_schema))
        pytest.fail(
            f"{snap.SNAPSHOT_PATH.name} lệch so với API hiện tại.\n{summary}\n\n"
            f"Nếu thay đổi API này là CÓ CHỦ Ý: chạy `{snap.SNAPSHOT_COMMAND}` rồi commit file snapshot cùng "
            f"thay đổi API (frontend sẽ sinh lại types.ts từ file này).\n"
            f"Nếu không chủ ý: có thể do nâng phiên bản fastapi/pydantic (requirements.txt chưa ghim), xem docstring "
            f"scrapjd/api/openapi_snapshot.py."
        )


def test_build_schema_on_dinh_giua_cac_lan_goi(current_schema):
    # Snapshot chỉ có nghĩa nếu cùng code cho ra cùng schema. Cố ý bỏ cache của app để buộc FastAPI dựng lại.
    from scrapjd.api.app import app

    app.openapi_schema = None
    assert build_schema() == current_schema


def test_build_schema_tra_ban_sao_khong_dung_chung_cache(current_schema):
    # Sửa bản trả về không được làm bẩn schema dùng chung của app (và test khác).
    a = build_schema()
    a["info"]["title"] = "bị sửa"
    assert build_schema()["info"]["title"] == current_schema["info"]["title"]


# ---------------------------------------------------------------------------
# Tính chất của schema mà openapi-typescript (frontend) phụ thuộc
# ---------------------------------------------------------------------------

def _operations(schema):
    return snap._operations(schema)


def test_operation_id_co_mat_va_duy_nhat(current_schema):
    # openapi-typescript đặt tên type trong `operations` theo operationId; trùng hoặc thiếu thì type bị ghi đè.
    ids = [op.get("operationId") for op in _operations(current_schema).values()]
    assert all(ids), "có endpoint thiếu operationId"
    assert len(ids) == len(set(ids)), "operationId bị trùng"


def test_alias_cu_khong_nam_trong_schema(current_schema):
    # Alias /me/applications/{id}/cv-url (chờ xoá sau cutover) cố ý include_in_schema=False để types.ts chỉ thấy
    # path mới (xem scrapjd/api/routers/me.py). Nếu ai bỏ cờ đó, frontend sẽ sinh type cho path sắp xoá.
    assert "/me/applications/{application_id}/cv-url" not in current_schema["paths"]


def test_schema_khong_chua_gia_tri_bi_mat(current_schema):
    # Snapshot nằm trong repo public: không được lẫn secret nếu ai đó nhét giá trị mặc định thật vào model/mô tả.
    text = render(current_schema)
    secret = os.environ["JWT_SECRET_KEY"]
    assert secret not in text
    for name in ("API_KEY", "PGPASSWORD", "TAVILY_API_KEY", "GEMINI_API_KEY"):
        value = os.environ.get(name)
        if value:
            assert value not in text


# ---------------------------------------------------------------------------
# render / load_snapshot
# ---------------------------------------------------------------------------

def test_render_giu_thu_tu_khoa_va_tieng_viet(current_schema):
    text = render(current_schema)
    assert text.endswith("}\n")
    assert "\r" not in text
    # không escape tiếng Việt (ensure_ascii=False) để diff đọc được
    assert "\\u" not in text
    assert json.loads(text) == current_schema
    # không sort_keys: "openapi" luôn đứng đầu như FastAPI sinh ra, không phải theo abc
    assert list(json.loads(text).keys()) == list(current_schema.keys())
    assert next(iter(json.loads(text))) == "openapi"


def test_load_snapshot_chiu_duoc_crlf(tmp_path):
    # git trên Windows (core.autocrlf) có thể đổi LF thành CRLF khi checkout; test không được đỏ vì chuyện đó.
    path = tmp_path / "s.json"
    path.write_bytes(b'{\r\n  "openapi": "3.1.0",\r\n  "paths": {}\r\n}\r\n')
    assert load_snapshot(path) == {"openapi": "3.1.0", "paths": {}}


# ---------------------------------------------------------------------------
# summarize_diff
# ---------------------------------------------------------------------------

def _mini(**paths):
    return {"openapi": "3.1.0", "info": {"title": "t"}, "paths": paths, "components": {"schemas": {}}}


def test_summarize_diff_bang_nhau_la_rong():
    a = _mini(**{"/a": {"get": {"operationId": "a"}}})
    assert summarize_diff(a, json.loads(json.dumps(a))) == []


def test_summarize_diff_endpoint_them_bot_va_doi():
    old = _mini(**{"/a": {"get": {"operationId": "a"}}, "/b": {"get": {"operationId": "b"}}})
    new = _mini(**{"/a": {"get": {"operationId": "a2"}}, "/c": {"post": {"operationId": "c"}}})
    assert summarize_diff(old, new) == ["+ endpoint POST /c", "- endpoint GET /b", "~ endpoint GET /a"]


def test_summarize_diff_phan_biet_method_cung_path():
    old = _mini(**{"/a": {"get": {"operationId": "a"}}})
    new = _mini(**{"/a": {"get": {"operationId": "a"}, "delete": {"operationId": "d"}}})
    assert summarize_diff(old, new) == ["+ endpoint DELETE /a"]


def test_summarize_diff_schema_them_bot_va_doi():
    old = _mini()
    old["components"]["schemas"] = {"Cu": {"type": "object"}, "Giu": {"type": "object"}}
    new = _mini()
    new["components"]["schemas"] = {"Moi": {"type": "object"}, "Giu": {"type": "object", "required": ["x"]}}
    assert summarize_diff(old, new) == ["+ schema Moi", "- schema Cu", "~ schema Giu"]


def test_summarize_diff_bat_thay_doi_ngoai_paths_va_schemas():
    old = _mini()
    new = _mini()
    new["info"]["title"] = "khác"
    assert len(summarize_diff(old, new)) == 1
    new2 = _mini()
    new2["components"]["securitySchemes"] = {"k": {"type": "apiKey"}}
    assert len(summarize_diff(old, new2)) == 1


def test_summarize_diff_luon_bao_lech_ke_ca_khi_khong_phan_loai_duoc():
    # Khoá cấp path (vd `parameters` dùng chung) không thuộc nhóm nào ở trên nhưng vẫn là thay đổi thật.
    old = _mini(**{"/a": {"get": {"operationId": "a"}}})
    new = _mini(**{"/a": {"get": {"operationId": "a"}, "parameters": [{"name": "x"}]}})
    out = summarize_diff(old, new)
    assert len(out) == 1 and out[0].startswith("~")


def test_summarize_diff_cat_ngan_khi_qua_dai():
    old = _mini()
    new = _mini(**{f"/p{i}": {"get": {"operationId": f"p{i}"}} for i in range(100)})
    out = summarize_diff(old, new)
    assert len(out) == 41
    assert out[-1] == "... và 60 khác biệt nữa"


# ---------------------------------------------------------------------------
# Lệnh sinh / kiểm (main) — dùng file tạm, KHÔNG đụng openapi.snapshot.json thật
# ---------------------------------------------------------------------------

def test_main_ghi_roi_check_khop(tmp_path, capsys):
    path = tmp_path / "snap.json"
    assert main([], path=path) == 0
    assert load_snapshot(path) == build_schema()
    assert main(["--check"], path=path) == 0
    assert "khớp" in capsys.readouterr().out


def test_main_check_bao_loi_khi_thieu_file(tmp_path, capsys):
    assert main(["--check"], path=tmp_path / "khong_co.json") == 1
    assert snap.SNAPSHOT_COMMAND in capsys.readouterr().err


def test_main_check_bao_loi_khi_lech_va_khong_ghi_de(tmp_path, capsys):
    path = tmp_path / "snap.json"
    stale = build_schema()
    del stale["paths"]["/health"]
    path.write_text(render(stale), encoding="utf-8")
    before = path.read_text(encoding="utf-8")

    assert main(["--check"], path=path) == 1
    err = capsys.readouterr().err
    assert "+ endpoint GET /health" in err
    assert snap.SNAPSHOT_COMMAND in err
    assert path.read_text(encoding="utf-8") == before  # --check không được ghi


def test_main_ghi_de_snapshot_cu(tmp_path):
    path = tmp_path / "snap.json"
    path.write_text('{"cu": true}\n', encoding="utf-8")
    assert main([], path=path) == 0
    assert load_snapshot(path) == build_schema()
