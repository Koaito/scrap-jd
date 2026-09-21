"""Validator dùng CHUNG cho nhiều schema khác nhau trong package này.

Tách ra (migrate Next.js, Phần 1 mục 3.2 của plan) — trước đây
`_note_not_blank` bị định nghĩa LẶP LẠI y hệt nhau ở 4 nơi
(api/schemas/audit_logs.py, email_templates.py, contacts.py,
companies.py), và `ImportConfirmRequest.note`
(api/schemas/import_export.py) lại KHÔNG có validator này — chỉ dùng
`min_length=1` nên chấp nhận chuỗi toàn khoảng trắng (`"   "`) là hợp
lệ, khác 4 chỗ kia. Gom về 1 hàm gốc duy nhất ở đây để dùng lại đúng 1
lần, tránh lệch nhau khi có chỗ quên sửa theo trong tương lai.
"""


def validate_note_not_blank(v: str) -> str:
    """Chặn note toàn khoảng trắng (vd `"   "`) — `min_length=1` của
    Pydantic chỉ đếm SỐ KÝ TỰ, không tự chặn được trường hợp này, vì
    note toàn khoảng trắng thực chất tương đương "không có note".

    Cách dùng trong 1 schema (Pydantic v2 hỗ trợ tái sử dụng hàm
    validator độc lập như 1 class attribute, không cần copy lại thân
    hàm ở từng model):

        from pydantic import field_validator
        from api.schemas.validators import validate_note_not_blank

        class FooRequest(BaseModel):
            note: str = Field(..., min_length=1)
            _note_not_blank = field_validator("note")(validate_note_not_blank)
    """
    v = v.strip()
    if not v:
        raise ValueError("note không được để trống hoặc chỉ chứa khoảng trắng")
    return v
