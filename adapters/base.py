import logging
import random
import time
from abc import ABC, abstractmethod
from typing import Callable, Iterator, Optional

from curl_cffi import requests as curl_requests

from models import RawJobRecord
from config import REQUEST_DELAY_SECONDS, CRAWL_BLOCK_CONSECUTIVE_FAILURES

logger = logging.getLogger(__name__)

# Mã bất thường (BaseAdapter._flag_listing_anomaly) khi 1 trang listing SAU trang
# đầu tải thất bại (đã hết retry) và adapter dừng phân trang: lượt crawl chỉ lấy
# được 1 phần job nhưng vẫn kết thúc 'done'. Cờ này đánh dấu lượt đó là degraded
# thay vì để nó trông như đã crawl đủ.
ANOMALY_LISTING_PAGE_FAILED = "listing_page_failed"


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
    chặn ngay từ đầu" vs "chạy xong, đúng là 0 job mới".

    ĐỢT 3 (10/2026) — raise thêm ở GIỮA CHỪNG (ngắt mạch): khi
    CRAWL_BLOCK_CONSECUTIVE_FAILURES lần fetch liên tiếp thất bại (mỗi lần đã
    hết retry, chưa có request nào thành công xen giữa) thì dừng cả lượt
    thay vì để từng job còn lại tự retry đủ 3 lần rồi bỏ qua — xem
    BaseAdapter._note_fetch_failure(). Nguồn nào cũng dùng chung cơ chế này
    qua _fetch_html()/_post_json().

    `stats`: run_pipeline() gắn thống kê TẠM THỜI (số job đã lưu trước khi bị
    chặn...) vào đây trước khi raise tiếp, để execute() không làm mất con số
    đó khi ghi status='error'. None nếu lỗi raise từ adapter ngoài pipeline."""

    stats: Optional[dict] = None


# Bộ resolver mặc định của BaseAdapter.dedup_resolvers(); tên khớp pipeline.DEDUP_RESOLVERS.
DEFAULT_DEDUP_RESOLVERS = ("job_code", "repost")


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

        # Hook "URL này đã có trong DB và không cần vá nữa -> đừng tốn
        # request fetch trang chi tiết" (đợt 2, 10/2026). Mặc định None =
        # không bỏ qua gì cả, giữ hành vi cũ cho mọi nguồn/test không đặt
        # hook. pipeline.run_pipeline() là nơi đặt hook (xem
        # set_known_url_checker()), adapter nào fetch chi tiết SỚM ngay
        # trong fetch_jobs() (hiện chỉ CareerViet) thì gọi
        # _is_known_url() trước khi fetch để khỏi lặp lại request cho job
        # đã crawl từ các lần trước.
        self._known_url_checker: Optional[Callable[[str], bool]] = None
        # Số URL đã bị bỏ qua nhờ hook trên trong lượt chạy này —
        # pipeline đọc lại cuối lượt để đưa vào stats.
        self.skipped_known_count: int = 0

        # Ngắt mạch (đợt 3, 10/2026): đếm số lần fetch THẤT BẠI liên tiếp (đã
        # hết retry). Về 0 ngay khi có 1 request thành công. Đạt ngưỡng ->
        # raise CrawlBlockedError, xem _note_fetch_failure().
        self._consecutive_failures: int = 0
        self._block_threshold: int = CRAWL_BLOCK_CONSECUTIVE_FAILURES

        # Snapshot HTML/JSON gốc để debug (đợt 3) — None = tắt (CLI, test),
        # _snapshot() khi đó là no-op. api/crawl_runner.py gắn recorder thật.
        self.snapshot_recorder = None
        # Các bất thường adapter tự phát hiện ở TRANG LISTING ĐẦU (vd trang
        # tải được nhưng parse ra 0 job). pipeline đọc cuối lượt để đánh dấu
        # degraded — KHÁC CrawlBlockedError (không tải được trang).
        self.listing_anomalies: list = []
        # Số job adapter tự BỎ ngay trong fetch_jobs() vì không lấy/giải mã được
        # trang chi tiết (adapter nào tải chi tiết sớm: CareerViet, VietnamWorks).
        # Job bị bỏ kiểu này không bao giờ tới pipeline nên stats.fetched không
        # đếm; pipeline đọc cuối lượt để đưa vào stats.skipped_detail_unavailable.
        self.skipped_detail_unavailable_count: int = 0

    def set_snapshot_recorder(self, recorder) -> None:
        """Gắn (hoặc gỡ bằng None) SnapshotRecorder cho lượt crawl này."""
        self.snapshot_recorder = recorder

    def _snapshot(self, kind: str, url: str, body, reason: str = "sample") -> None:
        """Đề nghị lưu snapshot gốc. No-op nếu chưa gắn recorder; recorder
        tự quyết định có giữ hay không (xem snapshots.py). Không bao giờ
        raise — debug không được làm hỏng crawl."""
        recorder = self.snapshot_recorder
        if recorder is None:
            return
        try:
            recorder.offer(kind, url, body, reason)
        except Exception:  # noqa: BLE001
            logger.exception("Ghi snapshot lỗi (%s %s), bỏ qua", kind, url)

    def _flag_listing_anomaly(self, code: str) -> None:
        if code not in self.listing_anomalies:
            self.listing_anomalies.append(code)

    def _note_listing_page_failed(self, page_url: str) -> None:
        """Trang listing SAU trang đầu tải thất bại (hết retry): adapter dừng
        phân trang như cũ nhưng báo cờ để lượt này không trông như đã crawl đủ.
        Không raise: 1 trang lỗi lẻ có thể chỉ là tạm thời; nếu bị chặn thật thì
        ngắt mạch (_note_fetch_failure) đã tính các lần thất bại liên tiếp."""
        logger.warning(
            "Trang listing %s tải thất bại sau khi hết retry -> dừng phân trang, "
            "lượt crawl này chỉ lấy được một phần job.", page_url,
        )
        self._flag_listing_anomaly(ANOMALY_LISTING_PAGE_FAILED)

    def _note_job_dropped(self, url: str, reason: str) -> None:
        """Ghi nhận 1 job bị bỏ trong adapter vì trang chi tiết không tải/giải mã
        được (xem skipped_detail_unavailable_count)."""
        self.skipped_detail_unavailable_count += 1
        logger.warning("Bỏ qua job (%s): %s", reason, url)

    @staticmethod
    def _detail_is_blank(detail: Optional[dict]) -> bool:
        """True nếu dict chi tiết job KHÔNG có chút nội dung nào (mô tả, yêu
        cầu, quyền lợi, kỹ năng đều rỗng) — dấu hiệu rõ nhất selector trang
        chi tiết đã hỏng, vì 1 tin tuyển dụng thật luôn có ít nhất 1 khối."""
        if not detail:
            return True
        for key in ("job_description", "requirements", "perks"):
            value = detail.get(key)
            if isinstance(value, str) and value.strip():
                return False
        skills = detail.get("required_skills")
        return not skills

    # ------------------------------------------------------------------
    # Ngắt mạch (đợt 3) — dùng chung cho _fetch_html() và các hàm HTTP riêng
    # của adapter (vd VietnamWorksAdapter._post_json()).
    # ------------------------------------------------------------------
    def _note_fetch_success(self) -> None:
        self._consecutive_failures = 0

    def _note_fetch_failure(self, url: str, reason: str) -> None:
        """Ghi nhận 1 lần fetch thất bại SAU KHI đã hết retry. Đạt ngưỡng liên
        tiếp thì raise CrawlBlockedError để dừng cả lượt.

        Vì sao raise ở tầng HTTP mà không để pipeline tự đếm: thất bại có
        thể xảy ra ở listing, chi tiết hay hồ sơ công ty — chỉ tầng HTTP thấy
        đủ mọi loại request. Counter reset khi có request thành công nên 1 job
        lỗi lẻ tẻ (tin đã gỡ trả 5xx...) không kích hoạt."""
        self._consecutive_failures += 1
        threshold = self._block_threshold
        if threshold and self._consecutive_failures >= threshold:
            raise CrawlBlockedError(
                f"{self._consecutive_failures} lần fetch liên tiếp thất bại sau "
                f"khi hết retry (gần nhất: {url} — {reason}). Khả năng cao bị "
                f"{self.source_name} chặn (403/429/lỗi kết nối) hoặc site đang "
                f"lỗi — dừng lượt crawl để không đập tiếp vào site."
            )

    def set_known_url_checker(self, checker: Optional[Callable[[str], bool]]) -> None:
        """Đặt hàm kiểm tra "URL job này đã có trong DB và KHÔNG cần fetch
        lại chi tiết" (True = bỏ qua). Truyền None để tắt. Reset bộ đếm
        skipped_known_count mỗi lần đặt, vì mỗi lượt crawl là 1 lần đặt.

        checker PHẢI trả True CHỈ KHI bỏ qua là an toàn: job đã có đủ
        work_type/deadline/parsed_content. Job cũ còn thiếu các field đó
        phải trả False để vẫn đi qua pipeline và được vá như trước (xem
        pipeline._handle_existing_job)."""
        self._known_url_checker = checker
        self.skipped_known_count = 0

    def _is_known_url(self, url: str) -> bool:
        """True nếu hook đã đặt và báo URL này nên bỏ qua. Lỗi trong hook
        KHÔNG được làm hỏng lượt crawl: coi như "chưa biết" (False) để
        quay về hành vi cũ là fetch như bình thường."""
        if self._known_url_checker is None:
            return False
        try:
            known = bool(self._known_url_checker(url))
        except Exception:  # noqa: BLE001 - hook lỗi thì fetch như cũ, không dừng crawl
            logger.exception("known_url_checker lỗi cho %s, bỏ qua hook và fetch bình thường", url)
            return False
        if known:
            self.skipped_known_count += 1
        return known

    def job_code_url_regex(self, source_url: str) -> Optional[str]:
        """Optional: regex POSIX (cho Postgres `~`) khớp MỌI source_url của CÙNG
        MỘT job với source_url này, hoặc None nếu nguồn không có mã job ổn định
        trong URL (mặc định, đúng với TopCV/CareerViet).

        Dùng cho nguồn mà URL đổi theo tiêu đề tin nhưng mã số của job giữ nguyên
        (VietnamWorks): pipeline tìm job đã lưu cùng mã để cập nhật thay vì tạo
        job trùng. Regex phải đủ chặt để không khớp job khác (neo cả hai đầu mã)
        và chỉ dựng từ chữ số của mã, không chèn nguyên văn đoạn nào của URL."""
        return None

    def dedup_resolvers(self) -> tuple:
        """Các bước chống trùng cho job MỚI của nguồn này, THEO THỨ TỰ chạy (B1). Mỗi phần
        tử là tên một resolver đăng ký ở pipeline.DEDUP_RESOLVERS; bước nào khớp trước thì
        job được xử lý ở đó và các bước sau không chạy.

          "job_code"  cùng mã job trong URL (URL đổi vì sửa tiêu đề) -> cập nhật job cũ.
                      Cần job_code_url_regex(); chạy trước khi tạo tỉnh/công ty.
          "repost"    cùng công ty + tiêu đề + tỉnh dưới URL khác -> ghi URL làm nguồn phụ.
                      Giành khoá advisory theo khoá chống trùng trước khi tra.

        Mặc định (adapter không khai báo gì) là cả hai, đúng hành vi trước B1: "job_code"
        không tốn câu SQL nào khi nguồn không có mã job. Nguồn nào khai báo thì ghi rõ cái
        nó dùng, để đọc adapter là biết nó chống trùng bằng cách nào. Tên lạ hoặc lặp thì
        run_pipeline() báo lỗi ngay đầu lượt crawl."""
        return DEFAULT_DEDUP_RESOLVERS

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

        last_reason = "không rõ"
        for attempt in range(1, max_retries + 1):
            try:
                resp = self.session.get(url, timeout=20)
                if resp.status_code in (404, 410):
                    # Tin đã gỡ/hết hạn: trang KHÔNG tồn tại, gọi lại cũng ra
                    # đúng kết quả đó. Trước đây rơi vào raise_for_status()
                    # -> bị coi như lỗi kết nối và retry 3 lần (TopCV ~168s
                    # cho 1 tin đã gỡ). KHÔNG tính vào ngắt mạch: đây không
                    # phải dấu hiệu bị chặn, và 1 response hợp lệ cho thấy
                    # site vẫn đang trả lời bình thường.
                    logger.info("HTTP %d (trang không còn tồn tại): %s", resp.status_code, url)
                    self._last_request_time = time.monotonic()
                    self._note_fetch_success()
                    return None
                if resp.status_code in (429, 403):
                    # 429 = rate limit theo cửa sổ thời gian.
                    # 403 = WAF/Cloudflare chặn theo fingerprint request
                    # (có thể do thiếu header giống trình duyệt thật,
                    # HOẶC IP tạm thời bị đánh dấu do crawl dồn dập
                    # trước đó) — cả 2 trường hợp đều ĐÁNG thử lại sau
                    # khi chờ, thay vì bỏ cuộc ngay ở request đầu tiên.
                    last_reason = f"HTTP {resp.status_code}"
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
                self._note_fetch_success()
                return resp.text
            except curl_requests.exceptions.RequestException as exc:
                # Retry cả lỗi kết nối không có status code (vd HTTP/2
                # stream reset, timeout...), không bỏ cuộc ngay ở lần
                # lỗi đầu tiên — đã xác nhận bằng dữ liệu thật (08/2026)
                # loại lỗi này thường chỉ TẠM THỜI (WAF chặn tạm do
                # request dồn dập), không phải trang đã đổi/hết dữ liệu.
                last_reason = f"{type(exc).__name__}: {exc}"
                wait = self._delay_seconds * (2 ** attempt)
                logger.warning(
                    "Lỗi kết nối tại %s (lần %d/%d): %s -> chờ %.1fs rồi thử lại",
                    url, attempt, max_retries, exc, wait,
                )
                time.sleep(wait)
                self._last_request_time = time.monotonic()
                continue

        logger.error("Bỏ cuộc sau %d lần liên tiếp (429/403/lỗi kết nối): %s", max_retries, url)
        self._note_fetch_failure(url, last_reason)
        return None
