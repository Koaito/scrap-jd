from fastapi import APIRouter, Depends

import db as db_module
from api.deps import get_db
from api.schemas import EngagementStatsOut, StatsOut
# CATEGORIES_BY_SOURCE từ sources_registry.py (nguồn sự thật duy nhất)
# — trước đây get_sources() bên dưới hardcode riêng từng nguồn
# (TOPCV_CATEGORIES/VIETNAMWORKS_CATEGORIES/CAREERVIET_CATEGORIES), và
# chính đây là 1 trong 4 nơi từng bị "quên" thêm CareerViet dù CLI đã
# crawl được (xem docstring sources_registry.py). Giờ vòng lặp bên dưới
# tự động chạy qua MỌI nguồn trong registry, không hardcode tên nguồn
# nào nữa -> thêm nguồn mới vào registry là endpoint này tự khớp theo,
# không cần sửa file này nữa.
from sources_registry import CATEGORIES_BY_SOURCE
import constants

router = APIRouter(tags=["meta"])


@router.get("/stats", response_model=StatsOut)
def get_stats(conn=Depends(get_db)):
    """Số liệu tổng quan cho dashboard — tổng job, tổng công ty, tỷ lệ
    đã có social, phân bố theo ngành/nguồn, tổng đơn ứng tuyển (thêm
    08/2026), jobs_by_status và total_students (thêm 09/2026 — xem
    StatsOut ở api/schemas/stats.py)."""
    return db_module.get_stats_summary(conn)


@router.get("/stats/engagement", response_model=EngagementStatsOut)
def get_engagement_stats(conn=Depends(get_db)):
    """Thêm 08/2026 — riêng cho dashboard tab 'Gợi ý học viên' (JD
    "ế": đăng lâu mà 0 lượt lưu/ứng tuyển) và tab 'Báo cáo tháng' (%
    tăng/giảm học viên lưu/ứng tuyển so tháng trước).

    Tách khỏi GET /stats vì 2 việc này cần query khác hẳn (JOIN +
    GROUP BY theo từng job đang OPEN, và FILTER theo tháng) — không
    gộp vào StatsOut để không làm chậm GET /stats hiện có (dashboard
    tổng quan gọi liên tục, chỉ cần đếm tổng đơn giản).

    `jobs`: MỌI job đang OPEN kèm application_count/saved_count —
    frontend tự lọc "job created_at > 30 ngày trước VÀ cả 2 count = 0"
    phía client, tránh phải thêm tham số lọc riêng ở đây (frontend có
    thể đổi ngưỡng 30 ngày mà không cần sửa backend).
    `monthly`: tổng ứng tuyển/lưu job THÁNG NÀY vs THÁNG TRƯỚC, để
    frontend tự tính % chênh lệch."""
    return {
        "jobs": db_module.get_job_engagement_counts(conn),
        "monthly": db_module.get_monthly_engagement_stats(conn),
    }


@router.get("/sources")
def get_sources():
    """Danh sách source + category có sẵn để crawl — frontend dùng để
    render dropdown cho form POST /crawl, không cần hard-code lại phía
    frontend, luôn khớp với sources_registry.py hiện hành.

    08/2026 — VIẾT LẠI để lặp qua CATEGORIES_BY_SOURCE (sources_registry.py)
    thay vì liệt kê thủ công từng nguồn (topcv/vietnamworks/careerviet).
    Bản cũ từng "quên" thêm careerviet ở đúng chỗ này dù backend đã
    crawl được qua CLI (xem docstring sources_registry.py) — với vòng
    lặp này, thêm 1 nguồn mới vào registry là endpoint tự động trả về
    đúng, không còn khả năng quên hardcode ở đây nữa."""
    return {
        source: {key: cfg["label"] for key, cfg in categories.items()}
        for source, categories in CATEGORIES_BY_SOURCE.items()
    }


@router.get("/enums")
def get_enums():
    """Danh sách tất cả enum values dùng trong hệ thống — frontend fetch
    động để build map VN ↔ backend codes, thay vì hardcode ~10 dict
    _MAP trong crawler_client.py.

    Thêm 08/2026 để fix vấn đề: mỗi khi backend đổi enum (như EXPIRED ->
    CLOSED trong JOB_STATUS_VALUES), phải nhớ sửa ở 2 nơi. Giờ frontend
    gọi route này lúc khởi động hoặc khi cần, tự động sync với backend
    thật, không bị lệch.

    Response format: {"enum_name": ["VALUE1", "VALUE2", ...]}
    Frontend tự quyết định có map sang tiếng Việt hay không (có thể
    hardcode map VN ở frontend, nhưng ít nhất danh sách values luôn đúng).

    `province_name` (thêm 09/2026): danh sách tỉnh/thành HỢP LỆ để chọn
    ở form thêm/sửa job — 34 tỉnh sau sáp nhập + "Khác"/"Remote", đúng
    thứ tự seed trong DB (Bắc -> Nam, KHÔNG sắp theo bảng chữ cái —
    frontend tự sort nếu muốn). Cố ý KHÔNG đọc bảng `provinces` thật:
    route này không đọc DB (xem test_get_enums_has_no_parameters), và
    bảng thật có thể còn dòng tên tỉnh CŨ chỉ để giữ tham chiếu dữ liệu
    lịch sử — không được cho chọn lại. Danh sách này được test tự động
    so khớp với sql/schema.sql + migration + province_alias.py."""
    return {
        "job_status": constants.JOB_STATUS_VALUES,
        "work_type": constants.WORK_TYPE_VALUES,
        "salary_type": constants.SALARY_TYPE_VALUES,
        "salary_period": constants.SALARY_PERIOD_VALUES,
        "level_code": constants.LEVEL_CODE_VALUES,
        "currency": constants.CURRENCY_VALUES,
        "contact_status": constants.CONTACT_STATUS_VALUES,
        "partnership_potential": constants.PARTNERSHIP_POTENTIAL_VALUES,
        "province_name": constants.PROVINCE_VALUES,
        "user_role": constants.USER_ROLE_VALUES,
        "entity_type": constants.ENTITY_TYPE_VALUES,
        "action_type": constants.ACTION_TYPE_VALUES,
    }
