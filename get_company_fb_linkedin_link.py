"""
Script RIÊNG (không nằm trong pipeline crawl TopCV chính) — điền thêm
companies.fanpage_url / companies.linkedin_url.

Ý tưởng: 2 field này KHÔNG có trên TopCV. Nhưng công ty thường tự gắn
link Facebook/LinkedIn thật của họ ngay trên website riêng (thường ở
phần header hoặc footer) — nên với những company ĐÃ CÓ companies.website
(lấy được từ fetch_company_profile() của TopCV trước đó), ta có thể vào
thẳng website đó để tìm 2 link này, thay vì đoán mò qua Google (rủi ro
bắt nhầm trang của công ty khác trùng tên / fanpage giả mạo).

Company nào không có website, hoặc website không có link social nào ->
để trống (NULL), không cố tìm bằng cách khác. Ghi sai vào cột này còn
tệ hơn để trống — dữ liệu sai trông giống dữ liệu đúng.

GIỚI HẠN ĐÃ BIẾT — WEBSITE DẠNG SPA/CSR (React/Next.js/Vue...) (08/2026):
Script này fetch HTML thô qua curl_cffi + parse bằng BeautifulSoup —
KHÔNG chạy JavaScript. Nhiều website công ty hiện đại (đặc biệt startup/
công nghệ, vd ikameglobal.com, lynkid.vn — phát hiện thật 08/2026) render
toàn bộ <body> (kể cả link social ở header/footer) bằng JS SAU khi trang
tải, khiến HTML thô server trả về gần như rỗng -> script không tìm thấy
gì dù link social THẬT SỰ có khi mở bằng trình duyệt. Đây là giới hạn nền
tảng của cách tiếp cận requests-based, không phải lỗi parse có thể vá
bằng regex khác (fallback regex quét toàn bộ text HTML thô đã đủ rộng —
nếu vẫn không thấy gì, nghĩa là bản thân response không hề chứa dấu vết
facebook.com/linkedin.com, không phải do quét thiếu).

_is_likely_js_rendered_shell() phát hiện heuristic các trang kiểu này
(body gần rỗng, 0 link) và TÁCH RIÊNG khỏi thống kê "no_link_found" ->
xem stats["likely_js_rendered"] sau khi chạy để biết công ty nào cần
công cụ khác (headless browser như Playwright) mới lấy được, tránh hiểu
nhầm "công ty này không có mạng xã hội" khi thực ra chỉ là script chưa
đọc được.

TẦNG 1 (08/2026) — 5 nguyên nhân khác gây "báo sai là không có link",
VẪN Ở TẦNG HTML TĨNH (không cần chạy JS thật, không thêm dependency
nặng), đã vá trong đợt này:

  1. Challenge/anti-bot page (Cloudflare "Just a moment...", captcha xác
     minh trình duyệt) — server trả HTTP 200 nhưng nội dung là trang xác
     minh, không phải trang thật. TRƯỚC ĐÂY chỉ coi status_code >= 400 là
     lỗi fetch nên case này lọt qua, bị hiểu nhầm thành "không có link"
     hoặc "SPA rỗng" tuỳ độ dài nội dung challenge. Giờ _is_challenge_page()
     phát hiện riêng -> stats["challenge_page"], KHÔNG kết luận gì thêm.

  2. Link social chỉ nằm ở trang con (Liên hệ/Giới thiệu), không phải
     trang chủ — _try_fallback_subpages() thử thêm tối đa
     _MAX_SUBPAGE_TRIES URL con đoán được (/lien-he, /contact, /gioi-thieu,
     /about) TRƯỚC KHI kết luận "không có link"/"SPA rỗng", dừng ngay khi
     tìm được. Không thử toàn bộ danh sách nếu không cần, tránh tốn quá
     nhiều request cho công ty vốn thật sự không có gì.

  3. Facebook Page Plugin nhúng qua iframe (thường
     <iframe src="/plugins/page.php?href=https%3A%2F%2Fwww.facebook.com%2F...">,
     đôi khi qua <a>) — TRƯỚC ĐÂY code chủ động loại bỏ mọi path
     '/plugins/' (đúng, vì bản thân URL đó không phải link thật), nhưng
     CHƯA bóc tách URL fanpage thật nằm trong query param 'href' của
     chính iframe/link đó -> bỏ lỡ dữ liệu ngay trước mắt.
     _extract_facebook_plugin_href() xử lý case này; find_social_links()
     giờ cũng quét thêm <iframe src> (không chỉ <a href> như trước).

  4. LinkedIn trỏ tới hồ sơ CÁ NHÂN (/in/ten-ceo) thay vì trang CÔNG TY
     (/company/ten-cty) — TRƯỚC ĐÂY không phân biệt, có thể lưu nhầm
     profile CEO thành linkedin_url của công ty. _pick_best_linkedin()
     ưu tiên '/company/'; nếu trang CHỈ có '/in/', KHÔNG lưu (đúng nguyên
     tắc "thà thiếu còn hơn sai" xuyên suốt project) -> đếm riêng
     stats["linkedin_personal_only_skipped"] để biết công ty nào cần
     người kiểm tra tay, không hiểu nhầm là "không có LinkedIn".

  5. companies.website vô tình CHÍNH LÀ URL Facebook/LinkedIn (lỡ lọt qua
     bước enrich trước đó) — TRƯỚC ĐÂY script sẽ cố crawl thẳng
     facebook.com/linkedin.com như 1 website thường, gần như chắc chắn bị
     chặn/parse sai. Giờ kiểm tra TRƯỚC khi fetch: nếu website đã là
     domain social, dùng LUÔN làm fanpage_url/linkedin_url (nếu đúng định
     dạng trang công ty) thay vì crawl vô ích -> stats["website_is_social_domain"].

CHƯA VÁ (để dành cho quyết định sau, xem README/chat log):
  - Case cần chạy JS THẬT mới lấy được (CSR gốc như ikameglobal.com/
    lynkid.vn, nội dung chỉ hiện sau tương tác, Cloudflare Turnstile...)
    -> vẫn nằm trong stats["likely_js_rendered"], chỉ headless browser
    (Playwright) mới giải quyết được, CHƯA triển khai trong đợt này.

Tách thành script riêng, KHÔNG gộp vào pipeline.py, vì đây là nguồn dữ
liệu khác hẳn TopCV (mỗi website công ty một kiểu HTML, tỷ lệ lỗi/timeout
cao hơn nhiều so với crawl TopCV) — không nên làm chậm/rủi ro luồng crawl
job chính. Chạy độc lập, khi nào cần vá thì chạy lại.

Cách chạy:
    python get_company_fb_linkedin_link.py
    python get_company_fb_linkedin_link.py --limit 50   # test thử ít công ty trước
"""

import argparse
import logging
import re
import time
from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, unquote

from curl_cffi import requests
from bs4 import BeautifulSoup

import db
from config import DEFAULT_HEADERS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Độ trễ giữa các request (giây) — mỗi website là 1 domain khác nhau nên
# không có rủi ro bị 1 server chặn dồn dập như crawl TopCV, nhưng vẫn giữ
# 1 khoảng nghỉ lịch sự, tránh gửi request dồn dập hàng loạt domain lạ.
# Áp dụng luôn cho request tới URL con (Tầng 1, case #2) — cùng 1 fetcher,
# cùng 1 nhịp throttle, không cần hằng số riêng.
REQUEST_DELAY_SECONDS = 2.0

# Timeout ngắn hơn so với TopCV (20s) vì đây là hàng trăm domain lạ khác
# nhau, nhiều site chậm/die — không đáng chờ lâu cho 1 field không bắt buộc.
REQUEST_TIMEOUT_SECONDS = 10

FACEBOOK_PATTERN = re.compile(r"(https?:)?//(www\.)?facebook\.com/[^\s\"'<>]+", re.IGNORECASE)
LINKEDIN_PATTERN = re.compile(r"(https?:)?//(www\.)?linkedin\.com/[^\s\"'<>]+", re.IGNORECASE)

# Các path Facebook/LinkedIn KHÔNG phải fanpage/company page thật, mà là
# nút chia sẻ/đăng nhập chung của mọi website có gắn nút social share —
# nếu không loại, sẽ bắt nhầm hàng loạt công ty ra CÙNG 1 link giả.
# '/plugins/' vẫn nằm trong danh sách này (URL plugin gốc không phải link
# thật) — nhưng giờ được xử lý riêng bởi _extract_facebook_plugin_href()
# TRƯỚC KHI rơi vào check ignore này, nên không còn bị loại oan (case #3).
_FACEBOOK_IGNORE_PATH_PREFIXES = (
    "/sharer", "/share.php", "/share/", "/dialog/", "/plugins/", "/login", "/tr/",
)
_LINKEDIN_IGNORE_PATH_PREFIXES = (
    "/sharing/", "/shareArticle", "/login", "/uas/", "/legal/", "/help/",
)

# Tầng 1, case #2: URL con hay gặp chứa social link khi trang chủ không
# có (nhiều công ty chỉ gắn icon social ở footer trang Liên hệ/Giới
# thiệu, không phải trang chủ). Thử theo đúng thứ tự này, dừng ngay khi
# tìm được — KHÔNG thử hết danh sách nếu không cần (giới hạn bởi
# _MAX_SUBPAGE_TRIES bên dưới), tránh tốn quá nhiều request cho công ty
# thật sự không có gì.
_FALLBACK_SUBPATHS = ("/lien-he", "/contact", "/gioi-thieu", "/about")
_MAX_SUBPAGE_TRIES = 2

# Tầng 1, case #1: chuỗi nhận diện challenge/anti-bot page (Cloudflare...).
# So khớp KHÔNG phân biệt hoa thường trên toàn bộ HTML thô — các cụm này
# đặc trưng riêng cho trang xác minh trình duyệt, không xuất hiện tình cờ
# trong nội dung trang thật.
_CHALLENGE_PAGE_MARKERS = (
    "just a moment",
    "checking your browser before accessing",
    "cf-browser-verification",
    "cf-chl-widget",
    "enable javascript and cookies to continue",
    "attention required! | cloudflare",
    "verifying you are human",
    "please verify you are a human",
)


def _clean_social_url(raw_url: str) -> str:
    """Chuẩn hoá URL tìm được: thêm scheme nếu thiếu (link dạng //domain),
    bỏ query/fragment (thường là tracking params không cần thiết).

    Ngoại lệ: 'profile.php' của Facebook dùng query '?id=...' làm ĐỊNH
    DANH bắt buộc (không có nó thì URL vô nghĩa, không trỏ tới ai cả) —
    khác với các link dạng '/tencongty?ref=...' nơi query chỉ là tracking
    thêm vào, phải giữ lại query string cho riêng case này."""
    url = raw_url.strip().rstrip("/.,;")
    if url.startswith("//"):
        url = "https:" + url
    split = urlsplit(url)
    if split.path.lower().endswith("/profile.php") and "id=" in split.query.lower():
        return urlunsplit((split.scheme or "https", split.netloc, split.path, split.query, ""))
    return urlunsplit((split.scheme or "https", split.netloc, split.path, "", ""))


def _is_facebook_domain(url: str) -> bool:
    """Check ĐÚNG netloc, không phải substring — tránh bắt nhầm link nội
    bộ dạng '/facebook.com/xyz' (relative path vô tình chứa chữ
    'facebook.com') bị urljoin biến thành 'https://<domain-cong-ty>/
    facebook.com/xyz', trông giống nhưng KHÔNG phải domain Facebook thật."""
    netloc = urlsplit(url).netloc.lower()
    return netloc == "facebook.com" or netloc.endswith(".facebook.com")


def _is_linkedin_domain(url: str) -> bool:
    netloc = urlsplit(url).netloc.lower()
    return netloc == "linkedin.com" or netloc.endswith(".linkedin.com")


def _is_ignored_facebook_path(url: str) -> bool:
    path = urlsplit(url).path.lower()
    if path in ("", "/"):
        return True
    if any(path.startswith(p) for p in _FACEBOOK_IGNORE_PATH_PREFIXES):
        return True
    # 'profile.php' KHÔNG kèm '?id=...' không trỏ tới ai cụ thể -> vô
    # nghĩa nếu lưu lại (case thật gặp: /profile.php bị strip mất query
    # gốc, chỉ còn lại chuỗi rỗng không dùng được).
    if path.endswith("/profile.php") and "id=" not in urlsplit(url).query.lower():
        return True
    return False


def _is_ignored_linkedin_path(url: str) -> bool:
    path = urlsplit(url).path.lower()
    return any(path.startswith(p) for p in _LINKEDIN_IGNORE_PATH_PREFIXES) or path in ("", "/")


def _extract_facebook_plugin_href(url: str) -> Optional[str]:
    """Tầng 1, case #3: bóc URL fanpage THẬT nằm trong query param 'href'
    của Facebook Page Plugin (dạng
    '/plugins/page.php?href=https%3A%2F%2Fwww.facebook.com%2F...',
    thường nhúng qua <iframe src=...>, đôi khi qua <a href=...>).

    TRƯỚC ĐÂY mọi path '/plugins/' bị loại thẳng (đúng, vì bản thân URL
    plugin không phải link fanpage thật) nhưng KHÔNG bóc query param bên
    trong -> bỏ lỡ dữ liệu ngay trước mắt dù nó nằm sẵn trong HTML.

    Trả None nếu: không phải path '/plugins/', không có param 'href',
    hoặc href giải mã ra KHÔNG trỏ tới domain facebook.com thật (tránh mở
    khoá lỗ hổng ngược — 1 site nhúng plugin nhưng href trỏ domain khác
    không đáng tin)."""
    split = urlsplit(url)
    if not split.path.lower().startswith("/plugins/"):
        return None
    params = dict(parse_qsl(split.query))
    href = params.get("href", "")
    if not href:
        return None
    href = unquote(href)
    if not _is_facebook_domain(href):
        return None
    return _clean_social_url(href)


def _pick_best_linkedin(candidates: list) -> tuple:
    """Tầng 1, case #4: ưu tiên link '/company/...' (trang công ty thật)
    trong số các candidate LinkedIn tìm được trên trang. Nếu CHỈ tìm được
    '/in/...' (hồ sơ cá nhân, vd CEO/nhân viên) -> KHÔNG lưu làm
    linkedin_url của công ty, đúng nguyên tắc "thà thiếu còn hơn sai"
    xuyên suốt project (dữ liệu sai trông giống dữ liệu đúng, còn tệ hơn
    để trống).

    Trả (linkedin_url, is_personal_only):
      - Có '/company/' trong candidates -> (url đó, False).
      - Có candidate nhưng KHÔNG cái nào là '/company/' -> ("", True) —
        nơi gọi dùng cờ này để đếm riêng stats["linkedin_personal_only_skipped"],
        không hiểu nhầm thành "trang không có LinkedIn".
      - Không có candidate nào -> ("", False)."""
    for candidate in candidates:
        if urlsplit(candidate).path.lower().startswith("/company/"):
            return candidate, False
    if candidates:
        return "", True
    return "", False


def _is_likely_js_rendered_shell(soup: BeautifulSoup) -> bool:
    """Phát hiện heuristic: trang này CÓ VẺ LÀ SPA (React/Next.js/Vue...)
    render nội dung <body> HOÀN TOÀN bằng JavaScript SAU khi tải trang,
    khiến HTML thô mà curl_cffi nhận được gần như rỗng (chỉ có <head>
    cho SEO, <body> chỉ có 1-2 thẻ div rỗng chờ JS "vẽ" vào).

    PHÁT HIỆN THẬT (08/2026): 2 công ty ikameglobal.com và lynkid.vn có
    social link RÕ RÀNG khi mở bằng trình duyệt thật, nhưng script báo
    "không tìm thấy" — xác nhận bằng cách fetch thẳng: <body> HTML thô
    server trả về gần như trống, nội dung thật (bao gồm link social) chỉ
    xuất hiện sau khi JS chạy. Đây LÀ GIỚI HẠN NỀN TẢNG của cách tiếp cận
    requests+BeautifulSoup (không chạy JS được) — không phải lỗi parse
    logic có thể vá bằng regex/selector khác.

    Heuristic: <body> không tồn tại, HOẶC có rất ít text (<200 ký tự) VÀ
    0 thẻ <a href> nào — ngưỡng này phân biệt "trang tĩnh thật sự không
    gắn social link" (vẫn có nội dung, menu, footer... bình thường) khỏi
    "vỏ HTML SPA rỗng chờ JS" (gần như không có gì ngoài <script> tag).

    Dùng để LOG/THỐNG KÊ RIÊNG (không phải "không có link") — vì 2 tình
    huống này cần xử lý khác nhau: "thật sự không có" -> để trống là
    đúng và không cần làm gì thêm; "không đọc được vì SPA" -> cần công cụ
    khác (headless browser) mới lấy được, KHÔNG nên hiểu nhầm là công ty
    không có mạng xã hội."""
    body = soup.find("body")
    if body is None:
        return True
    text_len = len(body.get_text(strip=True))
    anchor_count = len(body.find_all("a", href=True))
    return text_len < 200 and anchor_count == 0


def _is_challenge_page(html: str) -> bool:
    """Tầng 1, case #1: phát hiện heuristic trang challenge/anti-bot
    (Cloudflare "Just a moment...", captcha xác minh trình duyệt...) —
    server trả HTTP 200 OK nhưng nội dung là trang xác minh, không phải
    trang thật. TRƯỚC ĐÂY code chỉ coi status_code >= 400 là lỗi fetch
    (xem SocialLinkFetcher.fetch()) nên case này lọt qua, bị hiểu nhầm
    thành "không có link" hoặc "SPA rỗng" (nội dung challenge page cũng
    thường rất ngắn) tuỳ độ dài nội dung challenge cụ thể.

    So khớp các cụm từ đặc trưng riêng cho trang xác minh, không xuất
    hiện tình cờ trong nội dung trang công ty bình thường."""
    lowered = html.lower()
    return any(marker in lowered for marker in _CHALLENGE_PAGE_MARKERS)


def find_social_links(html: str, page_url: str) -> tuple:
    """Quét href/src trong trang tìm link Facebook/LinkedIn thật. Ưu tiên
    quét thẻ <a href> và <iframe src> trước (chính xác hơn regex trên raw
    HTML vì tránh dính link nằm trong script/comment không liên quan),
    fallback sang regex trên toàn bộ HTML nếu không thấy gì (một số site
    nhúng link social qua JS, chỉ có trong đoạn script chứ không phải thẻ
    <a>/<iframe> thật).

    Tầng 1 bổ sung so với bản gốc:
      - Quét thêm <iframe src> (không chỉ <a href>) — cần thiết để bắt
        Facebook Page Plugin, thường nhúng qua iframe chứ không phải <a>.
      - Bóc URL thật trong link/iframe dạng '/plugins/page.php?href=...'
        thay vì loại bỏ hoàn toàn (case #3).
      - Ưu tiên LinkedIn '/company/...', không lưu nhầm hồ sơ cá nhân
        '/in/...' làm linkedin_url của công ty (case #4).

    Trả (fanpage_url, linkedin_url, linkedin_personal_only) — cờ thứ 3
    True khi trang CÓ candidate LinkedIn nhưng chỉ là hồ sơ cá nhân
    (không tìm được link '/company/' nào), để nơi gọi phân biệt khỏi
    "trang thật sự không có LinkedIn" và đếm riêng, tự kiểm tra tay."""
    soup = BeautifulSoup(html, "html.parser")

    raw_urls = (
        [a["href"] for a in soup.find_all("a", href=True)]
        + [f["src"] for f in soup.find_all("iframe", src=True)]
    )

    fanpage_candidates = []
    linkedin_candidates = []

    for raw in raw_urls:
        href = urljoin(page_url, raw.strip())

        # Case #3: Facebook Page Plugin — bóc href thật trong query param
        # trước khi áp dụng check ignore path thông thường (bản thân URL
        # plugin luôn nằm trong _FACEBOOK_IGNORE_PATH_PREFIXES, nên phải
        # xử lý nhánh này TRƯỚC, không thì không bao giờ tới được).
        plugin_href = _extract_facebook_plugin_href(href)
        if plugin_href is not None:
            if not _is_ignored_facebook_path(plugin_href):
                fanpage_candidates.append(plugin_href)
            continue

        candidate = _clean_social_url(href)
        if _is_facebook_domain(candidate) and not _is_ignored_facebook_path(candidate):
            fanpage_candidates.append(candidate)
        if _is_linkedin_domain(candidate) and not _is_ignored_linkedin_path(candidate):
            linkedin_candidates.append(candidate)

    fanpage_url = fanpage_candidates[0] if fanpage_candidates else ""
    linkedin_url, linkedin_personal_only = _pick_best_linkedin(linkedin_candidates)

    # Fallback: regex trên toàn bộ HTML (bắt cả link nhúng qua JS/script).
    # Regex đã neo theo "//(www.)?facebook.com/" nên không dính lỗi
    # substring như nhánh duyệt thẻ ở trên (không cần urljoin ở đây).
    if not fanpage_url:
        m = FACEBOOK_PATTERN.search(html)
        if m:
            candidate = _clean_social_url(m.group(0))
            if not _is_ignored_facebook_path(candidate):
                fanpage_url = candidate
    if not linkedin_url and not linkedin_personal_only:
        m = LINKEDIN_PATTERN.search(html)
        if m:
            candidate = _clean_social_url(m.group(0))
            if not _is_ignored_linkedin_path(candidate):
                if urlsplit(candidate).path.lower().startswith("/company/"):
                    linkedin_url = candidate
                else:
                    linkedin_personal_only = True

    return fanpage_url, linkedin_url, linkedin_personal_only


class SocialLinkFetcher:
    """Fetch HTML từ website công ty — độc lập với TopCVAdapter vì đây là
    hàng trăm domain lạ khác nhau, không phải 1 domain TopCV duy nhất, nên
    không cần né WAF theo TLS fingerprint riêng của TopCV. Vẫn dùng
    curl_cffi cho nhất quán với phần còn lại của project và vì 1 số
    website doanh nghiệp cũng có Cloudflare/WAF cơ bản."""

    def __init__(self):
        self.session = requests.Session(impersonate="chrome124")
        self.session.headers.update(DEFAULT_HEADERS)
        self._last_request_time: Optional[float] = None

    def fetch(self, url: str) -> Optional[str]:
        self._throttle()
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT_SECONDS)
            self._last_request_time = time.monotonic()
            if resp.status_code >= 400:
                logger.warning("HTTP %d tại %s -> bỏ qua", resp.status_code, url)
                return None
            return resp.text
        except requests.exceptions.RequestException as exc:
            self._last_request_time = time.monotonic()
            logger.warning("Lỗi fetch %s: %s -> bỏ qua", url, exc)
            return None

    def _throttle(self):
        if self._last_request_time is None:
            return
        elapsed = time.monotonic() - self._last_request_time
        remaining = REQUEST_DELAY_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)


def _try_fallback_subpages(fetcher: SocialLinkFetcher, website: str):
    """Tầng 1, case #2: nhiều công ty chỉ gắn social link ở trang Liên
    hệ/Giới thiệu, không phải trang chủ. Thử tối đa _MAX_SUBPAGE_TRIES URL
    con đoán được (theo thứ tự _FALLBACK_SUBPATHS) TRƯỚC KHI kết luận
    "không có link"/"SPA rỗng" — dừng ngay khi tìm được link đầu tiên,
    KHÔNG thử hết cả danh sách nếu không cần (tránh tốn request thừa cho
    công ty thật sự không có gì).

    Trang con là challenge page (case #1) hoặc fetch lỗi -> bỏ qua, thử
    tiếp URL con kế tiếp (không tính là "đã thử hết", vẫn còn cơ hội).

    Trả (fanpage_url, linkedin_url, linkedin_personal_only, found_via) —
    found_via = "subpage" nếu tìm thấy bất kỳ tín hiệu gì (kể cả chỉ
    linkedin cá nhân) qua nhánh này, "" nếu không tìm thấy gì."""
    tries = 0
    for subpath in _FALLBACK_SUBPATHS:
        if tries >= _MAX_SUBPAGE_TRIES:
            break
        sub_url = urljoin(website, subpath)
        tries += 1  # tính NGAY khi thử, kể cả fetch lỗi/challenge page bên dưới —
                     # đúng ý định "tối đa 2 request phụ", không phụ thuộc kết quả
        html = fetcher.fetch(sub_url)
        if html is None:
            continue
        if _is_challenge_page(html):
            continue
        fanpage_url, linkedin_url, linkedin_personal_only = find_social_links(html, sub_url)
        if fanpage_url or linkedin_url or linkedin_personal_only:
            logger.info("    (tìm thấy qua URL con %s)", sub_url)
            return fanpage_url, linkedin_url, linkedin_personal_only, "subpage"
    return "", "", False, ""


def _handle_website_is_social_domain(website: str) -> tuple:
    """Tầng 1, case #5: companies.website đã CHÍNH LÀ URL Facebook/
    LinkedIn (lỡ lọt qua bước enrich trước đó, vd enrich_company_web_info.py
    trích xuất nhầm 1 fanpage thành "website chính thức"). KHÔNG crawl
    domain này như 1 website công ty thông thường — gần như chắc chắn bị
    chặn (Facebook/LinkedIn chặn scraper mạnh) hoặc parse sai be bét nếu
    cứ đối xử như HTML công ty bình thường.

    Vì bản thân URL đã trỏ đúng vào Facebook/LinkedIn, dùng LUÔN làm
    fanpage_url/linkedin_url NẾU đúng định dạng trang thật (không phải
    path bị loại kiểu /sharer, /login...; với LinkedIn còn yêu cầu đúng
    '/company/' — theo cùng nguyên tắc case #4, không suy đoán '/in/' là
    trang công ty). Không đúng định dạng -> bỏ trống cả 2, để log cảnh
    báo cho người kiểm tra tay (không tự ý coi 1 URL lạ là đúng)."""
    website_with_scheme = website if "://" in website else "https://" + website
    candidate = _clean_social_url(website_with_scheme)

    fanpage_url = ""
    linkedin_url = ""
    if _is_facebook_domain(candidate) and not _is_ignored_facebook_path(candidate):
        fanpage_url = candidate
    elif _is_linkedin_domain(candidate) and not _is_ignored_linkedin_path(candidate):
        if urlsplit(candidate).path.lower().startswith("/company/"):
            linkedin_url = candidate

    return fanpage_url, linkedin_url


def run(limit: Optional[int] = None) -> dict:
    stats = {
        "checked": 0, "updated": 0, "no_link_found": 0,
        "likely_js_rendered": 0, "fetch_failed": 0,
        # Tầng 1 — thống kê mới, xem docstring đầu file mục "TẦNG 1".
        "challenge_page": 0, "found_via_subpage": 0,
        "linkedin_personal_only_skipped": 0, "website_is_social_domain": 0,
    }

    conn = db.get_connection()
    fetcher = SocialLinkFetcher()
    try:
        companies = db.get_companies_needing_social_links(conn)
        if limit:
            companies = companies[:limit]

        logger.info("Tìm thấy %d công ty cần enrich fanpage/linkedin", len(companies))

        for company_id, company_name, website in companies:
            stats["checked"] += 1
            logger.info("[%d/%d] %s -> %s", stats["checked"], len(companies), company_name, website)

            # Case #5: website vốn đã là domain Facebook/LinkedIn -> xử lý
            # riêng, KHÔNG crawl như 1 website công ty thông thường.
            website_with_scheme = website if "://" in website else "https://" + website
            if _is_facebook_domain(website_with_scheme) or _is_linkedin_domain(website_with_scheme):
                stats["website_is_social_domain"] += 1
                fanpage_url, linkedin_url = _handle_website_is_social_domain(website)
                if fanpage_url or linkedin_url:
                    db.update_company_social_links(
                        conn, company_id, fanpage_url=fanpage_url, linkedin_url=linkedin_url
                    )
                    conn.commit()
                    stats["updated"] += 1
                    logger.info(
                        "  -> website vốn là domain social, dùng trực tiếp: fanpage=%s | linkedin=%s",
                        fanpage_url or "(không hợp lệ)", linkedin_url or "(không hợp lệ)",
                    )
                else:
                    logger.warning(
                        "  -> website là domain social nhưng không đúng định dạng trang công ty "
                        "-> bỏ trống, cần kiểm tra tay: %s", website,
                    )
                continue

            html = fetcher.fetch(website)
            if html is None:
                stats["fetch_failed"] += 1
                continue

            # Case #1: challenge/anti-bot page -> KHÔNG kết luận gì, khác
            # hẳn "không có link" hay "SPA rỗng".
            if _is_challenge_page(html):
                stats["challenge_page"] += 1
                logger.info(
                    "  -> Trang trả về challenge/anti-bot page (vd Cloudflare xác minh "
                    "trình duyệt), không phải nội dung thật -> bỏ qua, không kết luận."
                )
                continue

            fanpage_url, linkedin_url, linkedin_personal_only = find_social_links(html, website)
            found_via = "homepage"

            if not fanpage_url and not linkedin_url and not linkedin_personal_only:
                soup = BeautifulSoup(html, "html.parser")
                is_spa = _is_likely_js_rendered_shell(soup)

                # Case #2: thử thêm URL con trước khi kết luận hẳn.
                fanpage_url, linkedin_url, linkedin_personal_only, found_via = (
                    _try_fallback_subpages(fetcher, website)
                )

                if not fanpage_url and not linkedin_url and not linkedin_personal_only:
                    if is_spa:
                        stats["likely_js_rendered"] += 1
                        logger.info(
                            "  -> HTML thô gần như rỗng (khả năng site SPA/React/Next.js "
                            "render bằng JS), đã thử cả URL con -> KHÔNG kết luận là 'không "
                            "có link', bỏ trống và đánh dấu cần công cụ khác (headless "
                            "browser) mới đọc được."
                        )
                    else:
                        stats["no_link_found"] += 1
                        logger.info(
                            "  -> Không tìm thấy link social nào (đã thử cả trang chủ + "
                            "URL con), bỏ trống."
                        )
                    continue

            if found_via == "subpage":
                stats["found_via_subpage"] += 1

            # Case #4: chỉ có LinkedIn cá nhân -> không lưu, đếm riêng.
            if linkedin_personal_only and not linkedin_url:
                stats["linkedin_personal_only_skipped"] += 1
                logger.info(
                    "  -> Chỉ tìm thấy LinkedIn hồ sơ cá nhân (vd CEO), không phải trang "
                    "công ty '/company/...' -> bỏ trống linkedin_url, tránh lưu nhầm."
                )

            if not fanpage_url and not linkedin_url:
                # Chỉ có tín hiệu linkedin cá nhân (đã log/đếm ở trên) —
                # không có gì để ghi vào DB.
                continue

            db.update_company_social_links(
                conn, company_id, fanpage_url=fanpage_url, linkedin_url=linkedin_url
            )
            conn.commit()
            stats["updated"] += 1
            logger.info(
                "  -> fanpage=%s | linkedin=%s",
                fanpage_url or "(không thấy)",
                linkedin_url or "(không thấy)",
            )
    finally:
        conn.close()

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Điền companies.fanpage_url / linkedin_url bằng cách crawl website công ty"
    )
    parser.add_argument("--limit", type=int, default=None,
                         help="Giới hạn số công ty xử lý (dùng để test thử trước khi chạy full)")
    args = parser.parse_args()

    stats = run(limit=args.limit)

    print("\n===== KẾT QUẢ =====")
    print(f"Đã kiểm tra              : {stats['checked']}")
    print(f"Đã cập nhật              : {stats['updated']}")
    print(f"Không tìm thấy link      : {stats['no_link_found']}")
    print(f"Tìm thấy qua URL con     : {stats['found_via_subpage']}")
    print(f"Fetch website lỗi        : {stats['fetch_failed']}")
    print(f"Challenge/anti-bot page  : {stats['challenge_page']}")
    print(f"Website vốn là domain social (fb/linkedin) : {stats['website_is_social_domain']}")
    print(f"LinkedIn chỉ có hồ sơ cá nhân (đã bỏ qua)   : {stats['linkedin_personal_only_skipped']}")
    print(f"🕸️  Khả năng site SPA/JS-rendered (chưa đọc được, cần công cụ khác) : "
          f"{stats['likely_js_rendered']}")


if __name__ == "__main__":
    main()
