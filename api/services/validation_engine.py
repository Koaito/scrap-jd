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
    ) | set(spec.number_fields) | set(spec.email_fields) | _extra_optional_fields(entity_type)
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

        # Chuẩn hoá và strip TẤT CẢ field trước khi validate — để required
        # check có thể detect string chỉ toàn space (Requirement 11.6).
        normalized = {}
        for f, raw_val in row_dict.items():
            if raw_val is None:
                normalized[f] = None
            elif isinstance(raw_val, str):
                stripped = raw_val.strip()
                normalized[f] = None if stripped == "" else stripped
            else:
                # Giữ nguyên non-string (int/float/Timestamp...) để xử lý ở
                # bước type-specific validation bên dưới.
                normalized[f] = raw_val

        # Required field check — PHẢI chạy SAU khi normalize/strip.
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

        for f, raw_val in normalized.items():
            # raw_val đã được normalize: None hoặc string đã strip hoặc
            # non-string (int/float...). Xử lý theo type của field.
            if raw_val is None:
                cleaned[f] = None
                continue

            if f in spec.date_fields:
                try:
                    # raw_val đã được strip ở trên nếu là string. Convert về date.
                    cleaned[f] = _parse_date_value(str(raw_val))
                except ValueError:
                    errors.append(
                        ValidationError(
                            row_number=row_number,
                            field_name=f,
                            rule="type_date",
                            message=(
                                f"Dòng {row_number}, cột '{f}': kỳ vọng ngày định dạng "
                                f"YYYY-MM-DD, nhận được '{raw_val}'"
                            ),
                        )
                    )
                    row_ok = False
                continue

            if f in spec.number_fields:
                try:
                    # raw_val có thể là:
                    # - int: pandas đọc được số nguyên nhỏ
                    # - float: pandas đọc số (kể cả nguyên) thành float64
                    # - str: số viết dạng text ("1000", "1,000", "20000.5"...)
                    # Convert hết về int, bỏ phần thập phân nếu có.
                    if isinstance(raw_val, (int, float)):
                        cleaned[f] = int(raw_val)
                    else:
                        # raw_val là string đã strip. Remove dấu phẩy ngăn cách
                        # nghìn (vd "1,000,000" → "1000000"), parse qua float()
                        # trước (để xử lý "20000.5") rồi mới cast int.
                        cleaned[f] = int(float(str(raw_val).replace(",", "")))
                except (ValueError, OverflowError):
                    errors.append(
                        ValidationError(
                            row_number=row_number,
                            field_name=f,
                            rule="type_number",
                            message=(
                                f"Dòng {row_number}, cột '{f}': kỳ vọng số nguyên, "
                                f"nhận được '{raw_val}'"
                            ),
                        )
                    )
                    row_ok = False
                continue

            if f in spec.email_fields:
                # raw_val có thể là string (đã strip) hoặc non-string (pandas
                # infer nhầm, vd số). Convert về string trước khi regex match.
                email_str = raw_val if isinstance(raw_val, str) else str(raw_val)
                if not _EMAIL_RE.match(email_str):
                    errors.append(
                        ValidationError(
                            row_number=row_number,
                            field_name=f,
                            rule="type_email",
                            message=f"Dòng {row_number}, cột '{f}': email không hợp lệ '{email_str}'",
                        )
                    )
                    row_ok = False
                else:
                    cleaned[f] = email_str
                continue

            if f in spec.enum_fields:
                allowed = spec.enum_fields[f]
                # raw_val đã là string đã strip. Upper để so sánh case-insensitive.
                val_upper = raw_val.upper() if isinstance(raw_val, str) else str(raw_val).upper()
                if val_upper not in allowed:
                    errors.append(
                        ValidationError(
                            row_number=row_number,
                            field_name=f,
                            rule="business_rule_enum",
                            message=(
                                f"Dòng {row_number}, cột '{f}': giá trị '{raw_val}' không hợp lệ, "
                                f"chỉ nhận {allowed}"
                            ),
                        )
                    )
                    row_ok = False
                else:
                    cleaned[f] = val_upper
                continue

            # Field không thuộc type đặc biệt nào (date/number/email/enum) →
            # text field tự do. raw_val đã strip ở trên nếu là string, giữ
            # nguyên nếu là type khác (ít khi xảy ra, nhưng để pandas tự infer
            # nên có thể gặp). Convert hết về string để nhất quán.
            cleaned[f] = str(raw_val) if not isinstance(raw_val, str) else raw_val

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
    (vd matching_industry, level_code, province_name của Job — text tự
    do, chỉ cần strip, không cần validate type/enum riêng)."""
    if entity_type == "job":
        return {"matching_industry", "level_code", "province_name", "currency", "ss_team_notes"}
    if entity_type == "company":
        return {"website", "industry", "company_size", "address", "province_name",
                "fanpage_url", "linkedin_url"}
    if entity_type == "contact":
        return {"social_link", "phone_number", "found_source"}
    return set()
