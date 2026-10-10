"""
db.pg_types — kiểu và vài hàm đọc dòng dùng chung cho tầng truy vấn PostgreSQL (M2a).

Không import gì từ scrapjd (chỉ psycopg2), nên import từ bất kỳ module nào cũng
không gây vòng import.

Kiểu:
- Conn: connection psycopg2 (cái `get_connection()` / `get_pooled_connection()`
  trả về và mọi hàm db nhận qua tham số `conn`). Gắn kiểu thật thay vì để trống
  để mypy kiểm được `conn.cursor()`, `fetchone()`, ... bên trong từng hàm.
- Row: một dòng kết quả đọc bằng `RealDictCursor` (khoá là tên cột). Thực chất
  là `psycopg2.extras.RealDictRow` (lớp con của dict), khai là dict thường vì
  chỗ gọi chỉ cần giao diện dict.

Hàm đọc dòng: bọc `fetchone()` / `fetchall()` của psycopg2, trả ĐÚNG giá trị
psycopg2 trả (không sao chép, không đổi kiểu lúc chạy). Việc duy nhất chúng làm
thêm là nói cho mypy biết điều câu SQL đã bảo đảm, nên chỗ gọi khỏi lặp
`assert row is not None`:
- fetch_scalar / fetch_one_row: dùng cho câu SQL CHẮC CHẮN trả đúng 1 dòng
  (`INSERT ... RETURNING`, `SELECT count(*)` không GROUP BY, `current_setting()`).
  Nếu không có dòng thì raise AssertionError kèm lời nhắc, thay cho
  `TypeError: 'NoneType' object is not subscriptable` khó đọc như trước.
- fetch_optional_row: dòng CÓ THỂ không tồn tại (tra theo khoá), trả None như cũ.
- fetch_all_rows: danh sách dòng của RealDictCursor khai là list[Row].
"""

from typing import Any, TypeAlias, cast

from psycopg2.extensions import connection, cursor
from psycopg2.extras import RealDictCursor

__all__ = [
    "Conn",
    "Row",
    "fetch_scalar",
    "fetch_one_row",
    "fetch_optional_row",
    "fetch_all_rows",
]

Conn: TypeAlias = connection
Row: TypeAlias = dict[str, Any]


def fetch_scalar(cur: cursor) -> Any:
    """Cột đầu tiên của dòng duy nhất mà câu SQL vừa chạy chắc chắn trả về."""
    row = cur.fetchone()
    assert row is not None, "câu SQL phải trả đúng 1 dòng nhưng không có dòng nào"
    return row[0]


def fetch_one_row(cur: RealDictCursor) -> Row:
    """Dòng duy nhất mà câu SQL vừa chạy chắc chắn trả về (đọc bằng RealDictCursor)."""
    row = cur.fetchone()
    assert row is not None, "câu SQL phải trả đúng 1 dòng nhưng không có dòng nào"
    return cast(Row, row)


def fetch_optional_row(cur: RealDictCursor) -> Row | None:
    """Dòng đầu tiên, hoặc None nếu câu SQL không trả dòng nào."""
    return cast("Row | None", cur.fetchone())


def fetch_all_rows(cur: RealDictCursor) -> list[Row]:
    """Mọi dòng còn lại của cursor, khai là list[Row] thay vì list[RealDictRow]."""
    return cast("list[Row]", cur.fetchall())
