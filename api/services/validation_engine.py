"""
Validation Engine — kiểm tra 1 DataFrame đã parse (file_parser.py) theo
đúng schema từng entity (entity_specs.py). Xem requirements.md
Requirement 2 (Validation) + Requirement 10 (Error message chi tiết
row/field/rule).

ĐỔI 08/2026 (bỏ "reject nguyên file"): TRƯỚC ĐÂY bất kỳ dòng nào lỗi 1
trong các rule type/format/business-rule đều khiến CẢ FILE bị 422 ngay
ở đây, không có preview nào được tạo để staff sửa. Theo yêu cầu mới
(staff muốn sửa tại chỗ trên bảng preview thay vì phải sửa file gốc rồi
upload lại), giờ CHỈ CÒN 1 trường hợp reject nguyên file:

  - required_column_missing: thiếu HẲN 1 cột bắt buộc trong header —
    không có ô nào trên bảng để sửa (khác "có cột nhưng vài dòng sai
    giá trị"), nên vẫn phải sửa file gốc rồi upload lại.

Mọi lỗi CÒN LẠI (required theo từng dòng, type_date, type_number,
type_email, business_rule_enum, business_rule_non_negative,
business_rule_salary_range) giờ KHÔNG append vào ValidationResult.
errors nữa (không làm is_valid=False) — mà gắn vào field_errors của
TỪNG DÒNG (cleaned["_field_errors"]), set cleaned[field] = None (giữ
giá trị gốc ở cleaned["_<field>_raw"] để staff biết mình đã gõ gì), rồi
vẫn CHO DÒNG ĐÓ VÀO cleaned_rows như bình thường. preview_manager.py
đọc field_errors này ra để đánh dấu needs_field_fix + gắn kèm
widget_type/options (xem entity_specs.field_widget_type/field_options)
cho FE render ô sửa trực tiếp trên bảng preview, thay vì reject.

Cùng pattern này ĐÃ có sẵn từ trước cho riêng field level_code (Job) —
xem nhánh spec.strict_enum_fields bên dưới (needs_level_resolve, giữ
NGUYÊN không đổi vì có luồng resolve riêng ở import_executor.py) — giờ
tổng quát hoá cho mọi field type/format/business-rule khác qua
field_errors.

validate_single_field() (dùng chung bởi validate_dataframe() lúc build
preview VÀ import_executor.py::_apply_field_fixes() lúc confirm) là nơi
DUY NHẤT chứa logic convert từng field theo type — tránh lệch logic 2
nơi khi staff sửa giá trị trên UI rồi gửi lại lúc confirm.

KHÔNG đụng tới DB ở đây (không check company/level/province có tồn tại
hay không) — đó là việc của company_resolver.py (chạy SAU khi
validate_dataframe() pass), vì "company_name gõ đúng chính tả nhưng
chưa có trong DB" KHÔNG phải lỗi validate dữ liệu (vẫn là input hợp lệ
kiểu string), mà là 1 case cần resolve/gợi ý riêng.
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

import pandas as pd

from api.services.entity_specs import EntitySpec, get_spec

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_TRUE_VALUES = {"true", "1", "yes"}
_FALSE_VALUES = {"false", "0", "no"}


@dataclass
class ValidationError:
    row_number: int  # 1-based, KHỚP với số dòng trong file gốc (header = dòng 1)
    field_name: str
    rule: str
    message: str


@dataclass
class ValidationResult:
    is_valid: bool
    errors: list[ValidationError]
    # DataFrame đã convert đúng kiểu (date -> date object, number -> int/float,
    # bool -> bool) — CHỈ có giá trị dùng được khi is_valid=True, để tầng
    # sau (conflict_detector/import_executor) không phải tự parse lại.
    cleaned_rows: Optional[list[dict]] = None


def _parse_date_value(raw: str) -> Optional[date]:
    """Requirement 11.8: chấp nhận ISO 8601 (YYYY-MM-DD)."""
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _parse_bool_value(raw: str) -> bool:
    """Requirement 11.10: true/false/1/0/yes/no, không phân biệt hoa/thường."""
    lowered = raw.strip().lower()
    if lowered in _TRUE_VALUES:
        return True
    if lowered in _FALSE_VALUES:
        return False
    raise ValueError(f"'{raw}' không phải giá trị boolean hợp lệ")


def validate_single_field(spec: EntitySpec, field_name: str, val_str: str):
    """Convert 1 giá trị string (đã strip, KHÔNG rỗng) theo đúng type khai
    báo trong spec cho field_name. Trả (converted_value, error) — error là
    None khi hợp lệ, hoặc dict {"rule", "message"} (message KHÔNG kèm
    "Dòng N, cột 'x':" — 2 chỗ gọi hàm này tự thêm tiền tố phù hợp ngữ
    cảnh riêng) khi giá trị sai.

    Field không thuộc date/number/email/enum/strict_enum nào trong spec
    -> coi là text tự do, luôn hợp lệ, trả nguyên val_str.

    Dùng chung bởi validate_dataframe() (lúc build preview, duyệt cả
    file) VÀ import_executor.py::_apply_field_fixes() (lúc confirm,
    re-validate LẠI giá trị staff vừa sửa trên UI trước khi ghi DB thật —
    không tin ngầm dữ liệu FE gửi lên dù FE cũng validate phía client) —
    tách ra đây để 2 nơi không lệch logic convert khi sau này sửa/thêm
    rule."""
    if field_name in spec.date_fields:
        try:
            return _parse_date_value(val_str), None
        except ValueError:
            return None, {
                "rule": "type_date",
                "message": f"kỳ vọng ngày định dạng YYYY-MM-DD, nhận được '{val_str}'",
            }

    if field_name in spec.number_fields:
        try:
            # Remove dấu phẩy ngăn cách nghìn (vd "1,000,000"), parse qua
            # float (để xử lý "20000.5"), rồi cast int.
            return int(float(val_str.replace(",", ""))), None
        except (ValueError, OverflowError):
            return None, {
                "rule": "type_number",
                "message": f"kỳ vọng số nguyên, nhận được '{val_str}'",
            }

    if field_name in spec.email_fields:
        if not _EMAIL_RE.match(val_str):
            return None, {
                "rule": "type_email",
                "message": f"email không hợp lệ '{val_str}'",
            }
        return val_str, None

    if field_name in spec.enum_fields:
        allowed = spec.enum_fields[field_name]
        val_upper = val_str.upper()
        if val_upper not in allowed:
            return None, {
                "rule": "business_rule_enum",
                "message": f"giá trị '{val_str}' không hợp lệ, chỉ nhận {allowed}",
            }
        return val_upper, None

    if field_name in spec.strict_enum_fields:
        allowed = spec.strict_enum_fields[field_name]
        matched = next((a for a in allowed if a.lower() == val_str.lower()), None)
        if matched is None:
            return None, {
                "rule": "business_rule_enum",
                "message": f"giá trị '{val_str}' không khớp danh sách hợp lệ {allowed}",
            }
        return matched, None

    return val_str, None


def validate_dataframe(df: pd.DataFrame, entity_type: str) -> ValidationResult:
    spec = get_spec(entity_type)
    errors: list[ValidationError] = []
    cleaned_rows: list[dict] = []

    # Requirement 11.5: cột thừa không có trong schema -> bỏ qua, không lỗi.
    known_fields = set(spec.required_fields) | set(spec.enum_fields) | set(
        spec.date_fields
    ) | set(spec.number_fields) | set(spec.email_fields) | set(
        spec.strict_enum_fields
    ) | _extra_optional_fields(entity_type)
    df = df[[c for c in df.columns if c in known_fields]]

    # Requirement 2.4: required field phải CÓ MẶT trong header, không chỉ
    # có giá trị ở từng dòng — thiếu hẳn cột thì báo lỗi 1 lần, không lặp
    # lại N lần cho N dòng.
    missing_columns = [f for f in spec.required_fields if f not in df.columns]
    if missing_columns:
        errors.append(
            ValidationError(
                row_number=0,
                field_name=", ".join(missing_columns),
                rule="required_column_missing",
                message=(
                    f"Thiếu cột bắt buộc: {', '.join(missing_columns)}"
                ),
            )
        )
        return ValidationResult(is_valid=False, errors=errors)

    for idx, row in df.iterrows():
        row_number = idx + 2  # +1 về 1-based, +1 vì header chiếm dòng 1
        row_dict = row.to_dict()
        cleaned: dict = {}
        # field -> {"rule", "message"} — lỗi CỦA RIÊNG DÒNG NÀY, không còn
        # đẩy lên `errors` (list cấp file) như trước, xem docstring đầu
        # file. preview_manager.py đọc cleaned["_field_errors"] ra để đánh
        # dấu needs_field_fix.
        field_errors: dict[str, dict] = {}

        # file_parser.py đã convert MỌI cell thành string (đã strip).
        # Empty string sẽ được convert thành None tại đây.
        # Logic đơn giản: string → parse theo type, empty string → None.
        normalized = {}
        for f, raw_val in row_dict.items():
            # raw_val luôn là string (hoặc str của số từ Excel).
            # Strip và convert empty → None.
            val_str = str(raw_val).strip()
            normalized[f] = None if val_str == "" else val_str

        # Required field check — chạy SAU khi normalize. Thiếu giá trị ở
        # 1 dòng (KHÁC thiếu hẳn cột — đã reject ở missing_columns phía
        # trên) giờ chỉ đánh dấu field_errors, KHÔNG reject cả file — ô
        # đó vẫn hiện trên preview (rỗng), staff gõ trực tiếp vào đó.
        for req_field in spec.required_fields:
            if normalized.get(req_field) is None:
                field_errors[req_field] = {
                    "rule": "required",
                    "message": f"Dòng {row_number}: thiếu giá trị bắt buộc cho '{req_field}'",
                }

        for f, val_str in normalized.items():
            # val_str là None hoặc string đã strip (không empty).
            if val_str is None:
                cleaned[f] = None
                continue

            if f in spec.strict_enum_fields:
                # level_code (Job) — pattern needs_level_resolve CÓ SẴN từ
                # trước 08/2026, giữ NGUYÊN không đổi (có luồng resolve
                # riêng ở import_executor.py, tách khỏi field_errors tổng
                # quát bên dưới). Sai giá trị KHÔNG reject cả file — chỉ
                # set cleaned[f] = None + giữ chuỗi gốc ở
                # cleaned["_<field>_raw"] để preview_manager.py đánh dấu
                # needs_level_resolve, staff chọn lại qua dropdown tĩnh
                # liệt kê spec.strict_enum_fields[f].
                allowed = spec.strict_enum_fields[f]
                matched = next((a for a in allowed if a.lower() == val_str.lower()), None)
                if matched is not None:
                    cleaned[f] = matched
                else:
                    cleaned[f] = None
                    cleaned[f"_{f}_raw"] = val_str
                continue

            if (
                f in spec.date_fields
                or f in spec.number_fields
                or f in spec.email_fields
                or f in spec.enum_fields
            ):
                value, err = validate_single_field(spec, f, val_str)
                if err is not None:
                    field_errors[f] = {
                        "rule": err["rule"],
                        "message": f"Dòng {row_number}, cột '{f}': {err['message']}",
                    }
                    cleaned[f] = None
                    cleaned[f"_{f}_raw"] = val_str
                else:
                    cleaned[f] = value
                continue

            # Text field — giữ nguyên string đã strip.
            cleaned[f] = val_str

        # Business rule liên trường (không gắn với 1 field đơn lẻ):
        if entity_type == "job":
            _validate_job_business_rules(cleaned, row_number, field_errors)

        cleaned["_row_index"] = int(idx)  # 0-based, dùng làm khoá nội bộ preview
        if field_errors:
            cleaned["_field_errors"] = field_errors
        cleaned_rows.append(cleaned)

    if errors:
        return ValidationResult(is_valid=False, errors=errors)
    return ValidationResult(is_valid=True, errors=[], cleaned_rows=cleaned_rows)


def _validate_job_business_rules(cleaned: dict, row_number: int, field_errors: dict) -> None:
    """salary_min >= 0 (nếu có), salary_max >= salary_min (nếu cả 2 có) —
    Requirement business rule Job, xem design.md. Ghi thẳng vào
    field_errors của dòng (không còn reject cả file) — chỉ set khi
    salary_min/salary_max ĐÃ parse được thành số (None nghĩa là field đó
    đã có lỗi type_number riêng rồi, khỏi kiểm tra chồng thêm business
    rule lên 1 giá trị chưa hợp lệ)."""
    salary_min = cleaned.get("salary_min")
    salary_max = cleaned.get("salary_max")

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


def _extra_optional_fields(entity_type: str) -> set[str]:
    """Field optional KHÔNG nằm trong required/enum/date/number/email
    (vd matching_industry, province_name của Job — text tự do, chỉ cần
    strip, không cần validate type/enum riêng).

    level_code KHÔNG còn ở đây (08/2026) — chuyển sang
    spec.strict_enum_fields (xem entity_specs.py) vì nó CÓ 1 danh sách
    giá trị cố định (khớp bảng levels), chỉ khác enum_fields ở việc
    KHÔNG reject cả file khi sai, mà đánh dấu riêng dòng đó cần resolve.
    known_fields ở validate_dataframe() đã gộp thêm set(spec.
    strict_enum_fields) nên level_code vẫn được giữ lại đúng cột, không
    cần khai báo lặp lại ở đây."""
    if entity_type == "job":
        return {"matching_industry", "province_name", "currency", "ss_team_notes"}
    if entity_type == "company":
        return {"tax_id", "website", "industry", "company_size", "address",
                "province_name", "fanpage_url", "linkedin_url"}
    if entity_type == "contact":
        return {"social_link", "phone_number", "found_source"}
    return set()
