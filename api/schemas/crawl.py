"""
Crawl trigger — schema request/response cho POST /crawl, GET /crawl/{run_id}.
Tách từ api/schemas.py (08/2026) — xem docstring api/schemas/__init__.py.
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


# ------------------------------------------------------------------
# Crawl trigger
# ------------------------------------------------------------------

class CrawlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    
    source: str = Field(..., examples=["topcv", "vietnamworks"])
    category: str = Field(..., examples=["data-analyst", "data-engineer", "software-engineering"])
    # Optional (khác bản cũ default=3) — để phân biệt được "không truyền
    # pages" với "truyền đúng giá trị mặc định", giống cách main.py CLI
    # xử lý --pages/--max-jobs (xem crawl_runner.resolve_effective_pages()).
    # Nếu bỏ trống CẢ pages lẫn max_jobs, route sẽ tự áp DEFAULT_MAX_PAGES.
    pages: Optional[int] = Field(default=None, ge=1, le=20)
    # Giới hạn TỔNG SỐ JD sẽ crawl, dừng ngay khi đủ (không cần đợi hết
    # pages) — khớp 1-1 với --max-jobs đã có ở CLI (main.py). Có thể
    # dùng CÙNG pages (dừng ở điều kiện nào tới trước); nếu chỉ truyền
    # max_jobs mà không truyền pages, tự động nới pages đủ lớn để
    # max_jobs là giới hạn thực sự (xem crawl_runner.py).
    max_jobs: Optional[int] = Field(default=None, ge=1, le=1000)


class CrawlAccepted(BaseModel):
    run_id: str
    status: str  # "queued" | "running" | "done" | "error"


class CrawlStatusOut(BaseModel):
    run_id: str
    status: str
    source: str
    category: str
    pages: int
    max_jobs: Optional[int] = None
    # 08/2026 (xem sql/migration_add_crawl_runs.sql) — admin nào bấm
    # crawl. NULL dành sẵn cho crawl tự động theo lịch sau này (không
    # ai bấm), không phải lỗi dữ liệu — cùng quy ước actor_id ở
    # AuditLogOut.
    triggered_by: Optional[str] = None
    triggered_by_name: Optional[str] = Field(
        default=None, description="full_name của admin đã bấm crawl, join sống "
                                   "TẠI THỜI ĐIỂM TRUY VẤN — null nếu triggered_by "
                                   "null hoặc tài khoản đã bị xoá.",
    )
    started_at: datetime
    finished_at: Optional[datetime] = None
    stats: Optional[dict] = None
    error: Optional[str] = None
    # 08/2026 (xem sql/migration_add_crawl_progress_logs.sql) — snapshot
    # tiến độ MỚI NHẤT, ghi đè liên tục trong lúc status='running'
    # ({"fetched": int, "inserted": int, "last_update": iso str}).
    # None khi chưa có heartbeat nào (vd status vẫn 'queued', hoặc lượt
    # crawl chạy TRƯỚC KHI tính năng này tồn tại) — frontend coi None
    # như "chưa có số liệu" chứ không phải lỗi.
    progress: Optional[dict] = None


# ------------------------------------------------------------------
# Log live — GET /crawl/{run_id}/logs (08/2026, xem docstring
# sql/migration_add_crawl_progress_logs.sql và api/crawl_runner.py::
# _RunLogHandler)
# ------------------------------------------------------------------

class CrawlLogOut(BaseModel):
    id: int
    level: str
    message: str
    created_at: datetime


class CrawlLogsOut(BaseModel):
    # last_id: id LỚN NHẤT trong "items" (0 nếu rỗng) — client dùng đúng
    # giá trị này làm after_id cho lần poll KẾ TIẾP, không tự cộng dồn ở
    # phía client (tránh lệch nếu có dòng bị bỏ sót do limit).
    last_id: int
    items: list[CrawlLogOut]


# ------------------------------------------------------------------
# Lịch sử crawl — GET /crawl (08/2026, phương án "bảng crawl_runs
# riêng", xem sql/migration_add_crawl_runs.sql)
# ------------------------------------------------------------------

class PaginatedCrawlRuns(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[CrawlStatusOut]

