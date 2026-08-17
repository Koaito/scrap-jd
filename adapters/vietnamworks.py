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
     đúng (không đoán 0 = FULL_TIME). 2, 4, 5... vẫn chưa có đối chiếu.
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
from datetime import date, datetime
from typing import Iterator, Optional
from urllib.parse import urljoin, urlsplit

from curl_cffi import requests
from bs4 import BeautifulSoup

from adapters.base import BaseAdapter
from models import RawJobRecord
from config import (
    VIETNAMWORKS_CATEGORIES,
    VNW_SEARCH_URL,
    VNW_HEADERS,
    VNW_HITS_PER_PAGE,
    VNW_RETRIEVE_FIELDS,
    REQUEST_DELAY_SECONDS,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.vietnamworks.com"

# typeWorkingId -> nhãn tiếng Việt KHỚP ĐÚNG key trong normalize._WORK_TYPE_MAP
# (chữ thường, không dấu câu) -> tái dùng normalize.normalize_work_type()
# có sẵn, KHÔNG cần sửa normalize.py. Chỉ map số ĐÃ XÁC NHẬN bằng dữ liệu
# thật (xem "Việc 1" trong cuộc trò chuyện trước) — số khác để trống, để
# normalize_work_type() tự trả None thay vì đoán sai.
_TYPE_WORKING_ID_MAP = {
    1: "Toàn thời gian",
    3: "Thực tập",
}

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
        self.session = session or requests.Session(impersonate="chrome124")
        self.session.headers.update(VNW_HEADERS)
        self._last_request_time: Optional[float] = None

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
                logger.warning("Không lấy được response trang %d, dừng lại.", page)
                break

            jobs = self._extract_job_list(data)
            if not jobs:
                logger.info("Trang %d không còn job -> dừng phân trang.", page)
                break

            new_count = 0
            for job in jobs:
                record = self._parse_job(job, matching_industry)
                if record is None:
                    continue
                if record.source_url in seen_urls:
                    continue
                seen_urls.add(record.source_url)
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
                "Trang %d: %d job mới (tổng %s hits, %s trang theo API)",
                page, new_count, meta.get("nbHits", "?"), nb_pages if nb_pages is not None else "?",
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
    # Internal — HTTP (throttle + retry dùng chung cho cả POST search lẫn
    # GET trang công ty, giống nguyên tắc "mọi request qua 1 cửa" của TopCV)
    # ------------------------------------------------------------------
    def _throttle(self):
        if self._last_request_time is None:
            return
        elapsed = time.monotonic() - self._last_request_time
        remaining = REQUEST_DELAY_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)

    def _post_json(self, url: str, body: dict, max_retries: int = 3) -> Optional[dict]:
        self._throttle()
        for attempt in range(1, max_retries + 1):
            try:
                resp = self.session.post(url, json=body, timeout=20)
                if resp.status_code in (429, 403):
                    wait = REQUEST_DELAY_SECONDS * (2 ** attempt)
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
                    return resp.json()
                except (json.JSONDecodeError, ValueError):
                    logger.error("Response không phải JSON hợp lệ tại %s", url)
                    return None
            except requests.exceptions.RequestException as exc:
                logger.error("Lỗi POST %s: %s", url, exc)
                self._last_request_time = time.monotonic()
                return None
        logger.error("Bỏ cuộc sau %d lần liên tiếp bị chặn (429/403): %s", max_retries, url)
        return None

    def _fetch_html(self, url: str, max_retries: int = 3) -> Optional[str]:
        """Dùng cho trang công ty (SSR) — GET thường, không phải API JSON."""
        self._throttle()
        for attempt in range(1, max_retries + 1):
            try:
                resp = self.session.get(url, timeout=20)
                if resp.status_code in (429, 403):
                    wait = REQUEST_DELAY_SECONDS * (2 ** attempt)
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
            except requests.exceptions.RequestException as exc:
                logger.error("Lỗi fetch %s: %s", url, exc)
                self._last_request_time = time.monotonic()
                return None
        logger.error("Bỏ cuộc sau %d lần liên tiếp bị chặn (429/403): %s", max_retries, url)
        return None

    # ------------------------------------------------------------------
    # Parse response search -> list job dict
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_job_list(data) -> list:
        """CHƯA XÁC NHẬN bằng response thật đầy đủ — thử các key phổ biến
        theo thứ tự khả năng cao nhất, log CẢNH BÁO nếu không khớp key
        nào (khác với coi im lặng là 'hết job', tránh hiểu nhầm dừng crawl
        sớm do đoán sai key thay vì thật sự hết trang)."""
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
                "CẦN kiểm tra lại response thật để sửa _extract_job_list().",
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
        work_type_text = _TYPE_WORKING_ID_MAP.get(job.get("typeWorkingId"), "")
        deadline_text = self._format_deadline(job.get("expiredOn"))

        # experience_text: XÁC NHẬN bằng dữ liệu thật (08/2026) — VNW CÓ
        # field "yearsOfExperience" (số nguyên) -> chuyển thành text "X năm"
        # khớp THẲNG với regex r"(\d+)\s*năm" đã có sẵn trong
        # normalize.infer_level(), không cần sửa normalize.py. Đáng tin hơn
        # jobLevelVI (nhãn cấp bậc chung chung, không khớp pattern nào của
        # infer_level()).
        # yearsOfExperience == 0: CHƯA RÕ nghĩa là "không yêu cầu kinh
        # nghiệm" hay "dữ liệu trống/chưa có" (job mẫu thật duy nhất thấy
        # được đều =0 nhưng cũng đồng thời rỗng gần hết field khác, khả
        # năng cao là tin đăng thiếu dữ liệu chứ không phải cố ý "0 năm")
        # -> AN TOÀN: chỉ set khi > 0, để trống khi =0 (infer_level() sẽ tự
        # fallback về "Junior" mặc định, giống hệt kết quả nếu hiểu 0 là
        # "không yêu cầu" nên không mất gì khi đoán sai theo hướng này).
        years_exp = job.get("yearsOfExperience")
        experience_text = f"{years_exp} năm" if isinstance(years_exp, int) and years_exp > 0 else ""

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
            deadline_text=deadline_text,
            matching_industry=matching_industry,
            company_url=company_url,
            raw_tags=job.get("skills") or [],
        )

        # Cache chi tiết job (JD/requirement/perks/skills) NGAY TỪ ĐÂY vì
        # search API đã trả đủ -> fetch_job_full_detail() dùng lại, không
        # gọi thêm request nào cho nguồn này.
        self._detail_cache[source_url] = {
            "work_type": work_type_text,
            "deadline_text": deadline_text,
            "job_description": self._strip_html(job.get("jobDescription", "")),
            "requirements": self._strip_html(job.get("jobRequirement", "")),
            "perks": self._format_benefits(job.get("benefits")),
            "required_skills": self._extract_skills(job.get("skills")),
        }

        return record

    # ------------------------------------------------------------------
    # fetch_job_full_detail — override để dùng cache thay vì fetch thêm
    # ------------------------------------------------------------------
    def fetch_job_full_detail(self, source_url: str) -> Optional[dict]:
        """Khác hẳn TopCV: dữ liệu này đã có sẵn từ lúc fetch_jobs() chạy
        (search API trả kèm luôn) -> trả từ cache, MIỄN PHÍ (0 request).

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

        intro_idx = page_text.find("Về chúng tôi")
        if intro_idx != -1:
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
        CHƯA XÁC NHẬN expiredOn là epoch giây, mili-giây, hay chuỗi ISO ->
        thử lần lượt, log cảnh báo nếu không parse được thay vì âm thầm
        trả rỗng (để dev nhận ra cần sửa lại hàm này)."""
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
