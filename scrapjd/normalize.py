"""
Module CHUẨN HÓA — phần "dùng chung" của pipeline, không quan tâm dữ liệu
đến từ nguồn nào. Nhận vào text thô (từ RawJobRecord) -> trả ra dữ liệu
đã parse, sẵn sàng insert DB theo đúng kiểu cột trong schema.sql.
"""

import re
import hashlib
import logging
import unicodedata
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
    parsed_numbers = [
        _parse_number(n) for n in re.findall(r"[\d][\d.,]*", text)
    ]
    numbers: list[float] = [n for n in parsed_numbers if n is not None]

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

# Phiên bản BỘ QUY TẮC suy level (derive_level / level_from_title / các regex
# tiêu đề bên dưới). Tăng số này mỗi khi cùng một đầu vào có thể ra level KHÁC
# so với trước. Job lưu kèm phiên bản đã dùng nên lệnh tính lại level chỉ cần
# chọn job có phiên bản cũ hơn, không phải đoán theo giá trị level.
#   1 = bộ quy tắc đầu tiên có đánh phiên bản (thêm "Sr.", "Head of",
#       "Phó phòng"/"Deputy Head", "Junior" trong tiêu đề; tiêu đề liệt kê
#       nhiều cấp như "Junior, Senior" không còn tự chốt level mà để số năm
#       quyết định — 10/2026). Chưa job nào được đóng dấu phiên bản trước khi
#       quy tắc này chốt nên chưa cần tăng số.
LEVEL_RULE_VERSION = 1

# Level được suy từ đâu (cột job_postings.level_source). derive_level() chỉ trả
# 6 giá trị đầu; "manual" do tầng ứng dụng đặt khi người dùng sửa tay (và
# không bao giờ bị tính lại đè lên).
LEVEL_SOURCE_TITLE = "title"      # từ khoá trong tiêu đề
LEVEL_SOURCE_LABEL = "label"      # nhãn cố định của nguồn: "Không yêu cầu", "Dưới 1 năm", "Trên 5 năm"
LEVEL_SOURCE_YEARS = "years"      # số năm kinh nghiệm đọc được
LEVEL_SOURCE_TITLE_RANGE = "title_range"  # tiêu đề liệt kê khoảng cấp mà không có số năm: lấy cấp THẤP nhất trong khoảng
LEVEL_SOURCE_HINT = "hint"        # nhãn cấp bậc nguồn tự gán (level_hint)
LEVEL_SOURCE_DEFAULT = "default"  # không có căn cứ nào -> Junior
LEVEL_SOURCE_MANUAL = "manual"    # người dùng sửa tay
LEVEL_SOURCES = (
    LEVEL_SOURCE_TITLE, LEVEL_SOURCE_LABEL, LEVEL_SOURCE_YEARS,
    LEVEL_SOURCE_TITLE_RANGE, LEVEL_SOURCE_HINT, LEVEL_SOURCE_DEFAULT, LEVEL_SOURCE_MANUAL,
)

# Tín hiệu THÔ mà derive_level() đọc ngoài tiêu đề (cột job_postings.level_signals,
# JSONB). Lưu đúng chuỗi adapter trả về lúc crawl để tính lại level sau này không
# phải tải lại trang. Tiêu đề không nằm trong đây vì đã có cột job_title.
LEVEL_SIGNAL_KEYS = ("experience_text", "level_hint")


# ----------------------------------------------------------------------
# Từ khoá cấp bậc trong TIÊU ĐỀ
# ----------------------------------------------------------------------
# Khớp theo TỪ nguyên vẹn, không khớp chuỗi con. Lỗi cũ (`"lead" in title`):
# "Leadership Development Program" và "ROX Global Leaders" ra Lead; cùng kiểu,
# "intern" khớp nhầm "International"/"Internal Audit" (rất phổ biến trong tiêu
# đề tuyển dụng). Dữ liệu thật đã gặp: "Digital Leadership", "Techlead .NET"
# (phải GIỮ là Lead: viết liền, nên có nhánh riêng cho team/tech + lead).
_INTERN_TITLE = re.compile(r"\bintern(?:s|ship|ships)?\b|thực tập|thuc tap")
_FRESHER_TITLE = re.compile(r"\bfreshers?\b")
# "lead generation"/"lead gen" là việc tìm khách hàng tiềm năng, không phải cấp bậc.
_LEAD_TITLE = re.compile(r"\b(?:(?:team|tech)\s?lead|lead(?:er)?)\b(?!\s*gen)|trưởng nhóm")
# Phó phòng / Deputy Head: cấp Lead (dưới Trưởng phòng). Phải xét TRƯỚC
# _MANAGER_TITLE vì "Deputy Head of ..." và "Phó trưởng phòng" chứa sẵn
# "head of"/"trưởng phòng".
_DEPUTY_TITLE = re.compile(r"\bdeputy\s+head\b|phó\s+(?:trưởng\s+)?phòng")
# "Head of X" = người đứng đầu bộ phận X (tương đương Trưởng phòng). \bof\b để
# "Head Office" (trụ sở chính) không khớp.
_MANAGER_TITLE = re.compile(r"\bmanager\b|trưởng phòng|giám đốc|\bhead\s+of\b")
# "Sr." = Senior. Chỉ khớp khi theo sau là một CHỮ (Sr. Specialist, Sr Developer)
# để mã kiểu "SR-001", "SR 2026" không bị nhận nhầm.
_SENIOR_TITLE = re.compile(r"\bsenior\b|\bsr\b(?:\.\s*|\s+)(?=[^\W\d_])")
# "Junior" trong tiêu đề. KHÔNG tính "Junior+" (từ Junior trở lên) và tiêu đề
# ghi nhiều cấp ("Junior/Middle", "Junior - Mid", "Junior, Senior"): các trường
# hợp đó để số năm quyết định (xem level_from_title).
_JUNIOR_TITLE = re.compile(r"\bjunior\b(?!\s*\+)")
_MIDDLE_TITLE = re.compile(r"\bmid(?:dle)?\b")
# "Trợ lý giám đốc", "Thư ký trưởng phòng", "Assistant to Head of Sales": người
# hỗ trợ chứ không phải người giữ chức vụ đó. Cắt cụm này đi trước khi tìm từ
# khoá Lead/Manager.
_ASSISTANT_TO_BOSS = re.compile(
    r"(?:trợ lý|thư ký)\s+(?:(?:ban|tổng|phó)\s+)*(?:giám đốc|trưởng phòng|trưởng nhóm|phó phòng)"
    r"|\b(?:assistant|secretary|pa)\s+to\s+(?:the\s+)?(?:deputy\s+head|head\s+of)\b"
)


# Dấu/từ nối nằm GIỮA hai từ cấp bậc liền kề -> tiêu đề đang LIỆT KÊ một khoảng
# cấp ("Junior, Senior", "Middle/Senior", "Fresher- Junior", "Junior to Middle"),
# khác với chức danh ghép chỉ là MỘT cấp ("Senior Team Lead", "Junior Team Lead").
_LEVEL_RANGE_SEPARATOR = re.compile(r"^\s*(?:[/,&|+\-\u2013\u2014]|\b(?:or|and|to)\b|hoặc|và)\s*$")

# Thứ tự ưu tiên khi chức danh ghép nhiều cấp (cấp CAO hơn thắng, giữ hành vi
# cũ): "Senior Product Manager" -> Manager, "Junior Team Lead" -> Lead.
_TITLE_LEVEL_PRIORITY = ("Intern", "Fresher", "Lead", "Manager", "Senior", "Junior")


def _title_level_matches(title: str) -> list:
    """Mọi từ khoá cấp bậc trong tiêu đề (đã lower, đã cắt cụm "trợ lý ...")
    dạng [(start, end, level)], sắp theo vị trí. Có cả "Middle" (chỉ để nhận ra
    tiêu đề liệt kê khoảng cấp; tiêu đề chỉ có "Middle" thì KHÔNG quyết định
    level, số năm quyết định)."""
    spans: list[tuple[int, int, str]] = []
    for level, rx in (
        ("Intern", _INTERN_TITLE), ("Fresher", _FRESHER_TITLE), ("Lead", _LEAD_TITLE),
        ("Lead", _DEPUTY_TITLE), ("Manager", _MANAGER_TITLE),
        ("Senior", _SENIOR_TITLE), ("Junior", _JUNIOR_TITLE), ("Middle", _MIDDLE_TITLE),
    ):
        spans.extend((m.start(), m.end(), level) for m in rx.finditer(title))
    # "Deputy Head of X"/"Phó trưởng phòng" chứa sẵn "head of"/"trưởng phòng":
    # từ khoá Manager nằm trong vùng của Phó phòng không được tính.
    deputy = [(a, b) for a, b, lv in spans if lv == "Lead" and _DEPUTY_TITLE.fullmatch(title[a:b])]
    spans = [
        (a, b, lv) for a, b, lv in spans
        if not (lv == "Manager" and any(da < b and a < db for da, db in deputy))
    ]
    return sorted(spans)


def _analyze_title(job_title: str) -> tuple[Optional[str], frozenset[str]]:
    """Phân tích từ khoá cấp bậc trong tiêu đề -> (level, khoảng).

    - level: cấp do tiêu đề quyết định, hoặc None.
    - khoảng: frozenset các cấp mà tiêu đề LIỆT KÊ (chỉ khác rỗng khi tiêu đề
      ghi một khoảng cấp, khi đó level luôn là None).
    """
    title = _ASSISTANT_TO_BOSS.sub(" ", (job_title or "").lower())
    matches = _title_level_matches(title)
    levels = frozenset(lv for _, _, lv in matches)
    if not (levels - {"Middle"}):
        return None, frozenset()  # không từ khoá nào, hoặc chỉ "Middle": để số năm quyết định

    is_range = "Junior" in levels and "Middle" in levels
    if not is_range:
        for (_, end_a, lv_a), (start_b, _, lv_b) in zip(matches, matches[1:]):
            if lv_a != lv_b and _LEVEL_RANGE_SEPARATOR.match(title[end_a:start_b]):
                is_range = True
                break
    if is_range:
        return None, levels

    for level in _TITLE_LEVEL_PRIORITY:
        if level in levels:
            return level, frozenset()
    return None, frozenset()


def level_from_title(job_title: str) -> Optional[str]:
    """Level do TỪ KHOÁ trong tiêu đề quyết định (Intern/Fresher/Lead/Manager/
    Senior/Junior), hoặc None khi tiêu đề không có từ khoá nào HOẶC không xác
    định được một cấp duy nhất. Là bước đầu và ưu tiên cao nhất của
    derive_level(): tiêu đề nêu đúng một cấp thì số năm kinh nghiệm không đổi
    được kết quả. Tách riêng để script vá dữ liệu biết job nào level đã do
    tiêu đề chốt (không cần tải lại trang).

    Nguyên tắc: tiêu đề chỉ quyết định khi nó nói về MỘT cấp. Tiêu đề liệt kê
    nhiều cấp ("Junior, Senior", "Middle/Senior", "Fresher - Junior", "Junior
    to Middle") không nói rõ cấp nào, nên trả None để số năm (rồi nhãn nguồn)
    quyết định. "Junior" đi cùng "Middle" cũng là khoảng cấp, còn "Junior+" (từ
    Junior trở lên) bị bỏ qua. Chức danh ghép chỉ là một cấp ("Senior Team
    Lead", "Senior Product Manager") vẫn do tiêu đề quyết định, cấp CAO hơn thắng
    theo thứ tự Intern > Fresher > Lead (kể cả Phó phòng) > Manager > Senior >
    Junior.
    """
    return _analyze_title(job_title)[0]


@dataclass(frozen=True)
class LevelDecision:
    """Kết quả suy level: giá trị + căn cứ đã dùng (một trong LEVEL_SOURCES,
    không bao giờ là "manual")."""
    level: str
    source: str


def derive_level(experience_text: str, job_title: str = "", level_hint: str = "") -> LevelDecision:
    """Suy luận level KÈM căn cứ. Thứ tự ưu tiên: từ khoá trong tiêu đề > nhãn
    cố định của nguồn > số năm kinh nghiệm trong experience_text > cận dưới của
    khoảng cấp tiêu đề liệt kê > level_hint (nhãn cấp bậc nguồn tự gán, vd
    jobLevel của VietnamWorks, adapter đã đổi sang 1 giá trị trong LEVEL_ORDER)
    > mặc định Junior.

    level_hint CHỈ là phương án dự phòng khi không đọc được số năm: nhãn cấp
    bậc do nhà tuyển dụng tự chọn nên nhiễu (vd vị trí "Data Modeler" gắn
    "Trưởng phòng"), không để nó ghi đè số năm đã đọc được.

    Hàm thuần (không DB, không mạng): cùng đầu vào + cùng LEVEL_RULE_VERSION
    luôn ra cùng kết quả, nên tính lại hàng loạt được mà không cần tải trang."""
    text = (experience_text or "").lower()

    from_title = level_from_title(job_title)
    if from_title:
        return LevelDecision(from_title, LEVEL_SOURCE_TITLE)

    if "không yêu cầu" in text or "khong yeu cau" in text:
        return LevelDecision("Fresher", LEVEL_SOURCE_LABEL)
    if "dưới 1 năm" in text or "duoi 1 nam" in text:
        return LevelDecision("Fresher", LEVEL_SOURCE_LABEL)

    # "Trên 5 năm" (nhãn của TopCV) PHẢI xét TRƯỚC regex "(\d+) năm" bên dưới:
    # regex đó bắt được "5 năm" trong chính cụm này nên nếu để sau thì job
    # "Trên 5 năm" ra Senior (5 năm) và nhánh Lead không bao giờ chạy tới.
    if "trên 5 năm" in text or "tren 5 nam" in text:
        return LevelDecision("Lead", LEVEL_SOURCE_LABEL)

    m = re.search(r"(\d+)\s*năm", text)
    if m:
        years = int(m.group(1))
        if years <= 1:
            level = "Junior"
        elif years <= 3:
            level = "Middle"
        elif years <= 5:
            level = "Senior"
        else:
            level = "Lead"
        return LevelDecision(level, LEVEL_SOURCE_YEARS)

    # Tiêu đề liệt kê khoảng cấp mà không có số năm/nhãn: lấy cấp THẤP nhất trong
    # khoảng (ngang với số năm = yêu cầu tối thiểu). Vẫn nằm trong những cấp chính
    # tiêu đề nêu ra, tốt hơn rơi xuống hint nhiễu hoặc mặc định Junior
    # ("Senior/Leader" không thể là Junior).
    title_range = _analyze_title(job_title)[1]
    if title_range:
        floor = min(title_range, key=LEVEL_ORDER.index)
        return LevelDecision(floor, LEVEL_SOURCE_TITLE_RANGE)

    if level_hint in LEVEL_ORDER:
        return LevelDecision(level_hint, LEVEL_SOURCE_HINT)

    return LevelDecision("Junior", LEVEL_SOURCE_DEFAULT)  # mặc định an toàn khi không rõ


def infer_level(experience_text: str, job_title: str = "", level_hint: str = "") -> str:
    """Như derive_level() nhưng chỉ trả tên level (giữ nguyên chữ ký cũ cho
    pipeline, adapter và các script backfill đang gọi)."""
    return derive_level(experience_text, job_title, level_hint).level


def build_level_signals(experience_text: str = "", level_hint: str = "") -> dict:
    """Dựng dict tín hiệu thô để lưu vào job_postings.level_signals: đúng hai chuỗi
    mà derive_level() nhận, KHÔNG sửa gì (để tính lại ra đúng kết quả lúc crawl, kể cả
    khi chuỗi có khoảng trắng thừa). Luôn đủ cả hai khoá, rỗng nghĩa là nguồn không có tín hiệu đó. Phân biệt
    với NULL ở cột: NULL = chưa từng lưu (job cũ), {...rỗng} = đã lưu và nguồn không
    có gì."""
    return {
        "experience_text": experience_text or "",
        "level_hint": level_hint or "",
    }


def derive_level_from_signals(job_title: str, signals: Optional[dict]) -> LevelDecision:
    """Tính lại level từ tiêu đề hiện tại + tín hiệu đã lưu (build_level_signals).
    Cùng kết quả với derive_level() lúc crawl nếu tiêu đề không đổi và cùng
    LEVEL_RULE_VERSION. signals None (job chưa lưu tín hiệu) vẫn gọi được nhưng
    kết quả khi tiêu đề không có từ khoá sẽ là mặc định 'default', nên nơi gọi phải
    tự xử lý trường hợp đó (lệnh tính lại chỉ tin kết quả 'title' khi signals là None)."""
    signals = signals or {}
    return derive_level(
        signals.get("experience_text", ""), job_title, signals.get("level_hint", ""),
    )


# ----------------------------------------------------------------------
# Đọc số năm kinh nghiệm tối thiểu từ đoạn văn yêu cầu công việc
# ----------------------------------------------------------------------
# Dùng cho nguồn không có trường "số năm" đáng tin (VietnamWorks: API search
# luôn trả yearsOfExperience=0). Chỉ tính con số đứng GẦN cụm "kinh nghiệm"/
# "experience" để không bắt nhầm "sinh viên năm cuối", "năm 2026"...
#
# Quy tắc (đã chốt với người dùng, 10/2026):
#  - Khoảng "3-5 năm" lấy SỐ NHỎ (mức tối thiểu để ứng tuyển) -> 3.
#  - Nhiều đoạn nêu số năm thì lấy đoạn xuất hiện ĐẦU TIÊN (yêu cầu chính
#    thường ghi trước; các đoạn sau hay là "trong đó X năm ở vị trí quản lý",
#    "ưu tiên", "nice to have").
#  - "Không yêu cầu kinh nghiệm", "dưới 1 năm", "X tháng" (X < 12) -> 0.
_YEARS_NEAR = 40  # ký tự tối đa giữa con số và chữ "kinh nghiệm"/"experience"
_MAX_PLAUSIBLE_YEARS = 40

_VI_YEARS_RE = re.compile(
    r"(dưới\s*)?(\d{1,2})(?:\s*(?:-|–|—|~|đến|tới)\s*\d{1,2})?\s*\+?\s*năm",
    re.IGNORECASE,
)
_EN_YEARS_RE = re.compile(
    r"(less\s+than\s+)?(\d{1,2})(?:\s*(?:-|–|—|~|to)\s*\d{1,2})?\s*\+?\s*(?:years?|yrs?)\b",
    re.IGNORECASE,
)
_MONTHS_RE = re.compile(r"(\d{1,2})\s*(?:tháng|months?)", re.IGNORECASE)
_EXPERIENCE_WORD_RE = re.compile(r"kinh nghiệm|experience|\bexp\b", re.IGNORECASE)
_NO_EXPERIENCE_RE = re.compile(
    r"(?:không|khong)\s+(?:yêu\s+cầu|cần|đòi\s+hỏi)\s+(?:có\s+)?kinh\s+nghiệm"
    r"|chưa\s+có\s+kinh\s+nghiệm"
    r"|no\s+(?:prior\s+)?experience(?:\s+(?:is\s+)?required)?",
    re.IGNORECASE,
)


def _near_experience_word(text: str, start: int, end: int) -> bool:
    window = text[max(0, start - _YEARS_NEAR): end + _YEARS_NEAR]
    return _EXPERIENCE_WORD_RE.search(window) is not None


def _year_candidates(text: str) -> list[tuple[int, int]]:
    """Các mức số năm kinh nghiệm đọc được trong `text`: list (vị trí, số năm),
    chưa sắp xếp. Chỉ tính con số đứng gần chữ \"kinh nghiệm\"/\"experience\"."""
    candidates = []  # (vị trí, số năm)

    for regex in (_VI_YEARS_RE, _EN_YEARS_RE):
        for m in regex.finditer(text):
            if not _near_experience_word(text, m.start(), m.end()):
                continue
            years = int(m.group(2))
            if years > _MAX_PLAUSIBLE_YEARS:
                continue
            if m.group(1):  # "dưới 1 năm" / "less than 1 year"
                years = 0
            candidates.append((m.start(), years))

    for m in _MONTHS_RE.finditer(text):
        if not _near_experience_word(text, m.start(), m.end()):
            continue
        months = int(m.group(1))
        if months < 12:
            candidates.append((m.start(), 0))

    for m in _NO_EXPERIENCE_RE.finditer(text):
        candidates.append((m.start(), 0))

    return candidates


def extract_min_years(text: str) -> Optional[int]:
    """Số năm kinh nghiệm TỐI THIỂU ghi trong `text` (0 = không yêu cầu/dưới 1
    năm), hoặc None nếu không đọc được. Xem ghi chú quy tắc ở trên."""
    if not text:
        return None
    candidates = _year_candidates(text)
    if not candidates:
        return None
    return min(candidates)[1]


# ----------------------------------------------------------------------
# Ước lượng "Trên 5 năm" (nhãn TopCV) từ phần yêu cầu — CHỈ để vá dữ liệu cũ
# ----------------------------------------------------------------------
# Trước khi infer_level() xét "Trên 5 năm" riêng, job TopCV mang nhãn đó bị lưu
# là Senior, và DB không lưu nhãn gốc. Đây là phương án dự phòng khi không tải
# lại được trang: đọc chữ trong phần yêu cầu. Chỉ đáng tin theo MỘT chiều
# (Senior -> Lead), vì Senior cũ chỉ có thể đến từ tiêu đề, nhãn "4/5 năm"
# (đúng, giữ nguyên) hoặc nhãn "Trên 5 năm" (sai, cần vá).
_OVER_YEARS_RE = re.compile(
    r"(?:trên|hơn|over|more\s+than)\s*(\d{1,2})\s*\+?\s*(?:năm|years?|yrs?)\b",
    re.IGNORECASE,
)


def infer_level_from_requirements(text: str) -> Optional[str]:
    """\"Lead\" khi phần yêu cầu đòi HƠN 5 năm kinh nghiệm, ngược lại None
    (không đủ căn cứ để đổi level). Hai tín hiệu, tính theo đoạn nêu số năm
    đầu tiên như extract_min_years():
      - cụm \"trên/hơn/over/more than N năm\" với N >= 5 (nghĩa là > 5 năm);
      - số năm tối thiểu >= 6.
    \"Từ 5 năm\", \"tối thiểu 5 năm\", \"5+ năm\" là đúng 5 năm (Senior), KHÔNG
    đổi."""
    if not text:
        return None
    candidates = _year_candidates(text)
    if not candidates:
        return None
    first_pos, first_years = min(candidates)
    if first_years >= 6:
        return "Lead"
    for m in _OVER_YEARS_RE.finditer(text):
        # Cụm "trên N năm" bắt đầu TRƯỚC chữ số nên vị trí <= vị trí của chính
        # đoạn đó trong candidates; đoạn nêu số năm sau đó thì không tính.
        if m.start() <= first_pos and int(m.group(1)) >= 5 and _near_experience_word(
            text, m.start(), m.end()
        ):
            return "Lead"
    return None


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
    danh (xem _ANONYMOUS_EMPLOYER_RE) — dùng ở scrapjd/pipeline.py để BỎ QUA
    hẳn job này (không tạo company/job mới), tránh rác kiểu "Vietnamworks'
    Client" lọt vào bảng companies.

    Chỉ áp dụng cho job MỚI (scrapjd/pipeline.py check trước khi
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


# ----------------------------------------------------------------------
# So khớp tiêu đề: "hai tiêu đề này có còn là cùng một vị trí không"
# ----------------------------------------------------------------------
# Nhà tuyển dụng VietnamWorks có thể sửa một tin đăng thành vị trí KHÁC HẲN mà
# vẫn giữ mã job (dữ liệu thật: mã 2110157 từng là "Account Manager", nay là
# "Sales Assistant"; mã 2109152 từng là "BACK-END DEVELOPER", nay là "Kỹ Sư An
# Toàn Thông Tin"). Gộp hai tin như vậy vào một dòng sẽ làm dòng đó sai, nên cả
# scripts/backfill/backfill_vnw_detail.py lẫn pipeline crawl chỉ coi là cùng một job khi tiêu đề
# còn "gần giống". Đặt ở đây (không phải trong script) vì scrapjd/pipeline.py không được
# import từ script.
MIN_TITLE_OVERLAP = 0.5


def _title_tokens(title: str) -> set:
    text = unicodedata.normalize("NFKD", (title or "").lower().replace("đ", "d"))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return {t for t in re.findall(r"\w+", text) if len(t) > 1}


def title_overlap(a: str, b: str) -> float:
    """Hệ số trùng từ giữa hai tiêu đề, 0.0 đến 1.0: số từ chung chia cho số từ
    của tiêu đề NGẮN hơn (bỏ dấu, không phân biệt hoa thường, bỏ từ 1 ký tự).
    Chia theo tiêu đề ngắn để việc thêm đuôi như "/ Tuyển Gấp" hay "(Hà Nội)"
    không kéo điểm xuống.

    Thiếu một trong hai tiêu đề (hoặc không còn từ nào sau khi tách) thì không
    đủ cơ sở để kết luận khác nhau nên trả 1.0, đúng như cách titles_similar()
    luôn coi trường hợp đó là "giống"."""
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return 1.0
    return len(ta & tb) / min(len(ta), len(tb))


def titles_similar(a: str, b: str) -> bool:
    """True nếu hai tiêu đề đủ giống để coi là cùng một vị trí (title_overlap
    >= MIN_TITLE_OVERLAP)."""
    return title_overlap(a, b) >= MIN_TITLE_OVERLAP
