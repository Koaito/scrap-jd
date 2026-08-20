from fastapi import APIRouter, Depends

import db as db_module
from api.deps import get_db
from api.schemas import EngagementStatsOut, StatsOut
from config import TOPCV_CATEGORIES, VIETNAMWORKS_CATEGORIES

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
    }
