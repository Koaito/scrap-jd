"""Dựng mẫu cho `ILIKE` từ chuỗi người dùng gõ.

Trong LIKE/ILIKE, `%` khớp mọi chuỗi và `_` khớp đúng một ký tự bất kỳ. Nếu ghép thẳng `f"%{keyword}%"`
thì gõ `_` hoặc `%` sẽ khớp gần như mọi dòng, và gõ `\\` làm Postgres báo lỗi "LIKE pattern must not end
with escape character". Hàm này escape cả ba ký tự bằng `\\` (ký tự escape mặc định của Postgres, nên
câu SQL không cần thêm mệnh đề ESCAPE).
"""

_SPECIALS = ("\\", "%", "_")


def escape_like(text: str) -> str:
    """Escape `\\`, `%`, `_` để chúng được hiểu là ký tự thường. `\\` phải xử lý trước."""
    for ch in _SPECIALS:
        text = text.replace(ch, "\\" + ch)
    return text


def contains_pattern(text: str) -> str:
    """Mẫu `%…%` khớp mọi chuỗi chứa nguyên văn `text` (dùng làm tham số cho `ILIKE %s`)."""
    return f"%{escape_like(text)}%"
