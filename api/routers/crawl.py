from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException

from api import crawl_runner
from api.deps import require_admin
from api.schemas import CrawlAccepted, CrawlRequest, CrawlStatusOut
from config import TOPCV_CATEGORIES, VIETNAMWORKS_CATEGORIES

router = APIRouter(prefix="/crawl", tags=["crawl"])

_CATEGORIES_BY_SOURCE = {
    "topcv": TOPCV_CATEGORIES,
    "vietnamworks": VIETNAMWORKS_CATEGORIES,
}


@router.post("", response_model=CrawlAccepted, status_code=202)
def trigger_crawl(
    payload: CrawlRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_admin),
):
    """Kích hoạt 1 lượt crawl CHẠY NỀN — trả về run_id ngay, KHÔNG chờ
    crawl xong (crawl thật có thể mất vài phút - vài chục phút). Dùng
    GET /crawl/{run_id} để theo dõi tiến độ.

    Body hỗ trợ CẢ pages lẫn max_jobs (khớp --pages/--max-jobs ở CLI):
    dùng max_jobs mà bỏ trống pages -> tự nới pages đủ lớn để max_jobs
    là giới hạn thực sự; dùng cả 2 cùng lúc -> dừng ở điều kiện nào tới
    trước; bỏ trống cả 2 -> dùng DEFAULT_MAX_PAGES như trước giờ.

    BẮT BUỘC đăng nhập VÀ role='admin' (thêm 08/2026, xem
    Depends(require_admin)) — chặt hơn POST /jobs, POST /companies
    (chỉ cần đăng nhập, không cần admin), vì kích hoạt crawl tốn tài
    nguyên server thật (network + CPU parse trong vài phút) nên hạn chế
    ai cũng bấm được, tránh spam nhiều lượt crawl chạy song song ngoài
    ý muốn."""
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

    run_id = crawl_runner.start_crawl(
        payload.source, payload.category, payload.pages, max_jobs=payload.max_jobs,
    )
    background_tasks.add_task(crawl_runner.execute, run_id)
    return CrawlAccepted(run_id=run_id, status="queued")


@router.get("/{run_id}", response_model=CrawlStatusOut)
def get_crawl_status(run_id: str):
    run = crawl_runner.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy run_id này")
    return run
