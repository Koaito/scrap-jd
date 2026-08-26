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

08/2026 — THÊM heartbeat/tiến độ real-time + log live (xem migration
sql/migration_add_crawl_progress_logs.sql):
  - crawl_runs.progress: snapshot {fetched, inserted, last_update} ghi
    đè liên tục (throttle 1 lần/giây) qua callback on_progress truyền
    xuống pipeline.run_pipeline() — khu "Hiện tại" ở /crawl poll cột
    này qua GET /crawl/{run_id} (đã có sẵn field "progress" trong
    response, xem api/schemas/crawl.py::CrawlStatusOut).
  - crawl_run_logs (bảng riêng): từng dòng log kiểu terminal, ghi qua
    _RunLogHandler (logging.Handler gắn tạm vào root logger trong lúc
    execute() chạy) — khu "Xem log live" ở /crawl poll GET
    /crawl/{run_id}/logs?after_id=N.

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
import time
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


class _RunLogHandler(logging.Handler):
    """logging.Handler tạm thời, gắn vào ROOT logger đúng lúc execute()
    bắt đầu chạy pipeline thật và GỠ RA ngay khi xong (finally) — nhờ
    vậy bắt được TOÀN BỘ log do pipeline.py, adapters/topcv.py,
    adapters/vietnamworks.py phát ra qua logger chuẩn
    (logging.getLogger(__name__) ở từng file) trong đúng khoảng thời
    gian lượt crawl này chạy, mà KHÔNG cần sửa từng file logger.info()
    rải rác thành 2 lời gọi (1 cái cũ + 1 cái ghi DB).

    Gắn ở ROOT (không phải 1 logger cụ thể) vì pipeline.py/adapters/*.py
    mỗi file có 1 logger riêng theo tên module — bắt ở root là cách duy
    nhất tóm được hết mà không cần liệt kê tên từng module.

    Rủi ro: nếu server chạy NHIỀU crawl cùng lúc (2 nguồn TopCV +
    VietnamWorks chạy song song, đúng use case thật của trang /crawl),
    2 handler cùng gắn vào root cùng lúc — MỖI handler tự lọc bằng cách
    chỉ nhận log record nào có run_id khớp (gắn kèm run_id vào LogRecord
    qua logging.LoggerAdapter ở nơi gọi... nhưng pipeline.py/adapters
    dùng logger thường, không phải LoggerAdapter, nên KHÔNG có run_id
    trong record để lọc).

    -> Chấp nhận: khi 2 lượt crawl chạy song song, log của cả 2 sẽ được
    ghi lẫn vào CẢ HAI run's log (mỗi handler ghi mọi record nó nhận
    được, kể cả record phát sinh từ lượt crawl kia). Đây là đánh đổi
    chấp nhận được cho use case xem log kiểu "console" (người xem tự
    phân biệt qua nội dung dòng log, vd có tên nguồn TopCV/VietnamWorks
    trong message) — KHÔNG dùng bảng crawl_run_logs này cho mục đích cần
    tách bạch tuyệt đối theo run_id (vd audit). Nếu sau này cần tách
    tuyệt đối, đổi pipeline.py/adapters/*.py sang dùng
    logging.LoggerAdapter(extra={"run_id": ...}) truyền run_id thật vào
    LogRecord, rồi lọc record.run_id != self.run_id ở đây."""

    def __init__(self, run_id: str):
        super().__init__(level=logging.INFO)
        self.run_id = run_id
        # Mở connection RIÊNG cho việc ghi log (không dùng chung conn với
        # execute()/run_pipeline() — record log có thể tới bất kỳ lúc nào
        # giữa các câu lệnh SQL khác của run_pipeline(), dùng chung conn
        # sẽ làm rối transaction đang dang dở của nó).
        self._conn = db_module.get_connection()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            db_module.append_crawl_run_log(
                self._conn, self.run_id, record.levelname, self.format(record),
            )
        except Exception:  # noqa: BLE001 - lỗi ghi log KHÔNG được làm crawl thật bị dừng
            pass

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        super().close()


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


def get_latest_run() -> Optional[dict]:
    """Đọc lượt crawl GẦN NHẤT (bất kể status) — dùng cho GET
    /crawl/latest-log-run, để khung "Log live" ở frontend luôn có 1
    run_id để hiện log ngay cả khi không có lượt nào đang chạy (xem
    lịch sử trao đổi "khung Log live luôn hiện cố định trên trang")."""
    conn = db_module.get_connection()
    try:
        return db_module.get_latest_crawl_run(conn)
    finally:
        conn.close()


def get_logs(run_id: str, after_id: int = 0, limit: int = 500):
    """Đọc các dòng log MỚI (id > after_id) của 1 lượt crawl — dùng cho
    GET /crawl/{run_id}/logs?after_id=N (xem db.get_crawl_run_logs)."""
    conn = db_module.get_connection()
    try:
        return db_module.get_crawl_run_logs(conn, run_id, after_id=after_id, limit=limit)
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


def start_batch(source: str, categories: list, pages: Optional[int],
                 max_jobs: Optional[int] = None,
                 triggered_by: Optional[str] = None):
    """08/2026 (xem docstring sql/migration_add_crawl_batches.sql) —
    "crawl nhiều category liên tục": tạo 1 crawl_batches mới + 1 dòng
    crawl_runs ĐẦU TIÊN (category đầu tiên trong `categories`,
    batch_position=0), trả về (batch_id, first_run_id).

    Nơi gọi (route POST /crawl/batch) CHỈ cần add background task gọi
    execute(first_run_id) NHƯ BÌNH THƯỜNG (y hệt crawl đơn lẻ) — KHÔNG
    cần tự lo các category còn lại: execute() tự phát hiện run này
    thuộc 1 batch (qua run["batch_id"]) và tự tạo + chạy tiếp category
    kế sau khi run hiện tại xong, xem docstring execute() bên dưới.

    Raise db.ActiveCrawlExistsError nếu source này đang có 1 lượt
    'queued'/'running' chưa xong (category đầu tiên đụng UNIQUE INDEX
    y hệt start_crawl() đơn lẻ) — router bắt lỗi này để trả 409, batch
    KHÔNG được tạo trong trường hợp đó (create_run() raise TRƯỚC khi
    kịp ghi crawl_batches... thật ra crawl_batches đã ghi trước, xem
    lưu ý bên dưới)."""
    if not categories:
        raise ValueError("categories rỗng — cần ít nhất 1 category để tạo batch.")

    effective_pages = resolve_effective_pages(pages, max_jobs)
    conn = db_module.get_connection()
    try:
        # Tạo crawl_batches TRƯỚC (cần batch_id để category đầu trỏ
        # vào) — nếu create_crawl_run() bên dưới raise
        # ActiveCrawlExistsError, dòng crawl_batches này sẽ CÔ LẬP
        # (không có run con nào, mãi 'running') vì đã commit riêng.
        # Chấp nhận đánh đổi này (giống mọi hàm create_batch/create_run
        # khác trong module, tự commit ngay, không dùng transaction lồng
        # nhau) — router vẫn trả đúng 409 cho người dùng thấy ngay tại
        # chỗ, dòng batch mồ côi này vô hại (không hiện ở GET
        # /crawl/batch nếu frontend chỉ query theo status='running' gần
        # đây, và không chặn crawl nguồn khác) — dọn tay nếu cần thiết,
        # không phải lỗi nghiêm trọng ở quy mô nội bộ hiện tại.
        batch_id = db_module.create_crawl_batch(
            conn, source=source, categories=categories, pages=effective_pages,
            max_jobs=max_jobs, triggered_by=triggered_by,
        )
        first_run_id = db_module.create_crawl_run(
            conn, source=source, category=categories[0], pages=effective_pages,
            max_jobs=max_jobs, triggered_by=triggered_by,
            batch_id=batch_id, batch_position=0,
        )
        return batch_id, first_run_id
    finally:
        conn.close()


def get_batch(batch_id: str) -> Optional[dict]:
    """Đọc 1 batch KÈM danh sách run con (đúng thứ tự) + tiến độ tổng
    (total/completed) — dùng cho GET /crawl/batch/{batch_id}."""
    conn = db_module.get_connection()
    try:
        return db_module.get_crawl_batch_with_items(conn, batch_id)
    finally:
        conn.close()


def list_batches(*, source: Optional[str] = None, status: Optional[str] = None,
                  triggered_by: Optional[str] = None, limit: int = 50, offset: int = 0):
    """Đọc danh sách lịch sử batch — dùng cho GET /crawl/batch."""
    conn = db_module.get_connection()
    try:
        return db_module.list_crawl_batches(
            conn, source=source, status=status, triggered_by=triggered_by,
            limit=limit, offset=offset,
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
    với transaction của run_pipeline().

    08/2026 (xem docstring sql/migration_add_crawl_batches.sql) — nếu
    run này thuộc 1 batch ("crawl nhiều category liên tục"), SAU KHI
    run xong (done HOẶC error, cả 2 đều tính là "category này đã xử lý
    xong") hàm TỰ tạo + gọi tiếp execute() cho category kế tiếp trong
    batch, ngay trong CÙNG background task này (không cần
    BackgroundTasks.add_task() thêm lần nào nữa, không cần APScheduler/
    queue riêng) — đây là lý do toàn bộ cơ chế batch không cần đổi gì ở
    BackgroundTasks/router cho các category thứ 2 trở đi, xem
    start_batch(). Đệ quy tail-call — chấp nhận được vì 1 batch tối đa
    vài chục category (giới hạn ở CrawlBatchRequest), không có rủi ro
    tràn stack thực tế."""
    next_run_id = _execute_one(run_id)
    if next_run_id is not None:
        execute(next_run_id)


def _execute_one(run_id: str) -> Optional[str]:
    """Chạy ĐÚNG 1 run (1 category), trả về run_id của category KẾ TIẾP
    nếu run này thuộc 1 batch và batch còn category chưa crawl, hoặc
    None nếu run đơn lẻ / batch đã hết category — execute() ở trên là
    vòng lặp (đệ quy tail-call) gọi hàm này liên tục cho tới khi trả về
    None. Tách riêng khỏi execute() để execute() không phình to lẫn 2
    trách nhiệm (chạy 1 run thật + quyết định có chạy tiếp hay không)
    trong cùng 1 khối try/finally lồng nhau khó đọc."""
    conn = db_module.get_connection()
    next_run_id: Optional[str] = None
    try:
        run = db_module.get_crawl_run(conn, run_id)
        if run is None:
            logger.error("execute() gọi với run_id không tồn tại: %s", run_id)
            return None

        adapter_cls = _SOURCE_ADAPTERS.get(run["source"])
        if adapter_cls is None:
            db_module.mark_crawl_run_error(
                conn, run_id, f"Source '{run['source']}' không tồn tại.",
            )
        else:
            db_module.mark_crawl_run_running(conn, run_id)

            log_handler = _RunLogHandler(run_id)
            root_logger = logging.getLogger()
            root_logger.addHandler(log_handler)

            # Throttle ghi progress xuống DB tối đa 1 lần/giây — on_progress
            # trong pipeline.py gọi lại SAU MỖI JOB (có thể hàng chục
            # job/giây với trang ít lỗi mạng), ghi DB mỗi lần sẽ tốn round
            # -trip vô ích và làm chậm crawl thật không cần thiết. Progress
            # dùng để NGƯỜI XEM theo dõi bằng mắt + watchdog phát hiện
            # "treo lâu" (phút, không phải giây) nên 1 lần/giây là đủ mịn.
            _last_write = {"t": 0.0}

            def _on_progress(p: dict) -> None:
                now = time.monotonic()
                if now - _last_write["t"] < 1.0:
                    return
                _last_write["t"] = now
                db_module.update_crawl_run_progress(conn, run_id, {
                    "fetched": p["fetched"],
                    "inserted": p["inserted"],
                    "last_update": datetime.now(timezone.utc).isoformat(),
                })

            try:
                adapter = adapter_cls()
                stats = run_pipeline(
                    adapter, conn, run["category"], run["pages"],
                    max_jobs=run.get("max_jobs"), on_progress=_on_progress,
                )
                # Ghi progress LẦN CUỐI không qua throttle — đảm bảo con số
                # hiển thị cuối cùng khớp đúng stats thật trả về, không kẹt
                # lại ở giá trị của lần throttle gần nhất (có thể cũ hơn tới
                # gần 1 giây so với thực tế).
                db_module.update_crawl_run_progress(conn, run_id, {
                    "fetched": stats["fetched"],
                    "inserted": stats["inserted"],
                    "last_update": datetime.now(timezone.utc).isoformat(),
                })
                db_module.mark_crawl_run_done(conn, run_id, stats)
            except Exception as exc:  # noqa: BLE001 - ghi lại lỗi vào run, không làm chết background task
                logger.error("Crawl run %s lỗi: %s", run_id, exc)
                db_module.mark_crawl_run_error(conn, run_id, str(exc))
            finally:
                root_logger.removeHandler(log_handler)
                log_handler.close()

        # 08/2026 (xem docstring execute() ở trên) — run vừa xong (done
        # HOẶC error, kể cả nhánh "source không tồn tại" phía trên đều
        # tính) thuộc 1 batch thì tự tạo category kế tiếp. Đặt SAU khối
        # if/else trên (không phải bên trong) để chạy cho MỌI nhánh kết
        # thúc của run này, không riêng nhánh "chạy pipeline thành công".
        if run.get("batch_id"):
            try:
                next_run_id = db_module.advance_crawl_batch(
                    conn, run["batch_id"], run["batch_position"],
                )
            except Exception as exc:  # noqa: BLE001 - lỗi advance KHÔNG được làm mất kết quả run vừa xong
                logger.error(
                    "Batch %s: không tạo được category kế tiếp sau run %s: %s",
                    run["batch_id"], run_id, exc,
                )
                db_module.mark_crawl_batch_error(conn, run["batch_id"], str(exc))
    finally:
        conn.close()
    return next_run_id
