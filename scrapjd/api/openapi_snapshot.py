"""Snapshot OpenAPI của API (Mục 2c): `openapi.snapshot.json` ở gốc repo là bản schema ĐÃ COMMIT, test so nó với
`app.openapi()` hiện tại. Frontend (nextjs-job-post) sinh `lib/api/types.ts` từ chính file này (Mục 2d), nên đổi
API mà quên cập nhật snapshot thì CI backend đỏ, thay vì để type bên frontend lệch âm thầm.

Vì sao không lấy schema qua `/openapi.json` đang chạy: route đó TẮT mặc định (ENABLE_DOCS, xem scrapjd/api/app.py) và
không đi qua X-API-Key. `app.openapi()` gọi thẳng trong Python thì không cần bật docs, không cần DB, không cần mạng
(đã kiểm: kết quả giống hệt nhau giữa các lần chạy, các PYTHONHASHSEED khác nhau và khi đổi ENABLE_DOCS/API_KEY/...).

CÁCH DÙNG (chạy từ gốc repo):
    python -m scrapjd.api.openapi_snapshot            # sinh lại và ghi openapi.snapshot.json
    python -m scrapjd.api.openapi_snapshot --check    # chỉ so, thoát mã 1 nếu lệch (không ghi gì)

Khi nào phải sinh lại: sau khi thêm/sửa/xoá route, tham số, body hoặc schema (pydantic model) của API. Commit file
snapshot CÙNG commit với thay đổi API để review thấy đúng phần API đổi.

LƯU Ý: kết quả phụ thuộc phiên bản fastapi/pydantic/starlette. requirements.txt chưa ghim các thư viện này, nên bản
mới của chúng có thể làm test đỏ dù code API không đổi (cùng lý do ghi ở bước mypy trong .github/workflows/test.yml,
cách xử lý gốc là lockfile). Khi đó đọc phần tóm tắt trong lỗi: nếu chỉ là thay đổi cách sinh schema thì sinh lại
snapshot và nhớ rằng types.ts bên frontend cũng sẽ đổi theo.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

# Gốc repo = hai cấp trên thư mục này (scrapjd/api/ -> scrapjd/ -> gốc).
SNAPSHOT_PATH = Path(__file__).resolve().parents[2] / "openapi.snapshot.json"

SNAPSHOT_COMMAND = "python -m scrapjd.api.openapi_snapshot"

_HTTP_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")

# Số dòng tối đa trong phần tóm tắt khác biệt (schema lệch lớn thì đừng in cả trăm dòng ra log CI).
_MAX_SUMMARY_LINES = 40


def build_schema() -> dict[str, Any]:
    """Schema OpenAPI hiện tại của app, dạng dict thuần JSON.

    Import app trong hàm (không ở đầu file) để import module này không kéo theo cả app và các yêu cầu môi trường của
    nó (JWT_SECRET_KEY). Đi qua json.dumps/loads để có bản sao tách khỏi cache `app.openapi_schema`, tránh ai đó sửa
    nhầm vào schema dùng chung của app."""
    from scrapjd.api.app import app

    schema: dict[str, Any] = json.loads(json.dumps(app.openapi()))
    return schema


def render(schema: dict[str, Any]) -> str:
    """Chuỗi JSON để ghi ra file.

    KHÔNG sort_keys: giữ thứ tự FastAPI sinh ra (theo thứ tự khai báo route), để types.ts sinh từ snapshot có cùng
    thứ tự với types.ts sinh từ server đang chạy, đỡ đổi cả ngàn dòng chỉ vì thứ tự. Thứ tự này ổn định giữa các
    lần chạy. `ensure_ascii=False` để tiếng Việt trong mô tả đọc được khi xem diff."""
    return json.dumps(schema, ensure_ascii=False, indent=2) + "\n"


def load_snapshot(path: Path = SNAPSHOT_PATH) -> dict[str, Any]:
    """Đọc snapshot đã commit. Mở ở chế độ text nên CRLF do git trên Windows (autocrlf) được đổi về LF, và vì test so
    dict đã parse chứ không so chuỗi nên khoảng trắng/xuống dòng cũng không làm lệch."""
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def _operations(schema: dict[str, Any]) -> dict[str, Any]:
    """Gom mọi operation thành {"POST /jobs": {...}}."""
    ops: dict[str, Any] = {}
    for path, item in schema.get("paths", {}).items():
        for method in _HTTP_METHODS:
            if method in item:
                ops[f"{method.upper()} {path}"] = item[method]
    return ops


def summarize_diff(old: dict[str, Any], new: dict[str, Any]) -> list[str]:
    """Liệt kê ngắn gọn cái gì đổi giữa snapshot đã commit (`old`) và app hiện tại (`new`).

    Ký hiệu: `+` mới có ở app, `-` có trong snapshot nhưng app không còn, `~` cùng tên nhưng nội dung khác. Chỉ để
    người đọc lỗi CI biết đổi ở đâu, không phải diff đầy đủ (muốn đầy đủ thì sinh lại rồi `git diff`)."""
    lines: list[str] = []

    old_ops, new_ops = _operations(old), _operations(new)
    for key in sorted(new_ops.keys() - old_ops.keys()):
        lines.append(f"+ endpoint {key}")
    for key in sorted(old_ops.keys() - new_ops.keys()):
        lines.append(f"- endpoint {key}")
    for key in sorted(old_ops.keys() & new_ops.keys()):
        if old_ops[key] != new_ops[key]:
            lines.append(f"~ endpoint {key}")

    old_models = old.get("components", {}).get("schemas", {})
    new_models = new.get("components", {}).get("schemas", {})
    for name in sorted(new_models.keys() - old_models.keys()):
        lines.append(f"+ schema {name}")
    for name in sorted(old_models.keys() - new_models.keys()):
        lines.append(f"- schema {name}")
    for name in sorted(old_models.keys() & new_models.keys()):
        if old_models[name] != new_models[name]:
            lines.append(f"~ schema {name}")

    # Phần còn lại ngoài paths và components.schemas (openapi, info, securitySchemes, ...).
    def rest(schema: dict[str, Any]) -> dict[str, Any]:
        trimmed = {k: v for k, v in schema.items() if k not in ("paths", "components")}
        trimmed["components"] = {k: v for k, v in schema.get("components", {}).items() if k != "schemas"}
        return trimmed

    if rest(old) != rest(new):
        lines.append("~ phần khác (openapi, info, securitySchemes, ...)")

    # Phòng chỗ lệch mà các nhóm trên không phân loại được (vd khoá cấp path như `parameters` dùng chung): vẫn phải báo
    # lệch, vì verdict của test và của --check là `old != new`, phần tóm tắt chỉ để đọc.
    if old != new and not lines:
        lines.append("~ khác biệt không phân loại được; sinh lại snapshot rồi xem bằng git diff")

    if len(lines) > _MAX_SUMMARY_LINES:
        extra = len(lines) - _MAX_SUMMARY_LINES
        lines = lines[:_MAX_SUMMARY_LINES] + [f"... và {extra} khác biệt nữa"]
    return lines


def main(argv: Sequence[str] | None = None, path: Path = SNAPSHOT_PATH) -> int:
    parser = argparse.ArgumentParser(description="Sinh hoặc kiểm tra openapi.snapshot.json")
    parser.add_argument("--check", action="store_true", help="chỉ so với file đã commit, không ghi; lệch thì thoát mã 1")
    args = parser.parse_args(argv)

    # app.py raise RuntimeError ngay lúc import nếu thiếu JWT_SECRET_KEY (xem scrapjd/api/security.py). Giá trị này chỉ
    # để import được, không ký token nào; nếu .env hoặc môi trường đã có thì giữ nguyên.
    os.environ.setdefault("JWT_SECRET_KEY", "openapi_snapshot_placeholder_not_a_secret")

    current = build_schema()

    if args.check:
        if not path.exists():
            print(f"Chưa có {path.name}. Sinh bằng: {SNAPSHOT_COMMAND}", file=sys.stderr)
            return 1
        diff = summarize_diff(load_snapshot(path), current)
        if diff:
            print(f"{path.name} lệch so với app hiện tại:", file=sys.stderr)
            print("\n".join(diff), file=sys.stderr)
            print(f"Sinh lại bằng: {SNAPSHOT_COMMAND}", file=sys.stderr)
            return 1
        print(f"{path.name} khớp với app.")
        return 0

    # newline="\n": ghi LF cả trên Windows để file giống nhau giữa các máy.
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(render(current))
    print(f"Đã ghi {path} ({len(_operations(current))} endpoint).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
