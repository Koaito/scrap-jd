import logging
import random
import time
from abc import ABC, abstractmethod
from typing import Iterator, Optional

from curl_cffi import requests as curl_requests

from models import RawJobRecord
from config import REQUEST_DELAY_SECONDS

logger = logging.getLogger(__name__)


class CrawlBlockedError(Exception):
    """Raise khi TRANG ĐẦU TIÊN (page 1, hoặc page 0 với adapter đánh số
    từ 0 như VietnamWorks) của 1 lượt crawl không lấy được HTML/response
    sau khi đã hết retry (403/429/lỗi kết nối liên tục) — đây là tín hiệu
    RẤT CÓ THỂ bị chặn (WAF/rate-limit), KHÁC HẲN với "chạy xong nhưng
    đúng là hết job" (0 job mới nhưng có lấy được HTML).

    QUAN TRỌNG: chỉ raise ở TRANG ĐẦU. Các trang SAU (page >= 2/1) thất
    bại vẫn giữ nguyên hành vi cũ (break, coi là hết trang/tạm dừng bình
    thường) — không phải mọi lần fetch thất bại giữa chừng đều là bị
    chặn, có thể chỉ là đã hết dữ liệu thật.

    KHÔNG bị pipeline.run_pipeline() nuốt mất (nó raise ra ngoài vòng
    for, không nằm trong try/except bọc từng job) — lan thẳng lên
    api/crawl_runner.py::execute(), nơi bắt bằng except Exception chung
    và ghi status='error' + error message thay vì 'done' với stats toàn
    số 0, để UI (bảng Lịch sử crawl) phân biệt được 2 tình huống: "bị
    chặn ngay từ đầu" vs "chạy xong, đúng là 0 job mới"."""


class BaseAdapter(ABC):
    """
    Mọi adapter nguồn (TopCV, ITviec, VietnamWorks, ...) phải kế thừa class
    này và implement fetch_jobs(). Pipeline lõi (pipeline.py) chỉ gọi qua
    interface này -> không cần biết chi tiết bên trong từng nguồn.
    """

    source_name: str = "unknown"

    def __init__(
        self,
        session: Optional["curl_requests.Session"] = None,
        headers: Optional[dict] = None,
        delay_seconds: float = REQUEST_DELAY_SECONDS,
        jitter_seconds: float = 0.0,
    ):
        """Hạ tầng HTTP dùng chung cho MỌI adapter — session curl_cffi +
        throttle + retry/backoff.

        08/2026: rút ra sau khi xác nhận TopCVAdapter/VietnamWorksAdapter/
        CareerVietAdapter có __init__(), _fetch_html(), _throttle() gần
        như copy-paste 1:1 giữa 3 file (chỉ khác biến delay) — hậu quả
        thực tế: 1 bug retry logic (lỗi kết nối không có status code,
        vd HTTP/2 stream reset, trước đây bỏ cuộc ngay không thử lại)
        từng phải vá THỦ CÔNG lặp lại ở cả 3 nơi (xem comment "SỬA
        08/2026 (đồng bộ với careerviet.py/vietnamworks.py)" còn sót
        lại trong lịch sử các file adapter). Gộp về đây để sửa 1 chỗ
        duy nhất áp dụng cho mọi nguồn hiện tại lẫn nguồn thêm sau này.

        delay_seconds/jitter_seconds cho subclass tự chỉnh độ trễ riêng
        mà KHÔNG cần override lại _throttle()/_fetch_html(). Trường hợp
        điển hình: TopCV cần delay cao hơn + jitter ngẫu nhiên do bị
        chặn theo IP reputation khi crawl từ server (xem docstring
        TOPCV_REQUEST_DELAY_SECONDS/TOPCV_REQUEST_JITTER_SECONDS ở
        config.py), trong khi VietnamWorks/CareerViet dùng chung mức
        delay mặc định (REQUEST_DELAY_SECONDS), không cần jitter.

        impersonate="chrome124" mặc định cho MỌI adapter (giả lập TLS/
        JA3 fingerprint Chrome — TopCV chặn 403 theo tầng bắt tay TLS,
        không phải chỉ theo header, xem docstring gốc trong lịch sử
        topcv.py). CareerViet/VietnamWorks chưa có bằng chứng cần điều
        này, nhưng dùng chung không mất gì và cả 3 adapter đã tự chọn
        y hệt nhau trước khi gộp — giờ chỉ còn 1 chỗ quyết định.
        """
        self.session = session or curl_requests.Session(impersonate="chrome124")
        if headers:
            self.session.headers.update(headers)
        # Mốc thời gian của request GẦN NHẤT (bất kể listing/job
        # detail/company profile) — dùng để throttle MỌI request ở 1
        # chỗ duy nhất trong _fetch_html(), thay vì rải rác time.sleep()
        # ở từng nơi gọi (dễ quên, dễ sót -> vẫn bị 429 dù đã tăng delay).
        self._last_request_time: Optional[float] = None
        self._delay_seconds = delay_seconds
        self._jitter_seconds = jitter_seconds

    @abstractmethod
    def fetch_jobs(self, category_key: str, max_pages: int) -> Iterator[RawJobRecord]:
        """Trả về (yield) từng RawJobRecord tìm được cho category_key."""
        raise NotImplementedError

    def fetch_company_profile(self, company_url: str) -> dict:
        """Optional: crawl sâu vào trang hồ sơ công ty để lấy thêm website
        thật, địa chỉ, quy mô, lĩnh vực. Mặc định trả dict rỗng (nguồn nào
        không hỗ trợ thì bỏ qua, pipeline vẫn chạy bình thường)."""
        return {}

    def fetch_job_full_detail(self, source_url: str) -> Optional[dict]:
        """Optional: crawl sâu vào trang chi tiết job để lấy thêm các field
        chỉ hiển thị ở đó (không có trên trang listing) — vd 'work_type'
        (Loại hình làm việc), 'deadline_text' (Hạn ứng tuyển), và nội dung
        mô tả đầy đủ ('job_description', 'requirements', 'perks',
        'required_skills').

        Trả dict (có thể có field rỗng "" / [] nếu trang không có đủ mọi
        khối — không coi là lỗi) khi fetch THÀNH CÔNG.
        Trả None khi fetch THẤT BẠI thật sự (network error, bị chặn...)
        — pipeline dùng tín hiệu None này để quyết định bỏ hẳn job đó
        thay vì insert với dữ liệu thiếu một cách âm thầm.
        Mặc định trả dict rỗng-an-toàn (nguồn nào không hỗ trợ tính năng
        này thì coi như luôn "thành công" với dữ liệu rỗng, pipeline vẫn
        chạy bình thường, không bị hiểu nhầm là "fetch thất bại")."""
        return {
            "work_type": "", "deadline_text": "", "job_description": "",
            "requirements": "", "perks": "", "required_skills": [],
        }

    # ------------------------------------------------------------------
    # HTTP layer dùng chung (throttle + retry/backoff) — xem docstring
    # __init__() phía trên để biết lý do rút lên đây. Subclass ghi đè
    # delay_seconds/jitter_seconds qua super().__init__() thay vì
    # override lại 2 method này; chỉ override thật sự nếu nguồn dùng
    # giao thức khác hẳn GET-HTML (vd VietnamWorksAdapter._post_json()
    # cho API JSON riêng — vẫn tận dụng lại _throttle() dùng chung).
    # ------------------------------------------------------------------
    def _throttle(self):
        """Đảm bảo khoảng cách tối thiểu delay_seconds (+ jitter ngẫu
        nhiên nếu subclass truyền jitter_seconds > 0, vd TopCV) giữa MỌI
        request, bất kể listing/job detail/company profile."""
        min_delay = self._delay_seconds
        if self._jitter_seconds:
            # jitter ngẫu nhiên: khoảng cách giữa các request KHÔNG cố
            # định tăm tắp — pattern đều đặn dễ bị WAF nhận diện là bot
            # hơn khoảng dao động tự nhiên như người dùng thật.
            min_delay += random.uniform(0, self._jitter_seconds)
        if self._last_request_time is None:
            return
        elapsed = time.monotonic() - self._last_request_time
        remaining = min_delay - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _fetch_html(self, url: str, max_retries: int = 3) -> Optional[str]:
        """MỌI request HTTP GET-HTML của adapter (listing, job detail,
        company profile) nên đi qua đây -> throttle + retry-backoff áp
        dụng đồng đều, không phụ thuộc nơi gọi có nhớ delay hay không.

        Lỗi cũ (trước khi gộp về BaseAdapter): delay chỉ được sleep()
        giữa các trang listing trong fetch_jobs() của từng adapter,
        trong khi fetch_job_full_detail()/fetch_company_profile() (gọi
        cho MỖI job / MỖI công ty mới trong pipeline.py) gọi thẳng
        _fetch_html() không qua throttle -> bắn hàng chục request liên
        tiếp không nghỉ dù config đã tăng delay lên."""
        self._throttle()

        for attempt in range(1, max_retries + 1):
            try:
                resp = self.session.get(url, timeout=20)
                if resp.status_code in (429, 403):
                    # 429 = rate limit theo cửa sổ thời gian.
                    # 403 = WAF/Cloudflare chặn theo fingerprint request
                    # (có thể do thiếu header giống trình duyệt thật,
                    # HOẶC IP tạm thời bị đánh dấu do crawl dồn dập
                    # trước đó) — cả 2 trường hợp đều ĐÁNG thử lại sau
                    # khi chờ, thay vì bỏ cuộc ngay ở request đầu tiên.
                    wait = self._delay_seconds * (2 ** attempt)
                    logger.warning(
                        "%d tại %s (lần %d/%d) -> chờ %.1fs",
                        resp.status_code, url, attempt, max_retries, wait,
                    )
                    time.sleep(wait)
                    self._last_request_time = time.monotonic()
                    continue
                resp.raise_for_status()
                self._last_request_time = time.monotonic()
                return resp.text
            except curl_requests.exceptions.RequestException as exc:
                # Retry cả lỗi kết nối không có status code (vd HTTP/2
                # stream reset, timeout...), không bỏ cuộc ngay ở lần
                # lỗi đầu tiên — đã xác nhận bằng dữ liệu thật (08/2026)
                # loại lỗi này thường chỉ TẠM THỜI (WAF chặn tạm do
                # request dồn dập), không phải trang đã đổi/hết dữ liệu.
                wait = self._delay_seconds * (2 ** attempt)
                logger.warning(
                    "Lỗi kết nối tại %s (lần %d/%d): %s -> chờ %.1fs rồi thử lại",
                    url, attempt, max_retries, exc, wait,
                )
                time.sleep(wait)
                self._last_request_time = time.monotonic()
                continue

        logger.error("Bỏ cuộc sau %d lần liên tiếp (429/403/lỗi kết nối): %s", max_retries, url)
        return None
