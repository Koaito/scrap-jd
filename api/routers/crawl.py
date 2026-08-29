from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request

import db as db_module
from api import crawl_runner
from api.deps import require_admin, require_role
from api.rate_limit import get_user_id_or_ip, limiter
from api.schemas import (
    CrawlAccepted, CrawlBatchAccepted, CrawlBatchRequest, CrawlBatchStatusOut,
    CrawlLogsOut, CrawlRequest, CrawlStatusOut, PaginatedCrawlBatches, PaginatedCrawlRuns,
)
# _CATEGORIES_BY_SOURCE giờ import từ sources_registry.py (nguồn sự
# thật duy nhất) thay vì tự khai báo lặp lại — route bên dưới validate
# category theo ĐÚNG dict của từng source (khác dict -> khác tập
# category hợp lệ, tự nhiên đúng, kể cả CareerViet chỉ có 5/6 category
# vì thiếu "ui-ux-design", xem comment trong config.py). Xem docstring
# sources_registry.py để biết cách thêm nguồn crawl mới sau này.
from sources_registry import CATEGORIES_BY_SOURCE as _CATEGORIES_BY_SOURCE

router = APIRouter(prefix="/crawl", tags=["crawl"])

_VALID_CRAWL_STATUSES = {"queued", "running", "done", "error"}


@router.post("", response_model=CrawlAccepted, status_code=202)
@limiter.limit("10/hour", key_func=get_user_id_or_ip)
def trigger_crawl(
    request: Request,
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
    thẳng API là chạy song song vô hạn.

    Rate limit 10/hour theo user_id (thêm 08/2026) — guard 409 ở trên chỉ
    chặn CHẠY SONG SONG cùng nguồn, KHÔNG chặn bấm lặp TUẦN TỰ (nguồn A
    xong rồi bấm lại nguồn A, hoặc đổi qua nguồn B liên tục). Mỗi lượt
    tốn network + CPU thật vài phút — giới hạn này để chặn spam gây tải
    server ngoài ý muốn, 10 lượt/giờ vẫn dư cho vận hành thực tế (nhiều
    nguồn x nhiều category)."""
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


@router.post("/batch", response_model=CrawlBatchAccepted, status_code=202)
@limiter.limit("10/hour", key_func=get_user_id_or_ip)
def trigger_crawl_batch(
    request: Request,
    payload: CrawlBatchRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_admin),
):
    """08/2026 (xem docstring sql/migration_add_crawl_batches.sql) —
    "crawl nhiều category liên tục": tick nhiều category cùng lúc cho 1
    nguồn, bấm 1 lần, hệ thống tự crawl TUẦN TỰ hết — thay cho việc gõ
    tay nhiều lệnh `python main.py crawl` nối tiếp nhau.

    Chỉ tạo + kích hoạt CATEGORY ĐẦU TIÊN ngay trong request này (giống
    hệt POST /crawl đơn lẻ) — các category còn lại tự động được tạo +
    chạy nối tiếp bởi CHÍNH background task đó (xem
    api/crawl_runner.py::execute()), KHÔNG cần request/background task
    nào khác cho các category sau.

    CÙNG mức quyền 'admin' như POST /crawl đơn lẻ (kích hoạt crawl tốn
    tài nguyên thật, không nới lỏng gì thêm chỉ vì gộp nhiều category).

    Trả 409 NGAY nếu source này đang có 1 lượt 'queued'/'running' chưa
    xong — giống hệt POST /crawl đơn lẻ (category đầu tiên của batch
    cũng phải qua đúng UNIQUE INDEX này, xem crawl_runner.start_batch()).

    Rate limit 10/hour theo user_id, TÍNH CHUNG với POST /crawl đơn lẻ
    (cùng key_func, cùng limiter instance — slowapi đếm theo path riêng
    từng route nên thực chất đây là 2 hạn mức 10/hour độc lập, xem ghi
    chú thêm ở POST /crawl phía trên nếu cần biết lý do chọn mốc này)."""
    if payload.source not in crawl_runner._SOURCE_ADAPTERS:
        raise HTTPException(
            status_code=400,
            detail=f"Source '{payload.source}' không tồn tại. "
                   f"Có sẵn: {list(crawl_runner._SOURCE_ADAPTERS.keys())}",
        )
    valid_categories = _CATEGORIES_BY_SOURCE[payload.source]
    unknown = [c for c in payload.categories if c not in valid_categories]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Category {unknown} không tồn tại cho source '{payload.source}'. "
                   f"Có sẵn: {list(valid_categories.keys())}",
        )

    # Loại category trùng lặp (giữ đúng thứ tự xuất hiện đầu tiên) —
    # người dùng tick nhầm trùng 1 ô trên UI không phải lỗi cần chặn cả
    # request, chỉ cần âm thầm crawl 1 lần cho category đó.
    seen: set = set()
    categories: list = []
    for c in payload.categories:
        if c not in seen:
            seen.add(c)
            categories.append(c)

    try:
        batch_id, first_run_id = crawl_runner.start_batch(
            payload.source, categories, payload.pages,
            max_jobs=payload.max_jobs, triggered_by=user["sub"],
        )
    except db_module.ActiveCrawlExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    background_tasks.add_task(crawl_runner.execute, first_run_id)
    return CrawlBatchAccepted(batch_id=batch_id, first_run_id=first_run_id, status="running")


@router.get("/batch", response_model=PaginatedCrawlBatches)
def list_crawl_batches(
    source: Optional[str] = Query(None, description="Lọc theo nguồn, vd 'topcv'"),
    status: Optional[str] = Query(None, description="running | done | error"),
    triggered_by: Optional[str] = Query(None, description="Lọc theo ss_user_id admin đã bấm"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_role("ss_team")),
):
    """Lịch sử batch — đối xứng GET /crawl (lịch sử run đơn lẻ). ĐẶT
    TRƯỚC GET /{run_id} không bắt buộc về mặt kỹ thuật (khác số lượng
    segment path: "/crawl/batch" so với "/crawl/{run_id}" — FastAPI
    không nhầm 2 pattern này) nhưng đặt gần POST /crawl/batch để dễ đọc
    theo nhóm tính năng."""
    if status is not None and status not in _VALID_CRAWL_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status '{status}' không hợp lệ — có sẵn: {sorted(_VALID_CRAWL_STATUSES)}",
        )
    if triggered_by is not None and not db_module.is_valid_uuid(triggered_by):
        raise HTTPException(status_code=400, detail=f"triggered_by '{triggered_by}' không đúng định dạng UUID.")

    rows, total = crawl_runner.list_batches(
        source=source, status=status, triggered_by=triggered_by,
        limit=limit, offset=offset,
    )
    return PaginatedCrawlBatches(total=total, limit=limit, offset=offset, items=rows)


@router.get("/batch/{batch_id}", response_model=CrawlBatchStatusOut)
def get_crawl_batch(batch_id: str, user: dict = Depends(require_role("ss_team"))):
    """Poll tiến độ TỔNG của 1 batch — trả kèm "items" (từng run con
    theo đúng thứ tự category) + "total"/"completed" để frontend hiện
    kiểu "2/6 category xong" mà không cần tự đếm lại từ GET /crawl."""
    if not db_module.is_valid_uuid(batch_id):
        raise HTTPException(status_code=400, detail=f"batch_id '{batch_id}' không đúng định dạng UUID.")
    batch = crawl_runner.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy batch_id này")
    return batch


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


@router.get("/latest-log-run", response_model=Optional[CrawlStatusOut])
def get_latest_log_run(user: dict = Depends(require_role("ss_team"))):
    """Trả lượt crawl GẦN NHẤT (bất kể status) — khung "Log live" ở
    frontend gọi endpoint này lúc mở trang /crawl để luôn có 1 run_id
    hiện log, kể cả khi không có lượt nào đang chạy (hiện log của lượt
    gần nhất đã xong/lỗi thay vì để trống, xem lịch sử trao đổi "khung
    Log live luôn hiện cố định trên trang").

    ĐẶT TRƯỚC route GET /{run_id} bên dưới trong file này — FastAPI
    match theo THỨ TỰ ĐĂNG KÝ, nếu để sau thì "/latest-log-run" sẽ bị
    hiểu nhầm thành run_id="latest-log-run" (path param nuốt mất route
    cố định phía sau nó).

    Trả null (không phải 404) nếu bảng crawl_runs rỗng hoàn toàn (chưa
    từng crawl lần nào) — đây là trạng thái hợp lệ, không phải lỗi."""
    return crawl_runner.get_latest_run()


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
