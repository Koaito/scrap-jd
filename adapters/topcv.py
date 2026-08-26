"""
Adapter RIÊNG cho TopCV.

Kết luận Discovery (đã xác nhận bằng web_fetch thật ngày 08/08/2026):
- Trang chủ /viec-lam là Vue SPA, chưa hydrate -> KHÔNG dùng được.
- Trang danh mục nghề /tim-viec-lam-<ten>-cr..cb..cl.. LÀ server-side
  rendered -> requests + BeautifulSoup lấy được dữ liệu thật, không cần
  Playwright.

Chiến lược parse: thay vì bám vào TÊN CLASS CSS (dễ đổi mỗi lần TopCV
redesign giao diện), adapter này bám vào PATTERN URL ổn định hơn nhiều:
  - Link chi tiết job luôn chứa "/viec-lam/" hoặc "/brand/.../tuyen-dung/"
  - Link công ty luôn chứa "/cong-ty/" hoặc "/brand/"
Từ 1 link job, đi ngược lên cha gần nhất mà chỉ chứa ĐÚNG 1 link job
(tránh gộp nhầm nhiều card lại với nhau - đây là lỗi đã gặp và sửa ở lần
làm trước, xem README mục "Ghi chú kỹ thuật").

LƯU Ý QUAN TRỌNG: TopCV có thể đổi giao diện bất kỳ lúc nào. Nếu chạy mà
ra 0 job, xem README mục "Debug khi TopCV đổi giao diện" để tự sửa lại
2-3 dòng regex/selector bên dưới.
"""

import re
import time
import logging
from typing import Iterator, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

# ĐỔI từ `requests` thuần sang `curl_cffi.requests` (08/2026): TopCV bắt
# đầu chặn 403 dựa trên TLS/JA3 fingerprint (cách bắt tay HTTPS ở tầng
# thấp) — KHÁC với chặn theo header HTTP thông thường. Thư viện `requests`
# thuần luôn "lộ" ra không phải trình duyệt thật ở tầng handshake này, DÙ
# header (User-Agent, Accept...) đã set giống Chrome y hệt. `curl_cffi`
# giả lập ĐÚNG cách Chrome thật bắt tay TLS -> vượt qua được kiểu chặn
# này. API gần như giống hệt `requests` (Session, .get(), .status_code,
# .text, .raise_for_status()) nên phần còn lại của file KHÔNG cần đổi gì
# thêm ngoài import + cách khởi tạo Session.
from curl_cffi import requests
from bs4 import BeautifulSoup

from adapters.base import BaseAdapter, CrawlBlockedError
from models import RawJobRecord
from config import TOPCV_CATEGORIES, DEFAULT_HEADERS, REQUEST_DELAY_SECONDS

logger = logging.getLogger(__name__)

BASE_URL = "https://www.topcv.vn"

# Các tỉnh/thành hay gặp trên TopCV, dùng để nhận diện province_text
# trong đoạn text thô của card (không cần class CSS riêng).
#
# Cập nhật theo đúng 34 tỉnh/thành sau sáp nhập (Nghị quyết 202/2025/
# QH15, hiệu lực từ 01/7/2025) — TopCV hiển thị tên tỉnh MỚI trên trang
# listing, có thể kèm hậu tố "(mới)" (vd "Hồ Chí Minh (mới)", đã xác
# nhận trong fixture thật) — so khớp theo substring nên hậu tố này
# không ảnh hưởng, không cần liệt kê riêng.
#
# Tên tỉnh CŨ đã sáp nhập vào tỉnh khác (vd "Bình Dương", "Bà Rịa",
# "Vũng Tàu" -> nay thuộc "Hồ Chí Minh"; "Kiên Giang" -> nay thuộc "An
# Giang") KHÔNG còn xuất hiện trên trang TopCV nữa nên đã bỏ khỏi danh
# sách, tránh gán nhầm province cho job crawl mới.
KNOWN_PROVINCES = [
    "Tuyên Quang", "Cao Bằng", "Lai Châu", "Lào Cai", "Thái Nguyên",
    "Điện Biên", "Lạng Sơn", "Sơn La", "Phú Thọ", "Bắc Ninh",
    "Quảng Ninh", "Hà Nội", "Hải Phòng", "Hưng Yên", "Ninh Bình",
    "Thanh Hóa", "Nghệ An", "Hà Tĩnh", "Quảng Trị", "Huế",
    "Đà Nẵng", "Quảng Ngãi", "Gia Lai", "Đắk Lắk", "Khánh Hòa",
    "Lâm Đồng", "Đồng Nai", "Tây Ninh", "Hồ Chí Minh", "Đồng Tháp",
    "An Giang", "Vĩnh Long", "Cần Thơ", "Cà Mau",
]

SALARY_PATTERN = re.compile(
    r"(Thoả thuận|Th\u1ecfa thu\u1eadn|"
    r"(T\u1edbi|Từ|Tu)\s*[\d.,]+\s*(tri\u1ec7u|USD)|"
    r"[\d.,]+\s*-\s*[\d.,]+\s*(tri\u1ec7u|USD)|"
    r"[\d.,]+\s*(tri\u1ec7u|USD))",
    re.IGNORECASE,
)

EXPERIENCE_PATTERN = re.compile(
    r"(Kh\u00f4ng y\u00eau c\u1ea7u|D\u01b0\u1edbi 1 n\u0103m|Tr\u00ean 5 n\u0103m|\d+\s*n\u0103m)"
)

POSTED_PATTERN = re.compile(r"\u0110\u0103ng\s+.+?tr\u01b0\u1edbc")


class TopCVAdapter(BaseAdapter):
    source_name = "TopCV"

    def __init__(self, session: Optional[requests.Session] = None):
        # impersonate="chrome124" -> giả lập TLS fingerprint của Chrome
        # 124 (khớp với User-Agent Chrome/124.0.0.0 trong DEFAULT_HEADERS
        # ở config.py). Đây là phần THAY THẾ requests.Session() thường.
        self.session = session or requests.Session(impersonate="chrome124")
        self.session.headers.update(DEFAULT_HEADERS)
        # Mốc thời gian của request GẦN NHẤT (bất kể listing/job
        # detail/company profile) — dùng để throttle MỌI request ở 1
        # chỗ duy nhất trong _fetch_html(), thay vì rải rác time.sleep()
        # ở từng nơi gọi (dễ quên, dễ sót -> vẫn bị 429 dù đã tăng delay).
        self._last_request_time: Optional[float] = None

    # ------------------------------------------------------------------
    # Public API (bắt buộc theo BaseAdapter)
    # ------------------------------------------------------------------
    def fetch_jobs(self, category_key: str, max_pages: int = 3) -> Iterator[RawJobRecord]:
        if category_key not in TOPCV_CATEGORIES:
            raise ValueError(
                f"Category '{category_key}' chưa khai báo trong config.py. "
                f"Các category có sẵn: {list(TOPCV_CATEGORIES.keys())}"
            )
        cat = TOPCV_CATEGORIES[category_key]
        base_url = cat["url"]
        matching_industry = cat["matching_industry"]

        seen_urls = set()

        for page in range(1, max_pages + 1):
            page_url = self._build_page_url(base_url, page)
            logger.info("Fetching page %d: %s", page, page_url)

            html = self._fetch_html(page_url)
            if html is None:
                if page == 1:
                    # Trang ĐẦU TIÊN thất bại sau khi hết retry -> rất có
                    # thể bị TopCV chặn (403/429/lỗi kết nối liên tục),
                    # KHÔNG PHẢI "hết job" (làm gì có trang nào lấy được
                    # để biết còn/hết job). Raise thay vì chỉ log+break,
                    # để execute() (api/crawl_runner.py) ghi status='error'
                    # thay vì 'done' với 0 job mới -> UI không hiển thị
                    # nhầm "Hoàn tất" cho 1 lượt crawl thực chất bị chặn
                    # hoàn toàn. Xem docstring CrawlBlockedError.
                    raise CrawlBlockedError(
                        f"Không lấy được HTML trang 1 ({page_url}) sau khi "
                        f"hết retry — khả năng bị TopCV chặn (403/429/lỗi "
                        f"kết nối liên tục), không phải hết job."
                    )
                logger.warning("Không lấy được HTML trang %d, dừng lại.", page)
                break

            records = list(self._parse_listing_page(html, matching_industry))
            if not records:
                logger.info("Trang %d không còn job -> dừng phân trang.", page)
                break

            new_count = 0
            for rec in records:
                if rec.source_url in seen_urls:
                    continue
                seen_urls.add(rec.source_url)
                new_count += 1
                yield rec

            logger.info("Trang %d: %d job mới", page, new_count)
            # Không cần time.sleep() thủ công ở đây nữa — _fetch_html()
            # đã tự throttle MỌI request (kể cả các request crawl sâu
            # fetch_job_full_detail/fetch_company_profile bên pipeline.py
            # gọi ngay sau đây, vốn TRƯỚC ĐÂY hoàn toàn không có delay).

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------
    def _build_page_url(self, base_url: str, page: int) -> str:
        sep = "&" if "?" in base_url else "?"
        return base_url if page == 1 else f"{base_url}{sep}page={page}"

    def _fetch_html(self, url: str, max_retries: int = 3) -> Optional[str]:
        """MỌI request HTTP của adapter (listing, job detail, company
        profile) đều phải đi qua đây -> throttle + retry-backoff áp
        dụng đồng đều, không phụ thuộc nơi gọi có nhớ delay hay không.

        Lỗi cũ: REQUEST_DELAY_SECONDS chỉ được sleep() giữa các trang
        listing trong fetch_jobs(), trong khi fetch_job_full_detail()
        và fetch_company_profile() (gọi cho MỖI job / MỖI công ty mới
        trong pipeline.py) gọi thẳng _fetch_html() không qua throttle
        -> bắn hàng chục request liên tiếp không nghỉ dù config đã tăng
        delay lên 4s."""
        self._throttle()

        for attempt in range(1, max_retries + 1):
            try:
                resp = self.session.get(url, timeout=20)
                if resp.status_code in (429, 403):
                    # 429 = rate limit theo cửa sổ thời gian.
                    # 403 = WAF/Cloudflare chặn theo fingerprint request
                    # (có thể do thiếu header giống trình duyệt thật, HOẶC
                    # IP tạm thời bị đánh dấu do crawl dồn dập trước đó) —
                    # cả 2 trường hợp đều ĐÁNG thử lại sau khi chờ, thay vì
                    # bỏ cuộc ngay ở request đầu tiên.
                    wait = REQUEST_DELAY_SECONDS * (2 ** attempt)
                    logger.warning(
                        "%d %s tại %s (lần %d/%d) -> chờ %.1fs",
                        resp.status_code,
                        "Too Many Requests" if resp.status_code == 429 else "Forbidden",
                        url, attempt, max_retries, wait,
                    )
                    time.sleep(wait)
                    self._last_request_time = time.monotonic()
                    continue
                resp.raise_for_status()
                self._last_request_time = time.monotonic()
                return resp.text
            except requests.exceptions.RequestException as exc:
                # SỬA 08/2026 (đồng bộ với careerviet.py/vietnamworks.py) —
                # retry cả lỗi kết nối không có status code (VD: HTTP/2
                # stream reset, timeout...), không bỏ cuộc ngay ở lần lỗi
                # đầu tiên như trước, vì đã xác nhận loại lỗi này có thể
                # chỉ tạm thời (WAF chặn tạm do request dồn dập).
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
        """Đảm bảo khoảng cách tối thiểu REQUEST_DELAY_SECONDS giữa
        MỌI request, bất kể là listing, job detail hay company profile."""
        if self._last_request_time is None:
            return
        elapsed = time.monotonic() - self._last_request_time
        remaining = REQUEST_DELAY_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)

    # Tracking params TopCV gắn vào MỌI link job trong trang kết quả tìm
    # kiếm, đổi giá trị theo TỪNG PHIÊN tải trang (không phải 1 phần định
    # danh job thật) — vd "?ta_source=JobSearchList_LinkDetail&u_sr_id=
    # Y31QThYAbEWavWHsgLBRk8rXGTYN4R63nl1BlZR7_1786284204". Đã xác nhận
    # bằng dữ liệu thật (08/2026): CÙNG 1 job (cùng job_id trong URL,
    # /2257507.html) xuất hiện với u_sr_id KHÁC NHAU giữa 2 lần crawl ở 2
    # phiên khác nhau -> nếu giữ nguyên làm source_url, chống trùng theo
    # source_url (db.get_job_probe_by_source_url so khớp CHÍNH XÁC chuỗi)
    # sẽ KHÔNG nhận ra đây là cùng 1 job -> insert trùng vào DB.
    _TRACKING_PARAMS = {"ta_source", "u_sr_id"}

    @classmethod
    def _canonicalize_url(cls, href: str) -> str:
        """urljoin + bỏ CHỈ các tracking params đã biết, GIỮ LẠI mọi query
        param khác (vd "/brand/topcv?id=264410" — id là định danh công ty
        THẬT SỰ CẦN THIẾT, không phải rác tracking, phải giữ nguyên)."""
        full = urljoin(BASE_URL, href)
        split = urlsplit(full)
        kept_params = [
            (k, v) for k, v in parse_qsl(split.query, keep_blank_values=True)
            if k not in cls._TRACKING_PARAMS
        ]
        new_query = urlencode(kept_params)
        return urlunsplit((split.scheme, split.netloc, split.path, new_query, ""))

    def _parse_listing_page(self, html: str, matching_industry: str) -> Iterator[RawJobRecord]:
        soup = BeautifulSoup(html, "html.parser")

        # Bước 1: tìm mọi link job chi tiết. TopCV dùng 2 dạng URL job:
        #   /viec-lam/<slug>/<id>.html
        #   /brand/<company>/tuyen-dung/<slug>-j<id>.html
        job_links = soup.find_all(
            "a",
            href=re.compile(r"(/viec-lam/[^\"'#]+\.html|/tuyen-dung/[^\"'#]+\.html)"),
        )

        # Loại bỏ trùng theo href, chỉ giữ href có text (tránh bắt link ảnh <a><img></a> lặp)
        seen_href = {}
        for a in job_links:
            href = a.get("href", "")
            if not href:
                continue
            title = a.get_text(strip=True) or a.get("title", "")
            if not title:
                continue
            # Giữ bản có title dài nhất (thẻ <a> bọc <h3> thường có text đầy đủ hơn)
            if href not in seen_href or len(title) > len(seen_href[href][1]):
                seen_href[href] = (a, title)

        for href, (anchor, title) in seen_href.items():
            card = self._find_card_container(anchor, job_links)
            if card is None:
                card = anchor.parent

            card_text = card.get_text(" ", strip=True)

            # Loại bỏ CHÍNH TEXT TIÊU ĐỀ job ra khỏi phần dùng để quét
            # lương/kinh nghiệm/ngày đăng. Lý do: nhiều job đặt tiêu đề
            # kiểu "câu view" tự nhét sẵn số + đơn vị tiền y hệt định
            # dạng lương, vd "[Thu Nhập 20 Triệu+++ Tại Hà Nội]". Vì tiêu
            # đề luôn nằm ở ĐẦU card_text (trước cả badge lương thật), và
            # _extract_first() lấy match XUẤT HIỆN SỚM NHẤT trong chuỗi
            # (không phải "đẹp nhất") -> nếu không loại trừ, regex bắt
            # NHẦM số trong tiêu đề thay vì badge lương thật nằm sau đó.
            # Đã xác nhận bằng dữ liệu thật (08/2026): job "...Thu Nhập
            # 20 Triệu+++..." bị lưu raw salary = "20 Triệu" (từ tiêu đề)
            # thay vì "15 - 20 triệu" (badge lương thật trên trang).
            title_in_card = anchor.get_text(" ", strip=True)
            badge_text = (
                card_text.replace(title_in_card, "", 1) if title_in_card else card_text
            )

            company_name, company_url = self._extract_company(card, anchor)
            salary_text = self._extract_first(SALARY_PATTERN, badge_text)
            experience_text = self._extract_first(EXPERIENCE_PATTERN, badge_text)
            province_text = self._extract_province(badge_text)
            posted_text = self._extract_first(POSTED_PATTERN, badge_text)

            yield RawJobRecord(
                job_title=title,
                company_name=company_name or "Chưa xác định",
                source_url=self._canonicalize_url(href),
                source_name=self.source_name,
                salary_text=salary_text,
                province_text=province_text,
                experience_text=experience_text,
                posted_text=posted_text,
                matching_industry=matching_industry,
                company_url=company_url,
            )

    def _find_card_container(self, anchor, all_job_links, max_levels: int = 6):
        """
        Đi ngược lên cha, dừng lại ở cấp cha GẦN NHẤT mà chỉ chứa đúng 1
        link job (đúng chính anchor này). Nếu đi lên gặp cấp chứa >1 link
        job -> đã đi quá xa, gộp nhầm nhiều card -> lùi lại 1 cấp trước đó.

        Đây là fix cho lỗi đã gặp lúc trước: "đi lên 4 cấp cha cố định"
        làm gộp nhầm nhiều card liền kề vào 1 khối.
        """
        node = anchor
        last_good = anchor.parent
        for _ in range(max_levels):
            if node.parent is None:
                break
            node = node.parent
            links_in_node = node.find_all(
                "a", href=re.compile(r"(/viec-lam/[^\"'#]+\.html|/tuyen-dung/[^\"'#]+\.html)")
            )
            distinct_hrefs = {a.get("href") for a in links_in_node if a.get("href")}
            if len(distinct_hrefs) == 1:
                last_good = node
            else:
                # node này đã chứa job khác -> dừng, dùng last_good
                break
        return last_good

    def _extract_company(self, card, job_anchor):
        company_links = card.find_all(
            "a", href=re.compile(r"(/cong-ty/[^\"'#]+|/brand/[^\"'#/]+(?:\?|$))")
        )
        for a in company_links:
            if a is job_anchor:
                continue
            # Dùng separator " " vì thẻ <a> có thể chứa badge "Pro" (gói
            # công ty trả phí) nằm trong 1 thẻ con riêng ngay trước tên
            # công ty -> get_text(strip=True) KHÔNG separator sẽ dính
            # liền thành "ProCÔNG TY..." (đã xác nhận bằng dữ liệu thật).
            text = a.get_text(" ", strip=True)
            if text.startswith("Pro"):
                rest = text[3:].lstrip()
                # Chỉ cắt khi phần còn lại rõ ràng là 1 tên công ty mới
                # (bắt đầu bằng chữ hoa) -> tránh cắt nhầm tên công ty
                # thật sự bắt đầu bằng "Pro" (vd "Procter...").
                if rest and rest[0].isupper():
                    text = rest
            if text:
                return text, self._canonicalize_url(a.get("href", ""))
        return "", ""

    def _extract_first(self, pattern: re.Pattern, text: str) -> str:
        m = pattern.search(text)
        return m.group(0).strip() if m else ""

    def _extract_province(self, text: str) -> str:
        for prov in KNOWN_PROVINCES:
            if prov in text:
                return prov
        return ""

    # ------------------------------------------------------------------
    # Crawl sâu vào trang hồ sơ công ty (vd /cong-ty/cel/274063.html)
    # để lấy website thật, địa chỉ, quy mô, lĩnh vực.
    #
    # Xác nhận bằng ảnh chụp thật (08/2026): trang này SSR, có breadcrumb
    # "Trang chủ > Danh sách công ty > <tên>", có link website thật hiện
    # ngay cạnh tên công ty (không phải link topcv.vn), và có 3 mục
    # "Quy mô", "Lĩnh vực hoạt động", "Địa điểm công ty" ở khối bên phải.
    #
    # Parser bám theo NHÃN TIẾNG VIỆT thay vì tên class CSS — bền hơn khi
    # TopCV đổi giao diện, giống logic đã dùng cho parser job.
    # ------------------------------------------------------------------
    def fetch_company_profile(self, company_url: str) -> dict:
        """Trả về dict rỗng nếu không lấy được (không làm crash pipeline)."""
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

        # 1) Website thật: link <a href> ĐẦU TIÊN xuất hiện SAU thẻ <h1>
        # (tên công ty) trong thứ tự tài liệu — KHÔNG phải link ngoài đầu
        # tiên trong TOÀN trang.
        #
        # Lý do: mọi trang TopCV đều có widget "Hỗ trợ qua Zalo" ở phần
        # đầu HTML (trước cả nội dung công ty) chứa link CỐ ĐỊNH
        # https://zalo.me/946504486043251830 — GIỐNG HỆT NHAU ở mọi công
        # ty. Quét "link ngoài đầu tiên trong toàn trang" (cách cũ) luôn
        # bắt trúng link Zalo này trước, vì nó nằm sớm hơn nhiều so với
        # website thật (nằm ngay dưới <h1> tên công ty). Đã xác nhận bằng
        # HTML thật (08/2026): mọi company_id đều bị lưu nhầm cùng 1 link
        # zalo.me/946504486043251830 làm "website".
        #
# Phòng hờ thêm (defense-in-depth): dù đã scope theo <h1>, vẫn
        # LOẠI TRỪ THẲNG link hỗ trợ Zalo cố định này nếu lỡ lọt qua (vd
        # trang không có <h1>, hoặc TopCV đổi vị trí widget hỗ trợ) — thà
        # để trống còn hơn lưu sai dữ liệu vào cột website.
        # Trang Brand Pro (vd /brand/tuyendungvietabank?id=...) có nhiều
        # section xen giữa <h1> và khối "Liên hệ" (Video, Hình ảnh, Sản
        # phẩm/dịch vụ, Giải thưởng...) — quét "mọi link sau <h1>" như
        # trang thường sẽ đi qua rất nhiều link không liên quan (Google
        # Maps embed, link blog TopCV, ảnh...) TRƯỚC khi tới link website
        # thật, dễ bắt nhầm. Nếu trang có heading "Liên hệ" (chỉ Brand Pro
        # mới có, trang thường không có mục này) -> ưu tiên quét link
        # NGAY SAU heading đó trước, phạm vi hẹp hơn nên chính xác hơn hẳn.
        contact_heading = soup.find(
            lambda tag: tag.name in ("h2", "h3") and tag.get_text(strip=True) == "Liên hệ"
        )
        h1 = soup.find("h1")
        if contact_heading:
            anchors_after_h1 = contact_heading.find_all_next("a", href=True)
        elif h1:
            anchors_after_h1 = h1.find_all_next("a", href=True)
        else:
            anchors_after_h1 = soup.find_all("a", href=True)
        for a in anchors_after_h1:
            href = a.get("href", "").strip()
            if not href.startswith("http"):
                continue
            # Loại trừ MỌI link thuộc chính domain TopCV (breadcrumb,
            # "Xem thêm tin của TopCV", link nội bộ khác...) — kiểm tra
            # theo NETLOC (domain) chứa "topcv", không phải substring
            # "topcv.vn" trên toàn URL như code cũ. Lý do đổi: TopCV còn
            # dùng domain PHỤ "topcv.com.vn" cho một số link nội bộ (đã
            # xác nhận bằng dữ liệu thật: nhiều công ty bị lưu nhầm
            # "https://topcv.com.vn/" làm real_website) — chuỗi
            # "topcv.vn" KHÔNG phải substring của "topcv.com.vn" (có
            # ".com" chen giữa "topcv" và "vn") nên filter cũ không bắt
            # được. Check theo netloc chứa "topcv" thì bắt được mọi biến
            # thể domain TopCV sở hữu, kể cả domain phụ nào phát sinh
            # sau này, không cần liệt kê từng domain riêng.
            netloc = urlsplit(href).netloc.lower()
            if "topcv" in netloc:
                continue
            if self._is_support_widget_link(href):
                continue  # link widget cố định của TopCV (Zalo/LinkedIn...), không phải website công ty -> bỏ qua, tìm tiếp
            if not self._is_single_clean_url(href):
                # href chứa khoảng trắng / nhiều URL dính liền nhau (dữ
                # liệu bẩn, không rõ nguồn gốc do đâu) -> thà bỏ qua tìm
                # tiếp, còn hơn lưu 1 chuỗi 2 URL gộp lại không dùng được.
                logger.warning("Bỏ qua href bất thường (nhiều URL dính nhau?): %r", href)
                continue
            result["real_website"] = href
            break

        page_text = soup.get_text("\n", strip=True)
        result["tax_id"] = self._extract_after_label(page_text, "Mã số thuế")
        result["company_size"] = self._extract_after_label(page_text, "Quy mô")
        result["industry"] = self._extract_after_label(
            page_text, "Lĩnh vực hoạt động", multi_line=True
        )
        # Trang Brand Pro (vd /brand/tuyendungvietabank?id=...) dùng label
        # KHÁC cho cùng 1 field so với trang company profile thường:
        # "Địa chỉ trụ sở chính" thay vì "Địa điểm công ty" -> thử cả 2,
        # lấy label nào ra kết quả trước (xác nhận bằng HTML thật 08/2026,
        # trang VietABank Brand Pro).
        result["address"] = (
            self._extract_after_label(page_text, "Địa điểm công ty")
            or self._extract_after_label(page_text, "Địa chỉ trụ sở chính")
        )

        # "Giới thiệu công ty" -> lấy đoạn text ngay sau heading này
        intro_idx = page_text.find("Giới thiệu công ty")
        if intro_idx != -1:
            after = page_text[intro_idx + len("Giới thiệu công ty"):]
            # Cắt tới trước dòng chỉ chứa lại chính website (thường lặp lại ở cuối đoạn)
            lines = [l for l in after.split("\n") if l.strip()]
            desc_lines = []
            for line in lines[:15]:  # giới hạn, tránh nuốt luôn cả phần sau trang
                if line.strip().startswith("www.") or line.strip().startswith("http"):
                    break
                desc_lines.append(line.strip())
                if len(" ".join(desc_lines)) > 600:  # đủ dài thì dừng
                    break
            result["description"] = " ".join(desc_lines).strip()

        return result

    # Các nhãn tiếng Việt hay gặp trên trang hồ sơ công ty — dùng để biết
    # khi nào một giá trị nhiều dòng (vd "Lĩnh vực hoạt động") nên DỪNG lại,
    # tránh nuốt luôn cả nhãn tiếp theo vào giá trị.
    _KNOWN_LABELS = [
        "Mã số thuế", "Quy mô", "Lĩnh vực hoạt động", "Lĩnh vực chính",
        "Địa điểm công ty", "Địa chỉ trụ sở chính", "Giới thiệu công ty",
        # Label RIÊNG của trang Brand Pro, không có ở trang company profile
        # thường -> thêm vào đây để "Lĩnh vực hoạt động" (multi_line=True)
        # dừng đúng chỗ, không nuốt nhầm "Năm thành lập"/"Độ tuổi trung
        # bình" (2 dòng số liệu chen giữa "Mã số thuế" và "Quy mô" chỉ có
        # ở trang Brand Pro, xác nhận bằng HTML thật 08/2026, VietABank).
        "Năm thành lập", "Độ tuổi trung bình",
    ]

    # ------------------------------------------------------------------
    # Crawl sâu vào trang chi tiết job (vd /viec-lam/<slug>/<id>.html) để
    # lấy các field CHỈ hiển thị ở đó, không có trên trang listing:
    #   - work_type, deadline_text (đã có từ trước)
    #   - job_description, requirements, perks, required_skills (mới)
    #
    # Xác nhận bằng HTML thật (08/2026, view-source, không phải DOM sau
    # render): mọi khối nội dung lớn đều nằm trong
    #   <div class="box-job-information-detail-item ...">
    #       <h2 class="box-job-information-detail-item__title--title">
    #           <heading text>
    #       </h2>
    #       <div class="box-job-information-detail-item__text">...</div>
    #   </div>
    # phân biệt nhau bằng TEXT của <h2>, không phải class con (class con
    # như "box-job-information-required-candidate" chỉ có ở 1 số khối,
    # khối "Mô tả công việc" không có class con riêng) -> parser bám theo
    # text heading cho chắc, không bám class con.
    #
    # "Loại hình làm việc" nằm trong khối "Thông tin chung"
    # (box-job-information-general-info), lặp lại pattern
    # "...--content-title" / "...--content-desc" cho mỗi dòng info.
    #
    # "Hạn ứng tuyển" nằm riêng trong <div class="box-applied-cv"><p>
    # Hạn ứng tuyển: <span class="date">dd/mm/yyyy</span></p></div> —
    # bám thẳng span.date, không cần regex dò trong toàn trang.
    #
    # Nút "Xem đầy đủ mô tả công việc" (content-preview__toggle) CHỈ là
    # UI thu gọn bằng CSS — đã xác nhận bằng view-source thật: toàn bộ
    # nội dung đã nằm sẵn trong HTML server trả về, không cần xử lý gì
    # thêm để lấy phần "bị ẩn".
    #
    # BUG ĐÃ SỬA (08/2026, phát hiện qua audit dữ liệu thật — 4 job dạng
    # URL "/brand/<company>/tuyen-dung/..." bị lưu parsed_content/
    # work_type/deadline = NULL toàn bộ dù _fetch_html() lấy HTML thành
    # công): trang Brand Pro dùng TEMPLATE HOÀN TOÀN KHÁC trang thường,
    # KHÔNG có class "box-job-information-detail-item" nào cả -> vòng lặp
    # find_all() ở trên luôn rỗng, deadline_box cũng luôn None -> mọi
    # field trả về "" một cách im lặng (không phải fetch lỗi, HTML fetch
    # OK, chỉ là selector sai template).
    #
    # Xác nhận bằng HTML thật lấy qua view-source (không phải DevTools)
    # từ 2 job Brand Pro thật (08/2026, HappyMoney "Business Analyst" +
    # Elcom "Geospatial Data Engineer"):
    #   <div class="premium-job-description__box ...">
    #       <h2 class="premium-job-description__box--title">
    #           <heading text>
    #       </h2>
    #       <div class="premium-job-description__box--content">...</div>
    #   </div>
    # Heading text ở Brand Pro có 1 điểm khác trang thường: "Quyền lợi
    # được hưởng" (Brand Pro) thay vì "Quyền lợi ứng viên" (trang thường)
    # -> phải match CẢ 2 cách viết cho field "perks", nếu không sẽ lại
    # bỏ sót y hệt kiểu bug này.
    #
    # Deadline ở Brand Pro nằm trong
    #   <div class="job-detail__info--deadline">
    #       Hạn nộp hồ sơ:
    #       <div class="job-detail__info--deadline-date">dd/mm/yyyy</div>
    #       ...
    #   </div>
    # KHÔNG dùng class "date"/"box-applied-cv" như trang thường -> bám
    # thêm class "job-detail__info--deadline-date" làm phương án dự
    # phòng, thử SAU khi đã thử cách cũ (trang thường ưu tiên trước vì
    # đã xác nhận nhiều lần, tránh đảo thứ tự làm hỏng case cũ).
    #
    # "Loại hình làm việc" — ĐÃ TỪNG KẾT LUẬN SAI (sửa lại 08/2026): lúc
    # đầu tưởng trang Brand Pro không có field này (không thấy class
    # "box-job-information-general-info-list__item" của trang thường) và
    # kết luận vội để work_type = "" là đúng bản chất trang. Sau khi có
    # thêm HTML thật (view-source, khối "Thông tin chung" nằm ở sidebar
    # phải trang, KHÔNG nằm cùng khối JD chính) mới xác nhận field NÀY
    # VẪN CÓ, chỉ là nằm trong 1 khối class khác hẳn:
    # "premium-job-general-information" (không phải
    # "premium-job-description__box") — xem nhánh xử lý work_type riêng
    # trong fetch_job_full_detail() bên dưới. Bài học: "không tìm thấy
    # class cũ" không đồng nghĩa "trang không có field đó" — cần xác nhận
    # bằng HTML thật trước khi kết luận 1 field là "rỗng đúng bản chất".
    #
    # "Kỹ năng cần có" (tag rời): đã xác nhận bằng HTML thật, trang Brand
    # Pro KHÔNG có khối "required-tag" nào trong toàn trang -> để [] là
    # ĐÚNG với trang Brand Pro (khác với "chưa parse được"), không cố
    # suy luận thêm.
    # ------------------------------------------------------------------
    def fetch_job_full_detail(self, source_url: str) -> Optional[dict]:
        """Trả về dict CHỈ KHI fetch HTML thành công — dict khi đó vẫn có
        thể có field "" / [] cho khối nào trang không có (không phải JD
        nào cũng đủ mọi khối), đây KHÔNG coi là thất bại.

        Trả None (KHÁC HẲN dict rỗng-an-toàn) khi _fetch_html() thất bại
        thật sự (403/429 sau khi hết retry, timeout, network error,
        source_url rỗng) — để pipeline.py phân biệt được 2 trường hợp và
        BỎ HẲN job đó (theo quyết định: thà thiếu job còn hơn insert 1
        job với work_type/deadline/parsed_content = NULL một cách âm thầm
        không ai biết, dễ nhầm tưởng "trang không có dữ liệu" trong khi
        thực ra là bị chặn khi crawl)."""
        result = {
            "work_type": "",
            "deadline_text": "",
            "job_description": "",
            "requirements": "",
            "perks": "",
            "required_skills": [],
        }
        if not source_url:
            return None

        html = self._fetch_html(source_url)
        if html is None:
            return None

        soup = BeautifulSoup(html, "html.parser")

        # --- 3 khối nội dung lớn: Mô tả công việc / Yêu cầu ứng viên / Quyền lợi ---
        # Thử template trang thường TRƯỚC (đã xác nhận nhiều lần).
        for block in soup.find_all("div", class_="box-job-information-detail-item"):
            heading = block.find("h2", class_="box-job-information-detail-item__title--title")
            if not heading:
                continue
            heading_text = heading.get_text(strip=True)
            text_div = block.find("div", class_="box-job-information-detail-item__text")
            content = text_div.get_text("\n", strip=True) if text_div else ""

            if heading_text == "Mô tả công việc":
                result["job_description"] = content
            elif heading_text == "Yêu cầu ứng viên":
                result["requirements"] = content
            elif heading_text == "Quyền lợi ứng viên":
                result["perks"] = content
            elif heading_text == "Thông tin chung":
                # work_type nằm trong khối này, dạng title/desc lặp lại
                for item in block.find_all(
                    "div", class_="box-job-information-general-info-list__item"
                ):
                    title_el = item.find(
                        "div", class_="box-job-information-general-info-list__item--content-title"
                    )
                    desc_el = item.find(
                        "div", class_="box-job-information-general-info-list__item--content-desc"
                    )
                    if not title_el or not desc_el:
                        continue
                    if title_el.get_text(strip=True) == "Loại hình làm việc":
                        result["work_type"] = desc_el.get_text(strip=True)

        # --- Fallback template Brand Pro (chỉ chạy khi template trang
        # thường ở trên không tìm thấy gì — tránh chạy thừa/gộp nhầm dữ
        # liệu nếu 1 trang nào đó lỡ có cả 2 loại class do TopCV đổi giao
        # diện giữa chừng) ---
        if not any([result["job_description"], result["requirements"], result["perks"]]):
            for block in soup.find_all("div", class_="premium-job-description__box"):
                heading = block.find("h2", class_="premium-job-description__box--title")
                if not heading:
                    continue
                heading_text = heading.get_text(strip=True)
                content_div = block.find("div", class_="premium-job-description__box--content")
                content = content_div.get_text("\n", strip=True) if content_div else ""

                if heading_text == "Mô tả công việc":
                    result["job_description"] = content
                elif heading_text == "Yêu cầu ứng viên":
                    result["requirements"] = content
                elif heading_text in ("Quyền lợi ứng viên", "Quyền lợi được hưởng"):
                    result["perks"] = content
                # work_type Brand Pro KHÔNG nằm trong khối
                # "premium-job-description__box" này — xem nhánh riêng
                # bên dưới (premium-job-general-information).

        # work_type Brand Pro — nằm trong 1 khối SIDEBAR HOÀN TOÀN RIÊNG
        # ("premium-job-general-information"), KHÁC khối JD chính
        # ("premium-job-description__box") ở trên. Đây là chỗ đã từng
        # kết luận SAI (đã sửa 08/2026): lúc đầu tưởng Brand Pro không có
        # field "Loại hình làm việc" nên để work_type = "" mặc định, sau
        # xác nhận lại bằng HTML thật (view-source) thì field NÀY VẪN CÓ,
        # chỉ là nằm ở vị trí khác hẳn trang thường.
        #
        # BẪY cần tránh: có 1 field khác tên rất giống —
        # "Hình thức làm việc" (nghĩa là Onsite/Remote/Hybrid, vd "Làm
        # việc tại văn phòng / Onsite") — nằm ngay sát bên trong CÙNG 1
        # khối, dùng đúng cấu trúc HTML lặp lại (label/value row) như
        # "Loại hình làm việc" (nghĩa là Toàn thời gian/Bán thời gian).
        # Nếu match lỏng tay (vd contains thay vì so khớp chính xác từng
        # ký tự) sẽ dễ lấy nhầm giá trị "Onsite" gán vào work_type.
        # Chỉ xử lý nếu chưa lấy được work_type từ template trang thường
        # ở trên (tránh ghi đè nếu 1 trang lỡ có cả 2 loại class).
        if not result["work_type"]:
            general_info = soup.find("div", class_="premium-job-general-information")
            if general_info:
                for row in general_info.find_all(
                    "div", class_="premium-job-general-information__content--row"
                ):
                    label_el = row.find("div", class_="general-information-data__label")
                    value_el = row.find("div", class_="general-information-data__value")
                    if not label_el or not value_el:
                        continue
                    if label_el.get_text(strip=True) == "Loại hình làm việc":
                        result["work_type"] = value_el.get_text(strip=True)

        # --- Kỹ năng cần có: lấy trực tiếp từ tag TopCV, không suy luận ---
        # (chỉ dùng khi TopCV có sẵn tag này; nếu không có thì để rỗng —
        # không đáng để thêm 1 tầng suy luận/AI chỉ để lấp đầy field này.
        # Brand Pro không có khối này -> tự động để rỗng, đúng thực tế).
        for tag in soup.find_all("div", class_="required-tag"):
            title_el = tag.find("h3", class_="required-tag__content--title")
            if title_el and title_el.get_text(strip=True) == "Kỹ năng cần có":
                desc_el = tag.find("div", class_="required-tag__content--desc")
                if desc_el:
                    result["required_skills"] = [
                        s.strip() for s in desc_el.get_text(strip=True).split(",") if s.strip()
                    ]

        # --- Hạn ứng tuyển: bám thẳng span.date trong box-applied-cv
        # (trang thường) trước, sau đó thử class riêng của Brand Pro. ---
        deadline_box = soup.find("div", class_="box-applied-cv")
        if deadline_box:
            date_span = deadline_box.find("span", class_="date")
            if date_span:
                result["deadline_text"] = date_span.get_text(strip=True)
        if not result["deadline_text"]:
            brand_pro_date = soup.find("div", class_="job-detail__info--deadline-date")
            if brand_pro_date:
                result["deadline_text"] = brand_pro_date.get_text(strip=True)

        return result

    # Text của các nút toggle UI (không phải dữ liệu thật) — TopCV chèn
    # 2 nút này ngay sau nhiều giá trị nhiều dòng như "Lĩnh vực hoạt
    # động" (để thu gọn/mở rộng danh sách ngành hiển thị). Nếu không lọc,
    # chúng bị gom nhầm vào giá trị, vd:
    # "IT - Phần mềm / Xem thêm / Thu gọn" (đã xác nhận bằng dữ liệu thật).
    _UI_TOGGLE_TEXTS = {"Xem thêm", "Thu gọn"}

    # Link widget hỗ trợ CỐ ĐỊNH của TopCV, xuất hiện giống hệt nhau ở
    # MỌI trang company profile (không phải website riêng của công ty nào)
    # — nếu bị bắt nhầm thành "real_website" thì loại trừ, để trống thay
    # vì lưu sai.
    #
    # LỊCH SỬ BUG (liên tục lặp lại theo kiểu "vá 1 chỗ, lộ chỗ khác" khi
    # chặn theo TỪNG LINK CỤ THỂ): Zalo cố định -> icon "Theo dõi TopCV
    # trên LinkedIn" -> icon "Theo dõi TopCV trên Threads" -> link tải
    # app trên App Store (itunes.apple.com/us/app/topcv-...). Mỗi lần
    # TopCV thêm 1 icon mới cạnh tên công ty, code lại bắt nhầm icon đó
    # làm real_website ở các công ty KHÔNG có website thật — chặn theo
    # từng URL/prefix riêng lẻ không bao giờ theo kịp.
    #
    # GIẢI PHÁP TRIỆT ĐỂ: đảo ngược cách tiếp cận — thay vì cố liệt kê
    # "link nào là widget của TopCV" (danh sách mở, luôn thiếu), chặn
    # thẳng CẢ NHÓM DOMAIN mà về bản chất không bao giờ là website chính
    # thức của 1 công ty: mạng xã hội, app store, app nhắn tin. Một công
    # ty thật luôn có domain riêng (vd cel-consulting.com) — nếu link
    # "website" duy nhất tìm được lại trỏ vào 1 trong các domain này,
    # gần như chắc chắn đó là widget/link phụ của TopCV (hoặc trang mạng
    # xã hội công ty tự gắn, cũng không phải "website" theo đúng nghĩa
    # cột này) chứ không phải website chính thức -> để trống, đúng tinh
    # thần "thà thiếu còn hơn lưu sai" đã áp dụng xuyên suốt file này.
    _NON_COMPANY_WEBSITE_DOMAINS = (
        "linkedin.com", "threads.com", "facebook.com", "tiktok.com",
        "youtube.com", "twitter.com", "x.com", "instagram.com",
        "zalo.me", "itunes.apple.com", "apps.apple.com", "play.google.com",
        # Bổ sung sau khi debug trang Brand Pro (vd VietABank, 08/2026):
        # trang này chèn thêm Google Maps embed + link "Tìm hiểu thêm" trỏ
        # sang blog.topcv.vn (domain phụ KHÁC "topcv" nên lọt qua check
        # netloc chứa "topcv" ở trên) TRƯỚC vị trí link website thật trong
        # thứ tự tài liệu -> nếu không loại trừ, code bắt nhầm 1 trong 2
        # link này làm real_website thay vì link thật ở khối "Liên hệ".
        "google.com", "maps.google.com", "blog.topcv.vn",
    )

    @classmethod
    def _is_support_widget_link(cls, href: str) -> bool:
        netloc = urlsplit(href).netloc.lower().removeprefix("www.")
        return netloc in cls._NON_COMPANY_WEBSITE_DOMAINS

    @staticmethod
    def _is_single_clean_url(href: str) -> bool:
        """True nếu href là ĐÚNG 1 URL sạch, không phải nhiều URL dính
        liền nhau cách bởi khoảng trắng (đã gặp dữ liệu thật kiểu
        "http://a.com/ https://b.com/" gộp làm 1 -> không rõ do TopCV lỗi
        HTML gốc hay do bug ở lần crawl trước, nhưng dù nguyên nhân gì thì
        đây không phải URL dùng fetch được, nên loại thẳng)."""
        if not href or " " in href or "\t" in href or "\n" in href:
            return False
        return href.count("http://") + href.count("https://") == 1

    @classmethod
    def _extract_after_label(cls, page_text: str, label: str, multi_line: bool = False) -> str:
        """Tìm dòng chứa đúng label, trả về (các) dòng KẾ TIẾP KHÁC LABEL
        làm giá trị. Trang TopCV có thể lặp lại cùng 1 nhãn 2 lần (vd tiêu
        đề mục + label con bên trong) — nếu chỉ lấy "dòng kế tiếp" đơn giản
        sẽ bắt nhầm ngay dòng label thứ 2 thay vì giá trị thật, nên phải
        bỏ qua mọi dòng == label.

        multi_line=True: gộp nhiều dòng liên tiếp cho tới khi gặp 1 nhãn
        đã biết khác (vd "Lĩnh vực hoạt động" có thể xuống dòng thành
        nhiều ngành: "Marketing / Truyền thông" rồi "IT - Phần mềm").
        Cũng bỏ qua text của nút toggle "Xem thêm"/"Thu gọn" (UI, không
        phải dữ liệu) thường xuất hiện ngay sau giá trị nhiều dòng."""
        lines = page_text.split("\n")
        for i, line in enumerate(lines):
            if line.strip() == label:
                values = []
                for nxt in lines[i + 1: i + 6]:
                    nxt_clean = nxt.strip()
                    if not nxt_clean or nxt_clean == label:
                        continue
                    if nxt_clean in cls._KNOWN_LABELS or nxt_clean in cls._UI_TOGGLE_TEXTS:
                        break
                    values.append(nxt_clean)
                    if not multi_line:
                        break
                if values:
                    return " / ".join(values) if multi_line else values[0]
        return ""
