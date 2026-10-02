"""
Cả repo dùng đúng MỘT thư viện HTTP: curl_cffi (10/2026).

Trước đây api/storage.py import `requests` thật mà requirements.txt chỉ
nhắc qua trong comment, chạy được là nhờ thư viện khác cài kèm. Các adapter
thì `from curl_cffi import requests` (đặt tên trùng `requests`, rất dễ nhìn
nhầm là thư viện thật). Test này chặn việc quay lại import `requests` thật.

Đọc mã nguồn bằng AST, không cần mạng hay DB.
"""

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".ruff_cache", ".pytest_cache"}


def _python_files():
    for path in ROOT.rglob("*.py"):
        if not (set(path.relative_to(ROOT).parts) & _SKIP_DIRS):
            yield path


def _imports_real_requests(tree: ast.AST) -> list:
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hits += [node.lineno for a in node.names if a.name == "requests" or a.name.startswith("requests.")]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if node.module == "requests" or (node.module or "").startswith("requests."):
                hits.append(node.lineno)
    return hits


def test_no_module_imports_the_real_requests_package():
    offenders = []
    for path in sorted(_python_files()):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for lineno in _imports_real_requests(tree):
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}")
    assert not offenders, (
        "Import `requests` thật — dùng `from curl_cffi import requests as curl_requests`:\n"
        + "\n".join(offenders)
    )


def test_requirements_declares_curl_cffi_and_not_requests():
    lines = [
        ln.split("#", 1)[0].strip().lower()
        for ln in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    ]
    names = {re.split(r"[<>=!~\[ ;]", ln, maxsplit=1)[0] for ln in lines if ln}
    assert "curl_cffi" in names
    assert "requests" not in names, "requirements.txt không cần khai báo `requests` nữa"
