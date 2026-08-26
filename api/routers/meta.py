from fastapi import APIRouter, Depends

import db as db_module
from api.deps import get_db
from api.schemas import EngagementStatsOut, StatsOut
from config import TOPCV_CATEGORIES, VIETNAMWORKS_CATEGORIES, CAREERVIET_CATEGORIES
import constants

router = APIRouter(tags=["meta"])


@router.get("/stats", response_model=StatsOut)
def get_stats(conn=Depends(get_db)):
    """Số liệu tổng quan cho dashboard — tổng job, tổng công ty, tỷ lệ
    đã có social, phân bố theo ngành/nguồn, tổng đơn ứng tuyển (thêm
    08/2026)."""
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
    frontend, luôn khớp với config.py hiện hành."""
    return {
        "topcv": {key: cfg["label"] for key, cfg in TOPCV_CATEGORIES.items()},
        "vietnamworks": {key: cfg["label"] for key, cfg in VIETNAMWORKS_CATEGORIES.items()},
        # 08/2026 — THÊM careerviet: adapters/careerviet.py đã crawl
        # được từ trước và đã đăng ký ở api/crawl_runner.py +
        # api/routers/crawl.py (xem lịch sử trao đổi), nhưng riêng
        # GET /sources này (endpoint khác, frontend dùng để build
        # dropdown/checkbox nguồn) bị bỏ sót — kết quả là trang /crawl
        # phía frontend không hiện card CareerViet dù backend đã crawl
        # được qua CLI, giờ đã khớp đủ 3 nguồn.
        "careerviet": {key: cfg["label"] for key, cfg in CAREERVIET_CATEGORIES.items()},
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
    hardcode map VN ở frontend, nhưng ít nhất danh sách values luôn đúng)."""
    return {
        "job_status": constants.JOB_STATUS_VALUES,
        "work_type": constants.WORK_TYPE_VALUES,
        "salary_type": constants.SALARY_TYPE_VALUES,
        "salary_period": constants.SALARY_PERIOD_VALUES,
        "level_code": constants.LEVEL_CODE_VALUES,
        "currency": constants.CURRENCY_VALUES,
        "contact_status": constants.CONTACT_STATUS_VALUES,
        "partnership_potential": constants.PARTNERSHIP_POTENTIAL_VALUES,
        "user_role": constants.USER_ROLE_VALUES,
        "entity_type": constants.ENTITY_TYPE_VALUES,
        "action_type": constants.ACTION_TYPE_VALUES,
    }
