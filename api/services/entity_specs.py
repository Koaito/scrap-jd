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


@dataclass
class EntitySpec:
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


JOB_SPEC = EntitySpec(
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
)

COMPANY_SPEC = EntitySpec(
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


def get_spec(entity_type: str) -> EntitySpec:
    try:
        return ENTITY_SPECS[entity_type]
    except KeyError:
        raise ValueError(f"entity_type không hợp lệ: {entity_type!r} (chỉ nhận job/company/contact)")
