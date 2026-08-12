"""
Script RIÊNG (không nằm trong pipeline crawl chính) — vá thêm
companies.website / companies.tax_id cho công ty chưa có, bằng cách:
  1. Tavily search API — 2 LẦN GỌI RIÊNG BIỆT cho mỗi công ty (xem phần
     "TÁCH 2 QUERY TAVILY" bên dưới để biết lý do).
  2. Gemini (bản text thường, KHÔNG bật grounding) — đọc CẢ 2 bộ kết quả
     Tavily trong 1 lần gọi duy nhất, trích xuất website/tax_id ra JSON
     có cấu trúc, với confidence TÁCH RIÊNG cho từng field.

TẠI SAO TÁCH SCRIPT RIÊNG (giống nguyên tắc get_company_fb_linkedin_link.py):
Đây là nguồn dữ liệu KHÁC HẲN TopCV/VietnamWorks — tỷ lệ lỗi/nhầm lẫn cao
hơn nhiều (search engine + LLM trích xuất, không phải parse HTML có cấu
trúc cố định từ 1 nguồn tin cậy), không nên làm chậm/rủi ro luồng crawl
job chính. Chạy độc lập, khi nào cần vá thì chạy lại.

TÁCH 2 QUERY TAVILY (08/2026, sau khi soát log thật — 9/10 công ty ra
được tax_id nhưng chỉ 1/10 ra được website):
Nguyên nhân gốc: query gộp chung "{tên} website chính thức mã số thuế
doanh nghiệp" khiến top kết quả Tavily nghiêng hẳn về các trang TRA CỨU
MST (masothue.com, thongtindoanhnghiep.co...) — tốt cho tax_id (các
trang này liệt kê MST rõ ràng) nhưng DỞ cho website (các trang này nằm
trong _NON_COMPANY_WEBSITE_DOMAINS, bị loại thẳng, và thường KHÔNG in
website thật của công ty). Website thật (ít backlink hơn site tổng hợp)
thường bị đẩy ra ngoài top 5, nên Gemini không có gì để trích xuất.

Giải pháp: gọi Tavily 2 LẦN với 2 QUERY RIÊNG, mỗi query tối ưu cho đúng
1 mục đích:
  - Query 1 ("{tên} website chính thức"): nghiêng về trang chủ công ty.
  - Query 2 ("{tên} mã số thuế doanh nghiệp"): nghiêng về trang tra MST.
Rồi GỘP cả 2 bộ kết quả (có đánh nhãn rõ nguồn gốc từng bộ) vào ĐÚNG 1
prompt Gemini duy nhất — vẫn chỉ 1 lần gọi Gemini/công ty (không tốn
thêm quota RPM Gemini, vốn đang giới hạn 15/phút), chỉ Tavily tốn gấp
đôi credit/công ty (xem "CHI PHÍ TAVILY TĂNG GẤP ĐÔI" bên dưới).

CONFIDENCE TÁCH RIÊNG CHO WEBSITE/TAX_ID (bù trừ cho thay đổi trên):
TRƯỚC ĐÂY dùng 1 "confidence" chung cho cả 2 field — nếu Gemini không
chắc chắn về 1 field (thường là website, vì các trang tra MST hiếm khi
nói rõ website), confidence chung bị kéo xuống "low", loại luôn CẢ tax_id
dù bản thân tax_id có nguồn rõ ràng, đáng tin cậy riêng. Giờ tách thành
"website_confidence" / "tax_id_confidence" độc lập -> field nào có nguồn
tốt được giữ lại dù field kia không đủ tin cậy.

CHI PHÍ TAVILY TĂNG GẤP ĐÔI: mỗi công ty giờ tốn 2 credit Tavily thay vì
1 -> free tier 1.000 credit/tháng chỉ còn enrich được ~500 công ty/tháng
thay vì ~1.000 (xem comment TAVILY_API_KEY trong config.py). Nếu 1 trong
2 lần search lỗi/rỗng, script VẪN tiếp tục với bộ kết quả còn lại (không
bỏ hẳn công ty đó) — tránh lãng phí credit của lần search đã thành công.

NGUYÊN TẮC "THÀ THIẾU CÒN HƠN SAI" (xuyên suốt project, áp dụng NGHIÊM
NGẶT hơn ở đây vì độ tin cậy nguồn thấp hơn hẳn):
  - Chỉ chấp nhận field nào có confidence riêng "high" hoặc "medium"
    (website_confidence cho website, tax_id_confidence cho tax_id).
  - tax_id phải khớp ĐÚNG định dạng mã số doanh nghiệp Việt Nam (10 chữ
    số, hoặc 10 chữ số + "-" + 3 chữ số mã chi nhánh) — sai định dạng dù
    confidence cao vẫn bỏ, không lưu bừa.
  - website phải là URL http(s) hợp lệ VÀ KHÔNG thuộc danh sách domain
    chắc chắn không phải website chính thức của công ty (mạng xã hội,
    trang tuyển dụng, trang tra cứu MST, Wikipedia...) — cùng nguyên tắc
    _NON_COMPANY_WEBSITE_DOMAINS đã áp dụng trong adapters/topcv.py.
  - Không tìm được / không đủ tin cậy -> để trống, KHÔNG đoán mò.

CẢNH BÁO "NHẦM PHÁP NHÂN CHỊ EM CÙNG THƯƠNG HIỆU" (thêm 08/2026, sau khi
phát hiện case thật: Gemini trả website của "AEON Việt Nam" — chuỗi siêu
thị — cho công ty "AEONMALL Việt Nam" — vận hành trung tâm thương mại,
2 pháp nhân khác nhau cùng tập đoàn mẹ Nhật Bản, chỉ khác nhau ở hậu tố
"MALL"). Lỗi kiểu này ĐI QUA ĐƯỢC hết validator ở trên (domain hợp lệ,
không thuộc danh sách loại trừ, confidence Gemini báo "high") vì bản
chất không phải lỗi format — mà là Gemini gộp nhầm 2 pháp nhân cùng
thương hiệu khi đọc kết quả search.

_website_matches_company_name() so token (đã bỏ dấu, bỏ từ đệm loại
hình DN) giữa tên công ty và domain — nếu KHÔNG có token nào khớp
NGUYÊN VẸN (so "aeon" với "aeonmall" là KHÔNG khớp, dù 1 chuỗi chứa
chuỗi kia), in WARNING riêng để tự kiểm tra tay. CHỦ ĐÍCH KHÔNG tự động
xoá field khi nghi ngờ — heuristic này có false positive thật (vd công
ty tên tiếng Việt dài dùng domain viết tắt tiếng Anh không liên quan về
mặt chữ, "ABC Logistics Việt Nam" -> "abclog.vn" sẽ bị cảnh báo oan dù
đúng) — chỉ đủ tin cậy để LÀM DẤU, không đủ tin cậy để tự ý xoá dữ liệu
có thể đang đúng, đúng tinh thần "thà thiếu còn hơn sai" nhưng không
đánh đổi bằng việc xoá nhầm dữ liệu tốt.

Cách chạy:
    python enrich_company_web_info.py
    python enrich_company_web_info.py --limit 50   # test thử ít công ty trước
"""

import argparse
import json
import logging
import re
import time
from typing import Optional

from tavily import TavilyClient
from google import genai
from google.genai import errors as genai_errors

import db
from config import TAVILY_API_KEY, GEMINI_API_KEY, GEMINI_MODEL, ENRICH_REQUEST_DELAY_SECONDS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Mã số doanh nghiệp VN: 10 chữ số (công ty mẹ), hoặc 10 chữ số + "-" +
# 3 chữ số (đơn vị trực thuộc/chi nhánh) — theo Nghị định 01/2021/NĐ-CP.
_TAX_ID_PATTERN = re.compile(r"^\d{10}(-\d{3})?$")

# Domain CHẮC CHẮN không phải website chính thức của 1 công ty — cùng
# danh sách nguyên tắc với adapters/topcv.py (_NON_COMPANY_WEBSITE_DOMAINS),
# bổ sung thêm các trang đặc thù cho ngữ cảnh search/tra cứu doanh nghiệp
# (trang tra MST, Wikipedia, các job site khác ngoài TopCV).
_NON_COMPANY_WEBSITE_DOMAINS = (
    "linkedin.com", "threads.com", "facebook.com", "tiktok.com",
    "youtube.com", "twitter.com", "x.com", "instagram.com", "zalo.me",
    "itunes.apple.com", "apps.apple.com", "play.google.com",
    "topcv.vn", "vietnamworks.com", "itviec.com", "glints.com",
    "careerbuilder.vn", "careerlink.vn", "timviecnhanh.com",
    "wikipedia.org", "masothue.com", "masothue.vn",
    "thongtindoanhnghiep.co", "dangkykinhdoanh.gov.vn",
    "google.com", "bing.com",
)

_PROMPT_TEMPLATE = """\
Dưới đây là kết quả tìm kiếm web cho công ty "{company_name}" tại Việt Nam,
gồm 2 nhóm kết quả từ 2 truy vấn tìm kiếm khác nhau (nhóm 1 nghiêng về tìm
website chính thức, nhóm 2 nghiêng về tìm mã số thuế) — hãy dùng CẢ 2 nhóm
làm ngữ cảnh chung, không cần tách biệt khi trả lời.

{results_text}

Dựa CHỈ vào các kết quả trên (không dùng kiến thức khác của bạn), xác định:
1. "website": website CHÍNH THỨC của ĐÚNG pháp nhân "{company_name}" này
   (KHÔNG PHẢI trang mạng xã hội, KHÔNG PHẢI trang tuyển dụng như
   TopCV/VietnamWorks/LinkedIn, KHÔNG PHẢI trang tổng hợp/review/tra cứu
   MST). CHÚ Ý: nhiều tập đoàn có NHIỀU pháp nhân con/chị em dùng chung
   thương hiệu nhưng là công ty KHÁC NHAU về mặt pháp lý (vd "AEON Việt
   Nam" - chuỗi siêu thị - khác "AEONMALL Việt Nam" - vận hành trung tâm
   thương mại; công ty mẹ tập đoàn khác công ty con phụ trách 1 mảng cụ
   thể) — chỉ chọn website nếu chắc chắn đúng TÊN PHÁP NHÂN, không chỉ
   đúng thương hiệu/tập đoàn. Nếu không có kết quả nào đủ rõ ràng, để
   chuỗi rỗng "".
2. "website_confidence": "high" nếu có nguồn rõ ràng ghi ĐÚNG tên công ty
   này gắn với website đó; "medium" nếu khá chắc nhưng không 100% chắc
   chắn tên khớp; "low" nếu chỉ đoán/suy luận. Đánh giá ĐỘC LẬP với
   tax_id_confidence bên dưới — 2 field này KHÔNG phụ thuộc lẫn nhau,
   dù không có website vẫn có thể tự tin cao về tax_id và ngược lại.
3. "tax_id": mã số thuế / mã số doanh nghiệp (định dạng 10 chữ số, có
   thể kèm "-" và 3 chữ số) nếu có xuất hiện RÕ RÀNG trong kết quả. Nếu
   không thấy, để chuỗi rỗng "".
4. "tax_id_confidence": "high"/"medium"/"low" theo cùng tiêu chí như
   website_confidence nhưng CHỈ đánh giá riêng cho tax_id, không liên
   quan gì tới độ tin cậy của website.
5. "note": lý do ngắn gọn (1 câu) cho các lựa chọn trên.

QUAN TRỌNG: nếu không đủ căn cứ để chắc chắn, hãy để trống field đó thay
vì đoán — để trống còn tốt hơn thông tin sai. Field này thiếu không có
nghĩa là field kia cũng phải yếu theo.

Trả lời DUY NHẤT bằng JSON hợp lệ, không thêm chữ nào khác, không dùng
markdown code fence, đúng đúng format:
{{"website": "...", "website_confidence": "...", "tax_id": "...", "tax_id_confidence": "...", "note": "..."}}
"""


def _throttle(last_time: Optional[float]) -> float:
    if last_time is not None:
        elapsed = time.monotonic() - last_time
        remaining = ENRICH_REQUEST_DELAY_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)
    return time.monotonic()


# Số lần thử lại tối đa khi Gemini trả 429 (RESOURCE_EXHAUSTED) — CHỈ áp
# dụng cho 429 thoáng qua (vd 2 request rơi đúng vào cùng 1 giây do dao
# động thời gian xử lý), KHÔNG phải để né quota một cách hệ thống — nếu
# vẫn 429 sau từng đó lần thử, tức là ENRICH_REQUEST_DELAY_SECONDS đang
# đặt sai (quá sát ngưỡng RPM thật), nên bỏ qua công ty này và log rõ,
# không thử vô hạn.
_GEMINI_MAX_RETRIES = 2


def _call_gemini_with_retry(gemini_client, prompt: str, company_name: str):
    """Gọi Gemini generate_content, tự thử lại khi gặp 429 thoáng qua.
    Đọc 'retryDelay' Google trả về trong lỗi nếu có (đáng tin hơn đoán
    mò), fallback về ENRICH_REQUEST_DELAY_SECONDS * 2 nếu không đọc
    được. Trả None nếu hết lượt thử mà vẫn lỗi (bao gồm lỗi khác 429)."""
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

            wait_seconds = _extract_retry_delay_seconds(exc) or (ENRICH_REQUEST_DELAY_SECONDS * 2)
            logger.info(
                "'%s': Gemini 429 (rate limit), thử lại lần %d/%d sau %.1fs...",
                company_name, attempt + 1, _GEMINI_MAX_RETRIES, wait_seconds,
            )
            time.sleep(wait_seconds)
        except Exception as exc:  # noqa: BLE001 - lỗi khác (network...), không retry
            logger.warning("Gemini lỗi cho '%s': %s", company_name, exc)
            return None
    return None


def _extract_retry_delay_seconds(exc: "genai_errors.ClientError") -> Optional[float]:
    """Đọc field 'retryDelay' (vd '10s') Google trả về trong chi tiết
    lỗi 429 — đáng tin hơn tự đoán, vì đây là thời gian Google TÍNH TOÁN
    THẬT dựa trên quota còn lại của project, không phải giá trị cố định.
    Trả None nếu không tìm thấy/parse được (dùng fallback ở nơi gọi)."""
    try:
        details = (exc.details or {}).get("error", {}).get("details", [])
        for d in details:
            retry_delay = d.get("retryDelay")
            if retry_delay and retry_delay.endswith("s"):
                return float(retry_delay[:-1])
    except (AttributeError, ValueError, TypeError):
        pass
    return None


def _is_valid_tax_id(tax_id: str) -> bool:
    return bool(_TAX_ID_PATTERN.match((tax_id or "").strip()))


def _is_valid_company_website(url: str) -> bool:
    url = (url or "").strip()
    if not url.startswith("http"):
        return False
    from urllib.parse import urlsplit
    netloc = urlsplit(url).netloc.lower().removeprefix("www.")
    return netloc not in _NON_COMPANY_WEBSITE_DOMAINS


# ------------------------------------------------------------------
# Đối chiếu tên công ty <-> domain website — CHỈ để CẢNH BÁO (log),
# KHÔNG tự động chặn lưu. Lý do tách riêng khỏi _is_valid_company_website
# (vốn chỉ chặn domain "chắc chắn không phải website công ty"): kiểu lỗi
# này khác hẳn — domain hoàn toàn hợp lệ, chỉ là THUỘC VỀ 1 PHÁP NHÂN
# KHÁC cùng thương hiệu/tập đoàn (case thật gặp: Gemini trả website của
# "AEON Việt Nam" cho công ty "AEONMALL Việt Nam" — 2 pháp nhân khác
# nhau, cùng có chữ AEON). Không thể tự động chặn vì heuristic này có
# false positive thật (vd công ty tên tiếng Việt dài dùng domain viết
# tắt tiếng Anh, "ABC Logistics Việt Nam" -> "abclog.vn") — chỉ đủ tin
# cậy để LÀM DẤU cho người duyệt tự kiểm tra, không đủ tin cậy để tự ý
# xoá dữ liệu có thể đang đúng.
# ------------------------------------------------------------------

# Từ đệm loại hình DN + từ chung chung không mang tính nhận diện riêng
# công ty nào — loại khỏi token trước khi so khớp, tránh so khớp giả
# (vd 2 công ty khác nhau cùng là "công ty TNHH ... Việt Nam" sẽ luôn
# "khớp" nếu không loại các từ này).
_GENERIC_NAME_WORDS = {
    "cong", "ty", "tnhh", "co", "phan", "trach", "nhiem", "huu", "han",
    "chi", "nhanh", "tap", "doan", "nhom", "group", "ltd", "limited",
    "inc", "jsc", "corp", "corporation", "company", "vietnam", "viet",
    "nam", "vn", "the", "and", "of",
}


def _strip_diacritics(text: str) -> str:
    import unicodedata
    normalized = unicodedata.normalize("NFD", text)
    return "".join(c for c in normalized if unicodedata.category(c) != "Mn").replace("đ", "d").replace("Đ", "D")


def _tokenize_company_name(name: str) -> set:
    """Tách tên công ty thành tập token "có ý nghĩa nhận diện" — bỏ dấu
    tiếng Việt (để so được với domain vốn luôn là ASCII), bỏ từ đệm loại
    hình DN + từ chung chung, bỏ token quá ngắn (<3 ký tự, thường là rác
    viết tắt không mang nghĩa khi đứng riêng)."""
    ascii_name = _strip_diacritics(name).lower()
    raw_tokens = re.findall(r"[a-z0-9]+", ascii_name)
    return {t for t in raw_tokens if t not in _GENERIC_NAME_WORDS and len(t) >= 3}


def _tokenize_domain(url: str) -> set:
    """Tách phần tên miền chính (bỏ scheme, www, TLD) thành token — vd
    'https://www.aeonmall-vietnam.com' -> {'aeonmall', 'vietnam'}."""
    from urllib.parse import urlsplit
    netloc = urlsplit(url).netloc.lower().removeprefix("www.")
    # Bỏ phần TLD/subdomain quốc gia (vd '.com.vn', '.vn', '.com') —
    # chỉ giữ lại nhãn đầu tiên (second-level domain), nơi thường chứa
    # tên thương hiệu thật của công ty.
    first_label = netloc.split(".")[0] if netloc else ""
    raw_tokens = re.findall(r"[a-z0-9]+", first_label)
    return {t for t in raw_tokens if t not in _GENERIC_NAME_WORDS and len(t) >= 3}


def _website_matches_company_name(company_name: str, website: str) -> bool:
    """True nếu có ít nhất 1 token TRÙNG NGUYÊN VẸN giữa tên công ty và
    domain (không phải substring — 'aeon' và 'aeonmall' KHÔNG được coi
    là khớp, dù 'aeon' nằm trong 'aeonmall', vì đó chính xác là kiểu
    nhầm lẫn 2 pháp nhân chị em cần bắt được)."""
    company_tokens = _tokenize_company_name(company_name)
    domain_tokens = _tokenize_domain(website)
    if not company_tokens or not domain_tokens:
        return True  # không đủ dữ liệu để so -> không dám khẳng định sai, bỏ qua cảnh báo
    return bool(company_tokens & domain_tokens)


def _format_result_group(results: list, label: str, start_index: int = 1) -> tuple:
    """Format 1 nhóm kết quả Tavily thành text, đánh số bắt đầu từ
    start_index (để đánh số liên tục xuyên suốt cả 2 nhóm khi ghép lại,
    tránh Gemini nhầm '[Kết quả 1]' của nhóm 2 với '[Kết quả 1]' của
    nhóm 1). Trả về (text, số_kết_quả_đã_format)."""
    parts = []
    idx = start_index
    for r in results[:5]:
        title = (r.get("title") or "").strip()
        url = (r.get("url") or "").strip()
        content = (r.get("content") or "").strip()[:500]
        parts.append(f"[Kết quả {idx}] ({label})\nTiêu đề: {title}\nURL: {url}\nNội dung: {content}")
        idx += 1
    return "\n\n".join(parts), idx - start_index


def _build_results_text(website_results: list, tax_id_results: list) -> str:
    """Ghép gọn kết quả từ 2 lần search Tavily (query tìm website riêng +
    query tìm tax_id riêng — xem docstring đầu file, mục "TÁCH 2 QUERY
    TAVILY") thành 1 khối text đưa vào prompt Gemini, đánh nhãn rõ nguồn
    gốc từng nhóm để Gemini biết kết quả nào tối ưu cho mục đích nào.

    Dedupe theo URL giữa 2 nhóm — cùng 1 trang có thể xuất hiện ở cả 2
    lần search (vd trang chủ công ty vừa nói rõ tax_id vừa là chính trang
    chủ), không cần lặp lại 2 lần trong prompt, tốn token vô ích."""
    seen_urls = set()
    for r in website_results[:5]:
        url = (r.get("url") or "").strip()
        if url:
            seen_urls.add(url)

    deduped_tax_id = []
    for r in tax_id_results[:5]:
        url = (r.get("url") or "").strip()
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        deduped_tax_id.append(r)

    website_text, n = _format_result_group(website_results, "từ truy vấn tìm website", 1)
    tax_id_text, _ = _format_result_group(deduped_tax_id, "từ truy vấn tìm mã số thuế", n + 1)

    groups = [g for g in (website_text, tax_id_text) if g]
    return "\n\n".join(groups)


def _parse_gemini_json(text: str) -> Optional[dict]:
    """Gemini đôi khi vẫn bọc JSON trong ```json ... ``` dù đã dặn không
    làm vậy trong prompt — dọn qua trước khi parse, an toàn hơn là tin
    tuyệt đối vào việc model luôn tuân thủ đúng format yêu cầu."""
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Gemini trả về không phải JSON hợp lệ: %r", text[:200])
        return None


def _tavily_search(tavily_client: TavilyClient, query: str, company_name: str, purpose: str) -> list:
    """1 lần gọi Tavily search, trả list results (rỗng nếu lỗi/không có
    kết quả) — KHÔNG raise, để nơi gọi (enrich_one_company) tự quyết định
    có tiếp tục với bộ kết quả còn lại hay không thay vì crash cả công ty
    vì 1 trong 2 lần search lỗi (xem docstring đầu file, mục "CHI PHÍ
    TAVILY TĂNG GẤP ĐÔI" — không nên lãng phí credit của lần đã thành
    công chỉ vì lần kia lỗi)."""
    try:
        resp = tavily_client.search(query=query, max_results=5, search_depth="basic")
    except Exception as exc:  # noqa: BLE001 - lỗi mạng/API, coi như rỗng, không raise
        logger.warning("Tavily search (%s) lỗi cho '%s': %s", purpose, company_name, exc)
        return []
    return resp.get("results", []) if isinstance(resp, dict) else []


def enrich_one_company(tavily_client: TavilyClient, gemini_client, company_name: str) -> dict:
    """Trả về dict {"website": str, "tax_id": str} — CHỈ chứa field đã
    qua kiểm tra an toàn (confidence RIÊNG của field đó đủ cao + đúng
    định dạng), field nào không đạt sẽ vắng mặt trong dict trả về (không
    có key, KHÔNG phải key với giá trị rỗng) để phân biệt rõ "không tìm
    được" khỏi "tìm được nhưng rỗng".

    Gọi Tavily 2 LẦN (query riêng cho website, query riêng cho tax_id —
    xem docstring đầu file mục "TÁCH 2 QUERY TAVILY"), gộp cả 2 bộ kết
    quả vào ĐÚNG 1 lần gọi Gemini duy nhất."""
    result = {}

    website_results = _tavily_search(
        tavily_client, f"{company_name} website chính thức", company_name, "website"
    )
    tax_id_results = _tavily_search(
        tavily_client, f"{company_name} mã số thuế doanh nghiệp", company_name, "tax_id"
    )

    if not website_results and not tax_id_results:
        logger.info("Không có kết quả Tavily nào cho '%s' -> bỏ qua.", company_name)
        return result

    prompt = _PROMPT_TEMPLATE.format(
        company_name=company_name,
        results_text=_build_results_text(website_results, tax_id_results),
    )

    gemini_resp = _call_gemini_with_retry(gemini_client, prompt, company_name)
    if gemini_resp is None:
        return result

    parsed = _parse_gemini_json(gemini_resp.text or "")
    if parsed is None:
        return result

    note = parsed.get("note", "")
    website_confidence = (parsed.get("website_confidence") or "").strip().lower()
    tax_id_confidence = (parsed.get("tax_id_confidence") or "").strip().lower()

    website = (parsed.get("website") or "").strip()
    tax_id = (parsed.get("tax_id") or "").strip()

    # 2 field xét độc lập hoàn toàn — website_confidence thấp KHÔNG còn
    # kéo tax_id xuống theo (và ngược lại), đúng mục đích tách confidence.
    if website_confidence not in ("high", "medium"):
        if website:
            logger.info(
                "'%s': website=%r nhưng website_confidence=%r (thấp/không rõ) "
                "-> bỏ qua, không lưu. Ghi chú: %s",
                company_name, website, website_confidence, note,
            )
        website = ""

    if tax_id_confidence not in ("high", "medium"):
        if tax_id:
            logger.info(
                "'%s': tax_id=%r nhưng tax_id_confidence=%r (thấp/không rõ) "
                "-> bỏ qua, không lưu. Ghi chú: %s",
                company_name, tax_id, tax_id_confidence, note,
            )
        tax_id = ""

    if website and _is_valid_company_website(website):
        result["website"] = website
        if not _website_matches_company_name(company_name, website):
            result["website_name_mismatch"] = True
            logger.warning(
                "⚠️  NGHI NGỜ SAI LỆCH: '%s' -> website=%s (domain không khớp "
                "token nào trong tên công ty) — NÊN TỰ KIỂM TRA LẠI, KHÔNG tự "
                "động xoá.",
                company_name, website,
            )
    elif website:
        logger.warning(
            "'%s': Gemini trả website=%r nhưng KHÔNG hợp lệ/thuộc domain loại trừ -> bỏ.",
            company_name, website,
        )

    if tax_id and _is_valid_tax_id(tax_id):
        result["tax_id"] = tax_id
    elif tax_id:
        logger.warning(
            "'%s': Gemini trả tax_id=%r nhưng SAI định dạng MSDN VN -> bỏ.",
            company_name, tax_id,
        )

    return result


def run(limit: Optional[int] = None) -> dict:
    stats = {
        "checked": 0, "updated": 0, "no_result": 0, "errors": 0,
        "website_name_mismatch": 0, "merged_duplicate_company": 0,
    }

    if not TAVILY_API_KEY:
        print("❌ Thiếu TAVILY_API_KEY trong .env — xem .env.example.")
        return stats
    if not GEMINI_API_KEY:
        print("❌ Thiếu GEMINI_API_KEY trong .env — xem .env.example.")
        return stats

    tavily_client = TavilyClient(api_key=TAVILY_API_KEY)
    gemini_client = genai.Client(api_key=GEMINI_API_KEY)

    conn = db.get_connection()
    last_request_time = None
    try:
        companies = db.get_companies_needing_web_lookup(conn)
        if limit:
            companies = companies[:limit]

        logger.info("Tìm thấy %d công ty cần tra cứu thêm website/tax_id", len(companies))

        for company_id, company_name in companies:
            stats["checked"] += 1
            logger.info(
                "[%d/%d] %s", stats["checked"], len(companies), company_name
            )

            last_request_time = _throttle(last_request_time)
            try:
                found = enrich_one_company(tavily_client, gemini_client, company_name)
            except Exception as exc:  # noqa: BLE001 - không để 1 công ty lỗi dừng cả batch
                stats["errors"] += 1
                logger.error("Lỗi xử lý '%s': %s", company_name, exc)
                continue

            if not found:
                stats["no_result"] += 1
                continue

            if found.get("website_name_mismatch"):
                stats["website_name_mismatch"] += 1

            # Bọc riêng phần ghi DB: tax_id tra được có thể trùng với 1
            # company_id KHÁC đã tồn tại từ trước (vd cùng công ty được
            # crawl từ TopCV lẫn VietnamWorks với tên hơi khác nhau, xem
            # docstring update_company_profile_with_merge()). Hàm này tự
            # phát hiện + gộp 2 company_id lại, nhưng NẾU có lỗi DB bất
            # ngờ khác (network, constraint khác...), phải rollback()
            # trước khi sang công ty tiếp theo — nếu không, connection ở
            # trạng thái "aborted" khiến MỌI lệnh sau đó (kể cả của công
            # ty hoàn toàn không liên quan) đều lỗi theo, biến 1 lỗi nhỏ
            # thành mất trắng cả phần batch còn lại.
            try:
                final_company_id = db.update_company_profile_with_merge(
                    conn, company_id,
                    website=found.get("website", ""),
                    tax_id=found.get("tax_id", ""),
                )
                conn.commit()
            except Exception as exc:  # noqa: BLE001 - lỗi DB, không để dừng cả batch
                conn.rollback()
                stats["errors"] += 1
                logger.error("Lỗi ghi DB cho '%s': %s", company_name, exc)
                continue

            if final_company_id != company_id:
                stats["merged_duplicate_company"] += 1

            stats["updated"] += 1
            logger.info(
                "  -> Đã vá: website=%s | tax_id=%s%s",
                found.get("website") or "(không có)",
                found.get("tax_id") or "(không có)",
                f" (đã gộp vào company {final_company_id}, trùng tax_id với company khác)"
                if final_company_id != company_id else "",
            )
    finally:
        conn.close()

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Vá website/tax_id công ty còn thiếu, qua Tavily search + Gemini trích xuất"
    )
    parser.add_argument("--limit", type=int, default=None,
                         help="Giới hạn số công ty xử lý (dùng để test thử trước khi chạy full)")
    args = parser.parse_args()

    stats = run(limit=args.limit)

    print("\n===== KẾT QUẢ =====")
    print(f"Đã kiểm tra              : {stats['checked']}")
    print(f"Đã vá thêm dữ liệu       : {stats['updated']}")
    print(f"Không tìm thấy đủ tin cậy : {stats['no_result']}")
    print(f"Lỗi                      : {stats['errors']}")
    print(f"⚠️  Website nghi ngờ sai lệch (nên tự kiểm tra) : {stats['website_name_mismatch']}")
    print(f"🔀 Company trùng đã tự gộp (cùng tax_id)       : {stats['merged_duplicate_company']}")
    # Mỗi công ty tốn TỐI ĐA 2 Tavily credit (query website + query
    # tax_id, xem docstring đầu file mục "CHI PHÍ TAVILY TĂNG GẤP ĐÔI")
    # -> ước tính trần trên, số thực tế có thể thấp hơn nếu company nào
    # đó bị lỗi 1 trong 2 lần search.
    print(f"📊 Ước tính Tavily credit đã dùng (tối đa) : {stats['checked'] * 2} "
          f"/ 1.000 credit free-tier tháng này")


if __name__ == "__main__":
    main()
