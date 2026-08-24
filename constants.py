"""
Constants và enums dùng chung trong codebase — tập trung ở đây thay vì
rải rác khắp nơi, dễ bảo trì và đảm bảo nhất quán.

Thêm 08/2026 để expose qua GET /meta/enums cho frontend (xem
api/routers/meta.py), thay vì frontend hardcode lại ~10 dict _MAP
trong crawler_client.py — mỗi khi backend đổi enum (như vụ EXPIRED ->
CLOSED) phải nhớ sửa ở 2 nơi.
"""

# Job statuses — ĐỒNG BỘ với sql/migration_update_job_status_enum.sql
JOB_STATUS_VALUES = ["OPEN", "CLOSED"]

# Work types
WORK_TYPE_VALUES = ["FULL_TIME", "PART_TIME", "INTERNSHIP", "OTHER"]

# Salary types
SALARY_TYPE_VALUES = [
    "RANGE",
    "EXACT",
    "UPTO",
    "STARTING_FROM",
    "NEGOTIABLE",
    "UNPAID",
]

# Salary periods (chu kỳ trả lương)
SALARY_PERIOD_VALUES = ["MONTH", "YEAR"]

# Job levels
LEVEL_CODE_VALUES = [
    "Intern",
    "Fresher",
    "Junior",
    "Middle",
    "Senior",
    "Lead",
    "Manager",
]

# Currency
CURRENCY_VALUES = ["VNĐ", "USD"]

# Contact statuses
CONTACT_STATUS_VALUES = [
    "UNCONTACTED",
    "EMAIL_SENT",
    "RESPONDED",
    "IN_PARTNERSHIP",
]

# Partnership potential (company)
PARTNERSHIP_POTENTIAL_VALUES = ["HIGH", "MEDIUM", "LOW", "UNVERIFIED"]

# User roles
USER_ROLE_VALUES = ["user", "ss_team", "admin"]

# Entity types (for audit logs, import/export)
ENTITY_TYPE_VALUES = ["JOB", "COMPANY", "CONTACT"]

# Action types (audit logs) — subset quan trọng nhất, không cần liệt kê hết
# vì frontend chủ yếu dùng để filter, backend có đầy đủ trong db.ACTION_LOG_RULES
ACTION_TYPE_VALUES = [
    "CREATE_JOB",
    "UPDATE_JOB",
    "DELETE_JOB",
    "CREATE_COMPANY",
    "UPDATE_COMPANY",
    "DELETE_COMPANY",
    "CREATE_CONTACT",
    "UPDATE_CONTACT",
    "DELETE_CONTACT",
    "ASSIGN_CONTACT",
    "BULK_IMPORT_JOB",
    "BULK_IMPORT_COMPANY",
    "BULK_IMPORT_CONTACT",
]
