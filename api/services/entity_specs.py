"""
Field spec DÙNG CHUNG cho Import + Export của 3 entity (Job/Company/
Contact) — 1 nguồn sự thật duy nhất cho: cột nào bắt buộc, cột nào xuất
ra file export, enum nào hợp lệ. validation_engine.py, file_parser.py
(export columns), conflict_detector.py và import_executor.py đều tra
cứu từ đây, tránh 4 nơi định nghĩa lệch nhau (vd thêm field mới quên
update export nhưng đã update import).

Tên field ở đây = tên cột DB thật (Requirement 11.1/11.2/11.3: "column
headers matching the database schema field names") — CHỈ 2 ngoại lệ cố
ý, ghi rõ trong comment từng entity bên dưới, vì mapping text -> FK id
(company/level/province) không thể dùng thẳng tên cột UUID.
"""

from dataclasses import dataclass, field
from typing import Optional

from constants import LEVEL_CODE_VALUES


@dataclass
class EntitySpec:
    # Tên cột khoá chính (PK) của entity trong DB — vd "job_id",
    # "company_id", "contact_id". Thêm 08/2026: trước đây field này
    # không tồn tại tường minh ở đây, mọi nơi cần biết tên PK (vd
    # import_executor.py::_update_row đọc existing["company_id"]/
    # existing["job_id"]/existing["contact_id"]) đều tự hardcode
    # if/elif entity_type riêng — dễ quên cập nhật khi thêm entity mới.
    # LƯU Ý: id_field LUÔN là export_columns[0] theo convention hiện có
    # (mọi entity đặt cột id đầu tiên khi export) nhưng KHÔNG được suy
    # ngầm từ vị trí đó — khai báo tường minh để không vỡ âm thầm nếu
    # sau này thứ tự export_columns đổi vì lý do UI/UX nào đó.
    id_field: str

    # Cột xuất ra file export, ĐÚNG THỨ TỰ, đúng tên cột DB.
    export_columns: list[str]

    # Field bắt buộc phải có giá trị khi import (Requirement 2.4).
    required_fields: list[str]

    # field -> danh sách giá trị hợp lệ (business rule enum, Requirement 2.6).
    enum_fields: dict[str, list[str]] = field(default_factory=dict)

    # field kiểu date (ISO 8601 YYYY-MM-DD, Requirement 11.8).
    date_fields: list[str] = field(default_factory=list)

    # field kiểu số nguyên (salary...).
    number_fields: list[str] = field(default_factory=list)

    # field email cần validate format.
    email_fields: list[str] = field(default_factory=list)

    # BUG FIX (08/2026, level_code): field khớp DANH SÁCH CỐ ĐỊNH nhưng
    # KHÔNG được coi như enum_fields bình thường — enum_fields sai giá
    # trị -> reject NGUYÊN FILE ở 422 (không cho qua bước preview luôn),
    # trong khi level_code trước giờ hoàn toàn không nằm trong
    # enum_fields lẫn number/date/email_fields -> rơi vào nhánh "text
    # field, giữ nguyên string" cuối validate_dataframe(), không được so
    # khớp DB (bảng levels seed đúng case 'Senior' không phải 'SENIOR')
    # -> get_level_id() ở import_executor.py không tìm thấy, ÂM THẦM trả
    # None -> job tạo ra thiếu level dù staff đã gõ đúng ý, không ai biết
    # (khác lỗi 500 rõ ràng — đây là mất dữ liệu ÂM THẦM, phát hiện qua
    # đối chiếu 08/2026).
    #
    # Field mới: match KHÔNG phân biệt hoa/thường trước (đa số trường hợp
    # thật, vd "SENIOR"/"senior" trong file export cũ đều nên khớp
    # "Senior" ngay, không cần staff làm gì thêm) — chỉ khi KHÔNG khớp dù
    # đã chuẩn hoá case mới đánh dấu dòng "cần chọn lại" (needs_level_
    # resolve, xem preview_manager.py), giữ nguyên giá trị gốc trong file
    # để staff biết mình đã gõ gì, và chọn lại qua dropdown liệt kê tĩnh
    # NUMBER_CODE_VALUES bên dưới, KHÔNG chặn cả file như enum_fields
    # thường (Company/Job/Contact nào đúng chính tả vẫn qua bình thường,
    # chỉ riêng dòng gõ sai mới cần thao tác thêm — quyết định 08/2026,
    # chỉ áp dụng cho Job trước, Company/Contact chưa có field tương tự).
    strict_enum_fields: dict[str, list[str]] = field(default_factory=dict)


JOB_SPEC = EntitySpec(
    id_field="job_id",
    export_columns=[
        "job_id", "company_name", "job_title", "matching_industry",
        "level_code", "province_name", "work_type", "currency",
        "salary_min", "salary_max", "salary_type", "salary_period",
        "deadline", "job_status", "ss_team_notes",
        "created_at", "updated_at",
    ],
    # company_name (KHÔNG phải company_id) — file import Job dùng TÊN
    # công ty dạng text, company_resolver.py tự resolve/gợi ý sang
    # company_id thật trước khi ghi DB (xem quyết định: không bắt nhập
    # UUID tay, resolve qua tax_id trước, fallback gợi ý tên tương tự).
    required_fields=["job_title", "company_name", "deadline"],
    enum_fields={
        "job_status": ["OPEN", "EXPIRED", "CLOSED"],
        "work_type": ["FULL_TIME", "PART_TIME", "INTERNSHIP", "OTHER"],
        "salary_type": ["RANGE", "EXACT", "UPTO", "STARTING_FROM", "NEGOTIABLE", "UNPAID"],
        "salary_period": ["MONTH", "YEAR"],
    },
    date_fields=["deadline"],
    number_fields=["salary_min", "salary_max"],
    strict_enum_fields={
        "level_code": LEVEL_CODE_VALUES,
    },
)

COMPANY_SPEC = EntitySpec(
    id_field="company_id",
    export_columns=[
        "company_id", "company_name", "tax_id", "website", "industry",
        "company_size", "address", "province_name", "fanpage_url",
        "linkedin_url", "partnership_potential", "is_active",
        "created_at", "updated_at",
    ],
    required_fields=["company_name"],
    enum_fields={
        "partnership_potential": ["HIGH", "MEDIUM", "LOW", "UNVERIFIED"],
    },
)

CONTACT_SPEC = EntitySpec(
    id_field="contact_id",
    export_columns=[
        "contact_id", "company_name", "contact_name", "job_title",
        "work_email", "social_link", "phone_number", "found_source",
        "contact_status", "is_active", "created_at", "updated_at",
    ],
    # company_name (KHÔNG phải company_id) — cùng lý do như Job ở trên.
    required_fields=["contact_name", "work_email", "company_name"],
    enum_fields={
        "contact_status": ["UNCONTACTED", "EMAIL_SENT", "RESPONDED", "IN_PARTNERSHIP"],
    },
    email_fields=["work_email"],
)

ENTITY_SPECS: dict[str, EntitySpec] = {
    "job": JOB_SPEC,
    "company": COMPANY_SPEC,
    "contact": CONTACT_SPEC,
}

# Sanity check tại import-time: id_field phải khớp export_columns[0]
# (convention hiện có: cột id luôn đứng đầu khi export) — 2 field này
# giờ khai báo tách rời (xem comment EntitySpec.id_field ở trên) nên có
# nguy cơ lệch nhau âm thầm nếu ai đó sửa 1 trong 2 mà quên chỗ còn lại;
# fail ngay lúc import module thay vì để lỗi mờ xuất hiện lúc runtime
# (vd import_executor.py tra existing[spec.id_field] ra KeyError khó hiểu).
for _entity_type, _spec in ENTITY_SPECS.items():
    assert _spec.export_columns and _spec.export_columns[0] == _spec.id_field, (
        f"EntitySpec('{_entity_type}'): id_field={_spec.id_field!r} phải khớp "
        f"export_columns[0]={_spec.export_columns[0]!r}"
    )
del _entity_type, _spec


def get_spec(entity_type: str) -> EntitySpec:
    try:
        return ENTITY_SPECS[entity_type]
    except KeyError:
        raise ValueError(f"entity_type không hợp lệ: {entity_type!r} (chỉ nhận job/company/contact)")
