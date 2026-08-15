from fastapi import APIRouter, Depends

import db as db_module
from api.deps import get_db
from api.schemas import StatsOut
from config import TOPCV_CATEGORIES, VIETNAMWORKS_CATEGORIES

router = APIRouter(tags=["meta"])


@router.get("/stats", response_model=StatsOut)
def get_stats(conn=Depends(get_db)):
    """Số liệu tổng quan cho dashboard — tổng job, tổng công ty, tỷ lệ
    đã có social, phân bố theo ngành/nguồn, tổng đơn ứng tuyển (thêm
    08/2026)."""
    return db_module.get_stats_summary(conn)


@router.get("/sources")
def get_sources():
    """Danh sách source + category có sẵn để crawl — frontend dùng để
    render dropdown cho form POST /crawl, không cần hard-code lại phía
    frontend, luôn khớp với config.py hiện hành."""
    return {
        "topcv": {key: cfg["label"] for key, cfg in TOPCV_CATEGORIES.items()},
        "vietnamworks": {key: cfg["label"] for key, cfg in VIETNAMWORKS_CATEGORIES.items()},
    }
