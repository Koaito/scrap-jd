"""
Script RIÊNG (không nằm trong pipeline crawl chính) — vá companies.industry
cho công ty ĐÃ CÓ website nhưng còn thiếu industry, bằng cách đọc thẳng
trang chủ (+ trang giới thiệu nếu cần) của chính website đó rồi nhờ
Gemini phân loại ngành nghề.

THÊM 08/2026: cùng lần gọi Gemini đó, vá LUÔN products_services (sản
phẩm/dịch vụ chính) nếu model tự tin — "nhặt kèm" giống đúng tinh thần
backfill_company_profiles.py (không quét riêng DB cho field này, chỉ vá
khi đằng nào cũng đang xử lý company đó cho industry). LƯU Ý: điều kiện
chọn company để chạy (get_companies_needing_profile_from_website(),
xem db.py) VẪN CHỈ xét industry rỗng — company đã có industry nhưng
thiếu products_services sẽ KHÔNG được chọn lại qua script này, giống hệt
giới hạn đã biết của backfill_company_profiles.py với field này.

TẠI SAO CẦN SCRIPT NÀY (khác 2 script chị em industry-liên-quan đã có):
  - backfill_company_profiles.py đọc lại source_profile_url (TopCV/
    VietnamWorks/CareerViet) — nhưng CareerVietAdapter CỐ Ý không lấy
    industry (trang công ty CareerViet không hiển thị field này, xem
    adapters/careerviet.py) -> company nguồn CareerViet KHÔNG BAO GIỜ
    được vá industry qua backfill_company_profiles.py, dù company_size/
    address/website vẫn vá bình thường.
  - enrich_company_web_info.py (Tavily+Gemini) chỉ vá website/tax_id,
    KHÔNG đụng tới industry.
  -> industry của công ty nguồn CareerViet là khoảng trống thật sự,
     không script nào trong 2 script trên lấp được.

CÁCH RẺ HƠN Tavily: KHÔNG search web — công ty đã có sẵn companies.website
(tự điền, hoặc vừa được backfill_company_profiles.py vá) rồi, chỉ cần đọc
THẲNG trang đó là đủ để suy ra ngành nghề (About/Giới thiệu thường tự mô
tả rõ công ty làm gì) — không tốn Tavily credit, chỉ tốn 1 lần gọi Gemini/
công ty (đọc text, không grounding), rẻ hơn hẳn enrich_company_web_info.py.

INDUSTRY LÀ FREE TEXT, KHÔNG CÓ ENUM CỐ ĐỊNH: companies.industry hiện tại
lấy trực tiếp từ label "Lĩnh vực" của TopCV/VietnamWorks (xem
adapters/topcv.py, adapters/vietnamworks.py) — không có danh sách ngành cố
định nào trong hệ thống để đối chiếu. Prompt Gemini vì vậy chỉ yêu cầu trả
NHÃN NGẮN GỌN tiếng Việt kiểu cùng phong cách với dữ liệu đã crawl được
(vd "Phần Mềm CNTT/Dịch vụ Phần mềm", "Bảo hiểm", "Bán lẻ", "Ngân hàng"),
KHÔNG có validator đối chiếu enum như website/tax_id ở enrich_company_
web_info.py — CHỈ dựa vào "confidence" Gemini tự báo (high/medium/low),
CHỈ chấp nhận high/medium, đúng nguyên tắc "thà thiếu còn hơn sai" xuyên
suốt project. Đánh đổi: không có validator cứng như tax_id (regex 10 số)
hay website (domain loại trừ) vì industry vốn dĩ không có "định dạng đúng"
rõ ràng để kiểm — rủi ro sai cao hơn 1 chút so với 2 field kia, chấp nhận
được vì đây là field mô tả, không phải định danh.

FETCH HTML: dùng LẠI đúng pattern (curl_cffi impersonate + throttle +
retry 429/403, thử thêm URL con nếu trang chủ không đủ nội dung) như
get_company_fb_linkedin_link.py — nhưng KHÔNG import chéo từ file đó
(mỗi script tự chứa, đúng cấu trúc hiện tại), viết lại phần tối thiểu
cần cho MỤC ĐÍCH KHÁC (lấy text mô tả, không phải tìm link social).

Cách chạy:
    python enrich_company_profile_from_website.py
    python enrich_company_profile_from_website.py --limit 50   # test thử ít công ty trước
"""

import argparse
import json
import logging
import re
import time
from typing import Optional
from urllib.parse import urljoin

from curl_cffi import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import errors as genai_errors

import db
from config import DEFAULT_HEADERS, GEMINI_API_KEY, GEMINI_MODEL, ENRICH_REQUEST_DELAY_SECONDS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 10  # ngắn, giống get_company_fb_linkedin_link.py — hàng trăm
                               # domain lạ, không đáng chờ lâu cho 1 field không bắt buộc.
REQUEST_DELAY_SECONDS = 2.0

# Trang chủ đôi khi chỉ có banner/menu, không đủ mô tả ngành nghề — thử
# thêm URL con TRƯỚC KHI kết luận không đủ dữ liệu, cùng danh sách/giới
# hạn số lần thử như get_company_fb_linkedin_link.py để nhất quán.
_FALLBACK_SUBPATHS = ("/gioi-thieu", "/about", "/about-us", "/ve-chung-toi")
_MAX_SUBPAGE_TRIES = 2

# Cùng danh sách marker challenge page (Cloudflare...) như get_company_
# fb_linkedin_link.py — trang xác minh trình duyệt không phải nội dung
# thật, không nên đưa cho Gemini đọc (sẽ chỉ tốn quota mà không ra gì).
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

# Giới hạn độ dài text đưa vào prompt Gemini — trang About dài (vd lịch sử
# công ty hàng nghìn chữ) không cần đọc hết để suy ra ngành nghề, chỉ tốn
# token vô ích. 3000 ký tự đủ cho hầu hết trang giới thiệu.
_MAX_TEXT_CHARS = 3000

# THÊM 08/2026: cùng 1 lần đọc trang/gọi Gemini này, nhờ luôn model tóm
# tắt SẢN PHẨM/DỊCH VỤ CHÍNH (products_services) — không tốn thêm request
# nào vì nội dung trang đọc vào prompt vốn đã có sẵn cho industry. ĐÁNH
# ĐỔI đã bàn: JSON có 2 field thay vì 1 -> để tránh rủi ro "1 field lỗi
# kéo hỏng field kia" (coupling), industry và products_services có
# confidence RIÊNG BIỆT, được chấp nhận/từ chối ĐỘC LẬP với nhau ở phần
# xử lý response bên dưới (xem run()) — industry đúng vẫn được lưu dù
# products_services confidence thấp, và ngược lại. Chỉ có 1 rủi ro KHÔNG
# thể tách được: nếu response hỏng ở mức cú pháp JSON (parse thất bại
# hoàn toàn, vd bị cắt giữa chừng) thì mất cả 2 — không có cách nào cứu
# 1 phần của JSON không hợp lệ, chấp nhận như giới hạn cố hữu.
_PROMPT_TEMPLATE = """Bạn đang đọc nội dung trang web của 1 công ty tại Việt Nam để xác định (1) NGÀNH NGHỀ/LĨNH VỰC HOẠT ĐỘNG chính và (2) SẢN PHẨM/DỊCH VỤ CHÍNH của công ty đó.

Tên công ty: {company_name}

Nội dung trang web (trích từ trang chủ hoặc trang giới thiệu):
---
{page_text}
---

Trả lời DUY NHẤT 1 object JSON (không markdown, không giải thích thêm), đúng định dạng:
{{"industry": "<nhãn ngành nghề ngắn gọn tiếng Việt, vd 'Phần Mềm CNTT/Dịch vụ Phần mềm', 'Bán lẻ', 'Ngân hàng', 'Bất động sản', 'Sản xuất - Dược phẩm'>", "industry_confidence": "<high|medium|low>", "products_services": "<liệt kê ngắn gọn sản phẩm/dịch vụ chính, dưới 20 từ, vd 'Bảo hiểm phi nhân thọ: xe cơ giới, tài sản, sức khỏe'>", "products_services_confidence": "<high|medium|low>", "note": "<lý do ngắn, 1 câu>"}}

Quy tắc:
- 2 field industry và products_services ĐỘC LẬP nhau — có thể chắc chắn về 1 field nhưng không chắc field còn lại, vẫn điền confidence ĐÚNG cho từng field, không cần khớp nhau.
- Nếu nội dung KHÔNG đủ rõ để xác định 1 trong 2 -> field đó = "" và confidence riêng của field đó = "low". KHÔNG vì 1 field chắc chắn mà tự nâng confidence cho field còn lại.
- KHÔNG suy đoán/bịa nếu không chắc chắn — thà để confidence thấp còn hơn đoán sai.
- industry là 1 nhãn ngắn (dưới 6 từ). products_services là liệt kê ngắn gọn (dưới 20 từ), KHÔNG phải đoạn mô tả dài về lịch sử/sứ mệnh/văn hoá công ty.
"""


def _is_challenge_page(html: str) -> bool:
    lowered = html.lower()
    return any(marker in lowered for marker in _CHALLENGE_PAGE_MARKERS)


def _extract_visible_text(html: str) -> str:
    """Lấy text hiển thị trong <body>, bỏ script/style/nav lặt vặt — đủ
    dùng để Gemini đọc hiểu nội dung, không cần giữ cấu trúc HTML."""
    soup = BeautifulSoup(html, "html.parser")
    body = soup.find("body")
    if body is None:
        return ""
    for tag in body.find_all(["script", "style", "noscript"]):
        tag.decompose()
    text = body.get_text(" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:_MAX_TEXT_CHARS]


class _PageFetcher:
    """Fetch HTML từ website công ty — cùng pattern throttle/retry như
    get_company_fb_linkedin_link.py.SocialLinkFetcher, viết lại riêng ở
    đây để giữ script này tự chứa (xem docstring đầu file)."""

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


def _get_page_text(fetcher: _PageFetcher, website: str) -> str:
    """Lấy text trang chủ; nếu quá ngắn (<200 ký tự, cùng ngưỡng heuristic
    SPA-rỗng như get_company_fb_linkedin_link.py) thử thêm tối đa
    _MAX_SUBPAGE_TRIES URL con trước khi bỏ cuộc."""
    html = fetcher.fetch(website)
    if html and not _is_challenge_page(html):
        text = _extract_visible_text(html)
        if len(text) >= 200:
            return text

    tries = 0
    for subpath in _FALLBACK_SUBPATHS:
        if tries >= _MAX_SUBPAGE_TRIES:
            break
        tries += 1
        sub_html = fetcher.fetch(urljoin(website, subpath))
        if sub_html is None or _is_challenge_page(sub_html):
            continue
        text = _extract_visible_text(sub_html)
        if len(text) >= 200:
            return text

    return ""


def _parse_gemini_json(text: str) -> Optional[dict]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Gemini trả về không phải JSON hợp lệ: %r", text[:200])
        return None


_GEMINI_MAX_RETRIES = 2


def _call_gemini_with_retry(gemini_client, prompt: str, company_name: str):
    """Giống hệt pattern _call_gemini_with_retry() trong
    enrich_company_web_info.py — tự thử lại khi Gemini trả 429 thoáng qua."""
    for attempt in range(_GEMINI_MAX_RETRIES + 1):
        try:
            return gemini_client.models.generate_content(
                model=GEMINI_MODEL, contents=prompt
            )
        except genai_errors.ClientError as exc:
            is_rate_limit = getattr(exc, "code", None) == 429
            if not is_rate_limit or attempt == _GEMINI_MAX_RETRIES:
                logger.warning("Gemini lỗi cho '%s': %s", company_name, exc)
                return None
            wait_seconds = ENRICH_REQUEST_DELAY_SECONDS * (attempt + 1)
            logger.warning(
                "'%s': Gemini 429 (rate limit), thử lại lần %d/%d sau %.1fs...",
                company_name, attempt + 1, _GEMINI_MAX_RETRIES, wait_seconds,
            )
            time.sleep(wait_seconds)
    return None


def _throttle_gemini(last_request_time):
    if last_request_time is not None:
        elapsed = time.monotonic() - last_request_time
        remaining = ENRICH_REQUEST_DELAY_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)
    return time.monotonic()


def run(limit: Optional[int] = None) -> dict:
    stats = {
        "checked": 0, "updated": 0, "no_page_content": 0,
        "low_confidence": 0, "fetch_failed": 0, "errors": 0,
        # Tách riêng để biết mỗi field vá được bao nhiêu — 1 company có
        # thể chỉ vá được 1 trong 2 field (xem logic industry_ok/products_ok).
        "industry_updated": 0, "products_services_updated": 0,
    }

    if not GEMINI_API_KEY:
        print("❌ Thiếu GEMINI_API_KEY trong .env — xem .env.example.")
        return stats

    gemini_client = genai.Client(api_key=GEMINI_API_KEY)
    fetcher = _PageFetcher()

    conn = db.get_connection()
    last_gemini_call = None
    try:
        companies = db.get_companies_needing_profile_from_website(conn)
        if limit:
            companies = companies[:limit]

        logger.info("Tìm thấy %d công ty cần bù industry từ website", len(companies))

        for company_id, company_name, website in companies:
            stats["checked"] += 1
            logger.info("[%d/%d] %s -> %s", stats["checked"], len(companies), company_name, website)

            page_text = _get_page_text(fetcher, website)
            if not page_text:
                stats["no_page_content"] += 1
                logger.info("  -> Không đọc được nội dung đủ dùng (trang trống/SPA/chặn) -> bỏ qua.")
                continue

            last_gemini_call = _throttle_gemini(last_gemini_call)
            prompt = _PROMPT_TEMPLATE.format(company_name=company_name, page_text=page_text)
            try:
                gemini_resp = _call_gemini_with_retry(gemini_client, prompt, company_name)
            except Exception as exc:  # noqa: BLE001 - không để 1 công ty lỗi dừng cả batch
                stats["errors"] += 1
                logger.error("Lỗi gọi Gemini cho '%s': %s", company_name, exc)
                continue

            if gemini_resp is None:
                stats["errors"] += 1
                continue

            parsed = _parse_gemini_json(gemini_resp.text or "")
            if parsed is None:
                # Chỉ mất cả 2 field ở đây khi JSON hỏng cú pháp hoàn
                # toàn (không parse được) — không có cách tách phần nào
                # dùng được từ 1 JSON không hợp lệ, chấp nhận giới hạn
                # này (xem comment ở _PROMPT_TEMPLATE).
                stats["errors"] += 1
                continue

            note = parsed.get("note", "")

            # industry và products_services được đánh giá ĐỘC LẬP —
            # confidence thấp/rỗng ở field này KHÔNG loại field kia. Đây
            # chính là phần sửa "hướng 1" (tolerant theo field, thay vì
            # all-or-nothing như trước khi thêm products_services).
            industry = (parsed.get("industry") or "").strip()
            industry_confidence = (parsed.get("industry_confidence") or "").strip().lower()
            industry_ok = bool(industry) and industry_confidence in ("high", "medium")

            products_services = (parsed.get("products_services") or "").strip()
            products_confidence = (parsed.get("products_services_confidence") or "").strip().lower()
            products_ok = bool(products_services) and products_confidence in ("high", "medium")

            if not industry_ok and not products_ok:
                stats["low_confidence"] += 1
                logger.info(
                    "  -> Cả 2 field đều confidence thấp/rỗng "
                    "(industry=%r/%s, products_services=%r/%s) -> bỏ qua. Ghi chú: %s",
                    industry, industry_confidence, products_services, products_confidence, note,
                )
                continue

            update_kwargs = {}
            if industry_ok:
                update_kwargs["industry"] = industry
            if products_ok:
                update_kwargs["products_services"] = products_services

            try:
                db.update_company_profile(conn, company_id, **update_kwargs)
                conn.commit()
            except Exception as exc:  # noqa: BLE001 - lỗi DB, không để dừng cả batch
                conn.rollback()
                stats["errors"] += 1
                logger.error("Lỗi ghi DB cho '%s': %s", company_name, exc)
                continue

            stats["updated"] += 1
            if industry_ok:
                stats["industry_updated"] += 1
            if products_ok:
                stats["products_services_updated"] += 1
            logger.info(
                "  -> Đã vá industry=%s | products_services=%s "
                "(industry_conf=%s, products_conf=%s)",
                industry if industry_ok else "(bỏ qua)",
                products_services if products_ok else "(bỏ qua)",
                industry_confidence, products_confidence,
            )
    finally:
        conn.close()

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Vá companies.industry cho công ty đã có website nhưng còn thiếu, "
                    "bằng cách đọc website + Gemini phân loại (không dùng Tavily)."
    )
    parser.add_argument("--limit", type=int, default=None,
                         help="Giới hạn số công ty xử lý (dùng để test thử trước khi chạy full)")
    args = parser.parse_args()

    stats = run(limit=args.limit)

    print("\n===== KẾT QUẢ =====")
    print(f"Đã kiểm tra                    : {stats['checked']}")
    print(f"Đã vá ít nhất 1 field          : {stats['updated']}")
    print(f"  trong đó vá industry         : {stats['industry_updated']}")
    print(f"  trong đó vá products_services: {stats['products_services_updated']}")
    print(f"Trang không đủ nội dung đọc    : {stats['no_page_content']}")
    print(f"Cả 2 field confidence thấp     : {stats['low_confidence']}")
    print(f"Lỗi                            : {stats['errors']}")


if __name__ == "__main__":
    main()
