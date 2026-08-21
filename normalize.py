"""
Module CHUẨN HÓA — phần "dùng chung" của pipeline, không quan tâm dữ liệu
đến từ nguồn nào. Nhận vào text thô (từ RawJobRecord) -> trả ra dữ liệu
đã parse, sẵn sàng insert DB theo đúng kiểu cột trong schema.sql.
"""

import re
import hashlib
import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class NormalizedSalary:
    currency: str            # "VNĐ" | "USD"
    salary_min: Optional[int]
    salary_max: Optional[int]
    salary_type: str         # khớp salary_type_enum trong schema
    salary_period: str = "MONTH"  # "MONTH" | "YEAR" — khớp salary_period_enum


# Tín hiệu nhận biết lương NĂM trong text gốc (khác lương/tháng, vốn là
# mặc định). Chỉ bắt "năm" khi đi NGAY SAU dấu "/" (khớp đúng cách nguồn
# ghi chu kỳ trả lương, vd "₫/năm", "/ năm") — CỐ Ý không bắt chữ "năm"
# đứng lẻ trong text, vì "năm" còn xuất hiện trong ngữ cảnh khác không
# liên quan chu kỳ lương (vd "3 năm kinh nghiệm" nếu lỡ lẫn vào cùng
# chuỗi) -> bắt lẻ dễ nhận nhầm dương tính giả. Kèm biến thể tiếng Anh
# "annual"/"per year"/"yearly" hay gặp ở job cấp cao/nước ngoài, các biến
# thể này không có nguy cơ đụng "years of experience" vì luôn đi thành
# cụm cố định.
#
# BUG ĐÃ SỬA (08/2026, phát hiện qua audit thủ công 2 job "iOS Developer"
# và "Vendor Development" bị lưu salary_min/max SAI GẤP 12 LẦN): code cũ
# hoàn toàn không đọc chu kỳ trả lương trong text gốc, mặc định MỌI mức
# lương crawl được là lương/tháng. Text "200tr-500tr ₫/năm" (rõ ràng là
# lương NĂM) bị parse y hệt "200tr-500tr ₫/tháng" -> lưu thẳng
# 200,000,000-500,000,000 vào salary_min/max như thể đó là mức lương
# THÁNG, trong khi thực tế đây là mức lương CẢ NĂM.
#
# QUYẾT ĐỊNH THIẾT KẾ: salary_min/salary_max GIỮ NGUYÊN con số gốc theo
# đúng chu kỳ đã detect (không tự chia 12 để quy ra "tháng tương
# đương") — salary_period cho biết con số đó đang ở chu kỳ nào. Lý do:
# (1) chia 12 tạo số lẻ không khớp salary_raw_content gốc, khó đối chiếu
# khi audit; (2) "quy đổi tương đương" là 1 phép biến đổi có giả định
# (job ghi "300tr/năm" có thể đã gồm thưởng/lương tháng 13, không chắc
# chia đều 12 tháng là đúng) nên để tầng hiển thị/lọc tự quyết định cách
# quy đổi khi cần, thay vì áp đặt sẵn lúc ghi vào DB. Xem
# sql/migration_add_salary_period.sql.
_YEARLY_SALARY_MARKER = re.compile(
    r"/\s*n[ăa]m\b|\bannual(?:ly)?\b|\bper\s*year\b|\byearly\b",
    re.IGNORECASE,
)


def normalize_salary(salary_text: str) -> NormalizedSalary:
    """
    Parse text lương thô (TopCV hoặc VietnamWorks) thành dữ liệu có cấu trúc.

    Ví dụ input -> output (salary_period mặc định MONTH trừ khi ghi khác):
      "Thoả thuận"              -> NEGOTIABLE, (None, None)
      "10 - 30 triệu"           -> RANGE, (10_000_000, 30_000_000), VNĐ
      "Tới 3,000 USD"           -> UPTO, (None, 3000), USD
      "Từ 12 triệu"             -> STARTING_FROM, (12_000_000, None), VNĐ
      "15 triệu"                -> EXACT, (15_000_000, 15_000_000), VNĐ
      "15tr-30tr ₫/tháng"       -> RANGE, (15_000_000, 30_000_000), VNĐ  (VietnamWorks)
      "12,000-30,000 ₫/tháng"   -> RANGE, (12_000_000, 30_000_000), VNĐ  (VietnamWorks, xem BUG bên dưới)
      "$ 3,000-5,000 /tháng"    -> RANGE, (3_000, 5_000), USD          (VietnamWorks)
      "200tr-500tr ₫/năm"       -> RANGE, (200_000_000, 500_000_000), VNĐ, period=YEAR
                                   (số GIỮ NGUYÊN theo chu kỳ năm, KHÔNG chia 12 —
                                    xem docstring _YEARLY_SALARY_MARKER)
      ""                        -> NEGOTIABLE, (None, None)  (mặc định an toàn)

    BUG ĐÃ SỬA (08/2026, phát hiện qua đối chiếu dữ liệu thật đã crawl):
    VietnamWorks trả `prettySalary` theo 2 kiểu KHÁC NHAU cho cùng 1 đơn vị
    VNĐ, không phải lúc nào cũng có hậu tố "tr"/"triệu" đi kèm mỗi số:
      - "15tr-30tr ₫/tháng"      -> số nhỏ (15, 30), có hậu tố "tr" rõ ràng
        -> đúng là "triệu", nhân 1_000_000 là chuẩn.
      - "12,000-30,000 ₫/tháng"  -> số LỚN (12000, 30000), KHÔNG có "tr" —
        đây là số đã ở đơn vị "nghìn đồng" (12.000 nghìn đồng = 12 triệu),
        nếu vẫn nhân 1_000_000 như trên sẽ ra 12 TỶ (sai gấp 1000 lần).
    Bản cũ luôn nhân 1_000_000 cho MỌI số VNĐ bất kể độ lớn -> case thứ 2
    bị lỗi (xác nhận thực tế: job "Sales Engineer... Thu Nhập 15–30 Triệu"
    bị lưu salary_max = 30 tỷ). Sửa bằng cách suy luận multiplier THEO ĐỘ
    LỚN từng số (xem _vnd_multiplier() bên dưới) thay vì áp 1 hằng số cho
    toàn bộ chuỗi — an toàn với mọi case cũ vì lương thật luôn nằm trong
    khoảng vài trăm nghìn - vài trăm triệu đồng, không có chuyện 1 số vừa
    hợp lệ ở nghĩa "triệu" vừa hợp lệ ở nghĩa "nghìn đồng" cùng lúc.

    BUG THẬT KHÁC ĐÃ SỬA (08/2026, phát hiện qua đối chiếu dữ liệu thật
    khác đã crawl — job có prettySalary = "$ 13tr-15tr /tháng"): code cũ
    coi CÓ "$" BẤT KỲ ĐÂU trong chuỗi là USD tuyệt đối, rồi giữ nguyên số
    đọc được không nhân gì (numbers=13, 15 -> lưu thẳng 13-15 USD/tháng —
    vô lý, không ai trả lương 13 đô/tháng). Thực tế hậu tố "tr" đi kèm
    ngay sau số là tín hiệu VNĐ RÕ RÀNG HƠN dấu "$" đứng riêng lẻ (nhiều
    khả năng "$" ở đây là ký hiệu hiển thị lẫn/sai phía VietnamWorks, số
    thật vẫn là 13-15 TRIỆU đồng/tháng). Sửa bằng cách: nếu chuỗi có dấu
    hiệu "tr"/"triệu" gắn với số (vd "13tr") -> ưu tiên coi là VNĐ, BỎ
    QUA dấu "$", dù "$" có xuất hiện trong chuỗi. Chỉ coi là USD khi CÓ
    "$"/"usd" VÀ KHÔNG có tín hiệu "tr"/"triệu" nào — khớp đúng mọi case
    USD thật đã xác nhận (vd "$ 1,000-1,800 /tháng" không có "tr" nên vẫn
    đúng là USD như cũ)."""
    text = (salary_text or "").strip()
    if not text or "thoả thuận" in text.lower() or "thỏa thuận" in text.lower():
        return NormalizedSalary("VNĐ", None, None, "NEGOTIABLE", "MONTH")

    lowered = text.lower()

    # "tr" phải ĐI NGAY SAU 1 CHỮ SỐ mới tính là hậu tố "triệu" (vd
    # "13tr") — tránh khớp nhầm nếu chữ "tr" xuất hiện tình cờ ở chỗ khác
    # trong text (chưa gặp thực tế nhưng phòng hờ, \b đảm bảo không dính
    # vào giữa 1 từ dài hơn như "training").
    _has_million_marker = bool(re.search(r"\d\s*tr\b", lowered)) or "triệu" in lowered
    _has_dollar_sign = "usd" in lowered or "$" in text
    is_usd = _has_dollar_sign and not _has_million_marker
    if _has_dollar_sign and _has_million_marker:
        logger.warning(
            "normalize_salary(): text=%r vừa có dấu '$'/'usd' vừa có hậu "
            "tố 'tr'/'triệu' -> ưu tiên coi là VNĐ (bỏ qua '$'), khả năng "
            "cao nguồn hiển thị lẫn/sai ký hiệu tiền tệ.", text,
        )

    # Chu kỳ trả lương — xem docstring _YEARLY_SALARY_MARKER. Mặc định
    # "MONTH" khi text không có tín hiệu "/năm"/"annual"/... rõ ràng
    # (khớp hành vi cũ, vì đa số job crawl được ghi lương/tháng).
    salary_period = "YEAR" if _YEARLY_SALARY_MARKER.search(lowered) else "MONTH"

    # Bỏ hết ký tự không phải số / dấu chấm phẩy để tách các con số
    numbers = [
        _parse_number(n) for n in re.findall(r"[\d][\d.,]*", text)
    ]
    numbers = [n for n in numbers if n is not None]

    currency = "USD" if is_usd else "VNĐ"

    if not numbers:
        return NormalizedSalary(currency, None, None, "NEGOTIABLE", salary_period)

    def _scale(n: float) -> int:
        """Quy đổi 1 số thô -> đơn vị đồng thật. USD không quy đổi gì cả
        (số đọc được đã là USD). VNĐ suy luận theo độ lớn — xem docstring
        normalize_salary()."""
        if is_usd:
            return int(n)
        return int(n * _vnd_multiplier(n))

    if ("tới" in lowered or "toi " in lowered or lowered.startswith("upto")
            or "up to" in lowered):
        return NormalizedSalary(currency, None, _scale(numbers[0]), "UPTO", salary_period)

    if "từ" in lowered or lowered.startswith("tu "):
        return NormalizedSalary(
            currency, _scale(numbers[0]), None, "STARTING_FROM", salary_period
        )

    if len(numbers) >= 2:
        lo, hi = _scale(numbers[0]), _scale(numbers[1])
        if lo > hi:
            lo, hi = hi, lo
        return NormalizedSalary(currency, lo, hi, "RANGE", salary_period)

    val = _scale(numbers[0])
    return NormalizedSalary(currency, val, val, "EXACT", salary_period)


def _vnd_multiplier(number: float) -> int:
    """Suy luận hệ số nhân cho 1 số lương VNĐ THEO ĐỘ LỚN của chính số đó
    (không dựa vào có/không có chữ "triệu"/"tr" trong text, vì VietnamWorks
    có case số lớn KHÔNG kèm hậu tố này — xem BUG trong docstring
    normalize_salary()).

    Lương thật ở VN luôn rơi vào 1 trong 3 dải rõ rệt, không chồng lấn:
      - number < 1,000              -> đang ở đơn vị "triệu" (vd 15, 30,
                                        8.5) -> nhân 1_000_000.
      - 1,000 <= number < 1,000,000 -> đang ở đơn vị "nghìn đồng" (vd
                                        12_000, 30_000, đã bỏ dấu phẩy)
                                        -> nhân 1_000.
      - number >= 1,000,000         -> đã là số đồng đầy đủ (hiếm gặp,
                                        phòng hờ) -> không nhân thêm gì cả."""
    if number >= 1_000_000:
        return 1
    if number >= 1_000:
        return 1_000
    return 1_000_000


def _parse_number(raw: str) -> Optional[float]:
    """'3,000' -> 3000.0 ; '15.5' -> 15.5 ; '10' -> 10.0"""
    cleaned = raw.replace(",", "")
    # Nếu dùng dấu chấm làm phân cách nghìn kiểu VN (vd '3.000') và không
    # có phần thập phân thật -> bỏ dấu chấm luôn.
    if cleaned.count(".") == 1 and len(cleaned.split(".")[-1]) == 3:
        cleaned = cleaned.replace(".", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


# ----------------------------------------------------------------------
# Suy luận level từ số năm kinh nghiệm
# ----------------------------------------------------------------------
LEVEL_ORDER = ["Intern", "Fresher", "Junior", "Middle", "Senior", "Lead", "Manager"]


def infer_level(experience_text: str, job_title: str = "") -> str:
    text = (experience_text or "").lower()
    title = (job_title or "").lower()

    if "intern" in title or "thực tập" in title or "thuc tap" in title:
        return "Intern"
    if "fresher" in title:
        return "Fresher"
    if any(k in title for k in ["lead", "trưởng nhóm", "team lead"]):
        return "Lead"
    if any(k in title for k in ["manager", "trưởng phòng", "giám đốc"]):
        return "Manager"
    if "senior" in title:
        return "Senior"

    if "không yêu cầu" in text or "khong yeu cau" in text:
        return "Fresher"
    if "dưới 1 năm" in text or "duoi 1 nam" in text:
        return "Fresher"

    m = re.search(r"(\d+)\s*năm", text)
    if m:
        years = int(m.group(1))
        if years <= 1:
            return "Junior"
        if years <= 3:
            return "Middle"
        if years <= 5:
            return "Senior"
        return "Lead"

    if "trên 5 năm" in text:
        return "Lead"

    return "Junior"  # mặc định an toàn khi không rõ


# ----------------------------------------------------------------------
# Content hash — dùng để dedupe ở tầng ứng dụng (khớp logic hash trong
# trigger Postgres generate_job_hash(), phòng khi cần check trước khi
# insert mà chưa có company_id/level_id UUID sẵn).
# ----------------------------------------------------------------------
def compute_content_hash(company_name: str, job_title: str, level_code: str, province_name: str) -> str:
    normalized_title = re.sub(r"\s+", " ", (job_title or "").strip().lower())
    key = "|".join([
        (company_name or "").strip().lower(),
        normalized_title,
        (level_code or "").strip().lower(),
        (province_name or "").strip().lower(),
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# Khớp 1 URL dính liền vào ĐUÔI company_name, kèm dấu phân cách phía
# trước nó (nếu có) — vd "Bắc Á Bank - https://tuyendung.baca-bank.vn/"
# hay "VPBank - https://tuyendung.vpbank.com.vn/" (2 case thật đã phát
# hiện qua đối chiếu dữ liệu đã crawl, 08/2026: companyName trả về từ
# nguồn (field thô của công ty/nhà tuyển dụng, KHÔNG phải bug ở
# get_or_create_province() vừa sửa) tự dính thêm URL trang tuyển dụng
# riêng của công ty vào ngay sau tên, coi như 1 phần "tên hiển thị").
# \s*[-–|]?\s* phía trước: dấu gạch ngang thường/dài hoặc "|" tuỳ chọn,
# neo ở CUỐI CHUỖI ($) để không cắt nhầm URL nằm giữa tên công ty thật
# (chưa gặp thực tế nhưng an toàn hơn nếu có).
_TRAILING_URL_RE = re.compile(
    r"\s*[-–|]?\s*https?://\S+/?\s*$", re.IGNORECASE
)


def clean_company_name(name: str) -> str:
    text = re.sub(r"\s+", " ", (name or "").strip())
    # Cắt URL dính đuôi (xem docstring _TRAILING_URL_RE) trước khi trả về.
    # sub() ở đây an toàn: KHÔNG khớp giữa chuỗi vì regex neo cuối ($),
    # tên công ty thật không chứa "http://"/"https://" nên không có case
    # false-positive nào bị cắt nhầm.
    text = _TRAILING_URL_RE.sub("", text).strip()
    return text


# Nhà tuyển dụng ẨN DANH — thêm 08/2026, phát hiện qua đối chiếu dữ liệu
# thật: 1 số tin đăng trên TopCV/VietnamWorks/CareerViet không tiết lộ
# tên công ty thật, site tự điền 1 placeholder thay thế (case thật đã
# gặp: "Vietnamworks' Client"). Nếu để lọt, get_or_create_company_by_profile()
# sẽ tạo hẳn 1 "công ty" rác trong DB, không map được tới công ty thật
# nào cả — vô dụng cho mục đích tìm HR contact/công ty đối tác.
#
# 2 nhóm pattern:
#  - Tên chính 3 site nguồn (topcv/vietnamworks/careerviet) xuất hiện
#    NGAY TRONG tên công ty — dấu hiệu gần như chắc chắn đây là
#    placeholder của site, vì công ty thật không có lý do gì đặt tên
#    trùng/chứa tên 1 nền tảng tuyển dụng khác.
#  - Từ khóa ẩn danh chung, không gắn riêng site nào (le "Client",
#    "Confidential", "Ẩn danh", "giấu tên", "bảo mật thông tin") — để
#    bắt cả case CareerViet/TopCV không lặp lại đúng tên site trong
#    placeholder của họ.
#
# Dùng \b (word boundary) cho "client"/"confidential" để tránh khớp nhầm
# giữa 1 từ dài hơn tình cờ chứa chuỗi con đó (dù hiếm với tên công ty
# tiếng Việt, vẫn an toàn hơn không có).
_ANONYMOUS_EMPLOYER_RE = re.compile(
    r"topcv|vietnamworks|careerviet"
    r"|\bclient\b|\bconfidential\b"
    r"|ẩn danh|giấu tên|bảo mật thông tin",
    re.IGNORECASE,
)


def is_anonymous_employer_name(company_name: str) -> bool:
    """True nếu company_name khớp 1 trong các pattern nhà tuyển dụng ẩn
    danh (xem _ANONYMOUS_EMPLOYER_RE) — dùng ở pipeline.py để BỎ QUA
    hẳn job này (không tạo company/job mới), tránh rác kiểu "Vietnamworks'
    Client" lọt vào bảng companies.

    Chỉ áp dụng cho job MỚI (pipeline.py check trước khi
    get_or_create_company_by_profile()) — KHÔNG tự động xoá dữ liệu cũ
    đã lỡ insert từ trước, việc đó xử lý thủ công riêng."""
    return bool(_ANONYMOUS_EMPLOYER_RE.search(company_name or ""))


def normalize_deadline(deadline_text: str) -> Optional[date]:
    """Parse text hạn ứng tuyển thô của TopCV (dạng "30/08/2026") thành
    date object khớp kiểu cột `deadline DATE` trong schema.sql.

    Trả None nếu rỗng hoặc không parse được (an toàn, không làm crash
    pipeline — cột deadline vốn đã nullable)."""
    text = (deadline_text or "").strip()
    if not text:
        return None
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", text)
    if not m:
        return None
    day, month, year = (int(x) for x in m.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


# TopCV chỉ có đúng 4 lựa chọn cố định trong bộ lọc "Loại hình làm việc" —
# map 1-1 sang work_type_enum trong schema.sql. Giá trị lạ (TopCV đổi
# wording, hoặc parser bắt nhầm) -> None, KHÔNG insert thẳng text thô,
# tránh rác dữ liệu kiểu nhiều "biến thể" của cùng 1 giá trị.
_WORK_TYPE_MAP = {
    "toàn thời gian": "FULL_TIME",
    "bán thời gian": "PART_TIME",
    "thực tập": "INTERNSHIP",
    "khác": "OTHER",
}


def normalize_work_type(work_type_text: str) -> Optional[str]:
    """'Toàn thời gian' -> 'FULL_TIME' ; text lạ/rỗng -> None (an toàn,
    cột work_type vốn đã nullable, không làm crash insert)."""
    key = (work_type_text or "").strip().lower()
    return _WORK_TYPE_MAP.get(key)


# VietnamWorks trả company_size kèm hậu tố "nhân viên" (vd "100-499 nhân
# viên", "25-99 nhân viên"), trong khi TopCV/CareerViet chỉ trả khoảng số
# thuần (vd "100-499", "5.000-9.999") — cùng 1 cột company_size trong DB
# nên bị trộn lẫn 2 format. Regex chỉ bắt ĐÚNG hậu tố "nhân viên" (kể cả
# có/không khoảng trắng thừa trước đó) ở CUỐI chuỗi, không đụng gì khác.
_COMPANY_SIZE_SUFFIX_RE = re.compile(r"\s*nhân\s*viên\s*$", re.IGNORECASE)


def normalize_company_size(company_size_text: Optional[str]) -> str:
    """Bỏ hậu tố "nhân viên" khỏi company_size để đồng nhất format giữa
    các nguồn (xem docstring _COMPANY_SIZE_SUFFIX_RE ở trên). KHÔNG parse
    thành số/khoảng có cấu trúc — giữ nguyên phần còn lại dạng text tự do
    (vd "100-499", "5.000-9.999"), chỉ cắt bỏ đúng hậu tố này.

    Trả "" cho input rỗng/None (khớp default "" mà các hàm ghi DB
    company_size đang dùng, KHÔNG trả None để khỏi phải sửa thêm chỗ nào
    đang check `if company_size:`)."""
    text = (company_size_text or "").strip()
    if not text:
        return ""
    return _COMPANY_SIZE_SUFFIX_RE.sub("", text).strip()
