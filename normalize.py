"""
Module CHUẨN HÓA — phần "dùng chung" của pipeline, không quan tâm dữ liệu
đến từ nguồn nào. Nhận vào text thô (từ RawJobRecord) -> trả ra dữ liệu
đã parse, sẵn sàng insert DB theo đúng kiểu cột trong schema.sql.
"""

import re
import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass
class NormalizedSalary:
    currency: str            # "VNĐ" | "USD"
    salary_min: Optional[int]
    salary_max: Optional[int]
    salary_type: str         # khớp salary_type_enum trong schema


def normalize_salary(salary_text: str) -> NormalizedSalary:
    """
    Parse text lương thô của TopCV thành dữ liệu có cấu trúc.

    Ví dụ input -> output:
      "Thoả thuận"        -> NEGOTIABLE, (None, None)
      "10 - 30 triệu"     -> RANGE, (10_000_000, 30_000_000), VNĐ
      "Tới 3,000 USD"     -> UPTO, (None, 3000), USD
      "Từ 12 triệu"       -> STARTING_FROM, (12_000_000, None), VNĐ
      "15 triệu"          -> EXACT, (15_000_000, 15_000_000), VNĐ
      ""                  -> NEGOTIABLE, (None, None)  (mặc định an toàn)
    """
    text = (salary_text or "").strip()
    if not text or "thoả thuận" in text.lower() or "thỏa thuận" in text.lower():
        return NormalizedSalary("VNĐ", None, None, "NEGOTIABLE")

    is_usd = "usd" in text.lower() or "$" in text

    # Bỏ hết ký tự không phải số / dấu chấm phẩy để tách các con số
    numbers = [
        _parse_number(n) for n in re.findall(r"[\d][\d.,]*", text)
    ]
    numbers = [n for n in numbers if n is not None]

    currency = "USD" if is_usd else "VNĐ"
    unit_multiplier = 1 if is_usd else 1_000_000  # "triệu" -> nhân 1 triệu

    lowered = text.lower()

    if not numbers:
        return NormalizedSalary(currency, None, None, "NEGOTIABLE")

    if ("tới" in lowered or "toi " in lowered or lowered.startswith("upto")
            or "up to" in lowered):
        val = int(numbers[0] * unit_multiplier)
        return NormalizedSalary(currency, None, val, "UPTO")

    if "từ" in lowered or lowered.startswith("tu "):
        val = int(numbers[0] * unit_multiplier)
        return NormalizedSalary(currency, val, None, "STARTING_FROM")

    if len(numbers) >= 2:
        lo, hi = int(numbers[0] * unit_multiplier), int(numbers[1] * unit_multiplier)
        if lo > hi:
            lo, hi = hi, lo
        return NormalizedSalary(currency, lo, hi, "RANGE")

    val = int(numbers[0] * unit_multiplier)
    return NormalizedSalary(currency, val, val, "EXACT")


def _parse_number(raw: str) -> Optional[float]:
    """'3,000' -> 3000.0 ; '15.5' -> 15.5 ; '10' -> 10.0"""
    cleaned = raw.replace(",", "")
    # Nếu dùng dấu chấm làm phân cách nghìn kiểu VN (vd '3.000') và không
    # có phần thập phân thật -> bỏ dấu chấm luôn.
    if cleaned.count(".") == 1 and len(cleaned.split(".")[-1]) == 3:
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


# ----------------------------------------------------------------------
# Suy luận level từ số năm kinh nghiệm
# ----------------------------------------------------------------------
LEVEL_ORDER = ["Intern", "Fresher", "Junior", "Middle", "Senior", "Lead", "Manager"]


def infer_level(experience_text: str, job_title: str = "") -> str:
    text = (experience_text or "").lower()
    title = (job_title or "").lower()

    if "intern" in title or "thực tập" in title or "thuc tap" in title:
        return "Intern"
    if "fresher" in title:
        return "Fresher"
    if any(k in title for k in ["lead", "trưởng nhóm", "team lead"]):
        return "Lead"
    if any(k in title for k in ["manager", "trưởng phòng", "giám đốc"]):
        return "Manager"
    if "senior" in title:
        return "Senior"

    if "không yêu cầu" in text or "khong yeu cau" in text:
        return "Fresher"
    if "dưới 1 năm" in text or "duoi 1 nam" in text:
        return "Fresher"

    m = re.search(r"(\d+)\s*năm", text)
    if m:
        years = int(m.group(1))
        if years <= 1:
            return "Junior"
        if years <= 3:
            return "Middle"
        if years <= 5:
            return "Senior"
        return "Lead"

    if "trên 5 năm" in text:
        return "Lead"

    return "Junior"  # mặc định an toàn khi không rõ


# ----------------------------------------------------------------------
# Content hash — dùng để dedupe ở tầng ứng dụng (khớp logic hash trong
# trigger Postgres generate_job_hash(), phòng khi cần check trước khi
# insert mà chưa có company_id/level_id UUID sẵn).
# ----------------------------------------------------------------------
def compute_content_hash(company_name: str, job_title: str, level_code: str, province_name: str) -> str:
    normalized_title = re.sub(r"\s+", " ", (job_title or "").strip().lower())
    key = "|".join([
        (company_name or "").strip().lower(),
        normalized_title,
        (level_code or "").strip().lower(),
        (province_name or "").strip().lower(),
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def clean_company_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())


def normalize_deadline(deadline_text: str) -> Optional[date]:
    """Parse text hạn ứng tuyển thô của TopCV (dạng "30/08/2026") thành
    date object khớp kiểu cột `deadline DATE` trong schema.sql.

    Trả None nếu rỗng hoặc không parse được (an toàn, không làm crash
    pipeline — cột deadline vốn đã nullable)."""
    text = (deadline_text or "").strip()
    if not text:
        return None
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if not m:
        return None
    day, month, year = (int(x) for x in m.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


# TopCV chỉ có đúng 4 lựa chọn cố định trong bộ lọc "Loại hình làm việc" —
# map 1-1 sang work_type_enum trong schema.sql. Giá trị lạ (TopCV đổi
# wording, hoặc parser bắt nhầm) -> None, KHÔNG insert thẳng text thô,
# tránh rác dữ liệu kiểu nhiều "biến thể" của cùng 1 giá trị.
_WORK_TYPE_MAP = {
    "toàn thời gian": "FULL_TIME",
    "bán thời gian": "PART_TIME",
    "thực tập": "INTERNSHIP",
    "khác": "OTHER",
}


def normalize_work_type(work_type_text: str) -> Optional[str]:
    """'Toàn thời gian' -> 'FULL_TIME' ; text lạ/rỗng -> None (an toàn,
    cột work_type vốn đã nullable, không làm crash insert)."""
    key = (work_type_text or "").strip().lower()
    return _WORK_TYPE_MAP.get(key)
