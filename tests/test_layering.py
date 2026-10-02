"""
Kiểm tra ranh giới lớp: api/routers/ chỉ điều phối HTTP, KHÔNG chứa SQL thô.
SQL phải nằm ở db/ (nơi có test riêng và gom được theo domain).

Test này đọc mã nguồn, không cần DB. Nếu cần ngoại lệ, đưa SQL xuống db/
thay vì nới danh sách này.
"""

import re
from pathlib import Path

ROUTERS_DIR = Path(__file__).resolve().parent.parent / "api" / "routers"

# `conn.cursor(...)` hoặc `cur.execute(...)` / `.executemany(` trong code route
_RAW_SQL = re.compile(r"\.cursor\(|\.execute(?:many)?\(")


def test_routers_contain_no_raw_sql():
    offenders = []
    for path in sorted(ROUTERS_DIR.glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if _RAW_SQL.search(code):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "SQL thô trong api/routers/ — chuyển xuống db/ rồi gọi qua db_module:\n"
        + "\n".join(offenders)
    )
