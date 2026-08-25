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
from typing import Callable, Optional

from constants import LEVEL_CODE_VALUES


@dataclass
class CrossFieldRule:
    """1 rule nghiệp vụ liên trường (đọc/set lỗi lên NHIỀU field cùng lúc,
    khác enum_fields/date_fields/... vốn chỉ gắn được cho 1 field đơn lẻ).

    Thêm 08/2026 (thay cho `if entity_type == "job": ...` rải rác ở
    validation_engine.py/preview_manager.py/import_executor.py — 3 nơi
    cùng cần biết rule salary tồn tại nhưng phải tự hardcode entity_type
    đúng loại lỗi "quên sửa 1 trong N chỗ" đã gặp với tax_id/level_code).
    Khai báo rule ở ĐÚNG 1 nơi (trong EntitySpec.cross_field_rules của
    entity liên quan) — 3 nơi kia gọi qua run_cross_field_rules()/
    check_cross_field_rules() bên dưới, không cần biết rule cụ thể là gì
    hay entity_type có bao nhiêu domain. Thêm rule liên trường mới (cho
    Job/Company/Contact) chỉ cần thêm 1 CrossFieldRule vào spec tương ứng.

    fields: các field mà rule ĐỌC và có thể set lỗi lên — dùng để biết
        rule này có LIÊN QUAN tới 1 field cụ thể hay không (vd
        preview_manager.apply_field_fix() cần biết sửa field nào thì
        phải re-chạy rule nào, KHÔNG chạy mọi rule cho mọi field sửa).
    check: hàm (data, row_number, field_errors) -> None, MUTATE field_errors
        trực tiếp — giữ đúng chữ ký hàm rule salary cũ để hành vi không đổi.
    """
    fields: tuple[str, ...]
    check: Callable[[dict, object, dict], None]


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

    # Rule nghiệp vụ liên trường của entity này (vd Job: salary_min/
    # salary_max) — xem docstring CrossFieldRule ở trên. Rỗng cho
    # Company/Contact hiện tại (chưa có rule liên trường nào).
    cross_field_rules: list[CrossFieldRule] = field(default_factory=list)


def _check_job_salary_business_rules(data: dict, row_number, field_errors: dict) -> None:
    """salary_min >= 0 (nếu có), salary_max >= salary_min (nếu cả 2 có) —
    Requirement business rule Job, xem design.md. Ghi thẳng vào
    field_errors (không reject cả file) — chỉ set khi salary_min/
    salary_max ĐÃ parse được thành số (None nghĩa là field đó đã có lỗi
    type_number riêng rồi, khỏi kiểm tra chồng thêm business rule lên 1
    giá trị chưa hợp lệ).

    GỘP VỀ 1 NGUỒN (08/2026): rule này TỪNG bị viết tay lại tới 3 lần
    (validate_dataframe() lúc build preview, apply_field_fix() lúc staff
    sửa tại chỗ trên preview, _apply_field_fixes() lúc confirm cuối) —
    y hệt loại lỗi "quên sửa 1 trong N chỗ" từng gặp với tax_id/
    level_code. Giờ tồn tại ĐÚNG 1 chỗ ở đây, khai báo qua
    JOB_SPEC.cross_field_rules bên dưới — 3 nơi kia gọi qua
    run_cross_field_rules()/check_cross_field_rules(), không tự biết
    rule cụ thể là gì hay entity_type nào có rule liên trường."""
    salary_min = data.get("salary_min")
    salary_max = data.get("salary_max")

    if salary_min is not None and salary_min < 0:
        field_errors["salary_min"] = {
            "rule": "business_rule_non_negative",
            "message": f"Dòng {row_number}, cột 'salary_min': phải >= 0, nhận được {salary_min}",
        }

    if salary_min is not None and salary_max is not None and salary_max < salary_min:
        field_errors["salary_max"] = {
            "rule": "business_rule_salary_range",
            "message": (
                f"Dòng {row_number}: salary_max ({salary_max}) phải >= "
                f"salary_min ({salary_min})"
            ),
        }


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
        # EXPIRED đã bị loại khỏi job_status_enum trong DB (xem
        # sql/migration_remove_expired_job_status.sql, 08/2026 — gộp
        # "job hết hạn tự nhiên" vào chung CLOSED, không tách riêng
        # nữa). Giữ EXPIRED ở đây sẽ khiến import/export filter chấp
        # nhận 1 giá trị DB thật không còn ghi được nữa — sửa cho khớp
        # constants.JOB_STATUS_VALUES.
        "job_status": ["OPEN", "CLOSED"],
        "work_type": ["FULL_TIME", "PART_TIME", "INTERNSHIP", "OTHER"],
        "salary_type": ["RANGE", "EXACT", "UPTO", "STARTING_FROM", "NEGOTIABLE", "UNPAID"],
        "salary_period": ["MONTH", "YEAR"],
    },
    date_fields=["deadline"],
    number_fields=["salary_min", "salary_max"],
    strict_enum_fields={
        "level_code": LEVEL_CODE_VALUES,
    },
    cross_field_rules=[
        CrossFieldRule(fields=("salary_min", "salary_max"), check=_check_job_salary_business_rules),
    ],
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


# Thêm 08/2026 (đổi "reject nguyên file" -> "sửa tại chỗ trên preview",
# xem validation_engine.py): 2 hàm dưới đây suy WIDGET nào FE nên render
# cho 1 field bị lỗi (needs_field_fix), dựa THẲNG vào spec đã khai báo ở
# trên — CHỦ ĐÍCH không hardcode danh sách field ở FE hay ở
# preview_manager.py, để thêm/đổi field mới chỉ cần sửa EntitySpec Ở ĐÂY,
# khỏi phải nhớ sửa thêm chỗ suy loại widget.
def field_widget_type(entity_type: str, field_name: str) -> str:
    """Trả 1 trong "enum" | "date" | "number" | "email" | "text" — FE dùng
    để chọn <select>/<input type=date>/<input type=number>/<input>/
    <input> tương ứng khi render ô sửa cho field bị lỗi trên bảng
    preview. Field không khớp field nào trong spec (vd cột thừa không
    thuộc entity) mặc định "text" — về lý thuyết không xảy ra vì
    field_widget_type() chỉ được gọi cho field ĐÃ có trong field_errors
    (tức đã đi qua validate_dataframe() và khớp 1 field có khai báo)."""
    spec = get_spec(entity_type)
    if field_name in spec.enum_fields or field_name in spec.strict_enum_fields:
        return "enum"
    if field_name in spec.date_fields:
        return "date"
    if field_name in spec.number_fields:
        return "number"
    if field_name in spec.email_fields:
        return "email"
    return "text"


def field_options(entity_type: str, field_name: str) -> Optional[list[str]]:
    """Danh sách giá trị hợp lệ để FE render <select> khi
    field_widget_type() == "enum" — None cho mọi widget khác (FE không
    cần render dropdown)."""
    spec = get_spec(entity_type)
    if field_name in spec.enum_fields:
        return list(spec.enum_fields[field_name])
    if field_name in spec.strict_enum_fields:
        return list(spec.strict_enum_fields[field_name])
    return None


# Thêm 08/2026 (gộp cross-field rule vào EntitySpec — xem CrossFieldRule ở
# trên): 3 hàm dưới đây thay cho `if entity_type == "job": ...` hardcode ở
# validation_engine.py/preview_manager.py/import_executor.py. Cả 3 nơi giờ
# gọi qua đây, KHÔNG cần biết Job có rule salary hay entity_type nào có
# rule liên trường gì — thêm rule mới cho Company/Contact chỉ cần thêm 1
# CrossFieldRule vào spec tương ứng (entity_specs.py), không phải sửa lại
# 3 nơi gọi.
def run_cross_field_rules(spec: EntitySpec, data: dict, row_number, field_errors: dict) -> None:
    """Chạy MỌI cross_field_rules của spec, MUTATE field_errors trực tiếp —
    dùng lúc build preview (validate_dataframe() đã có sẵn field_errors
    của dòng, chỉ cần bổ sung thêm lỗi liên trường nếu có)."""
    for rule in spec.cross_field_rules:
        rule.check(data, row_number, field_errors)


def check_cross_field_rules(spec: EntitySpec, data: dict, row_number: object = "?") -> dict:
    """Như run_cross_field_rules() nhưng trả field_errors MỚI (rỗng nếu
    hợp lệ) thay vì mutate — dùng NGOÀI validate_dataframe(), ở nơi CHƯA
    có sẵn field_errors của dòng để mutate vào (preview_manager.py::
    apply_field_fix() sửa 1 ô tại chỗ, import_executor.py::
    _apply_field_fixes() lúc confirm cuối).

    row_number: mặc định "?" (context sửa 1 ô tại chỗ, không có ý nghĩa
    "dòng N trong file gốc"). Truyền số dòng thật (vd row_index + 1) khi
    gọi từ ngữ cảnh có biết rõ dòng nào trong file."""
    field_errors: dict = {}
    run_cross_field_rules(spec, data, row_number, field_errors)
    return field_errors


def cross_field_rule_fields(spec: EntitySpec) -> set[str]:
    """Hợp mọi field xuất hiện trong bất kỳ cross_field_rules nào của spec
    — dùng để biết 1 field vừa sửa có CẦN re-chạy rule liên trường hay
    không (preview_manager.py::apply_field_fix()), KHÔNG hardcode entity_
    type=="job" hay tên field salary_min/salary_max ở nơi gọi."""
    result: set[str] = set()
    for rule in spec.cross_field_rules:
        result.update(rule.fields)
    return result
