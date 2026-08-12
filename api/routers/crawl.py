from fastapi import APIRouter, BackgroundTasks, HTTPException

from api import crawl_runner
from api.schemas import CrawlAccepted, CrawlRequest, CrawlStatusOut
from config import TOPCV_CATEGORIES, VIETNAMWORKS_CATEGORIES

router = APIRouter(prefix="/crawl", tags=["crawl"])

_CATEGORIES_BY_SOURCE = {
    "topcv": TOPCV_CATEGORIES,
    "vietnamworks": VIETNAMWORKS_CATEGORIES,
}


@router.post("", response_model=CrawlAccepted, status_code=202)
def trigger_crawl(payload: CrawlRequest, background_tasks: BackgroundTasks):
    """Kích hoạt 1 lượt crawl CHẠY NỀN — trả về run_id ngay, KHÔNG chờ
    crawl xong (crawl thật có thể mất vài phút - vài chục phút). Dùng
    GET /crawl/{run_id} để theo dõi tiến độ."""
    if payload.source not in crawl_runner._SOURCE_ADAPTERS:
        raise HTTPException(
            status_code=400,
            detail=f"Source '{payload.source}' không tồn tại. "
                   f"Có sẵn: {list(crawl_runner._SOURCE_ADAPTERS.keys())}",
        )
    categories = _CATEGORIES_BY_SOURCE[payload.source]
    if payload.category not in categories:
        raise HTTPException(
            status_code=400,
            detail=f"Category '{payload.category}' không tồn tại cho source "
                   f"'{payload.source}'. Có sẵn: {list(categories.keys())}",
        )

    run_id = crawl_runner.start_crawl(payload.source, payload.category, payload.pages)
    background_tasks.add_task(crawl_runner.execute, run_id)
    return CrawlAccepted(run_id=run_id, status="queued")


@router.get("/{run_id}", response_model=CrawlStatusOut)
def get_crawl_status(run_id: str):
    run = crawl_runner.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy run_id này")
    return run
