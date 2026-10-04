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


# VietnamWorks KHÔNG dùng HTTP 3xx/404/410 mà trả HTTP 200 kèm một trang Next.js
# không có dữ liệu job, trong luồng có lệnh chuyển hướng phía client dạng
# `NEXT_REDIRECT;replace;<url>;307;`. Hai trường hợp thật đã gặp (10/2026):
#   - chuyển sang https://www.vietnamworks.com/410: trang báo "có thể đã bị xóa
#     hoặc tạm thời không hỗ trợ" (job 2089555, 2102990; file thật
#     tests/fixture_vietnamworks_gone.html);
#   - chuyển sang CÙNG job nhưng slug mới: nhà tuyển dụng sửa tiêu đề nên slug
#     đổi, URL cũ vẫn lưu trong DB (job 2104572, 2104545, 2102744). Trình duyệt
#     tự đi theo nên trang vẫn mở bình thường, còn HTTP client thì không.
_REDIRECT = re.compile(r"NEXT_REDIRECT;\w+;(https?://[^;\"\\\s]*);")
_JOB_ID_IN_URL = re.compile(r"-(\d+)-jv(?:[/?#]|$)")


def redirect_target(html: str) -> Optional[str]:
    """Địa chỉ mà trang yêu cầu chuyển hướng tới, hoặc None. Chỉ nên tin khi
    parse_detail_page() đã trả None: trang job thật không chứa lệnh này."""
    m = _REDIRECT.search(html or "")
    return m.group(1) if m else None


def job_id_from_url(url: str) -> Optional[str]:
    """Mã số job ở cuối URL VietnamWorks (...-<jobId>-jv), hoặc None."""
    m = _JOB_ID_IN_URL.search(url or "")
    return m.group(1) if m else None


def is_gone_page(html: str) -> bool:
    """True nếu trang chuyển hướng sang /410. LƯU Ý: đây là tín hiệu "có vẻ đã gỡ",
    chưa phải bằng chứng chắc chắn job đã đóng (chính trang đó ghi "có thể đã bị
    xóa hoặc tạm thời không hỗ trợ"), nên không dùng để tự đóng job."""
    target = redirect_target(html)
    return bool(target) and target.rstrip("/").endswith("/410")


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
