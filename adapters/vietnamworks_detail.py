"""
Giải mã trang chi tiết job VietnamWorks (https://www.vietnamworks.com/<slug>-<id>-jv).

Vì sao cần: API search chỉ trả jobDescription/jobRequirement đã bị CẮT ở
~250-570 ký tự (kết thúc bằng "...", 49/50 job trong mẫu thật 10/2026) và
yearsOfExperience luôn bằng 0. Trang chi tiết có bản đầy đủ của cả hai
(xác nhận bằng file thật job 2109772: yêu cầu 1859 byte, yearsOfExperience=3).

Cấu trúc trang (xác nhận 10/2026): KHÔNG có __NEXT_DATA__ và KHÔNG có JSON-LD.
Dữ liệu nằm trong luồng React Server Components của Next.js, gửi qua các lệnh
`self.__next_f.push([1, "<chuỗi>"])` trong thẻ <script>. Nối các chuỗi lại ta
được một luồng gồm nhiều "dòng":

    <id hex>:<JSON>\\n                 dòng JSON (kết thúc bằng xuống dòng)
    <id hex>:T<độ dài hex>,<văn bản>   dòng văn bản dài, độ dài tính bằng BYTE
                                       UTF-8, KHÔNG có xuống dòng phía sau

Object job nằm trong một dòng JSON, nhưng chuỗi dài (jobDescription,
jobRequirement...) và danh sách (skills, benefits...) được thay bằng tham
chiếu "$<id hex>" trỏ sang dòng khác. parse_detail_page() giải các tham chiếu
đó rồi trả về dict job đầy đủ, cùng tên khoá với API search.

Mọi lỗi (trang đổi cấu trúc, bị chặn trả HTML khác...) đều trả None, KHÔNG
raise: nơi gọi quyết định xử lý (xem VietnamWorksAdapter._fetch_detail_job).
"""

import json
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Một lệnh push([1, "<chuỗi JSON>"]). Khớp theo cú pháp chuỗi JSON (ký tự thường
# hoặc cặp \\x), không dựa vào thẻ </script> phía sau.
_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,("(?:[^"\\]|\\.)*")\]\)')
_ROW_ID_RE = re.compile(rb"([0-9a-f]+):")
_TEXT_HEAD_RE = re.compile(rb"T([0-9a-f]+),")
_REF_RE = re.compile(r"\$([0-9a-f]+)")

# Chặn đệ quy vô hạn nếu dữ liệu có tham chiếu vòng.
_MAX_REF_DEPTH = 20


def _join_stream(html: str) -> Optional[str]:
    parts = []
    for m in _PUSH_RE.finditer(html):
        try:
            parts.append(json.loads(m.group(1)))
        except ValueError:
            return None
    return "".join(parts) if parts else None


def _split_rows(stream: str) -> dict:
    """Luồng -> {id: ("T", văn bản) | ("J", chuỗi JSON/khác)}."""
    data = stream.encode("utf-8")
    rows = {}
    pos = 0
    size = len(data)
    while pos < size:
        head = _ROW_ID_RE.match(data, pos)
        if head is None:
            # Ký tự lạ ở đầu dòng: bỏ qua tới dòng kế để không kẹt vòng lặp.
            newline = data.find(b"\n", pos)
            if newline < 0:
                break
            pos = newline + 1
            continue
        row_id = head.group(1).decode()
        cursor = head.end()
        text_head = _TEXT_HEAD_RE.match(data, cursor) if data[cursor:cursor + 1] == b"T" else None
        if text_head is not None:
            length = int(text_head.group(1), 16)
            start = text_head.end()
            rows[row_id] = ("T", data[start:start + length].decode("utf-8", "replace"))
            pos = start + length
        else:
            newline = data.find(b"\n", cursor)
            end = size if newline < 0 else newline
            rows[row_id] = ("J", data[cursor:end].decode("utf-8", "replace"))
            pos = end + 1
    return rows


def _resolve(value: Any, rows: dict, depth: int = 0) -> Any:
    """Thay mọi chuỗi "$<id>" bằng nội dung dòng tương ứng (đệ quy)."""
    if depth > _MAX_REF_DEPTH:
        return value
    if isinstance(value, str):
        if value.startswith("$$"):  # "$$" là cách React escape một dấu "$" đứng đầu
            return value[1:]
        m = _REF_RE.fullmatch(value)
        if m and m.group(1) in rows:
            kind, body = rows[m.group(1)]
            if kind == "T":
                return body
            try:
                return _resolve(json.loads(body), rows, depth + 1)
            except ValueError:
                return value
        return value
    if isinstance(value, list):
        return [_resolve(item, rows, depth + 1) for item in value]
    if isinstance(value, dict):
        return {key: _resolve(item, rows, depth + 1) for key, item in value.items()}
    return value


def parse_detail_page(html: str) -> Optional[dict]:
    """HTML trang chi tiết -> dict job đầy đủ (tên khoá như API search), hoặc
    None nếu không tìm thấy object job (trang đổi cấu trúc / không phải trang job).

    Object job được nhận ra bằng việc có đủ khoá jobId + jobTitle +
    jobRequirement; nếu có nhiều ứng viên thì lấy dòng dài nhất."""
    try:
        stream = _join_stream(html or "")
        if not stream:
            return None
        rows = _split_rows(stream)
        best = None
        for kind, body in rows.values():
            if kind != "J" or not body.startswith("{"):
                continue
            if '"jobRequirement"' not in body or '"jobId"' not in body:
                continue
            try:
                candidate = json.loads(body)
            except ValueError:
                continue
            if isinstance(candidate, dict) and "jobTitle" in candidate:
                if best is None or len(body) > best[0]:
                    best = (len(body), candidate)
        if best is None:
            return None
        job = _resolve(best[1], rows)
        return job if isinstance(job, dict) else None
    except Exception:  # noqa: BLE001 - parser không được làm hỏng lượt crawl
        logger.exception("Giải mã trang chi tiết VietnamWorks lỗi bất ngờ")
        return None
