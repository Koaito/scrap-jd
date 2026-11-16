"""Sinh lại constraints.txt: ghim các thư viện GIÁN TIẾP (starlette, pydantic, numpy...) theo cây đã cài.

Thư viện trực tiếp đã ghim `==` ngay trong requirements.txt / requirements-dev.txt nên KHÔNG lặp lại ở constraints
(để Dependabot nâng một thư viện trực tiếp bằng cách sửa đúng một dòng, không đụng constraints).

Dùng khi nâng bản một thư viện (sửa số `==` trong requirements*.txt trước):

    python scripts/refresh_constraints.py

Script tạo venv sạch trong thư mục tạm, cài từ bản sao của hai file requirements đã bỏ dòng `-c` (để ràng buộc
cũ không ghim lại bản cũ), rồi `pip freeze` ra constraints.txt. Chạy bằng cùng bản Python với CI (3.12): bản
Python khác có thể kéo bản wheel khác. Sau đó chạy `ruff check .`, `mypy`, `pytest` trước khi commit.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP = {"pip", "setuptools", "wheel"}
HEADER = """\
# SINH TỰ ĐỘNG bởi scripts/refresh_constraints.py, KHÔNG sửa tay. Ghim thư viện GIÁN TIẾP (trực tiếp đã ghim ở requirements*.txt) để mỗi lần
# build ra đúng cùng một cây phiên bản. requirements.txt đọc file này qua `-c`. Thư viện nào chưa cài thì
# constraints bỏ qua, nên các gói chỉ có trên một hệ điều hành (uvloop, colorama...) không gây lỗi ở hệ khác.
"""


def _direct_names(*texts: str) -> set[str]:
    names: set[str] = set()
    for text in texts:
        for ln in text.splitlines():
            m = re.match(r"^([A-Za-z0-9][A-Za-z0-9_.-]*)", ln.strip())
            if m and not ln.lstrip().startswith(("#", "-")):
                names.add(re.sub(r"[-_.]+", "-", m.group(1)).lower())
    return names


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        req = [
            ln
            for ln in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if not ln.lstrip().startswith("-c")
        ]
        (t / "req.txt").write_text("\n".join(req) + "\n", encoding="utf-8")
        dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
        assert "-r requirements.txt" in dev, "requirements-dev.txt phải có dòng '-r requirements.txt'"
        (t / "dev.txt").write_text(dev.replace("-r requirements.txt", "-r req.txt"), encoding="utf-8")
        subprocess.run([sys.executable, "-m", "venv", str(t / "venv")], check=True)
        py = str(t / "venv" / ("Scripts" if sys.platform == "win32" else "bin") / "python")
        subprocess.run([py, "-m", "pip", "install", "-q", "-r", str(t / "dev.txt")], check=True)
        frozen = subprocess.run([py, "-m", "pip", "freeze"], check=True, capture_output=True, text=True).stdout
    direct = _direct_names("\n".join(req), dev)
    lines = sorted(
        (
            ln
            for ln in frozen.splitlines()
            if ln and re.sub(r"[-_.]+", "-", ln.split("==")[0]).lower() not in SKIP | direct
        ),
        key=str.lower,
    )
    (ROOT / "constraints.txt").write_text(HEADER + "\n".join(lines) + "\n", encoding="utf-8")
    print(f"Đã ghi constraints.txt ({len(lines)} thư viện).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
