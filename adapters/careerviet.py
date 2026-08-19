"""
Adapter RIÊNG cho CareerViet.vn.

=======================================================================
TRẠNG THÁI: Phần 1 (Discovery) + Phần 2 (viết code) theo checklist đã
thống nhất trong phiên chat 08/2026. PHẦN 3 (tích hợp hệ thống chung —
sửa normalize.py/infer_level, đăng ký adapter trong main.py/routers,
viết fixture test, cập nhật README) CỐ Ý CHƯA LÀM trong lần này theo
yêu cầu — file này viết SAO CHO CHẠY ĐƯỢC ĐỘC LẬP với normalize.py hiện
tại, KHÔNG cần sửa gì ở đó (xem phần "Tương thích ngược" bên dưới).
=======================================================================

Kết luận Discovery (xác nhận qua chat trước + 1 mẫu HTML thật, so1.txt —
trang JD "Data Engineer" tại CÔNG TY CP DƯỢC PHẨM FPT LONG CHÂU,
job_id=35C82AC4, fetch 08/2026):

1. Trang JD chi tiết (/vi/tim-viec-lam/<slug>.<JOB_ID>.html) LÀ SSR đầy
   đủ — requests/curl_cffi + BeautifulSoup lấy được ngay, không cần
   Playwright. Có 2 nguồn dữ liệu SONG SONG trong CÙNG 1 response:
     a) <script type="application/ld+json"> chứa "@type":"JobPosting"
        (schema.org chuẩn) — sạch, ổn định, ít đổi theo redesign UI.
     b) <section class="job-detail-content"> — HTML thường, render sẵn
        đầy đủ field UI (địa điểm, lương, kinh nghiệm, hạn nộp, phúc
        lợi, mô tả, yêu cầu...).
   Chiến lược: dùng JSON-LD làm nguồn CHÍNH cho các field có cấu trúc
   rõ (title, ngày tháng, lương, kinh nghiệm, loại hình, công ty, địa
   điểm) vì đây là structured data chuẩn SEO, bền hơn CSS class. Dùng
   HTML thường (section.job-detail-content) làm nguồn CHÍNH cho phần
   MÔ TẢ/YÊU CẦU/PHÚC LỢI vì JSON-LD gộp 2 khối "Mô tả công việc" +
   "Yêu cầu công việc" làm 1 chuỗi "description" duy nhất (khó tách lại
   an toàn), trong khi HTML thường có 2 <div>/<h2> TÁCH RIÊNG rõ ràng.

2. Trang search/listing (/viec-lam/<keyword>-k-vi.html) ĐÃ xác nhận
   render sẵn job card kèm link chi tiết, KHÔNG cần JS — nhưng phiên
   làm việc trước chỉ fetch qua công cụ trả về markdown-extracted text
   (không lưu lại HTML thô), nên KHÔNG có mẫu HTML thật của 1 job-card
   để bám CSS class chắc chắn (khác hẳn trang JD chi tiết, đã có mẫu
   HTML thật so1.txt). ĐỂ AN TOÀN, fetch_jobs() ở file này KHÔNG cố
   parse field (title/company/salary...) trực tiếp từ card listing —
   thay vào đó chỉ dùng trang listing để LẤY DANH SÁCH URL job chi
   tiết (bằng regex bám PATTERN URL ổn định "/vi/tim-viec-lam/...html",
   không bám class CSS nào cả -> sống sót được dù CareerViet đổi giao
   diện listing), rồi fetch NGAY trang chi tiết của từng job để lấy dữ
   liệu chất lượng cao (JSON-LD + section.job-detail-content) cho toàn
   bộ RawJobRecord. Đánh đổi: tốn 1 request/job ngay trong fetch_jobs()
   thay vì để dành cho fetch_job_full_detail() sau — nhưng pipeline.py
   VỐN DĨ đã gọi fetch_job_full_detail() cho MỌI job mới, nên tổng số
   request KHÔNG đổi, chỉ đổi THỨ TỰ (fetch sớm hơn + cache lại, xem
   _detail_cache bên dưới, giống pattern VietnamWorksAdapter đã dùng).

3. CẬP NHẬT 08/2026 — đã kiểm chứng thêm bằng dữ liệu/response thật
   (robots.txt + so2.txt trang công ty + so sánh page 1 vs page 2 +
   2/5 category còn lại):

   ĐÃ XÁC NHẬN (không còn là suy đoán):
   - robots.txt của careerviet.vn: KHÔNG chặn cả 3 loại path adapter
     này dùng (/vi/tim-viec-lam/..., /viec-lam/...-k-vi.html,
     /vi/nha-tuyen-dung/...) — được phép crawl với UA hiện tại
     (impersonate="chrome124", không tự nhận là bot).
   - Phân trang: "?page=N" (suy đoán ban đầu) ĐÃ XÁC NHẬN SAI — page 2
     trả về HTML y hệt page 1. ĐÃ TÌM RA pattern thật (08/2026, người
     dùng tự bắt được qua URL thanh địa chỉ khi bấm nút số trang trên
     web thật): ".../viec-lam/<keyword>-k-trang-{N}-vi.html" (trang 1
     vẫn dùng base_url không có "trang", đã test đúng từ đầu) — xem
     _build_page_url() đã sửa lại theo pattern này. max_pages mặc định
     đưa trở lại 3 (như thiết kế ban đầu, không cần hạ xuống 1 nữa).
   - Category "business-analyst" và "data-analyst": ĐÃ fetch thử thật,
     ra đúng job theo ngành — không còn là suy đoán.
   - Trang công ty (/vi/nha-tuyen-dung/<slug>.<id>.html): ĐÃ có mẫu
     HTML thật (so2.txt, trang FPT Long Châu) — fetch_company_profile()
     bên dưới viết lại BÁM ĐÚNG cấu trúc DOM thật thay vì dò label
     trong toàn bộ page_text như bản cũ (xem docstring của hàm đó).

   ĐÃ XÁC NHẬN THÊM (loại bỏ):
   - Category "ui-ux-design": tự kiểm tra thật bằng
     https://careerviet.vn/viec-lam/ui-ux-design-k-vi.html -> keyword
     KHÔNG tồn tại trên CareerViet (không phải ra job linh tinh, mà
     không có kết quả). ĐÃ XOÁ khỏi CAREERVIET_CATEGORIES (config.py) —
     CareerViet chỉ còn 5/6 category so với TopCV/VietnamWorks.
   - "Lĩnh vực hoạt động"/"Mã số thuế" không hiện trên trang công ty
     CareerViet (đã xác nhận, không phải do thiếu mẫu) -> KHÔNG PHẢI
     thiếu sót của fetch_company_profile(). Có script backend riêng
     (enrich_company_web_info.py, chạy sau pipeline, không thuộc file
     này) lo việc tra cứu/vá thêm tax_id qua web search + Gemini, nên
     2 field "" ở đây là ĐÚNG Ý ĐỒ, không cần adapter tự đoán mò.

   ĐÃ XÁC NHẬN THÊM: "data-scientist" và "software-engineer" — tự kiểm
   tra thật, ra đúng job theo ngành. Vậy TOÀN BỘ 5/5 category còn lại
   trong CAREERVIET_CATEGORIES (config.py) đã xác nhận đúng, không còn
   category nào ở trạng thái suy đoán.

   CÒN LẠI, CHƯA XÁC NHẬN (để nguyên TODO):
   - Có phải MỌI job đều có đủ validThrough/monthsOfExperience trong
     JSON-LD hay không (chỉ mới xác nhận 1 mẫu) — code đã viết fallback
     an toàn (để rỗng "" nếu thiếu) nên không crash, nhưng độ đầy đủ
     dữ liệu thật cần audit thêm sau khi chạy crawl thật vài trăm job.

Tương thích ngược (KHÔNG cần sửa normalize.py — Phần 3 để sau):
   - normalize_deadline() hiện chỉ parse "DD/MM/YYYY". CareerViet có 2
     nguồn hạn nộp: HTML "Hết hạn nộp" ĐÃ SẴN đúng định dạng này (dùng
     trực tiếp), JSON-LD "validThrough" là ISO datetime (khác định
     dạng) -> adapter TỰ CONVERT sang "DD/MM/YYYY" ngay trong file này
     (_iso_to_ddmmyyyy()) trước khi gán vào deadline_text, y hệt cách
     VietnamWorksAdapter._format_deadline() đã làm cho "expiredOn".
   - normalize._WORK_TYPE_MAP chỉ nhận text tiếng Việt ("toàn thời
     gian"...), không nhận "FULL_TIME" thẳng từ JSON-LD employmentType
     -> adapter TỰ MAP sang text tiếng Việt tương ứng
     (_EMPLOYMENT_TYPE_MAP) trước khi gán vào work_type.
   - infer_level() parse "X năm" từ text — JSON-LD chỉ cho
     "monthsOfExperience" dạng số tháng -> adapter TỰ CONVERT sang text
     "X năm" (_months_to_year_text()) khi HTML không có sẵn field
     "Kinh nghiệm" (trường hợp bình thường thì HTML đã có sẵn dạng text
     này rồi, không cần convert).
   Nhờ 3 điểm trên, CareerViet chạy được ngay với normalize.py hiện tại,
   không mất dữ liệu deadline/work_type/experience như lo ngại ban đầu.
   Khi làm Phần 3 sau này (sửa thẳng normalize.py để nhận cả ISO/số
   tháng), có thể bỏ bớt 3 lớp convert này trong adapter, không bắt
   buộc phải giữ mãi.

QUYẾT ĐỊNH đã chốt trong chat: dùng `curl_cffi.requests` (KHÔNG dùng
`requests` thuần) dù chưa có bằng chứng CareerViet chặn theo TLS
fingerprint như TopCV — vì `requests` thuần ĐÃ BỊ GỠ KHỎI
requirements.txt (08/2026, xem comment trong file đó), chỉ còn
curl_cffi. Dùng lại curl_cffi (API gần như giống hệt requests) ở đây để
KHÔNG phải thêm dependency mới vào requirements.txt (đúng tinh thần
"chưa sửa hệ thống chung" của lần làm này). Nếu sau này xác nhận
CareerViet KHÔNG chặn theo TLS fingerprint gì cả, có thể đổi lại
`requests` thuần + thêm lại vào requirements.txt lúc làm Phần 3.
"""

import json
import re
import time
import logging
from datetime import datetime
from typing import Iterator, Optional
from urllib.parse import urljoin, urlsplit

from curl_cffi import requests
from bs4 import BeautifulSoup

from adapters.base import BaseAdapter
from models import RawJobRecord
from config import CAREERVIET_CATEGORIES, DEFAULT_HEADERS, REQUEST_DELAY_SECONDS

logger = logging.getLogger(__name__)

BASE_URL = "https://careerviet.vn"

# Bám PATTERN URL ổn định của link job chi tiết, KHÔNG bám class CSS nào
# (giống triết lý TopCVAdapter) -> sống sót được dù CareerViet đổi giao
# diện trang listing. Loại trừ ký tự '"', "'", '#', '?' để không nuốt
# nhầm phần đuôi query string/tracking hoặc ký tự đóng thuộc tính HTML.
_JOB_DETAIL_HREF_RE = re.compile(r'/vi/tim-viec-lam/[^"\'#?]+\.html')

# JSON-LD employmentType -> text tiếng Việt khớp key có sẵn trong
# normalize._WORK_TYPE_MAP (xem docstring "Tương thích ngược" ở trên).
# Giá trị lạ/không nhận diện được -> "" (an toàn, work_type vốn nullable).
_EMPLOYMENT_TYPE_MAP = {
    "FULL_TIME": "Toàn thời gian",
    "PART_TIME": "Bán thời gian",
    "INTERN": "Thực tập",
    "INTERNSHIP": "Thực tập",
    "TEMPORARY": "Khác",
    "CONTRACTOR": "Khác",
    "VOLUNTEER": "Khác",
    "OTHER": "Khác",
}

_ISO_DATE_FORMATS = ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d")

# 2 cách viết heading đã thấy/suy đoán cho 2 khối nội dung chính (hoa
# thường có thể khác nhau tuỳ trang) — so khớp không phân biệt hoa
# thường ở _extract_section_text() nên liệt kê 1 dạng chuẩn là đủ,
# nhưng vẫn để nguyên biến thể ở đây làm tài liệu tham khảo.
_JOB_DESCRIPTION_HEADINGS = {"mô tả công việc"}
_JOB_REQUIREMENTS_HEADINGS = {"yêu cầu công việc"}


# BUG THẬT gặp 08/2026 (công ty "Coca-Cola Beverages Vietnam"): ô "Website"
# trên trang công ty CareerViet đôi khi bị chính công ty tự điền nhầm
# thành link LinkedIn/Facebook của họ thay vì website thật — code cũ chỉ
# check startswith("http") nên lưu thẳng luôn, kết quả companies.website
# = link LinkedIn. Danh sách domain loại trừ này CỐ Ý viết RIÊNG (không
# import từ TopCVAdapter._NON_COMPANY_WEBSITE_DOMAINS) để giữ mỗi adapter
# tự chứa (self-contained), đúng cấu trúc project hiện tại (mỗi nguồn 1
# file độc lập, không phụ thuộc chéo) — đánh đổi là 2 danh sách có thể
# trôi lệch nhau nếu sau này chỉ sửa 1 bên, chấp nhận được vì danh sách
# domain mạng xã hội hiếm khi đổi.
_NON_COMPANY_WEBSITE_DOMAINS = (
    "linkedin.com", "facebook.com", "tiktok.com", "youtube.com",
    "twitter.com", "x.com", "instagram.com", "threads.com", "zalo.me",
)


def _is_non_company_website(url: str) -> bool:
    netloc = urlsplit(url).netloc.lower().removeprefix("www.")
    return netloc in _NON_COMPANY_WEBSITE_DOMAINS


class CareerVietAdapter(BaseAdapter):
    source_name = "CareerViet"

    def __init__(self, session: Optional["requests.Session"] = None):
        # impersonate="chrome124" — xem docstring đầu file mục "QUYẾT
        # ĐỊNH đã chốt" (dùng lại curl_cffi của TopCV, chưa có bằng
        # chứng cần TLS fingerprint giả lập nhưng không mất gì khi bật).
        self.session = session or requests.Session(impersonate="chrome124")
        self.session.headers.update(DEFAULT_HEADERS)
        self._last_request_time: Optional[float] = None

        # Cache dữ liệu chi tiết job (work_type/deadline/mô tả/yêu cầu/
        # phúc lợi/kỹ năng) build sẵn NGAY TRONG fetch_jobs() (xem
        # docstring mục Discovery #2) -> fetch_job_full_detail() dùng
        # lại, KHÔNG tốn thêm request cho job vừa crawl trong lần chạy
        # này. Khác VietnamWorksAdapter: cache-miss ở đây (vd job cũ từ
        # lần crawl trước, đang được "vá" mà không nằm trong lần
        # fetch_jobs() gần nhất) VẪN fetch sống được bình thường, vì
        # CareerViet là trang SSR thường (không phải API riêng biệt như
        # VietnamWorks) -> luôn có thể fetch lại bằng source_url.
        self._detail_cache: dict = {}

    # ------------------------------------------------------------------
    # Public API (bắt buộc theo BaseAdapter)
    # ------------------------------------------------------------------
    def fetch_jobs(self, category_key: str, max_pages: int = 3) -> Iterator[RawJobRecord]:
        if category_key not in CAREERVIET_CATEGORIES:
            raise ValueError(
                f"Category '{category_key}' chưa khai báo trong "
                f"CAREERVIET_CATEGORIES (config.py). Các category có sẵn: "
                f"{list(CAREERVIET_CATEGORIES.keys())}"
            )
        cat = CAREERVIET_CATEGORIES[category_key]
        search_url = f"{BASE_URL}/viec-lam/{cat['keyword']}-k-vi.html"
        matching_industry = cat["matching_industry"]

        seen_urls: set = set()

        for page in range(1, max_pages + 1):
            page_url = self._build_page_url(search_url, page)
            logger.info("Fetching CareerViet listing page %d: %s", page, page_url)

            html = self._fetch_html(page_url)
            if html is None:
                logger.warning("Không lấy được HTML trang %d, dừng lại.", page)
                break

            job_urls = self._extract_job_detail_urls(html)
            new_urls = [u for u in job_urls if u not in seen_urls]
            if not new_urls:
                # Bình thường là trang thật sự đã hết job. Pattern phân
                # trang thật (".../k-trang-{N}-vi.html", xem
                # _build_page_url()) ĐÃ XÁC NHẬN hoạt động (08/2026),
                # khác hẳn "?page=N" đoán sai trước đó -> nhánh này giờ
                # hiếm khi bị kích hoạt do lỗi pattern, chủ yếu là tín
                # hiệu hết job thật. Vẫn giữ làm lưới an toàn cuối nếu
                # 1 category cụ thể nào đó có hành vi khác thường.
                logger.info("Trang %d không có job URL mới -> dừng phân trang.", page)
                break

            new_count = 0
            for job_url in new_urls:
                seen_urls.add(job_url)
                record = self._build_record_from_detail(job_url, matching_industry)
                if record is not None:
                    new_count += 1
                    yield record
            logger.info("Trang %d: %d job mới", page, new_count)

    def fetch_job_full_detail(self, source_url: str) -> Optional[dict]:
        cached = self._detail_cache.get(source_url)
        if cached is not None:
            return cached

        # Cache miss (job cũ đang được vá lại, không nằm trong lần
        # fetch_jobs() gần nhất trong tiến trình này) -> fetch sống lại
        # bằng chính source_url, KHÁC VietnamWorksAdapter (vốn không thể
        # tra API search theo 1 job_id cụ thể).
        html = self._fetch_html(source_url)
        if html is None:
            return None
        parsed = self._parse_detail_page(html, source_url)
        if parsed is None:
            return None

        detail = self._detail_dict_from_parsed(parsed)
        self._detail_cache[source_url] = detail
        return detail

    def fetch_company_profile(self, company_url: str) -> dict:
        """Viết lại 08/2026 dựa trên mẫu HTML thật (so2.txt, trang công
        ty "CÔNG TY CỔ PHẦN DƯỢC PHẨM FPT LONG CHÂU", fetch qua
        company_url lấy từ JSON-LD của 1 job JD thật) — thay THẲNG cho
        bản cũ (dò label trong page_text toàn trang, PHÒNG THỦ vì chưa
        có mẫu thật). Cấu trúc DOM thật của trang
        /vi/nha-tuyen-dung/<slug>.<id>.html:

            div.company-info .info .content
                h1.name                     tên công ty (không cần lấy
                                             lại ở đây, đã có company_name
                                             từ JSON-LD job JD)
                <strong>Địa điểm</strong>
                <p>...</p>                  -> địa chỉ, <p> LIỀN SAU <strong>
                <hr/>
                <strong>Thông tin công ty</strong>
                <ul>                        -> <ul> LIỀN SAU <strong> khác
                    <li><span class="mdi .../> Nhãn:Giá trị</li>
                    ...
                </ul>
            div.intro-section-1 .box-text .main-text p  -> mô tả công ty
                (nhiều đoạn nối bằng <br/><br/>, không có <p> riêng từng
                đoạn — lấy get_text(" ") rồi collapse whitespace, giống
                cách VietnamWorksAdapter ghép description).

        Mẫu thật chỉ có đúng 3 <li> trong "Thông tin công ty": "Quy mô
        công ty", "Loại hình hoạt động", "Website" — KHÔNG có "Lĩnh vực
        hoạt động"/"Mã số thuế" (khác giả định của bản cũ, ĐÃ XÁC NHẬN
        CareerViet không hiển thị 2 field này ở trang công ty, không
        phải do thiếu mẫu). Parse TỔNG QUÁT mọi <li> dạng "Nhãn:Giá trị"
        (tách theo dấu ":" đầu tiên) thay vì hard-code đúng 3 nhãn đã
        thấy, để tự bắt thêm nếu CareerViet sau này đổi UI thêm nhãn.
        "Loại hình hoạt động" (VD "Cổ phần") không map vào field nào có
        sẵn trong RawJobRecord/company profile hiện tại -> bị bỏ qua có
        chủ đích (không phải thiếu sót), chỉ giữ lại nếu sau này thêm
        field tương ứng.

        "industry"/"tax_id" rỗng "" ở đây là ĐÚNG Ý ĐỒ, không phải bug:
        script backend riêng enrich_company_web_info.py (không thuộc
        file này, chạy sau pipeline) đã lo việc tra cứu/vá thêm tax_id
        qua web search + Gemini cho MỌI nguồn (TopCV/VNW/CareerViet như
        nhau), không cần adapter tự đoán mò trên trang JD.
        """
        result = {
            "tax_id": "",
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
        content = soup.find("div", class_="content")
        if content is None:
            logger.warning(
                "Trang công ty %s không có div.company-info .content -> "
                "có thể CareerViet đã đổi cấu trúc trang công ty (khác "
                "mẫu so2.txt), cần lấy view-source mới để sửa lại "
                "fetch_company_profile().",
                company_url,
            )
            return result

        labels: dict = {}
        for strong in content.find_all("strong"):
            label = _clean(strong.get_text())
            sib = strong.find_next_sibling()
            if sib is None:
                continue
            if label == "Địa điểm" and sib.name == "p":
                result["address"] = _clean(sib.get_text())
            elif sib.name == "ul":
                for li in sib.find_all("li"):
                    text = _clean(li.get_text())
                    if ":" not in text:
                        continue
                    key, _, value = text.partition(":")
                    key, value = key.strip(), value.strip()
                    if key and value and key not in labels:
                        labels[key] = value

        result["company_size"] = labels.get("Quy mô công ty") or labels.get("Quy mô", "")
        result["industry"] = labels.get("Lĩnh vực hoạt động") or labels.get("Lĩnh vực", "")
        result["tax_id"] = labels.get("Mã số thuế", "")

        website = labels.get("Website", "")
        # Xem docstring _NON_COMPANY_WEBSITE_DOMAINS ở đầu file — công ty
        # đôi khi tự điền nhầm link LinkedIn/Facebook vào ô "Website" trên
        # CareerViet, KHÔNG lưu thẳng nếu domain khớp mạng xã hội (thà
        # thiếu còn hơn sai, cùng nguyên tắc xuyên suốt project).
        if website.startswith("http") and not _is_non_company_website(website):
            result["real_website"] = website

        desc_p = soup.select_one("div.intro-section-1 .box-text .main-text p")
        if desc_p:
            result["description"] = _clean(desc_p.get_text(" "))

        return result

    # ------------------------------------------------------------------
    # HTTP layer (throttle + retry) — mọi request đều đi qua đây, giống
    # nguyên tắc đã áp dụng ở TopCVAdapter._fetch_html().
    # ------------------------------------------------------------------
    def _fetch_html(self, url: str, max_retries: int = 3) -> Optional[str]:
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
                # SỬA 08/2026: TRƯỚC ĐÂY return None ngay ở lần lỗi đầu
                # tiên, không thử lại — trong khi lỗi kết nối kiểu
                # "curl: (92) HTTP/2 stream reset by server" (WAF/anti-bot
                # chặn tầng kết nối, KHÔNG có status code nên không rơi
                # vào nhánh 429/403 phía trên) đã xác nhận là TẠM THỜI: 4
                # URL CareerViet fail liên tiếp trong 1 lần chạy test lại
                # load bình thường khi fetch lại thủ công ngay sau đó, và
                # cũng những URL đó chạy trót lọt ở lần chạy trước —
                # KHÔNG PHẢI trang đã đổi/hết dữ liệu như log cũ suy đoán
                # sai. Giờ retry giống nhánh 429/403: backoff tăng dần,
                # chỉ thật sự bỏ cuộc sau khi hết max_retries.
                wait = REQUEST_DELAY_SECONDS * (2 ** attempt)
                logger.warning(
                    "Lỗi kết nối tại %s (lần %d/%d): %s -> chờ %.1fs rồi thử lại",
                    url, attempt, max_retries, exc, wait,
                )
                time.sleep(wait)
                self._last_request_time = time.monotonic()
                continue
        logger.error("Bỏ cuộc sau %d lần liên tiếp (429/403/lỗi kết nối): %s", max_retries, url)
        return None

    def _throttle(self):
        if self._last_request_time is None:
            return
        elapsed = time.monotonic() - self._last_request_time
        remaining = REQUEST_DELAY_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)

    # ------------------------------------------------------------------
    # Listing: chỉ lấy DANH SÁCH URL job chi tiết (xem docstring #2)
    # ------------------------------------------------------------------
    @staticmethod
    def _build_page_url(base_url: str, page: int) -> str:
        """ĐÃ SỬA (08/2026) — pattern phân trang thật do người dùng tự
        bắt được qua URL thanh địa chỉ khi bấm nút số trang trên web
        thật (KHÔNG phải "?page=N" như suy đoán/xác nhận sai trước đó):
            .../viec-lam/<keyword>-k-vi.html            (trang 1, base)
            .../viec-lam/<keyword>-k-trang-2-vi.html    (trang 2)
            .../viec-lam/<keyword>-k-trang-3-vi.html    (trang 3)
        -> chèn "trang-{page}-" vào giữa "-k-" và "vi.html" cho page > 1,
        giữ nguyên base_url cho page 1 (đã test thật, không cần đổi).
        CareerViet nhận keyword thường hay hoa cũng ra job giống nhau
        (đã thấy path viết hoa "Business-Analyst" trên UI thật nhưng
        cat['keyword'] trong config.py để thường "business-analyst") —
        web server không phân biệt hoa/thường ở path này, chưa gặp lỗi
        nào do casing, không cần .title()/.capitalize() gì thêm.
        CHƯA re-test lại job thực tế trang 2/3 có KHÁC trang 1 hay
        không sau khi đổi pattern này (trước đó chỉ test sai pattern
        "?page=N") — nhánh dừng vòng lặp khi 0 URL mới ở fetch_jobs()
        vẫn là lưới an toàn cuối nếu pattern này vẫn có vấn đề gì khác."""
        if page <= 1:
            return base_url
        if base_url.endswith("-k-vi.html"):
            return base_url[: -len("-k-vi.html")] + f"-k-trang-{page}-vi.html"
        # Fallback nếu base_url không đúng đuôi kỳ vọng (không nên xảy
        # ra với search_url build từ fetch_jobs(), nhưng phòng hờ thay
        # vì crash im lặng) — giữ hành vi cũ, an toàn nhờ lưới 0-URL-mới.
        logger.warning(
            "base_url %r không đúng đuôi '-k-vi.html' kỳ vọng -> không "
            "build được URL trang %d theo pattern mới, dùng base_url "
            "nguyên bản (có thể trả về trùng trang 1).", base_url, page,
        )
        return base_url

    @staticmethod
    def _extract_job_detail_urls(html: str) -> list:
        """Quét TOÀN BỘ response bằng regex bám pattern URL ổn định,
        KHÔNG cố định vào 1 khối DOM/class cụ thể nào của card -> ít bị
        vỡ nhất khi CareerViet đổi giao diện listing. Giữ đúng thứ tự
        xuất hiện đầu tiên (không dùng set() trực tiếp, để job xuất
        hiện trước trong trang được ưu tiên nếu sau này cần giới hạn
        max_jobs theo thứ tự)."""
        seen = set()
        ordered = []
        for m in _JOB_DETAIL_HREF_RE.finditer(html):
            full_url = urljoin(BASE_URL, m.group(0))
            if full_url not in seen:
                seen.add(full_url)
                ordered.append(full_url)
        return ordered

    # ------------------------------------------------------------------
    # Detail page: JSON-LD (chính) + section.job-detail-content (mô tả/
    # yêu cầu/phúc lợi + fallback field JSON-LD thiếu)
    # ------------------------------------------------------------------
    def _build_record_from_detail(
        self, url: str, matching_industry: str
    ) -> Optional[RawJobRecord]:
        html = self._fetch_html(url)
        if html is None:
            return None
        parsed = self._parse_detail_page(html, url)
        if parsed is None:
            return None

        # Cache lại phần "chi tiết" (work_type/deadline/mô tả/yêu cầu/
        # phúc lợi/kỹ năng) để fetch_job_full_detail() không tốn thêm
        # request nào cho job vừa crawl trong lần chạy này.
        self._detail_cache[url] = self._detail_dict_from_parsed(parsed)

        return RawJobRecord(
            job_title=parsed["job_title"],
            company_name=parsed["company_name"],
            source_url=url,
            source_name=self.source_name,
            salary_text=parsed["salary_text"],
            province_text=parsed["province_text"],
            experience_text=parsed["experience_text"],
            work_type_text=parsed["work_type"],
            posted_text=parsed["posted_text"],
            deadline_text=parsed["deadline_text"],
            matching_industry=matching_industry,
            company_url=parsed["company_url"],
            raw_tags=parsed["required_skills"],
        )

    @staticmethod
    def _detail_dict_from_parsed(parsed: dict) -> dict:
        return {
            "work_type": parsed["work_type"],
            "deadline_text": parsed["deadline_text"],
            "job_description": parsed["job_description"],
            "requirements": parsed["requirements"],
            "perks": parsed["perks"],
            "required_skills": parsed["required_skills"],
        }

    def _parse_detail_page(self, html: str, url: str) -> Optional[dict]:
        soup = BeautifulSoup(html, "html.parser")
        jsonld = self._extract_job_posting_jsonld(soup) or {}
        section = soup.find("section", class_="job-detail-content")

        if not jsonld and section is None:
            logger.warning(
                "Trang JD %s không có cả JSON-LD JobPosting lẫn "
                "section.job-detail-content -> coi là fetch thất bại "
                "(có thể trang đổi cấu trúc, hoặc job đã gỡ/hết hạn).",
                url,
            )
            return None

        info = self._extract_info_box_labels(section) if section else {}

        title = _clean(jsonld.get("title", "")) or self._extract_title_fallback(soup)

        hiring_org = jsonld.get("hiringOrganization") or {}
        company_name = _clean(hiring_org.get("name", ""))
        company_url = (hiring_org.get("url") or "").strip()

        salary_text = info.get("Lương") or self._extract_salary_from_jsonld(jsonld)

        province_text = (
            self._extract_province_from_map(soup)
            or _clean(
                ((jsonld.get("jobLocation") or {}).get("address") or {}).get(
                    "addressLocality", ""
                )
            )
        )

        experience_text = info.get("Kinh nghiệm")
        if not experience_text:
            months = ((jsonld.get("experienceRequirements") or {})).get(
                "monthsOfExperience"
            )
            experience_text = _months_to_year_text(months)

        deadline_text = info.get("Hết hạn nộp") or _iso_to_ddmmyyyy(
            jsonld.get("validThrough", "")
        )

        posted_text = info.get("Ngày cập nhật") or (jsonld.get("datePosted", "") or "")

        work_type = _extract_employment_type_text(jsonld)

        job_description = self._extract_section_text(soup, _JOB_DESCRIPTION_HEADINGS)
        requirements = self._extract_section_text(soup, _JOB_REQUIREMENTS_HEADINGS)
        perks = self._extract_perks(soup)

        if not job_description and not requirements:
            # Fallback hiếm gặp: section.job-detail-content không có 2
            # khối tách riêng -> dùng nguyên "description" gộp của
            # JSON-LD (đã bỏ tag HTML), nhét hết vào job_description,
            # để requirements rỗng (thà thiếu còn hơn tách sai).
            raw_desc = jsonld.get("description", "")
            if raw_desc:
                job_description = _clean_html_block(raw_desc)

        required_skills = []
        skills_raw = jsonld.get("skills", "")
        if isinstance(skills_raw, str) and skills_raw.strip():
            required_skills = [s.strip() for s in skills_raw.split(",") if s.strip()]

        return {
            "job_title": title,
            "company_name": company_name,
            "company_url": company_url,
            "salary_text": salary_text,
            "province_text": province_text,
            "experience_text": experience_text,
            "deadline_text": deadline_text,
            "posted_text": posted_text,
            "work_type": work_type,
            "job_description": job_description,
            "requirements": requirements,
            "perks": perks,
            "required_skills": required_skills,
        }

    # ------------------------------------------------------------------
    # JSON-LD helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_job_posting_jsonld(soup: BeautifulSoup) -> Optional[dict]:
        """Quét TẤT CẢ <script type="application/ld+json"> trong trang
        (không bám id cụ thể — trang mẫu có ÍT NHẤT 2 script JSON-LD
        khác nhau: 1 cái "@type":"WebSite" cố định toàn site, 1 cái
        "@type":"JobPosting" mới là dữ liệu job cần lấy; id "ld-schema"
        không đáng tin để phân biệt 2 cái vì đã thấy dữ liệu mẫu bị lỗi
        /trùng id khi copy tay) -> parse từng cái, chỉ nhận cái đúng
        "@type":"JobPosting". Bỏ qua script nào JSON không hợp lệ (an
        toàn hơn để crash cả hàm)."""
        for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
            raw = script.string
            if raw is None:
                raw = script.get_text()
            if not raw or not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("@type") == "JobPosting":
                return data
        return None

    @staticmethod
    def _extract_salary_from_jsonld(jsonld: dict) -> str:
        base = jsonld.get("baseSalary") or {}
        value = base.get("value")
        if not isinstance(value, dict):
            return ""
        # Chưa gặp thật, nhưng schema.org QuantitativeValue chuẩn có thể
        # dùng minValue/maxValue thay vì "value" đơn — phòng hờ cả 2 dạng.
        if "minValue" in value or "maxValue" in value:
            lo, hi = value.get("minValue"), value.get("maxValue")
            if lo is not None and hi is not None:
                return f"{lo} - {hi}"
            if lo is not None:
                return f"Từ {lo}"
            if hi is not None:
                return f"Tới {hi}"
        v = value.get("value")
        return str(v).strip() if v is not None else ""

    # ------------------------------------------------------------------
    # HTML fallback helpers (section.job-detail-content)
    # ------------------------------------------------------------------
    @staticmethod
    def _extract_info_box_labels(section) -> dict:
        """Trang mẫu có 2 khối <div class="detail-box..."> LẶP LẠI nội
        dung giống hệt nhau (khả năng cao là biến thể responsive ẩn/hiện
        theo breakpoint, 1 khối có style="display:none") -> duyệt hết
        mọi <li> trong section, mỗi label GIỮ GIÁ TRỊ ĐẦU TIÊN gặp (dữ
        liệu 2 khối giống nhau nên không ảnh hưởng, nhưng phòng hờ khác
        nhau thì ưu tiên khối xuất hiện trước theo thứ tự DOM)."""
        result: dict = {}
        if section is None:
            return result
        for li in section.find_all("li"):
            strong = li.find("strong")
            p = li.find("p")
            if not strong or not p:
                continue
            label = _clean(strong.get_text())
            value = _clean(p.get_text())
            if label and value and label not in result:
                result[label] = value
        return result

    @staticmethod
    def _extract_province_from_map(soup: BeautifulSoup) -> str:
        map_div = soup.find("div", class_="map")
        if map_div:
            a = map_div.find("a")
            if a:
                val = (a.get("title") or "").strip() or _clean(a.get_text())
                if val:
                    return val
        return ""

    @staticmethod
    def _extract_section_text(soup: BeautifulSoup, heading_options: set) -> str:
        for heading in soup.find_all(["h2", "h3"]):
            heading_text = _clean(heading.get_text()).lower()
            if heading_text not in heading_options:
                continue
            sib = heading.find_next_sibling("div")
            if sib is None:
                continue
            items = sib.find_all("li")
            if items:
                return "\n".join(_clean(li.get_text()) for li in items)
            text = _clean(sib.get_text("\n"))
            if text:
                return text
        return ""

    @staticmethod
    def _extract_perks(soup: BeautifulSoup) -> str:
        ul = soup.find("ul", class_="welfare-list")
        if not ul:
            return ""
        return "\n".join(_clean(li.get_text()) for li in ul.find_all("li"))

    @staticmethod
    def _extract_title_fallback(soup: BeautifulSoup) -> str:
        """Chỉ dùng khi JSON-LD thiếu "title" hoàn toàn (chưa gặp thật,
        phòng hờ). Ưu tiên <h1> nếu có, cuối cùng mới bóc từ <title> (bỏ
        phần đuôi " - CareerViet.vn" và tên công ty do CareerViet tự
        chèn vào thẻ <title>, xem mẫu so1.txt: "Tuyển dụng Data Engineer
        tại ... - CareerViet.vn")."""
        h1 = soup.find("h1")
        if h1:
            text = _clean(h1.get_text())
            if text:
                return text
        title_tag = soup.find("title")
        if title_tag:
            text = _clean(title_tag.get_text())
            text = re.sub(r"\s*-\s*CareerViet\.vn\s*$", "", text)
            text = re.sub(r"^Tuyển dụng\s+", "", text)
            text = re.sub(r"\s+tại\s+.+$", "", text)
            return text.strip()
        return ""


# --------------------------------------------------------------------
# Module-level helpers (thuần, không phụ thuộc self, dễ unit-test riêng)
# --------------------------------------------------------------------
def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _clean_html_block(html_fragment: str) -> str:
    inner = BeautifulSoup(html_fragment, "html.parser")
    return _clean(inner.get_text("\n"))


def _extract_employment_type_text(jsonld: dict) -> str:
    """employmentType JSON-LD có thể là list (đã thấy thật trong mẫu:
    ["\\"FULL_TIME\\""] — CÓ dấu ngoặc kép THỪA lồng bên trong chuỗi,
    khả năng cao là lỗi serialize phía CareerViet, KHÔNG phải chuẩn
    schema.org) hoặc string đơn — strip hết dấu ngoặc kép/khoảng trắng
    thừa trước khi so khớp để không bị lỗi này làm miss map."""
    raw = jsonld.get("employmentType")
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    if not isinstance(raw, str):
        return ""
    key = raw.strip().strip('"').strip("'").strip().upper()
    return _EMPLOYMENT_TYPE_MAP.get(key, "")


def _iso_to_ddmmyyyy(iso_text: str) -> str:
    """'2026-09-05T23:59:00Z' -> '05/09/2026', tương thích thẳng với
    normalize.normalize_deadline() hiện tại (xem docstring đầu file mục
    "Tương thích ngược"). Trả "" nếu rỗng/không parse được (an toàn)."""
    text = (iso_text or "").strip()
    if not text:
        return ""
    core = text[:19] if "T" in text else text[:10]
    for fmt in _ISO_DATE_FORMATS:
        try:
            return datetime.strptime(core, fmt).strftime("%d/%m/%Y")
        except ValueError:
            continue
    logger.warning(
        "Không parse được validThrough=%r sang dd/mm/yyyy -> để trống. "
        "CẦN xem giá trị thật để sửa _iso_to_ddmmyyyy().", iso_text,
    )
    return ""


def _months_to_year_text(months) -> str:
    """36 (tháng) -> '3 năm', tương thích thẳng với regex
    r"(\\d+)\\s*năm" trong normalize.infer_level() hiện tại (xem
    docstring đầu file mục "Tương thích ngược"). months=0/None/không
    phải số -> "" (an toàn, infer_level() tự fallback "Junior")."""
    if not isinstance(months, (int, float)) or months <= 0:
        return ""
    years = round(months / 12)
    if years <= 0:
        return "Dưới 1 năm"
    return f"{years} năm"
