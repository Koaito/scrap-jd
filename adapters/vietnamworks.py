"""
Adapter RIÊNG cho VietnamWorks.

Kết luận Discovery (xác nhận qua DevTools + cURL thật của user, 08/2026):
- Trang listing (/viec-lam?q=... và /<category>-kv) là CSR (Client-Side
  Rendered) -> KHÔNG dùng requests+BeautifulSoup được cho trang listing,
  PHẢI gọi thẳng API search mà frontend dùng.
- API search: POST https://ms.vietnamworks.com/job-search/v1.0/search
  (đã xác nhận bằng cURL thật user copy từ Network tab).
- ĐIỂM KHÁC BIỆT LỚN so với TopCV: response của API search đã trả sẵn
  FULL job description/requirement (field "jobDescription",
  "jobRequirement") NGAY TRONG 1 LẦN GỌI -> không cần fetch riêng trang
  chi tiết job như TopCV phải làm. Vì pipeline.py (dùng chung, không sửa
  được) vẫn LUÔN gọi fetch_job_full_detail(source_url) cho mỗi job, adapter
  này dùng 1 cache nội bộ (_detail_cache) để trả lại dữ liệu đã có sẵn từ
  fetch_jobs() thay vì gọi thêm request nào — xem chú thích ở
  fetch_job_full_detail() bên dưới.
- Trang công ty (/nha-tuyen-dung/<slug>-c<id>) LÀ SSR — xác nhận bằng
  web_fetch thật (job "Foster Electric (Bac Ninh)", 08/2026) — dùng
  requests+BeautifulSoup được, y hệt cách TopCV crawl trang công ty.

ĐÃ XÁC NHẬN (08/2026, đối chiếu 50 job thật trong 1 response search đầy
đủ + 1 lần bấm thật từ trang JD sang trang công ty):
  1. "companyUrl" LUÔN RỖNG "" ở TOÀN BỘ 50/50 job soi được, không có
     ngoại lệ -> field này KHÔNG DÙNG ĐƯỢC, bỏ hẳn. Cách đúng để có link
     trang công ty: tự build từ companyName + companyId theo đúng format
     đã xác nhận bằng URL thật (bấm từ JD "CV Phân Tích Dữ Liệu Thu Hồi
     Nợ" -> trang công ty VietinBank):
       https://www.vietnamworks.com/nha-tuyen-dung/<slug>-c<companyId>
     với companyName="Ngân Hàng TMCP Công Thương Việt Nam (VietinBank)",
     companyId=34511
       -> slug="ngan-hang-tmcp-cong-thuong-viet-nam-vietinbank"
     Quy tắc slug (suy ra từ đối chiếu): bỏ dấu tiếng Việt, bỏ ngoặc
     tròn (giữ nội dung bên trong), viết thường, mọi ký tự không phải
     chữ/số -> khoảng trắng, gộp khoảng trắng liên tiếp -> 1 dấu "-".
     Xem _slugify_company_name(). Query "?fromPage=jobDetail" trong URL
     mẫu chỉ là tracking, không cần thiết để trang load đúng.
  2. typeWorkingId: chỉ xác nhận chắc chắn 1=Toàn thời gian, 3=Thực tập.
     Mẫu mới nhất có 1 job với giá trị 0 (rỗng/không set) -> để None là
     đúng (không đoán 0 = FULL_TIME).
     CẬP NHẬT (08/2026): người dùng tự tay mở 4 job VietnamWorks có
     typeWorkingId KHÔNG PHẢI 1/3/0 (đúng 4 job trước đó bị work_type =
     NULL trong DB) và xác nhận CẢ 4/4 job đều hiển thị "Loại hình làm
     việc: Khác" trên trang thật -> đủ căn cứ để mọi typeWorkingId khác
     0/1/3 (số cụ thể là bao nhiêu KHÔNG quan trọng, vì UI luôn hiển thị
     "Khác" cho mọi trường hợp không phải 3 loại chính) fallback về
     "Khác" (-> OTHER qua normalize._WORK_TYPE_MAP đã có sẵn key "khác",
     không cần sửa normalize.py) thay vì để trống. Xem
     _work_type_text_from_id().
  3. expiredOn: XÁC NHẬN có giá trị thật ở toàn bộ 50/50 job (không rỗng),
     đúng định dạng ISO 8601 có timezone, vd "2026-08-13T23:59:59+07:00"
     -> _format_deadline() đã parse đúng nhánh "%Y-%m-%dT%H:%M:%S" (dùng
     expired_on[:19] cắt bỏ phần timezone), không cần sửa gì thêm.
  4. benefits: XÁC NHẬN là list[dict], mỗi dict có các key cố định:
     benefitId (int), benefitIconName (str), benefitName (str, tiếng
     Anh), benefitNameVI (str, tiếng Việt), benefitValue (str, nội dung
     chi tiết). KHÔNG dùng chung _strip_html() cho field này nữa (trước
     đây _strip_html() ép mỗi dict thành str(dict) kiểu Python repr —
     ra chuỗi "{'benefitId': 1, ...}" không đọc được, không phải JSON
     hợp lệ) -> dùng riêng _format_benefits() bên dưới, ghép
     "benefitNameVI: benefitValue" mỗi dòng, đọc được và không mất
     thông tin.
     workingLocations / address / skills: đã xác nhận đúng cấu trúc
     list[dict] qua nhiều mẫu, giữ nguyên logic hiện tại.

Vỏ response ĐÃ XÁC NHẬN đầy đủ (08/2026, response thật user gửi):
  {"meta": {"code", "nbHits", "page", "nbPages", "hitsPerPage", ...},
   "data": [ <job object đầy đủ, KHÔNG bị lọc theo retrieveFields gửi
              lên -> server luôn trả FULL object bất kể request gì> ],
   "facets": {...}}
"yearsOfExperience" (số nguyên) đã xác nhận là field CÓ THẬT -> dùng để
build experience_text ("X năm") khớp thẳng normalize.infer_level().

=> Trước khi chạy `python main.py crawl --source vietnamworks` cho thật
nhiều trang, NÊN chạy thử `--pages 1` trước, xem log WARNING (nếu có) và
kiểm tra vài dòng insert vào DB có hợp lý không (giống cách TopCV đã được
verify bằng fixture trong tests/).
"""

import json
import logging
import re
import time
from datetime import datetime
from typing import Iterator, Optional
from urllib.parse import urljoin, urlparse

from curl_cffi import requests
from bs4 import BeautifulSoup

import normalize
from adapters.base import BaseAdapter, CrawlBlockedError
from adapters.vietnamworks_detail import (
    is_gone_page,
    job_id_from_url,
    parse_detail_page,
    redirect_target,
)
from models import RawJobRecord
from config import (
    VIETNAMWORKS_CATEGORIES,
    VNW_SEARCH_URL,
    VNW_HEADERS,
    VNW_HITS_PER_PAGE,
    VNW_RETRIEVE_FIELDS,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.vietnamworks.com"

# typeWorkingId -> nhãn tiếng Việt KHỚP ĐÚNG key trong normalize._WORK_TYPE_MAP
# (chữ thường, không dấu câu) -> tái dùng normalize.normalize_work_type()
# có sẵn, KHÔNG cần sửa normalize.py. Chỉ 2 số ĐÃ XÁC NHẬN chắc chắn nằm
# trong bảng này (1, 3). Số 0/None -> "" và mọi số khác -> "Khác" được xử
# lý trong _work_type_text_from_id() bên dưới (xem docstring hàm đó).
_TYPE_WORKING_ID_MAP = {
    1: "Toàn thời gian",
    3: "Thực tập",
}


def _work_type_text_from_id(type_working_id) -> str:
    """0 hoặc None -> "" (VNW thật sự chưa set, đã xác nhận từ trước —
    giữ nguyên hành vi cũ, KHÔNG suy đoán 0 = FULL_TIME).
    1, 3 -> map trực tiếp (đã xác nhận chắc chắn).
    Mọi giá trị khác -> "Khác" (xác nhận bằng đối chiếu thật 08/2026,
    xem ghi chú Discovery đầu file — không cần biết số cụ thể là bao
    nhiêu vì UI VietnamWorks luôn hiển thị "Khác" cho các trường hợp
    này, không phải 1 nhãn cụ thể khác)."""
    if type_working_id in _TYPE_WORKING_ID_MAP:
        return _TYPE_WORKING_ID_MAP[type_working_id]
    if not type_working_id:  # None hoặc 0
        return ""
    return "Khác"

# Mã bất thường adapter tự báo (BaseAdapter._flag_listing_anomaly) khi tải được
# trang chi tiết nhưng không giải mã được -> lượt crawl bị đánh dấu degraded.
ANOMALY_DETAIL_UNPARSABLE = "vnw_detail_unparsable"


def _level_hint_from_job_level(job_level) -> str:
    """Nhãn jobLevel của VietnamWorks -> 1 giá trị trong normalize.LEVEL_ORDER,
    hoặc "" nếu không biết. Chỉ là dự phòng khi không đọc được số năm (xem
    normalize.infer_level). Nhãn ĐÃ THẤY trong dữ liệu thật (10/2026): "Fresher/
    Entry level", "Experienced (non-manager)", "Manager", "Director and above".
    "Experienced (non-manager)" chứa chữ "manager" nên PHẢI loại trước, nếu không
    toàn bộ nhân viên thường bị gán nhầm Manager."""
    label = str(job_level or "").strip().lower()
    if not label or "non-manager" in label:
        return ""
    if "intern" in label:
        return "Intern"
    if "fresher" in label or "entry" in label:
        return "Fresher"
    if "director" in label or "manager" in label:
        return "Manager"
    return ""


# Bảng chuyển ký tự có dấu tiếng Việt -> không dấu, dùng cho
# _slugify_company_name(). Liệt kê thủ công (không phụ thuộc thư viện
# ngoài như unidecode, giữ requirements.txt gọn) — phủ đủ nguyên âm có
# dấu + đ/Đ, đã đủ cho tên công ty tiếng Việt thông thường.
_VN_CHAR_MAP = {
    "à": "a", "á": "a", "ả": "a", "ã": "a", "ạ": "a",
    "ă": "a", "ằ": "a", "ắ": "a", "ẳ": "a", "ẵ": "a", "ặ": "a",
    "â": "a", "ầ": "a", "ấ": "a", "ẩ": "a", "ẫ": "a", "ậ": "a",
    "è": "e", "é": "e", "ẻ": "e", "ẽ": "e", "ẹ": "e",
    "ê": "e", "ề": "e", "ế": "e", "ể": "e", "ễ": "e", "ệ": "e",
    "ì": "i", "í": "i", "ỉ": "i", "ĩ": "i", "ị": "i",
    "ò": "o", "ó": "o", "ỏ": "o", "õ": "o", "ọ": "o",
    "ô": "o", "ồ": "o", "ố": "o", "ổ": "o", "ỗ": "o", "ộ": "o",
    "ơ": "o", "ờ": "o", "ớ": "o", "ở": "o", "ỡ": "o", "ợ": "o",
    "ù": "u", "ú": "u", "ủ": "u", "ũ": "u", "ụ": "u",
    "ư": "u", "ừ": "u", "ứ": "u", "ử": "u", "ữ": "u", "ự": "u",
    "ỳ": "y", "ý": "y", "ỷ": "y", "ỹ": "y", "ỵ": "y",
    "đ": "d",
}


def _slugify_company_name(company_name: str) -> str:
    """Chuyển tên công ty tiếng Việt thành slug khớp đúng format URL
    trang công ty VietnamWorks — XÁC NHẬN bằng URL thật (08/2026):
    "Ngân Hàng TMCP Công Thương Việt Nam (VietinBank)" -> companyId=34511
    -> "ngan-hang-tmcp-cong-thuong-viet-nam-vietinbank"

    Quy tắc: bỏ dấu tiếng Việt -> viết thường -> mọi ký tự không phải
    a-z0-9 (kể cả ngoặc, dấu chấm, dấu phẩy) thay bằng khoảng trắng,
    GIỮ LẠI nội dung bên trong ngoặc (không xóa hẳn) -> gộp khoảng
    trắng liên tiếp thành 1 dấu "-", bỏ "-" ở đầu/cuối."""
    lowered = company_name.lower()
    no_diacritics = "".join(_VN_CHAR_MAP.get(ch, ch) for ch in lowered)
    cleaned = re.sub(r"[^a-z0-9]+", " ", no_diacritics)
    return re.sub(r"\s+", "-", cleaned.strip())


def _build_company_url(company_name: str, company_id) -> str:
    """company_url = BASE_URL + '/nha-tuyen-dung/<slug>-c<companyId>'.
    Trả rỗng nếu thiếu company_id (không đoán mò URL sai)."""
    if not company_id:
        return ""
    slug = _slugify_company_name(company_name or "")
    if not slug:
        return ""
    return f"{BASE_URL}/nha-tuyen-dung/{slug}-c{company_id}"


class VietnamWorksAdapter(BaseAdapter):
    source_name = "VietnamWorks"

    def __init__(self, session: Optional[requests.Session] = None):
        # Session curl_cffi + throttle/retry dùng chung giờ nằm ở
        # BaseAdapter.__init__() (xem adapters/base.py) — VietnamWorks
        # dùng đúng delay mặc định (REQUEST_DELAY_SECONDS), không cần
        # jitter riêng như TopCV.
        super().__init__(session=session, headers=VNW_HEADERS)

        # Cache job detail đã có sẵn từ fetch_jobs() (search API trả kèm
        # jobDescription/jobRequirement luôn) -> fetch_job_full_detail()
        # dùng lại, KHÔNG gọi thêm request nào. Key = source_url.
        self._detail_cache: dict = {}

    # ------------------------------------------------------------------
    # Public API (bắt buộc theo BaseAdapter)
    # ------------------------------------------------------------------
    def fetch_jobs(self, category_key: str, max_pages: int = 3) -> Iterator[RawJobRecord]:
        if category_key not in VIETNAMWORKS_CATEGORIES:
            raise ValueError(
                f"Category '{category_key}' chưa khai báo trong config.py "
                f"(VIETNAMWORKS_CATEGORIES). Có sẵn: {list(VIETNAMWORKS_CATEGORIES.keys())}"
            )
        cat = VIETNAMWORKS_CATEGORIES[category_key]
        query = cat["query"]
        matching_industry = cat["matching_industry"]

        seen_urls = set()

        for page in range(max_pages):  # VNW dùng page 0-based (đã xác nhận qua cURL: "page":0)
            body = {
                "userId": 0,
                "query": query,
                "filter": [],
                "ranges": [],
                "order": [],
                "hitsPerPage": VNW_HITS_PER_PAGE,
                "page": page,
                "retrieveFields": VNW_RETRIEVE_FIELDS,
            }
            logger.info("Fetching VNW page %d cho query=%r", page, query)
            data = self._post_json(VNW_SEARCH_URL, body)
            if data is None:
                if page == 0:
                    # VNW đánh số trang từ 0 -> page 0 chính là TRANG ĐẦU
                    # TIÊN. Thất bại ngay ở đây sau khi hết retry rất có
                    # thể là bị chặn (403/429/lỗi kết nối liên tục), KHÔNG
                    # PHẢI "hết job" — đồng bộ với topcv.py, xem docstring
                    # CrawlBlockedError. Trang sau (page >= 1) thất bại vẫn
                    # giữ nguyên break như cũ.
                    raise CrawlBlockedError(
                        f"Không lấy được response trang đầu tiên (page=0, "
                        f"query={query!r}) sau khi hết retry — khả năng bị "
                        f"VietnamWorks chặn (403/429/lỗi kết nối liên tục), "
                        f"không phải hết job."
                    )
                logger.warning("Không lấy được response trang %d, dừng lại.", page)
                self._note_listing_page_failed(f"{VNW_SEARCH_URL} (page={page})")
                break

            jobs = self._extract_job_list(data)
            if page == 0 and jobs:
                # Snapshot response JSON trang đầu làm mẫu (đợt 3) — để thay
                # fixture tổng hợp bằng response thật khi cần.
                self._snapshot("listing", VNW_SEARCH_URL, self._json_text(data))
            if not jobs:
                if page == 0:
                    # Gọi API OK nhưng trang đầu không có job nào: có thể
                    # category thật sự rỗng, nhưng cũng là dấu hiệu vỏ
                    # response đổi (data/meta). Giữ lại để so sánh.
                    self._flag_listing_anomaly("first_page_no_jobs")
                    self._snapshot("listing", VNW_SEARCH_URL, self._json_text(data),
                                   reason="listing_empty")
                logger.info("Trang %d không còn job -> dừng phân trang.", page)
                break

            new_count = 0
            known_count = 0
            for job in jobs:
                record = self._parse_job(job, matching_industry)
                if record is None:
                    continue
                if record.source_url in seen_urls:
                    continue
                seen_urls.add(record.source_url)
                # Job đã có trong DB và không cần vá nữa -> bỏ qua TRƯỚC khi tải
                # trang chi tiết (mỗi trang tốn 1 request thật). Job cũ còn
                # thiếu field vẫn đi tiếp để pipeline vá, giống CareerViet.
                if self._is_known_url(record.source_url):
                    known_count += 1
                    continue
                source_url = record.source_url
                record = self._enrich_from_detail_page(record, job)
                if record is None:
                    self._note_job_dropped(source_url, "không tải được trang chi tiết")
                    continue
                new_count += 1
                yield record

            # "meta.nbPages" — XÁC NHẬN bằng response thật (08/2026): vỏ
            # response dạng {"meta": {"nbPages":..., "nbHits":...},
            # "data": [...]}. Dùng số này để biết CHÍNH XÁC còn trang hay
            # không, đáng tin hơn hẳn so với đoán qua "trang trả về ít hơn
            # hitsPerPage" (heuristic cũ, dễ sai nếu trang cuối vừa khéo
            # đủ hitsPerPage).
            meta = data.get("meta", {}) if isinstance(data, dict) else {}
            nb_pages = meta.get("nbPages")
            logger.info(
                "Trang %d: %d job mới, %d job đã có (bỏ qua, không tải chi tiết) "
                "(tổng %s hits, %s trang theo API)",
                page, new_count, known_count, meta.get("nbHits", "?"),
                nb_pages if nb_pages is not None else "?",
            )

            if isinstance(nb_pages, int) and page + 1 >= nb_pages:
                logger.info("Đã tới trang cuối theo meta.nbPages (%d) -> dừng.", nb_pages)
                break
            if nb_pages is None and len(jobs) < VNW_HITS_PER_PAGE:
                # Fallback nếu vì lý do gì đó response thiếu "meta.nbPages"
                # (chưa từng thấy xảy ra, nhưng phòng hờ) -> dùng lại
                # heuristic cũ thay vì crawl vô hạn.
                logger.info("Không có meta.nbPages, trang %d ít hơn hitsPerPage -> coi như hết.", page)
                break

    # ------------------------------------------------------------------
    # Internal — HTTP. _throttle() dùng chung từ BaseAdapter (xem
    # adapters/base.py). _post_json() bên dưới là phần RIÊNG của
    # VietnamWorks (API JSON, không phải GET-HTML) nên không rút lên
    # BaseAdapter được — nhưng PHẢI giữ cùng chính sách retry/backoff với
    # BaseAdapter._fetch_html() (nếu sửa 1 bên, nhớ sửa bên kia).
    # ------------------------------------------------------------------
    @staticmethod
    def _json_text(data) -> str:
        """JSON -> text để lưu snapshot; lỗi serialize không được làm hỏng crawl."""
        try:
            return json.dumps(data, ensure_ascii=False)
        except (TypeError, ValueError):
            return ""

    def _post_json(self, url: str, body: dict, max_retries: int = 3) -> Optional[dict]:
        """POST JSON tới API search — throttle + retry/backoff giống hệt
        BaseAdapter._fetch_html() (429/403 VÀ lỗi kết nối không có status
        code như HTTP/2 stream reset/timeout đều được thử lại; backoff
        theo self._delay_seconds của adapter, không dùng hằng số cứng).

        Trả None khi: hết retry, hoặc response không phải JSON hợp lệ
        (lỗi JSON không retry — gọi lại cũng ra đúng response đó)."""
        self._throttle()
        last_reason = "không rõ"
        for attempt in range(1, max_retries + 1):
            try:
                resp = self.session.post(url, json=body, timeout=20)
                if resp.status_code in (429, 403):
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
                try:
                    data = resp.json()
                except (json.JSONDecodeError, ValueError):
                    # 200 nhưng không phải JSON: thường là trang chặn/challenge
                    # của WAF — tính như 1 lần thất bại cho ngắt mạch (không
                    # retry: gọi lại cũng ra đúng response đó).
                    logger.error("Response không phải JSON hợp lệ tại %s", url)
                    self._note_fetch_failure(url, "response không phải JSON")
                    return None
                self._note_fetch_success()
                return data
            except requests.exceptions.RequestException as exc:
                # Trước đây trả None ngay ở lần lỗi đầu tiên (bug mà
                # BaseAdapter._fetch_html() đã sửa cho GET) -> 1 lần
                # stream reset thoáng qua ở trang đầu bị coi là "bị
                # chặn". Giờ thử lại như GET.
                last_reason = f"{type(exc).__name__}: {exc}"
                wait = self._delay_seconds * (2 ** attempt)
                logger.warning(
                    "Lỗi kết nối POST %s (lần %d/%d): %s -> chờ %.1fs rồi thử lại",
                    url, attempt, max_retries, exc, wait,
                )
                time.sleep(wait)
                self._last_request_time = time.monotonic()
                continue
        logger.error("Bỏ cuộc sau %d lần liên tiếp (429/403/lỗi kết nối): %s", max_retries, url)
        self._note_fetch_failure(url, last_reason)
        return None

    # _fetch_html() (dùng cho trang công ty SSR — GET thường, khác
    # _post_json() ở trên vốn gọi API JSON) giờ dùng chung từ
    # BaseAdapter (xem adapters/base.py), không override riêng nữa.

    # ------------------------------------------------------------------
    # Parse response search -> list job dict
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_job_list(data) -> list:
        """Lấy list job từ response search. Vỏ response ĐÃ XÁC NHẬN
        (08/2026, xem docstring đầu file): {"meta": {...}, "data": [...]}
        -> key "data" là đường chính. Các key còn lại (hits/results/...)
        chỉ là lưới an toàn nếu VNW đổi vỏ; log CẢNH BÁO nếu không khớp
        key nào (khác với coi im lặng là 'hết job', tránh hiểu nhầm dừng
        crawl sớm do đổi key thay vì thật sự hết trang)."""
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "hits", "results", "jobs", "items", "list"):
                val = data.get(key)
                if isinstance(val, list):
                    return val
            logger.warning(
                "Không tìm thấy key danh sách job quen thuộc trong response "
                "(đã thử: data/hits/results/jobs/items/list). Các key có sẵn: %s. "
                "VNW có thể đã đổi vỏ response -> kiểm tra lại để sửa _extract_job_list().",
                list(data.keys()),
            )
        return []

    def _parse_job(self, job: dict, matching_industry: str) -> Optional[RawJobRecord]:
        job_title = (job.get("jobTitle") or "").strip()
        company_name = (job.get("companyName") or "").strip()
        source_url = (job.get("jobUrl") or "").strip()
        if source_url and not source_url.startswith("http"):
            source_url = urljoin(BASE_URL, source_url)

        if not job_title or not company_name or not source_url:
            logger.warning(
                "Bỏ qua job thiếu field bắt buộc (title/company/url): %r", job.get("jobId")
            )
            return None

        salary_text = self._extract_salary_text(job)
        province_text = self._extract_province_text(job)
        work_type_text = _work_type_text_from_id(job.get("typeWorkingId"))
        deadline_text = self._format_deadline(job.get("expiredOn"))

        # experience_text / level_hint: xem _experience_fields(). API search luôn
        # trả yearsOfExperience=0 và jobRequirement bị cắt, nên với job MỚI giá
        # trị đầy đủ được cập nhật lại từ trang chi tiết trong
        # _enrich_from_detail_page(); ở đây chỉ là bản dựa trên dữ liệu search
        # (dùng làm dự phòng khi trang chi tiết không giải mã được).
        experience_text, level_hint = self._experience_fields(job)

        # company_url: "companyUrl"/"companyProfile" trả về từ API XÁC
        # NHẬN LUÔN RỖNG ở toàn bộ mẫu thật soi được (xem docstring đầu
        # file) -> không dùng nữa. Tự build từ companyName + companyId
        # theo đúng format đã xác nhận bằng URL thật.
        company_url = _build_company_url(company_name, job.get("companyId"))

        record = RawJobRecord(
            job_title=job_title,
            company_name=company_name,
            source_url=source_url,
            source_name=self.source_name,
            salary_text=salary_text,
            province_text=province_text,
            experience_text=experience_text,
            work_type_text=work_type_text,
            posted_text=(job.get("approvedOnText") or "").strip(),
            deadline_text=deadline_text,
            matching_industry=matching_industry,
            company_url=company_url,
            raw_tags=job.get("skills") or [],
            level_hint=level_hint,
        )

        # Cache chi tiết job (JD/requirement/perks/skills) từ dữ liệu search
        # -> fetch_job_full_detail() dùng lại, không gọi thêm request. LƯU Ý:
        # bản này bị API search CẮT NGẮN (mô tả/yêu cầu kết thúc bằng "...");
        # với job mới, _enrich_from_detail_page() ghi đè bằng bản đầy đủ.
        self._detail_cache[source_url] = self._detail_dict_from_job(
            job, work_type_text, deadline_text
        )

        return record

    # ------------------------------------------------------------------
    # Trang chi tiết job (10/2026): bản đầy đủ của mô tả/yêu cầu + số năm
    # ------------------------------------------------------------------
    def _enrich_from_detail_page(self, record: RawJobRecord, listing_job: dict) -> Optional[RawJobRecord]:
        """Tải trang chi tiết của job MỚI, cập nhật record + cache bằng dữ liệu
        đầy đủ. Trả record đã cập nhật, hoặc None nếu KHÔNG tải được trang
        (lỗi mạng/bị chặn/404) -> bỏ job này lượt này, cùng quy ước với
        pipeline ("thà thiếu 1 job, lượt sau nhặt lại, còn hơn lưu JD bị cắt
        một cách âm thầm"); lỗi liên tiếp thì ngắt mạch dừng cả lượt.

        Tải được nhưng KHÔNG giải mã được (VNW đổi cấu trúc trang): vẫn trả
        record dựa trên dữ liệu search (như trước đây), đồng thời lưu snapshot
        và báo degraded để người xem biết JD đang lưu là bản cắt ngắn."""
        url = record.source_url
        html = self._fetch_html(url)
        if html is None:
            return None

        detail_job = parse_detail_page(html)
        if detail_job is not None and str(detail_job.get("jobId")) != str(listing_job.get("jobId")):
            logger.warning(
                "Trang chi tiết %s trả job khác (jobId=%r, kỳ vọng %r) -> coi như không giải mã được",
                url, detail_job.get("jobId"), listing_job.get("jobId"),
            )
            detail_job = None

        if detail_job is None:
            logger.warning(
                "Không giải mã được trang chi tiết %s -> dùng dữ liệu search (mô tả/yêu cầu có thể bị cắt)", url
            )
            self._snapshot("detail", url, html, reason="detail_unparsable")
            self._flag_listing_anomaly(ANOMALY_DETAIL_UNPARSABLE)
            return record

        self._snapshot("detail", url, html)
        # Trang chi tiết là nguồn chính; khoá nào nó để trống thì giữ giá trị search.
        merged = {**listing_job, **{k: v for k, v in detail_job.items() if v not in (None, "")}}
        record.experience_text, record.level_hint = self._experience_fields(merged)
        record.posted_text = (merged.get("approvedOnText") or "").strip() or record.posted_text
        self._detail_cache[url] = self._detail_dict_from_job(
            merged, record.work_type_text, record.deadline_text
        )
        return record

    @classmethod
    def _experience_fields(cls, job: dict) -> tuple:
        """(experience_text, level_hint) từ 1 dict job (search hoặc đã gộp chi
        tiết). Thứ tự: (1) yearsOfExperience > 0 (trường có cấu trúc, chỉ có số
        thật ở trang chi tiết); (2) số năm tối thiểu đọc từ jobRequirement (xem
        normalize.extract_min_years); (3) không có số năm -> experience_text
        rỗng và để jobLevel làm level_hint dự phòng (infer_level quyết định).

        Số 0 từ extract_min_years nghĩa là "không yêu cầu"/"dưới 1 năm" nên
        đổi thành "Dưới 1 năm" (infer_level -> Fresher); yearsOfExperience == 0
        thì KHÔNG đoán gì (nguồn không phân biệt "không yêu cầu" với "chưa điền")."""
        level_hint = _level_hint_from_job_level(job.get("jobLevel"))
        years = job.get("yearsOfExperience")
        if isinstance(years, int) and not isinstance(years, bool) and years > 0:
            return f"{years} năm", level_hint
        parsed = normalize.extract_min_years(cls._strip_html(job.get("jobRequirement")))
        if parsed is None:
            return "", level_hint
        return ("Dưới 1 năm" if parsed == 0 else f"{parsed} năm"), level_hint

    @classmethod
    def _detail_dict_from_job(cls, job: dict, work_type_text: str, deadline_text: str) -> dict:
        return {
            "work_type": work_type_text,
            "deadline_text": deadline_text,
            "job_description": cls._strip_html(job.get("jobDescription", "")),
            "requirements": cls._strip_html(job.get("jobRequirement", "")),
            "perks": cls._format_benefits(job.get("benefits")),
            "required_skills": cls._extract_skills(job.get("skills")),
        }

    def job_code_url_regex(self, source_url: str) -> Optional[str]:
        """URL VietnamWorks có dạng ...-<mã job>-jv; nhà tuyển dụng sửa tiêu đề thì
        phần chữ đổi (URL mới) còn mã giữ nguyên. Regex neo cả hai đầu mã, giống
        adapters.vietnamworks_detail.job_id_from_url."""
        code = job_id_from_url(source_url)
        return f"-{code}-jv([/?#]|$)" if code else None

    # ------------------------------------------------------------------
    # fetch_job_full_detail — override để dùng cache thay vì fetch thêm
    # ------------------------------------------------------------------
    def fetch_job_full_detail(self, source_url: str) -> Optional[dict]:
        """Khác hẳn TopCV: dữ liệu này đã có sẵn từ lúc fetch_jobs() chạy
        (job mới: bản đầy đủ từ trang chi tiết, xem _enrich_from_detail_page();
        job đã biết: bản search, có thể bị cắt) -> trả từ cache, MIỄN PHÍ
        (0 request).

        Trường hợp cache miss (source_url không nằm trong lần fetch_jobs()
        gần nhất — vd job cũ trong DB từ lần crawl trước, nay chỉ đang
        được 'vá' mà không nằm trong trang kết quả mới) -> trả None
        (giống ngữ nghĩa 'fetch thất bại thật sự' mà pipeline.py đã định
        nghĩa), KHÔNG tự ý gọi lại search API để tìm đúng job đó (API
        search không có cách tra theo source_url/jobId trực tiếp trong
        những gì đã xác nhận) — đây là hạn chế đã biết, chấp nhận được vì
        pipeline sẽ tự thử lại ở lần crawl sau khi job đó xuất hiện lại
        trong kết quả search."""
        cached = self._detail_cache.get(source_url)
        if cached is None:
            logger.warning(
                "fetch_job_full_detail cache miss cho %s (job không nằm trong "
                "lần fetch_jobs() gần nhất) -> bỏ qua job này lần crawl này.",
                source_url,
            )
            return None
        return cached

    # ------------------------------------------------------------------
    # Tải lại trang chi tiết cho job ĐÃ LƯU (dùng bởi backfill_vnw_detail.py)
    # ------------------------------------------------------------------
    REFRESH_OK = "ok"
    REFRESH_UNAVAILABLE = "unavailable"
    REFRESH_GONE = "gone"
    REFRESH_UNPARSABLE = "unparsable"

    def fetch_refreshed_job(self, source_url: str) -> tuple:
        """Tải trang chi tiết của một job VietnamWorks đã có trong DB và trả
        (trạng thái, dữ liệu) để vá JD bị cắt + level sai của job cũ.

        Khác fetch_jobs(): không có dữ liệu search đi kèm, nên chỉ dùng thông tin
        trên chính trang chi tiết. Không đọc/ghi _detail_cache.

        Trạng thái:
          - REFRESH_OK: dữ liệu = {"page_title", "detail": {job_description,
            requirements, perks, required_skills}, "experience_text", "level_hint"}.
          - REFRESH_UNAVAILABLE: không tải được (404/410/lỗi mạng) -> dữ liệu None.
          - REFRESH_GONE: tải được (HTTP 200) nhưng trang chuyển hướng sang /410
            (xem is_gone_page). Chỉ là tín hiệu "có vẻ đã gỡ", chưa chắc chắn.
          Nếu trang chuyển hướng sang slug mới của CÙNG mã job thì đi theo 1 bước.
          - REFRESH_UNPARSABLE: tải được nhưng không giải mã được, hoặc trang trả
            job KHÁC với jobId trong URL (dạng ...-<jobId>-jv) -> dữ liệu None.
        CrawlBlockedError (ngắt mạch của BaseAdapter) vẫn được để lan lên."""
        html = self._fetch_html(source_url)
        if html is None:
            return self.REFRESH_UNAVAILABLE, None

        detail_job = parse_detail_page(html)
        if detail_job is None:
            if is_gone_page(html):
                logger.info("Chuyển hướng /410 (có vẻ đã bị gỡ): %s", source_url)
                return self.REFRESH_GONE, None
            # Slug cũ: VietnamWorks chuyển CÙNG job sang slug mới (nhà tuyển dụng sửa
            # tiêu đề). Đi theo đúng 1 bước, và chỉ khi đích nằm trên vietnamworks.com
            # với CÙNG mã job, để không bao giờ lấy nhầm JD của job khác.
            target = redirect_target(html)
            expected_id = job_id_from_url(source_url)
            if (target and target != source_url
                    and urlparse(target).hostname in ("www.vietnamworks.com", "vietnamworks.com")
                    and expected_id and job_id_from_url(target) == expected_id):
                logger.info("Slug đã đổi, đi theo chuyển hướng: %s -> %s", source_url, target)
                html = self._fetch_html(target)
                if html is None:
                    return self.REFRESH_UNAVAILABLE, None
                detail_job = parse_detail_page(html)
                if detail_job is None:
                    if is_gone_page(html):
                        return self.REFRESH_GONE, None
                    logger.warning("Không giải mã được trang sau chuyển hướng %s", target)
                    return self.REFRESH_UNPARSABLE, None
            else:
                logger.warning("Không giải mã được trang chi tiết %s (chuyển hướng: %s)",
                               source_url, target)
                return self.REFRESH_UNPARSABLE, None

        m = re.search(r"-(\d+)-jv", source_url)
        if m and str(detail_job.get("jobId")) != m.group(1):
            logger.warning(
                "Trang chi tiết %s trả job khác (jobId=%r, kỳ vọng %s)",
                source_url, detail_job.get("jobId"), m.group(1),
            )
            return self.REFRESH_UNPARSABLE, None

        experience_text, level_hint = self._experience_fields(detail_job)
        detail = self._detail_dict_from_job(detail_job, "", "")
        return self.REFRESH_OK, {
            "page_title": str(detail_job.get("jobTitle") or ""),
            "detail": {
                "job_description": detail["job_description"],
                "requirements": detail["requirements"],
                "perks": detail["perks"],
                "required_skills": detail["required_skills"],
            },
            "experience_text": experience_text,
            "level_hint": level_hint,
        }

    # ------------------------------------------------------------------
    # Company profile — trang SSR, dùng requests+BeautifulSoup như TopCV
    # ------------------------------------------------------------------
    def fetch_company_profile(self, company_url: str) -> dict:
        result = {
            "tax_id": "",  # VNW không hiển thị mã số thuế công ty (đã xác nhận không thấy trong ảnh chụp)
            "real_website": "",
            "description": "",
            "company_size": "",
            "industry": "",
            "address": "",
        }
        if not company_url:
            return result

        html = self._fetch_html(company_url)
        if html is None:
            return result

        soup = BeautifulSoup(html, "html.parser")
        page_text = soup.get_text("\n", strip=True)

        result["company_size"] = self._extract_after_label(page_text, "Quy mô")
        result["industry"] = self._extract_after_label(page_text, "Lĩnh vực")
        # "Địa chỉ" — xác nhận nhãn thật bằng ảnh chụp + view-source trang
        # hồ sơ công ty (08/2026, tab "Về chúng tôi", item trong danh sách
        # <li class="...ejuuLs"> có <p class="type">Địa chỉ</p> đứng trước
        # <div class="text">). TRƯỚC ĐÂY field này bị bỏ sót hoàn toàn (dict
        # khởi tạo "address": "" nhưng không có dòng nào gán lại) — khiến
        # MỌI công ty nguồn VietnamWorks có address = NULL vĩnh viễn, dù
        # trang hồ sơ có sẵn dữ liệu và đang được fetch cho company_size/
        # industry ngay phía trên. Cùng page_text đã có sẵn, không tốn
        # thêm request nào để vá field này.
        result["address"] = self._extract_after_label(page_text, "Địa chỉ")

        # Website thật KHÔNG nằm trong thẻ <a href> riêng như TopCV, mà
        # lẫn trong đoạn text giới thiệu dạng "Website: http://..." (đã
        # xác nhận bằng dữ liệu thật, job Foster Electric 08/2026).
        m = re.search(r"Website:\s*(https?://\S+)", page_text)
        if m:
            result["real_website"] = m.group(1).rstrip(".,;")

        # "Về chúng tôi" xuất hiện 2 LẦN LIÊN TIẾP trên trang thật (đã xác
        # nhận bằng HTML thật 08/2026, mẫu "Bảo hiểm VietinBank (VBI)"):
        # lần 1 là TÊN TAB trong tab bar ("Về chúng tôi" / "Vị trí đang
        # tuyển dụng"), lần 2 mới là <h2> heading thật đứng ngay trước nội
        # dung (Lĩnh vực/Liên hệ/đoạn mô tả). page_text.find() TRƯỚC ĐÂY
        # bắt occurrence #1 (tab) -> "after" bị dính luôn tên tab kia
        # ("Vị trí đang tuyển dụng") + heading thật ("Về chúng tôi") +
        # nhãn field kế tiếp ("Lĩnh vực"...) thay vì đoạn mô tả thật, sinh
        # ra description dạng rác kiểu "Vị trí đang tuyển dụng Về chúng
        # tôi Quy mô...". Sửa: lấy occurrence #2 nếu có; nếu trang chỉ có
        # 1 occurrence (hoặc template khác, không dùng cụm này) thì giữ
        # hành vi cũ (an toàn, không đổi kết quả các trường hợp khác).
        intro_idx = page_text.find("Về chúng tôi")
        if intro_idx != -1:
            second_idx = page_text.find("Về chúng tôi", intro_idx + 1)
            if second_idx != -1:
                intro_idx = second_idx
            after = page_text[intro_idx + len("Về chúng tôi"):]
            lines = [l.strip() for l in after.split("\n") if l.strip()]
            desc_lines = []
            for line in lines[:15]:
                if line.startswith("Website:"):
                    break
                desc_lines.append(line)
                if len(" ".join(desc_lines)) > 600:
                    break
            result["description"] = " ".join(desc_lines).strip()

        return result

    # ------------------------------------------------------------------
    # Helpers nhỏ
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_salary_text(job: dict) -> str:
        """Ưu tiên field "prettySalary" (text đã format sẵn kiểu VN, vd
        '10 - 20 triệu' / 'Tới 3,000 USD') vì như vậy TÁI DÙNG ĐƯỢC
        normalize.normalize_salary() có sẵn KHÔNG CẦN SỬA (module này parse
        đúng các định dạng tiếng Việt quen thuộc). CHƯA xác nhận giá trị
        thật của prettySalary trông thế nào -> fallback rỗng (NEGOTIABLE)
        nếu thiếu, an toàn."""
        if job.get("isSalaryVisible") is False:
            return ""  # ẩn lương -> normalize_salary("") = NEGOTIABLE, đúng ý nghĩa
        pretty = (job.get("prettySalary") or "").strip()
        if pretty:
            return pretty
        return ""

    # Giới hạn "trông giống tên tỉnh thật" — mọi tên tỉnh VN hợp lệ (kể cả
    # dạng ghép "Bà Rịa - Vũng Tàu") đều: không chứa chữ số, không chứa
    # dấu phẩy (địa chỉ cụ thể mới có, vd "89 Láng Hạ, Quận Đống Đa..."),
    # và không dài quá mức (tên tỉnh dài nhất hiện có trong DB ~20 ký tự,
    # 40 đã dư sức an toàn). Dùng để CHẶN _extract_province_text() lỡ lấy
    # nhầm tên phòng ban/địa chỉ chi tiết làm "tỉnh" (xem BUG THẬT bên
    # dưới) thay vì lấy đại bất cứ chuỗi nào field trả về.
    _MAX_PROVINCE_LEN = 40

    @classmethod
    def _looks_like_province_name(cls, value: str) -> bool:
        """True nếu `value` trông hợp lý là tên tỉnh/thành, False nếu
        nhiều khả năng là địa chỉ/tên phòng ban lẫn vào (xem
        _extract_province_text())."""
        if not value or len(value) > cls._MAX_PROVINCE_LEN:
            return False
        if "," in value or any(ch.isdigit() for ch in value):
            return False
        return True

    @classmethod
    def _extract_province_text(cls, job: dict) -> str:
        """XÁC NHẬN (08/2026) workingLocations là list[dict] (xem docstring
        đầu file) -> vấn đề còn lại KHÔNG phải cấu trúc, mà là field nào
        trong dict đó thật sự chứa tên tỉnh sạch — thử lần lượt 4 key
        theo độ tin cậy giảm dần.

        BUG THẬT ĐÃ SỬA (08/2026, phát hiện qua đối chiếu dữ liệu thật đã
        crawl — job "Chuyên Viên Chính/Chuyên Viên Cao Cấp Bán Hàng Trực
        Tiếp Hà Nội/HCM"): field "cityNameVI"/"cityName"/"provinceName"
        đều rỗng ở job này -> code CŨ rơi xuống field "name" (yếu nhất,
        không đảm bảo là tên tỉnh), và field "name" ở job này lại chứa
        NGUYÊN 1 CHUỖI ĐỊA CHỈ + TÊN PHÒNG BAN: "Khối Quản Trị Nguồn Nhân
        Lực - 89 Láng Hạ, Quận Đống Đa, Hà Nội" -> chuỗi này bị lưu thẳng
        làm "tên tỉnh" vào DB (get_or_create_province() tự tạo dòng mới
        nếu không khớp tên có sẵn -> tạo ra 1 "tỉnh" rác, không có cảnh
        báo gì). Sửa bằng cách CHỈ CHẤP NHẬN giá trị "trông giống tên
        tỉnh thật" (_looks_like_province_name() — không số, không dấu
        phẩy, không quá dài) trước khi trả về; nếu MỌI field (kể cả
        "name" lẫn "address") đều không hợp lệ -> trả "" (rỗng), để
        get_or_create_province() tự map về "Khác" (dòng có sẵn, an toàn)
        thay vì tạo dòng rác mới."""
        locations = job.get("workingLocations")
        if isinstance(locations, list) and locations:
            first = locations[0]
            if isinstance(first, dict):
                for key in ("cityNameVI", "cityName", "provinceName", "name"):
                    candidate = str(first.get(key) or "").strip()
                    if not candidate:
                        continue
                    if cls._looks_like_province_name(candidate):
                        if key == "name":
                            # "name" là field yếu nhất trong 4 field (không
                            # có gì đảm bảo nó luôn là tên tỉnh, chỉ là
                            # PHÙ HỢP FORMAT tỉnh ở lần này) -> log để dev
                            # để ý, khác im lặng hoàn toàn như code cũ.
                            logger.warning(
                                "_extract_province_text(): phải dùng field "
                                "'name' (yếu nhất, các field ưu tiên hơn "
                                "đều rỗng) cho job -> lấy được %r. Nên xem "
                                "lại nếu thấy lặp lại nhiều.", candidate,
                            )
                        return candidate
                    logger.warning(
                        "_extract_province_text(): field '%s' = %r KHÔNG "
                        "giống tên tỉnh thật (có số/dấu phẩy/quá dài) -> "
                        "bỏ qua, thử field tiếp theo.", key, candidate,
                    )
            elif isinstance(first, str):
                candidate = first.strip()
                if cls._looks_like_province_name(candidate):
                    return candidate
        address = job.get("address")
        if isinstance(address, str) and address.strip():
            candidate = address.strip()
            if cls._looks_like_province_name(candidate):
                return candidate
        return ""

    @staticmethod
    def _format_deadline(expired_on) -> str:
        """Trả về text dạng 'dd/mm/yyyy' để tương thích thẳng với
        normalize.normalize_deadline() có sẵn (không sửa normalize.py).
        expiredOn ĐÃ XÁC NHẬN là chuỗi ISO 8601 có timezone, vd
        "2026-08-13T23:59:59+07:00" (xem docstring đầu file mục 3) ->
        nhánh chính là "%Y-%m-%dT%H:%M:%S" trên expired_on[:19]. Các nhánh
        epoch giây/mili-giây/"%Y-%m-%d"/"%d/%m/%Y" giữ lại làm lưới an
        toàn; log cảnh báo nếu không parse được thay vì âm thầm trả rỗng."""
        if expired_on is None or expired_on == "":
            return ""
        try:
            if isinstance(expired_on, (int, float)):
                ts = expired_on / 1000 if expired_on > 10**12 else expired_on
                return datetime.fromtimestamp(ts).strftime("%d/%m/%Y")
            if isinstance(expired_on, str):
                for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y"):
                    try:
                        return datetime.strptime(expired_on[:19], fmt).strftime("%d/%m/%Y")
                    except ValueError:
                        continue
        except (ValueError, OSError, OverflowError):
            pass
        logger.warning(
            "Không parse được expiredOn=%r sang dd/mm/yyyy -> để trống. "
            "CẦN xem giá trị thật để sửa _format_deadline().", expired_on
        )
        return ""

    @staticmethod
    def _extract_skills(skills) -> list:
        if not skills:
            return []
        if isinstance(skills, list):
            out = []
            for s in skills:
                if isinstance(s, str):
                    out.append(s.strip())
                elif isinstance(s, dict):
                    val = s.get("skillName") or s.get("name") or s.get("value")
                    if val:
                        out.append(str(val).strip())
            return [s for s in out if s]
        return []

    @classmethod
    def _format_benefits(cls, benefits) -> str:
        """Format field "benefits" (list[dict] — xem docstring đầu file
        để biết cấu trúc đã xác nhận) thành text sạch, mỗi phúc lợi 1
        dòng dạng "<benefitNameVI>: <benefitValue>". benefitValue có thể
        chứa HTML/newline thô -> vẫn strip qua _strip_html() cho riêng
        phần value, KHÔNG áp dụng cho cả dict như code cũ (đó là nguyên
        nhân bug "{'benefitId': 1, ...}" xuất hiện trong DB)."""
        if not benefits or not isinstance(benefits, list):
            return ""
        lines = []
        for item in benefits:
            if not isinstance(item, dict):
                continue
            label = (item.get("benefitNameVI") or item.get("benefitName") or "").strip()
            value = cls._strip_html(item.get("benefitValue", ""))
            if not label and not value:
                continue
            if label and value:
                lines.append(f"{label}: {value}")
            else:
                lines.append(label or value)
        return "\n".join(lines)

    @classmethod
    def _strip_html(cls, value) -> str:
        """jobDescription/jobRequirement/benefits có thể chứa HTML (thường
        gặp ở API tuyển dụng dùng rich-text editor) -> tách text sạch,
        giống cách TopCV lấy .get_text() từ soup thay vì lưu HTML thô.

        XÁC NHẬN BẰNG LỖI THẬT (08/2026, chạy --source vietnamworks lần
        đầu): field "benefits" KHÔNG PHẢI string như đoán ban đầu, mà là
        1 LIST (mỗi phúc lợi 1 phần tử string, có thể vẫn chứa HTML từng
        item) -> code cũ gọi thẳng .strip() lên list -> AttributeError.
        Xử lý đệ quy cho list ở đây; nếu jobDescription/jobRequirement
        sau này cũng lộ ra là list (chưa xác nhận) thì hàm này đã sẵn
        sàng xử lý, không cần sửa lại lần nữa."""
        if not value:
            return ""
        if isinstance(value, list):
            parts = [cls._strip_html(item) for item in value]
            return "\n".join(p for p in parts if p)
        if not isinstance(value, str):
            # Phòng hờ thêm: kiểu dữ liệu lạ khác (dict, số...) -> ép về
            # chuỗi thay vì crash, thà hiển thị hơi xấu còn hơn mất cả job.
            value = str(value)
        if "<" in value and ">" in value:
            return BeautifulSoup(value, "html.parser").get_text("\n", strip=True)
        return value.strip()

    # Nhãn dùng để biết khi nào dừng gộp nhiều dòng lại (giống TopCV)
    _KNOWN_LABELS = ["Quy mô", "Lĩnh vực", "Về chúng tôi", "Website", "Địa chỉ"]

    @classmethod
    def _extract_after_label(cls, page_text: str, label: str) -> str:
        lines = page_text.split("\n")
        for i, line in enumerate(lines):
            if line.strip() == label:
                for nxt in lines[i + 1: i + 4]:
                    nxt_clean = nxt.strip()
                    if not nxt_clean or nxt_clean == label:
                        continue
                    if nxt_clean in cls._KNOWN_LABELS:
                        break
                    return nxt_clean
        return ""
