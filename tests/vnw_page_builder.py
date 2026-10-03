"""
Dựng trang chi tiết VietnamWorks GIẢ đúng định dạng thật (luồng React Server
Components của Next.js, xem adapters/vietnamworks_detail.py) để test không cần
mạng. Định dạng đối chiếu với trang thật job 2109772 (tests/
fixture_vietnamworks_detail.html), gồm các điểm dễ sai:

  - chuỗi dài (jobDescription, jobRequirement, companyProfile) nằm ở dòng
    "<id>:T<độ dài hex>," với độ dài tính bằng BYTE UTF-8 (tiếng Việt có dấu
    nhiều byte hơn số ký tự) và KHÔNG có xuống dòng phía sau;
  - skills/benefits là danh sách tham chiếu "$<id>" tới từng phần tử;
  - luồng được chia thành nhiều lệnh self.__next_f.push([1, "..."]).

Import kiểu `from vnw_page_builder import build_detail_html` (cùng cách các test
đang import `from conftest import ...`).
"""

import json

_LONG_TEXT_FIELDS = ("jobDescription", "jobRequirement", "companyProfile")
_LIST_FIELDS = ("skills", "benefits")


def build_detail_html(job: dict, *, split_into: int = 3, decoy_rows: bool = True) -> str:
    """job: dict kiểu response search (jobId, jobTitle, jobRequirement...). Trả
    HTML trang chi tiết giả chứa job đó."""
    rows = []
    next_id = [0x20]

    def new_id() -> str:
        next_id[0] += 1
        return format(next_id[0], "x")

    if decoy_rows:
        rows.append('1:HL["https://www.vietnamworks.com/assets/font.woff2","font",{"crossOrigin":""}]\n')
        rows.append('2:I[95751,[],""]\n')

    body = {}
    for key, value in job.items():
        if key in _LONG_TEXT_FIELDS and isinstance(value, str) and value:
            ref = new_id()
            raw = value.encode("utf-8")
            rows.append(f"{ref}:T{len(raw):x},{value}")
            body[key] = f"${ref}"
        elif key in _LIST_FIELDS and isinstance(value, list) and value:
            item_refs = []
            for item in value:
                item_ref = new_id()
                rows.append(f"{item_ref}:{json.dumps(item, ensure_ascii=False)}\n")
                item_refs.append(f"${item_ref}")
            list_ref = new_id()
            rows.append(f"{list_ref}:{json.dumps(item_refs)}\n")
            body[key] = f"${list_ref}"
        else:
            body[key] = value

    job_ref = new_id()
    rows.append(f"{job_ref}:{json.dumps(body, ensure_ascii=False)}\n")
    if decoy_rows:
        rows.append(f'{new_id()}:["$","div",null,{{"children":"$L{job_ref}"}}]\n')

    stream = "".join(rows)
    size = max(1, len(stream) // max(1, split_into))
    chunks = [stream[i:i + size] for i in range(0, len(stream), size)]
    scripts = "".join(
        f"<script>self.__next_f.push([1,{json.dumps(chunk, ensure_ascii=False)}])</script>"
        for chunk in chunks
    )
    return (
        "<!DOCTYPE html><html><head><title>Tuyển dụng</title></head><body><div id=\"root\"></div>"
        '<script>self.__next_f=self.__next_f||[]</script>'
        f"{scripts}</body></html>"
    )
