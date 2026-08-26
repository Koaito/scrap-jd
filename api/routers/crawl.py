from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query

import db as db_module
from api import crawl_runner
from api.deps import require_admin, require_role
from api.schemas import CrawlAccepted, CrawlLogsOut, CrawlRequest, CrawlStatusOut, PaginatedCrawlRuns
from config import TOPCV_CATEGORIES, VIETNAMWORKS_CATEGORIES

router = APIRouter(prefix="/crawl", tags=["crawl"])

_CATEGORIES_BY_SOURCE = {
    "topcv": TOPCV_CATEGORIES,
    "vietnamworks": VIETNAMWORKS_CATEGORIES,
}

_VALID_CRAWL_STATUSES = {"queued", "running", "done", "error"}


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

    BẮT BUỘC đăng nhập VÀ role='admin' (Depends(require_admin), nay là
    alias của require_role("admin") — xem api/deps.py) — chặt hơn POST
    /jobs, POST /companies (chỉ cần role 'ss_team' trở lên), vì kích
    hoạt crawl tốn tài nguyên server thật (network + CPU parse trong vài
    phút) nên hạn chế ai cũng bấm được, tránh spam nhiều lượt crawl chạy
    song song ngoài ý muốn.

    08/2026 (xem sql/migration_add_crawl_runs.sql): mỗi NGUỒN (source)
    tối đa 1 lượt 'queued'/'running' tại 1 thời điểm — trả 409 nếu
    nguồn này đang crawl dở (khác 2 nguồn khác nhau, vẫn chạy song song
    bình thường). Trước đây chỉ disable ở frontend, backend cho gọi
    thẳng API là chạy song song vô hạn."""
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

    try:
        run_id = crawl_runner.start_crawl(
            payload.source, payload.category, payload.pages,
            max_jobs=payload.max_jobs, triggered_by=user["sub"],
        )
    except db_module.ActiveCrawlExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    background_tasks.add_task(crawl_runner.execute, run_id)
    return CrawlAccepted(run_id=run_id, status="queued")


@router.get("", response_model=PaginatedCrawlRuns)
def list_crawl_runs(
    source: Optional[str] = Query(None, description="Lọc theo nguồn, vd 'topcv'"),
    status: Optional[str] = Query(None, description="queued | running | done | error"),
    triggered_by: Optional[str] = Query(None, description="Lọc theo ss_user_id admin đã bấm"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_role("ss_team")),
):
    """Trang "Lịch sử crawl" — 08/2026, xem sql/migration_add_crawl_runs.sql.

    QUYỀN: 'ss_team' trở lên (giống GET /audit-logs) — đọc lịch sử
    không tốn tài nguyên như bấm crawl thật (POST /crawl chặt hơn, chỉ
    'admin'), team nội bộ cần tra cứu được (vd xem admin nào vừa crawl
    nguồn gì, tỷ lệ lỗi ra sao) mà không cần lên hẳn quyền admin."""
    if status is not None and status not in _VALID_CRAWL_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status '{status}' không hợp lệ — có sẵn: {sorted(_VALID_CRAWL_STATUSES)}",
        )
    if triggered_by is not None and not db_module.is_valid_uuid(triggered_by):
        raise HTTPException(status_code=400, detail=f"triggered_by '{triggered_by}' không đúng định dạng UUID.")

    rows, total = crawl_runner.list_runs(
        source=source, status=status, triggered_by=triggered_by,
        limit=limit, offset=offset,
    )
    return PaginatedCrawlRuns(total=total, limit=limit, offset=offset, items=rows)


@router.get("/{run_id}", response_model=CrawlStatusOut)
def get_crawl_status(run_id: str, user: dict = Depends(require_role("ss_team"))):
    """Poll tiến độ/kết quả 1 lượt crawl.

    08/2026: THÊM yêu cầu đăng nhập tối thiểu 'ss_team' — trước đây
    route này KHÔNG yêu cầu đăng nhập gì cả (bất kỳ ai có run_id, kể cả
    đoán UUID ngẫu nhiên gần như không khả thi nhưng vẫn là lỗ hổng
    thiết kế, đều gọi được), khác hẳn POST /crawl vốn đã chặt 'admin'."""
    if not db_module.is_valid_uuid(run_id):
        raise HTTPException(status_code=400, detail=f"run_id '{run_id}' không đúng định dạng UUID.")
    run = crawl_runner.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy run_id này")
    return run


@router.get("/{run_id}/logs", response_model=CrawlLogsOut)
def get_crawl_logs(
    run_id: str,
    after_id: int = Query(0, ge=0, description="Chỉ lấy dòng log có id > after_id (poll tăng dần)"),
    limit: int = Query(500, ge=1, le=2000),
    user: dict = Depends(require_role("ss_team")),
):
    """Khu "Xem log live" ở trang /crawl — poll endpoint này lặp lại
    (vd mỗi 2 giây) với after_id = last_id của lần gọi trước, để chỉ
    tải các dòng MỚI thay vì tải lại toàn bộ log mỗi lần (log 1 lượt
    crawl có thể lên tới hàng trăm/nghìn dòng).

    Cùng mức quyền 'ss_team' như GET /crawl/{run_id} (đọc log không tốn
    tài nguyên hơn đọc status, không cần chặt hơn)."""
    if not db_module.is_valid_uuid(run_id):
        raise HTTPException(status_code=400, detail=f"run_id '{run_id}' không đúng định dạng UUID.")
    items = crawl_runner.get_logs(run_id, after_id=after_id, limit=limit)
    last_id = items[-1]["id"] if items else after_id
    return CrawlLogsOut(last_id=last_id, items=items)
