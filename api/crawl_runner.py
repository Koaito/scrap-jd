"""
Chạy pipeline crawl (adapters/*.py -> pipeline.run_pipeline()) ở NỀN, kích
hoạt từ 1 HTTP request (POST /crawl) nhưng không chặn request đó chờ tới
lúc crawl xong (crawl thật có thể mất vài phút - vài chục phút tuỳ số
trang, HTTP request giữ lâu vậy sẽ timeout ở phía client/proxy).

CÁCH HOẠT ĐỘNG:
  1. POST /crawl -> start_crawl() insert 1 dòng status='queued' vào
     bảng crawl_runs (Postgres), trả về run_id NGAY LẬP TỨC.
  2. FastAPI BackgroundTasks chạy execute() sau khi response đã trả -
     execute() tự mở connection DB riêng (không dùng chung connection
     của request gốc, vì request đó đã kết thúc), đổi status ->
     'running', gọi thẳng pipeline.run_pipeline() y hệt main.py CLI
     đang làm, rồi đổi status -> 'done'/'error'.
  3. Client gọi GET /crawl/{run_id} để poll tiến độ, đọc "status" +
     "stats" khi xong — đọc thẳng từ bảng crawl_runs.

max_jobs (08/2026, khớp với --max-jobs đã có ở CLI): body POST /crawl
có thể kèm "max_jobs" để giới hạn TỔNG SỐ JD thay vì tính theo trang —
xem resolve_effective_pages() để biết cách "pages" tự được tính lại khi
chỉ truyền "max_jobs" mà không truyền "pages".

08/2026 — ĐỔI TỪ _RUNS (dict RAM) SANG bảng crawl_runs (Postgres, xem
sql/migration_add_crawl_runs.sql), giải quyết 2 giới hạn cũ:
  - _RUNS mất hết nếu restart server -> crawl_runs sống bền qua restart.
  - _RUNS KHÔNG đồng bộ nếu chạy nhiều worker uvicorn (mỗi worker RAM
    riêng) -> mọi worker giờ đọc/ghi CHUNG 1 bảng Postgres.
  - Đồng thời enforce "mỗi nguồn tối đa 1 lượt crawl đang chạy" Ở TẦNG
    DB (UNIQUE INDEX có điều kiện, xem migration) thay vì chỉ dựa vào
    disable nút ở frontend như trước — xem db.ActiveCrawlExistsError.

GIỚI HẠN CÒN LẠI (chấp nhận được ở quy mô hiện tại — ít người dùng nội
bộ; KHÔNG phù hợp nếu cần queue thật với retry/backoff):
  - Không có cơ chế phát hiện "run kẹt mãi ở running" nếu process bị
    kill CỨNG giữa chừng (vd Render OOM-kill) — mark_error() nằm trong
    finally nên bắt được exception Python bình thường, nhưng không bắt
    được process bị giết từ bên ngoài. Dòng đó sẽ đứng yên ở 'running'
    mãi (và do UNIQUE INDEX, nguồn đó sẽ bị "khoá" không crawl lại được
    tới khi có người tự sửa tay status trong DB) — chấp nhận đánh đổi
    này vì tần suất Render bị OOM-kill giữa 1 lượt crawl là rất hiếm ở
    quy mô hiện tại; nếu cần chặt hơn, thêm 1 job định kỳ tự đổi
    'running' quá X phút chưa xong thành 'error'.

NÂNG CẤP SAU (chỉ làm khi thật sự cần, đừng làm sớm — đúng tinh thần
"free-tier, rẻ, an toàn trước" xuyên suốt project):
  - Cần retry/backoff, hàng đợi ưu tiên -> đổi sang queue thật (Celery +
    Redis, hoặc RQ) thay cho BackgroundTasks + bảng này.
  - Cần lịch crawl tự động định kỳ -> thêm APScheduler hoặc cron gọi
    thẳng main.py (không cần qua API) — triggered_by=NULL đã dành sẵn
    chỗ cho trường hợp này (xem sql/migration_add_crawl_runs.sql).
"""

import logging
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


def get_run(run_id: str) -> Optional[dict]:
    """Đọc 1 lượt crawl từ bảng crawl_runs — dùng cho GET /crawl/{run_id}.
    Tự mở/đóng connection riêng (route gọi hàm này KHÔNG truyền conn
    xuống, giữ chữ ký y hệt bản cũ để không phải sửa router nhiều hơn
    cần thiết)."""
    conn = db_module.get_connection()
    try:
        return db_module.get_crawl_run(conn, run_id)
    finally:
        conn.close()


def list_runs(*, source: Optional[str] = None, status: Optional[str] = None,
               triggered_by: Optional[str] = None, limit: int = 50, offset: int = 0):
    """Đọc danh sách lịch sử crawl — dùng cho GET /crawl."""
    conn = db_module.get_connection()
    try:
        return db_module.list_crawl_runs(
            conn, source=source, status=status, triggered_by=triggered_by,
            limit=limit, offset=offset,
        )
    finally:
        conn.close()


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
                 max_jobs: Optional[int] = None,
                 triggered_by: Optional[str] = None) -> str:
    """Tạo 1 run mới (INSERT vào crawl_runs, status='queued'), trả về
    run_id NGAY (chưa chạy thật) — nơi gọi (route) chịu trách nhiệm add
    background task gọi execute() sau.

    triggered_by: ss_user_id của admin đang gọi POST /crawl (user["sub"]
    từ JWT) — router PHẢI truyền giá trị này xuống (trước đây route gọi
    hàm này KHÔNG truyền user xuống dù đã có sẵn từ Depends(require_admin),
    audit_logs/crawl_runs sẽ không biết ai bấm nếu quên bước này).

    Raise db.ActiveCrawlExistsError nếu source này đang có 1 lượt
    'queued'/'running' chưa xong — router bắt lỗi này để trả 409
    (xem api/routers/crawl.py)."""
    effective_pages = resolve_effective_pages(pages, max_jobs)
    conn = db_module.get_connection()
    try:
        return db_module.create_crawl_run(
            conn, source=source, category=category, pages=effective_pages,
            max_jobs=max_jobs, triggered_by=triggered_by,
        )
    finally:
        conn.close()


def execute(run_id: str) -> None:
    """Chạy pipeline THẬT — gọi từ BackgroundTasks, KHÔNG gọi trực tiếp
    trong request handler. Tự mở/đóng connection riêng.

    Dùng 1 connection DUY NHẤT cho cả việc ghi trạng thái (mark_running/
    mark_done/mark_error) LẪN chạy run_pipeline() — run_pipeline() tự
    quản lý transaction insert job/company của riêng nó (xem
    pipeline.py), các hàm mark_*() ở db/crawl_runs.py tự commit() ngay
    sau mỗi lần gọi (xem docstring db/crawl_runs.py) nên không xung đột
    với transaction của run_pipeline()."""
    conn = db_module.get_connection()
    try:
        run = db_module.get_crawl_run(conn, run_id)
        if run is None:
            logger.error("execute() gọi với run_id không tồn tại: %s", run_id)
            return

        adapter_cls = _SOURCE_ADAPTERS.get(run["source"])
        if adapter_cls is None:
            db_module.mark_crawl_run_error(
                conn, run_id, f"Source '{run['source']}' không tồn tại.",
            )
            return

        db_module.mark_crawl_run_running(conn, run_id)
        try:
            adapter = adapter_cls()
            stats = run_pipeline(
                adapter, conn, run["category"], run["pages"],
                max_jobs=run.get("max_jobs"),
            )
            db_module.mark_crawl_run_done(conn, run_id, stats)
        except Exception as exc:  # noqa: BLE001 - ghi lại lỗi vào run, không làm chết background task
            logger.error("Crawl run %s lỗi: %s", run_id, exc)
            db_module.mark_crawl_run_error(conn, run_id, str(exc))
    finally:
        conn.close()
