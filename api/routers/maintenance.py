from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request

import db as db_module
from api import maintenance_runner
from api.deps import require_admin, require_role
from api.rate_limit import get_user_id_or_ip, limiter
from api.schemas import (
    MAINTENANCE_JOB_TYPES, MAINTENANCE_JOB_TYPES_REQUIRE_LIMIT,
    MaintenanceAccepted, MaintenanceLogsOut, MaintenanceRunRequest,
    MaintenanceStatusOut, PaginatedMaintenanceRuns,
)

router = APIRouter(prefix="/maintenance", tags=["maintenance"])

_VALID_MAINTENANCE_STATUSES = {"queued", "running", "done", "error"}

# CHỈ job_type này nhận dry_run/check_deadline_only (khớp
# api/schemas/maintenance.py::_CHECK_EXPIRED_JOBS, khai lại ở đây vì
# tên đó không export — router chỉ cần biết ĐÚNG 1 job_type này khác
# biệt, không cần import riêng 1 hằng số cho việc so sánh string).
_CHECK_EXPIRED_JOBS = "check_expired_jobs"


@router.post("/{job_type}", response_model=MaintenanceAccepted, status_code=202)
@limiter.limit("10/hour", key_func=get_user_id_or_ip)
def trigger_maintenance_run(
    request: Request,
    job_type: str,
    payload: MaintenanceRunRequest,
    background_tasks: BackgroundTasks,
    user: dict = Depends(require_admin),
):
    """Kích hoạt 1 lượt chạy job bảo trì dữ liệu CHẠY NỀN — trả về
    run_id ngay, KHÔNG chờ chạy xong (đối xứng POST /crawl, xem docstring
    api/maintenance_runner.py).

    BẮT BUỘC đăng nhập VÀ role='admin' — cùng mức chặt như POST /crawl
    (5 job này đều ghi/gọi API tốn tài nguyên, 2 job còn tốn PHÍ THẬT
    qua Tavily/Gemini).

    Mỗi job_type tối đa 1 lượt 'queued'/'running' tại 1 thời điểm — trả
    409 nếu job_type này đang chạy dở (khác job_type khác vẫn chạy song
    song bình thường, xem sql/migration_add_maintenance_runs.sql).

    Rate limit 10/hour theo user_id (thêm 08/2026) — guard 409 ở trên chỉ
    chặn CHẠY SONG SONG cùng job_type, KHÔNG chặn bấm lặp TUẦN TỰ (bấm,
    đợi xong, bấm tiếp). 2/5 job_type gọi Tavily/Gemini tốn phí thật mỗi
    lần chạy — giới hạn này là lớp phòng thủ chống bấm nhầm/lặp gây tốn
    phí ngoài ý muốn, không nhằm chặn admin dùng bình thường (10 lượt/giờ
    dư sức cho mọi kịch bản vận hành thực tế)."""
    if job_type not in MAINTENANCE_JOB_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"job_type '{job_type}' không tồn tại. Có sẵn: {list(MAINTENANCE_JOB_TYPES)}",
        )

    if job_type in MAINTENANCE_JOB_TYPES_REQUIRE_LIMIT and payload.limit is None:
        raise HTTPException(
            status_code=400,
            detail=f"job_type '{job_type}' gọi Tavily/Gemini (tốn phí thật) — "
                   f"bắt buộc truyền 'limit' khi kích hoạt từ web, không được để "
                   f"trống (tránh chạy hết toàn bộ company chưa có dữ liệu).",
        )

    if job_type != _CHECK_EXPIRED_JOBS and (
        payload.dry_run is not None or payload.check_deadline_only is not None
    ):
        raise HTTPException(
            status_code=400,
            detail=f"'dry_run'/'check_deadline_only' chỉ áp dụng cho job_type "
                   f"'{_CHECK_EXPIRED_JOBS}', không áp dụng cho '{job_type}'.",
        )

    # Chỉ đưa vào params những field THỰC SỰ được truyền (khác None) —
    # None nghĩa là "dùng default của run()" (limit=None chạy hết,
    # dry_run/check_deadline_only=False), không cần ghi tường minh vào
    # params JSONB cho gọn lịch sử.
    params = {
        k: v for k, v in payload.model_dump().items() if v is not None
    }

    try:
        run_id = maintenance_runner.start_run(job_type, params, triggered_by=user["sub"])
    except db_module.ActiveMaintenanceRunExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    background_tasks.add_task(maintenance_runner.execute, run_id)
    return MaintenanceAccepted(run_id=run_id, job_type=job_type, status="queued")


@router.get("", response_model=PaginatedMaintenanceRuns)
def list_maintenance_runs(
    job_type: Optional[str] = Query(None, description="Lọc theo job, vd 'check_expired_jobs'"),
    status: Optional[str] = Query(None, description="queued | running | done | error"),
    triggered_by: Optional[str] = Query(None, description="Lọc theo ss_user_id admin đã bấm"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict = Depends(require_role("ss_team")),
):
    """Trang "Lịch sử bảo trì" — đối xứng GET /crawl. QUYỀN 'ss_team'
    trở lên (đọc không tốn tài nguyên như bấm chạy thật, POST
    /maintenance/{job_type} chặt hơn, chỉ 'admin')."""
    if job_type is not None and job_type not in MAINTENANCE_JOB_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"job_type '{job_type}' không tồn tại. Có sẵn: {list(MAINTENANCE_JOB_TYPES)}",
        )
    if status is not None and status not in _VALID_MAINTENANCE_STATUSES:
        raise HTTPException(
            status_code=400,
            detail=f"status '{status}' không hợp lệ — có sẵn: {sorted(_VALID_MAINTENANCE_STATUSES)}",
        )
    if triggered_by is not None and not db_module.is_valid_uuid(triggered_by):
        raise HTTPException(status_code=400, detail=f"triggered_by '{triggered_by}' không đúng định dạng UUID.")

    rows, total = maintenance_runner.list_runs(
        job_type=job_type, status=status, triggered_by=triggered_by,
        limit=limit, offset=offset,
    )
    return PaginatedMaintenanceRuns(total=total, limit=limit, offset=offset, items=rows)


@router.get("/latest-log-runs", response_model=dict)
def get_latest_log_runs(user: dict = Depends(require_role("ss_team"))):
    """Trả {job_type: MaintenanceStatusOut|None} — lượt chạy GẦN NHẤT
    của MỖI job_type, để mỗi card trên trang web luôn có 1 run_id để
    hiện log lúc mở trang, kể cả khi job_type đó chưa từng chạy lần nào
    (trả None, không phải lỗi) — đối xứng GET /crawl/latest-log-run
    nhưng trả đủ 5 job_type 1 lần thay vì 1 nguồn.

    ĐẶT TRƯỚC route GET /{run_id} bên dưới trong file này — FastAPI
    match theo THỨ TỰ ĐĂNG KÝ, nếu để sau "latest-log-runs" sẽ bị hiểu
    nhầm thành run_id, giống lưu ý ở GET /crawl/latest-log-run."""
    return maintenance_runner.get_latest_run_per_job_type()


@router.get("/{run_id}", response_model=MaintenanceStatusOut)
def get_maintenance_status(run_id: str, user: dict = Depends(require_role("ss_team"))):
    """Poll tiến độ/kết quả 1 lượt chạy — đối xứng GET /crawl/{run_id}."""
    if not db_module.is_valid_uuid(run_id):
        raise HTTPException(status_code=400, detail=f"run_id '{run_id}' không đúng định dạng UUID.")
    run = maintenance_runner.get_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy run_id này")
    return run


@router.get("/{run_id}/logs", response_model=MaintenanceLogsOut)
def get_maintenance_logs(
    run_id: str,
    after_id: int = Query(0, ge=0, description="Chỉ lấy dòng log có id > after_id (poll tăng dần)"),
    limit: int = Query(500, ge=1, le=2000),
    user: dict = Depends(require_role("ss_team")),
):
    """Khu "Xem log live" — đối xứng GET /crawl/{run_id}/logs."""
    if not db_module.is_valid_uuid(run_id):
        raise HTTPException(status_code=400, detail=f"run_id '{run_id}' không đúng định dạng UUID.")
    items = maintenance_runner.get_logs(run_id, after_id=after_id, limit=limit)
    last_id = items[-1]["id"] if items else after_id
    return MaintenanceLogsOut(last_id=last_id, items=items)
