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

# Provinces (province_name của job/công ty) — 34 tỉnh/thành sau sáp nhập
# (Nghị quyết 202/2025/QH15, hiệu lực 01/7/2025) + 2 giá trị đặc biệt
# "Khác"/"Remote", THỨ TỰ y hệt seed trong sql/schema.sql (Bắc -> Nam).
#
# Thêm 09/2026 để frontend build dropdown "Địa điểm" của form thêm/sửa
# job từ danh sách CỐ ĐỊNH thay vì cho gõ tự do: db.get_province_id()
# tra `provinces` (bảng cứng) theo tên, KHÔNG khớp thì âm thầm gán về
# "Khác" (không có lỗi nào trả về) — gõ sai/chọn giá trị không có trong
# bảng sẽ mất thông tin mà không ai biết.
#
# ĐỒNG BỘ 3 nơi (tests/test_stats.py có test so khớp tự động, đổi 1
# nơi mà quên nơi kia sẽ đỏ test): sql/schema.sql (seed),
# sql/migration_update_provinces_2025.sql, province_alias.py (mọi tên
# MỚI mà PROVINCE_ALIAS_MAP quy đổi về).
#
# CHỈ chứa giá trị ĐƯỢC PHÉP CHỌN — bảng `provinces` trong DB thật có
# thể còn thêm các dòng tên tỉnh CŨ (vd "Bình Dương") do job crawl
# trước sáp nhập vẫn tham chiếu (xem migration_update_provinces_2025.sql:
# không xoá dòng cũ), nhưng những dòng đó KHÔNG được đưa vào dropdown.
PROVINCE_VALUES = [
    "Tuyên Quang", "Cao Bằng", "Lai Châu", "Lào Cai", "Thái Nguyên",
    "Điện Biên", "Lạng Sơn", "Sơn La", "Phú Thọ", "Bắc Ninh",
    "Quảng Ninh", "Hà Nội", "Hải Phòng", "Hưng Yên", "Ninh Bình",
    "Thanh Hóa", "Nghệ An", "Hà Tĩnh", "Quảng Trị", "Huế",
    "Đà Nẵng", "Quảng Ngãi", "Gia Lai", "Đắk Lắk", "Khánh Hòa",
    "Lâm Đồng", "Đồng Nai", "Tây Ninh", "Hồ Chí Minh", "Đồng Tháp",
    "An Giang", "Vĩnh Long", "Cần Thơ", "Cà Mau",
    "Khác", "Remote",
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
