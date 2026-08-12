"""
Chạy pipeline crawl (adapters/*.py -> pipeline.run_pipeline()) ở NỀN, kích
hoạt từ 1 HTTP request (POST /crawl) nhưng không chặn request đó chờ tới
lúc crawl xong (crawl thật có thể mất vài phút - vài chục phút tuỳ số
trang, HTTP request giữ lâu vậy sẽ timeout ở phía client/proxy).

CÁCH HOẠT ĐỘNG:
  1. POST /crawl -> tạo 1 run_id (uuid4), lưu status "queued" vào
     _RUNS (dict trong RAM), trả về run_id NGAY LẬP TỨC.
  2. FastAPI BackgroundTasks chạy execute() sau khi response đã trả -
     execute() tự mở connection DB riêng (không dùng chung connection
     của request gốc, vì request đó đã kết thúc), gọi thẳng
     pipeline.run_pipeline() y hệt main.py CLI đang làm.
  3. Client gọi GET /crawl/{run_id} để poll tiến độ, đọc "status" +
     "stats" khi xong.

max_jobs (08/2026, khớp với --max-jobs đã có ở CLI): body POST /crawl
có thể kèm "max_jobs" để giới hạn TỔNG SỐ JD thay vì tính theo trang —
xem resolve_effective_pages() để biết cách "pages" tự được tính lại khi
chỉ truyền "max_jobs" mà không truyền "pages".

GIỚI HẠN ĐÃ BIẾT (chấp nhận được ở quy mô hiện tại — 1 process, ít
người dùng nội bộ; KHÔNG phù hợp nếu deploy nhiều worker/instance):
  - _RUNS lưu trong RAM của process -> mất hết nếu restart server, và
    KHÔNG đồng bộ nếu chạy nhiều worker uvicorn (--workers > 1) vì mỗi
    worker có RAM riêng, request tạo run ở worker A nhưng poll trúng
    worker B sẽ không thấy.
  - Không giới hạn số crawl chạy song song -> nếu gọi API dồn dập nhiều
    lần, có thể có nhiều pipeline chạy cùng lúc, tốn tài nguyên/network
    hơn dự tính (adapter TopCV/VNW vẫn tự có REQUEST_DELAY_SECONDS
    riêng, không đến mức dội request quá nhanh, nhưng vẫn nên tự giới
    hạn ở phía frontend, vd disable nút "Crawl" khi đang chạy).

NÂNG CẤP SAU (chỉ làm khi thật sự cần, đừng làm sớm — đúng tinh thần
"free-tier, rẻ, an toàn trước" xuyên suốt project):
  - Cần chạy nhiều worker / cần chạy được cả khi server restart -> đổi
    sang queue thật (Celery + Redis, hoặc RQ) thay cho dict RAM này.
  - Cần lịch crawl tự động định kỳ -> thêm APScheduler hoặc cron gọi
    thẳng main.py (không cần qua API).
"""

import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import db as db_module
from pipeline import run_pipeline
from adapters.topcv import TopCVAdapter
from adapters.vietnamworks import VietnamWorksAdapter
from config import DEFAULT_MAX_PAGES

logger = logging.getLogger(__name__)

# Đăng ký nguồn — GIỮ ĐỒNG BỘ với SOURCES trong main.py. Nếu thêm nguồn
# mới (vd ITviec) nhớ sửa CẢ 2 chỗ, hoặc tốt hơn: sau này tách SOURCES
# ra 1 module dùng chung (vd sources_registry.py) để main.py và API đều
# import từ đó, tránh lệch nhau — chưa làm ở bản khung này để giữ đơn
# giản, không sửa main.py hiện có.
_SOURCE_ADAPTERS = {
    "topcv": TopCVAdapter,
    "vietnamworks": VietnamWorksAdapter,
}

# dict trong RAM: run_id -> thông tin lượt crawl. Xem giới hạn ở docstring trên.
_RUNS: dict[str, dict] = {}


def get_run(run_id: str) -> Optional[dict]:
    return _RUNS.get(run_id)


def resolve_effective_pages(pages: Optional[int], max_jobs: Optional[int]) -> int:
    """Y HỆT logic trong main.py cmd_crawl() (--pages/--max-jobs) — tách
    thành hàm riêng ở đây để CLI và API cùng resolve theo 1 quy tắc, dễ
    đối chiếu khi đọc code cả 2 phía (không copy-paste rời rạc).

    - Có truyền pages -> dùng đúng giá trị đó.
    - Không truyền pages nhưng có max_jobs -> nới pages lên rất cao,
      để max_jobs mới là giới hạn thực sự (pipeline.run_pipeline() dừng
      ngay khi đủ max_jobs, không thật sự crawl hết số trang này).
    - Không truyền gì cả -> dùng DEFAULT_MAX_PAGES như trước giờ."""
    if pages is not None:
        return pages
    if max_jobs is not None:
        return 999
    return DEFAULT_MAX_PAGES


def start_crawl(source: str, category: str, pages: Optional[int],
                 max_jobs: Optional[int] = None) -> str:
    """Tạo 1 run mới, trả về run_id NGAY (chưa chạy thật) — nơi gọi
    (route) chịu trách nhiệm add background task gọi execute() sau."""
    run_id = str(uuid.uuid4())
    effective_pages = resolve_effective_pages(pages, max_jobs)
    _RUNS[run_id] = {
        "run_id": run_id,
        "status": "queued",
        "source": source,
        "category": category,
        "pages": effective_pages,
        "max_jobs": max_jobs,
        "started_at": datetime.now(timezone.utc),
        "finished_at": None,
        "stats": None,
        "error": None,
    }
    return run_id


def execute(run_id: str) -> None:
    """Chạy pipeline THẬT — gọi từ BackgroundTasks, KHÔNG gọi trực tiếp
    trong request handler. Tự mở/đóng connection riêng."""
    run = _RUNS.get(run_id)
    if run is None:
        logger.error("execute() gọi với run_id không tồn tại: %s", run_id)
        return

    adapter_cls = _SOURCE_ADAPTERS.get(run["source"])
    if adapter_cls is None:
        run["status"] = "error"
        run["error"] = f"Source '{run['source']}' không tồn tại."
        run["finished_at"] = datetime.now(timezone.utc)
        return

    run["status"] = "running"
    conn = db_module.get_connection()
    try:
        adapter = adapter_cls()
        stats = run_pipeline(
            adapter, conn, run["category"], run["pages"],
            max_jobs=run.get("max_jobs"),
        )
        run["stats"] = stats
        run["status"] = "done"
    except Exception as exc:  # noqa: BLE001 - ghi lại lỗi vào run, không làm chết background task
        logger.error("Crawl run %s lỗi: %s", run_id, exc)
        run["status"] = "error"
        run["error"] = str(exc)
    finally:
        conn.close()
        run["finished_at"] = datetime.now(timezone.utc)
