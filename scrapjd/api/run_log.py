"""
Ghi log "live" của job nền (crawl, maintenance) xuống DB, tách riêng theo
từng lượt chạy.

Cách hoạt động
--------------
capture_run_logs() gắn tạm 1 logging.Handler vào ROOT logger và đặt 1
ContextVar đánh dấu "luồng code hiện tại đang chạy dưới run này". Handler
chỉ nhận record phát ra từ code mang đúng dấu đó, nên:

  - 2 lượt chạy song song (TopCV + VietnamWorks, hoặc crawl + maintenance)
    không còn ghi log lẫn vào nhau;
  - log của request API khác, của uvicorn hay của thư viện chạy ở thread
    khác trong lúc lượt này chạy cũng không lọt vào log của lượt.

Bắt ở ROOT (không gắn vào từng logger) vì scrapjd/pipeline.py, scrapjd/adapters/*.py và các
script enrich đều dùng logging.getLogger(__name__) riêng — gắn ở root là cách
duy nhất tóm được hết mà không phải sửa từng nơi gọi logger.

Giới hạn cần biết
-----------------
ContextVar đi theo thread/task đã đặt nó. Code chạy trong thread con do chính
job tạo ra (ThreadPoolExecutor, threading.Thread) KHÔNG thừa kế dấu này, log
từ thread đó sẽ không vào log của lượt. Hiện không có đoạn nào của crawl hay
maintenance làm vậy (đã kiểm tra); nếu sau này thêm, phải chạy thread con
bằng contextvars.copy_context().run(...) để giữ log.

Connection ghi log
------------------
Handler giữ 1 connection RIÊNG (không dùng chung với connection đang chạy job):
record log có thể tới bất kỳ lúc nào giữa các câu SQL của job, dùng chung sẽ
làm rối transaction đang dở. Connection này CỐ Ý mở ngoài pool (sống suốt
job, xem mục CONNECTION POOL trong docstring scrapjd/api/crawl_runner.py) nên được
tính vào ngân sách kết nối ở scrapjd/api/concurrency.py.

Hàm mở kết nối và hàm ghi log do nơi gọi truyền vào, không import db ở đây,
để mỗi runner vẫn dùng module db của chính nó (và để test thay thế được).
"""

import contextvars
import logging
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

# Dấu của lượt chạy hiện tại. Mỗi lần capture_run_logs() dùng 1 object mới
# (không dùng run_id) nên 2 lần capture cho cùng 1 run_id cũng không lẫn nhau.
_current_scope: contextvars.ContextVar[Optional[object]] = contextvars.ContextVar(
    "run_log_scope", default=None,
)


class RunLogHandler(logging.Handler):
    """Ghi mỗi record thuộc đúng lượt chạy xuống DB qua append_log(conn,
    run_id, level, message). Lỗi khi ghi bị nuốt: log live hỏng không được
    làm job thật dừng theo."""

    def __init__(self, scope: object, run_id: str, conn,
                 append_log: Callable[[object, str, str, str], None]):
        super().__init__(level=logging.INFO)
        self.run_id = run_id
        self._scope = scope
        self._conn = conn
        self._append_log = append_log
        self.addFilter(self._belongs_to_this_run)

    def _belongs_to_this_run(self, record: logging.LogRecord) -> bool:
        return _current_scope.get() is self._scope

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._append_log(self._conn, self.run_id, record.levelname, self.format(record))
        except Exception:  # noqa: BLE001 - lỗi ghi log KHÔNG được làm job thật bị dừng
            pass

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        super().close()


@contextmanager
def capture_run_logs(
    run_id: str,
    *,
    open_connection: Callable[[], object],
    append_log: Callable[[object, str, str, str], None],
) -> Iterator[RunLogHandler]:
    """Trong khối `with`, mọi log INFO trở lên phát ra từ code của lượt này
    được ghi vào DB qua append_log. Thoát khỏi khối (kể cả khi có lỗi) thì gỡ
    handler, đóng connection ghi log và trả ContextVar về giá trị cũ."""
    scope = object()
    handler = RunLogHandler(scope, run_id, open_connection(), append_log)
    token = _current_scope.set(scope)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    try:
        yield handler
    finally:
        root_logger.removeHandler(handler)
        handler.close()
        _current_scope.reset(token)
