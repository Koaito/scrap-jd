"""
Validation Engine — kiểm tra 1 DataFrame đã parse (file_parser.py) theo
đúng schema từng entity (entity_specs.py). Xem requirements.md
Requirement 2 (Validation) + Requirement 10 (Error message chi tiết
row/field/rule).

QUYẾT ĐỊNH: reject NGUYÊN FILE nếu có bất kỳ dòng nào lỗi (Requirement
2.7) — validate_dataframe() luôn duyệt HẾT mọi dòng để gom đủ lỗi trả
về 1 lần (không dừng ở lỗi đầu tiên), để staff sửa 1 lần thay vì fix
từng dòng rồi upload lại nhiều lần.

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
        row_ok = True

        # file_parser.py đã convert MỌI cell thành string (đã strip).
        # Empty string sẽ được convert thành None tại đây.
        # Logic đơn giản: string → parse theo type, empty string → None.
        normalized = {}
        for f, raw_val in row_dict.items():
            # raw_val luôn là string (hoặc str của số từ Excel).
            # Strip và convert empty → None.
            val_str = str(raw_val).strip()
            normalized[f] = None if val_str == "" else val_str

        # Required field check — chạy SAU khi normalize.
        for req_field in spec.required_fields:
            if normalized.get(req_field) is None:
                errors.append(
                    ValidationError(
                        row_number=row_number,
                        field_name=req_field,
                        rule="required",
                        message=f"Dòng {row_number}: thiếu giá trị bắt buộc cho '{req_field}'",
                    )
                )
                row_ok = False

        for f, val_str in normalized.items():
            # val_str là None hoặc string đã strip (không empty).
            if val_str is None:
                cleaned[f] = None
                continue

            if f in spec.date_fields:
                try:
                    cleaned[f] = _parse_date_value(val_str)
                except ValueError:
                    errors.append(
                        ValidationError(
                            row_number=row_number,
                            field_name=f,
                            rule="type_date",
                            message=(
                                f"Dòng {row_number}, cột '{f}': kỳ vọng ngày định dạng "
                                f"YYYY-MM-DD, nhận được '{val_str}'"
                            ),
                        )
                    )
                    row_ok = False
                continue

            if f in spec.number_fields:
                try:
                    # val_str là string. Remove dấu phẩy ngăn cách nghìn
                    # (vd "1,000,000"), parse qua float (để xử lý "20000.5"),
                    # rồi cast int.
                    cleaned[f] = int(float(val_str.replace(",", "")))
                except (ValueError, OverflowError):
                    errors.append(
                        ValidationError(
                            row_number=row_number,
                            field_name=f,
                            rule="type_number",
                            message=(
                                f"Dòng {row_number}, cột '{f}': kỳ vọng số nguyên, "
                                f"nhận được '{val_str}'"
                            ),
                        )
                    )
                    row_ok = False
                continue

            if f in spec.email_fields:
                # val_str là string đã strip.
                if not _EMAIL_RE.match(val_str):
                    errors.append(
                        ValidationError(
                            row_number=row_number,
                            field_name=f,
                            rule="type_email",
                            message=f"Dòng {row_number}, cột '{f}': email không hợp lệ '{val_str}'",
                        )
                    )
                    row_ok = False
                else:
                    cleaned[f] = val_str
                continue

            if f in spec.enum_fields:
                allowed = spec.enum_fields[f]
                # val_str là string. Upper để so sánh case-insensitive.
                val_upper = val_str.upper()
                if val_upper not in allowed:
                    errors.append(
                        ValidationError(
                            row_number=row_number,
                            field_name=f,
                            rule="business_rule_enum",
                            message=(
                                f"Dòng {row_number}, cột '{f}': giá trị '{val_str}' không hợp lệ, "
                                f"chỉ nhận {allowed}"
                            ),
                        )
                    )
                    row_ok = False
                else:
                    cleaned[f] = val_upper
                continue

            if f in spec.strict_enum_fields:
                # KHÁC enum_fields ở trên: sai giá trị KHÔNG reject cả
                # file (không append vào `errors`, không set row_ok=False)
                # — chỉ set cleaned[f] = None (đồng nghĩa "chưa xác định",
                # cùng convention với field khác) + giữ lại chuỗi gốc ở
                # cleaned["_<field>_raw"] để preview_manager.py biết dòng
                # này cần đánh dấu needs_level_resolve (tên field lấy
                # nguyên bản để không hardcode riêng "level_code" ở đây —
                # dù hiện chỉ Job/level_code dùng field này) và hiển thị
                # lại đúng giá trị staff đã gõ trong file (vd "SR", "Sr.")
                # cho staff biết mình cần sửa gì khi chọn lại qua dropdown
                # tĩnh liệt kê spec.strict_enum_fields[f] (xem
                # api/routers/import_export.py — KHÔNG cần gọi API gợi ý
                # như company, vì đây là danh sách hữu hạn cố định, không
                # phải fuzzy-match theo dữ liệu DB).
                allowed = spec.strict_enum_fields[f]
                matched = next((a for a in allowed if a.lower() == val_str.lower()), None)
                if matched is not None:
                    cleaned[f] = matched
                else:
                    cleaned[f] = None
                    cleaned[f"_{f}_raw"] = val_str
                continue

            # Text field — giữ nguyên string đã strip.
            cleaned[f] = val_str

        # Business rule liên trường (không gắn với 1 field đơn lẻ):
        if entity_type == "job":
            row_ok = _validate_job_business_rules(cleaned, row_number, errors) and row_ok

        if row_ok:
            cleaned["_row_index"] = int(idx)  # 0-based, dùng làm khoá nội bộ preview
            cleaned_rows.append(cleaned)

    if errors:
        return ValidationResult(is_valid=False, errors=errors)
    return ValidationResult(is_valid=True, errors=[], cleaned_rows=cleaned_rows)


def _validate_job_business_rules(cleaned: dict, row_number: int, errors: list[ValidationError]) -> bool:
    """salary_min >= 0 (nếu có), salary_max >= salary_min (nếu cả 2 có) —
    Requirement business rule Job, xem design.md."""
    ok = True
    salary_min = cleaned.get("salary_min")
    salary_max = cleaned.get("salary_max")

    if salary_min is not None and salary_min < 0:
        errors.append(
            ValidationError(
                row_number=row_number,
                field_name="salary_min",
                rule="business_rule_non_negative",
                message=f"Dòng {row_number}, cột 'salary_min': phải >= 0, nhận được {salary_min}",
            )
        )
        ok = False

    if salary_min is not None and salary_max is not None and salary_max < salary_min:
        errors.append(
            ValidationError(
                row_number=row_number,
                field_name="salary_max",
                rule="business_rule_salary_range",
                message=(
                    f"Dòng {row_number}: salary_max ({salary_max}) phải >= "
                    f"salary_min ({salary_min})"
                ),
            )
        )
        ok = False

    return ok


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
        return {"website", "industry", "company_size", "address", "province_name",
                "fanpage_url", "linkedin_url"}
    if entity_type == "contact":
        return {"social_link", "phone_number", "found_source"}
    return set()
