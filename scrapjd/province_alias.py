"""
Mapping tên tỉnh/thành CŨ (trước sáp nhập, Nghị quyết 202/2025/QH15,
hiệu lực 01/7/2025, cả nước giảm từ 63 xuống 34 đơn vị hành chính cấp
tỉnh) -> tên tỉnh/thành MỚI (1 trong 34 đơn vị hiện hành, đúng danh sách
đang seed trong sql/migration_update_provinces_2025.sql).

LÝ DO CÓ FILE NÀY (08/2026, phát hiện qua thực tế): dù bảng `provinces`
trong DB đã seed đúng 34 tỉnh mới, nhiều DOANH NGHIỆP đăng tin trên
TopCV/VietnamWorks VẪN ghi địa chỉ theo tên tỉnh CŨ (vd "Bình Dương",
"Hòa Bình", "Bắc Giang"...) — họ chưa cập nhật theo địa giới hành chính
mới. Nếu tra thẳng tên cũ vào bảng 34 tỉnh mới sẽ KHÔNG khớp, và (sau
khi đã sửa bug INSERT bừa trong get_or_create_province() cũ) sẽ bị rơi
hết vào "Khác" — mất thông tin thay vì map đúng về tỉnh mới đã sáp
nhập. File này là bước tra cứu trung gian: thử khớp tên MỚI trước (ưu
tiên, vì đa số nguồn đã cập nhật), không khớp thì tra qua đây để quy
đổi tên CŨ -> MỚI, chỉ khi vẫn không khớp mới thật sự fallback "Khác".

Nguồn: đối chiếu danh sách 23 tỉnh có sáp nhập (kèm tỉnh cũ hợp thành)
người dùng cung cấp, cộng thêm 11 tỉnh GIỮ NGUYÊN không đổi tên (không
nằm trong danh sách sáp nhập nhưng vẫn cần có mặt ở đây để tra cứu
thống nhất 1 chỗ, map về chính nó).

Key đã chuẩn hoá: strip() + bỏ tiền tố "TP. "/"Tp. " nếu có (dữ liệu
nguồn đôi khi ghi "TP. Hồ Chí Minh", đôi khi chỉ "Hồ Chí Minh") — xem
hàm resolve_province_alias() bên dưới, KHÔNG dùng trực tiếp dict này để
tra cứu từ code khác.
"""

import re
import unicodedata

# tỉnh cũ -> tỉnh mới (chỉ 23 tỉnh có sáp nhập thật sự đổi tên/hợp nhất)
_MERGED = {
    # -> Tuyên Quang
    "Hà Giang": "Tuyên Quang",
    "Tuyên Quang": "Tuyên Quang",
    # -> Lào Cai
    "Yên Bái": "Lào Cai",
    "Lào Cai": "Lào Cai",
    # -> Thái Nguyên
    "Bắc Kạn": "Thái Nguyên",
    "Thái Nguyên": "Thái Nguyên",
    # -> Phú Thọ
    "Vĩnh Phúc": "Phú Thọ",
    "Hòa Bình": "Phú Thọ",
    "Phú Thọ": "Phú Thọ",
    # -> Bắc Ninh
    "Bắc Giang": "Bắc Ninh",
    "Bắc Ninh": "Bắc Ninh",
    # -> Hưng Yên
    "Thái Bình": "Hưng Yên",
    "Hưng Yên": "Hưng Yên",
    # -> Hải Phòng
    "Hải Dương": "Hải Phòng",
    "Hải Phòng": "Hải Phòng",
    # -> Ninh Bình
    "Hà Nam": "Ninh Bình",
    "Nam Định": "Ninh Bình",
    "Ninh Bình": "Ninh Bình",
    # -> Quảng Trị
    "Quảng Bình": "Quảng Trị",
    "Quảng Trị": "Quảng Trị",
    # -> Đà Nẵng
    "Quảng Nam": "Đà Nẵng",
    "Đà Nẵng": "Đà Nẵng",
    # -> Quảng Ngãi
    "Kon Tum": "Quảng Ngãi",
    "Quảng Ngãi": "Quảng Ngãi",
    # -> Gia Lai
    "Bình Định": "Gia Lai",
    "Gia Lai": "Gia Lai",
    # -> Khánh Hòa
    "Ninh Thuận": "Khánh Hòa",
    "Khánh Hòa": "Khánh Hòa",
    # -> Lâm Đồng
    "Đắk Nông": "Lâm Đồng",
    "Bình Thuận": "Lâm Đồng",
    "Lâm Đồng": "Lâm Đồng",
    # -> Đắk Lắk
    "Phú Yên": "Đắk Lắk",
    "Đắk Lắk": "Đắk Lắk",
    # -> Hồ Chí Minh
    "Bà Rịa - Vũng Tàu": "Hồ Chí Minh",
    "Bà Rịa-Vũng Tàu": "Hồ Chí Minh",
    "Bình Dương": "Hồ Chí Minh",
    "Hồ Chí Minh": "Hồ Chí Minh",
    # -> Đồng Nai
    "Bình Phước": "Đồng Nai",
    "Đồng Nai": "Đồng Nai",
    # -> Tây Ninh
    "Long An": "Tây Ninh",
    "Tây Ninh": "Tây Ninh",
    # -> Cần Thơ
    "Sóc Trăng": "Cần Thơ",
    "Hậu Giang": "Cần Thơ",
    "Cần Thơ": "Cần Thơ",
    # -> Vĩnh Long
    "Bến Tre": "Vĩnh Long",
    "Trà Vinh": "Vĩnh Long",
    "Vĩnh Long": "Vĩnh Long",
    # -> Đồng Tháp
    "Tiền Giang": "Đồng Tháp",
    "Đồng Tháp": "Đồng Tháp",
    # -> Cà Mau
    "Bạc Liêu": "Cà Mau",
    "Cà Mau": "Cà Mau",
    # -> An Giang
    "Kiên Giang": "An Giang",
    "An Giang": "An Giang",
}

# 11 tỉnh GIỮ NGUYÊN, không nằm trong đợt sáp nhập -> map về chính nó,
# để resolve_province_alias() tra được TOÀN BỘ 34 tỉnh ở 1 chỗ duy nhất
# (không phải nhớ thêm "tỉnh nào có trong _MERGED, tỉnh nào không").
_UNCHANGED = [
    "Cao Bằng", "Lai Châu", "Điện Biên", "Lạng Sơn", "Sơn La",
    "Quảng Ninh", "Hà Nội", "Thanh Hóa", "Nghệ An", "Hà Tĩnh", "Huế",
]

PROVINCE_ALIAS_MAP = dict(_MERGED)
for _p in _UNCHANGED:
    PROVINCE_ALIAS_MAP[_p] = _p

# 2 giá trị đặc biệt filter TopCV dùng, không phải đơn vị hành chính
# thật -> map về chính nó để đi qua chung 1 luồng tra cứu, không cần
# case riêng ở nơi gọi.
PROVINCE_ALIAS_MAP["Khác"] = "Khác"
PROVINCE_ALIAS_MAP["Remote"] = "Remote"


# Biến thể hay gặp KHÔNG suy ra được từ việc bỏ dấu/hoa-thường: viết tắt,
# tên tiếng Anh, tên gọi thông tục, tên cũ dạng "Thừa Thiên Huế" (tỉnh
# này đổi tên thành "Huế" nên không nằm trong _MERGED/_UNCHANGED). Key ở
# dạng ĐÃ chuẩn hoá (xem _normalize_key): không dấu, chữ thường, chỉ chữ
# và số cách nhau 1 dấu cách, đã bỏ tiền tố "tp"/"thanh pho"/"tinh".
# Chỉ thêm khi nghĩa rõ ràng 1-1; KHÔNG thêm tên mơ hồ (vd "Vũng Tàu"
# riêng lẻ, "Bình Dương" đã có sẵn ở _MERGED).
_EXTRA_ALIASES = {
    "hcm": "Hồ Chí Minh",
    "tphcm": "Hồ Chí Minh",
    "hcmc": "Hồ Chí Minh",
    "ho chi minh city": "Hồ Chí Minh",
    "sai gon": "Hồ Chí Minh",
    "saigon": "Hồ Chí Minh",
    "hn": "Hà Nội",
    "hanoi": "Hà Nội",
    "thua thien hue": "Huế",
    "tt hue": "Huế",
}

_KEY_PREFIXES = ("thanh pho ", "tp ", "tinh ")


def _normalize_key(raw_name: str) -> str:
    """Đưa tên thô về dạng so khớp: bỏ dấu (kể cả đ/Đ), chữ thường, mọi ký
    tự không phải chữ/số (dấu chấm, gạch ngang, nhiều khoảng trắng...) thành
    1 dấu cách, rồi bỏ tiền tố "Thành phố"/"TP"/"Tỉnh" nếu có."""
    text = raw_name.replace("đ", "d").replace("Đ", "D")
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    text = re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()
    for prefix in _KEY_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break
    return text


# Chỉ mục tra cứu theo key đã chuẩn hoá. Dựng 1 lần lúc import. Chuẩn hoá
# bỏ dấu KHÔNG làm 2 tỉnh khác nhau trùng key (đã kiểm tra toàn bộ
# PROVINCE_ALIAS_MAP, xem tests/test_province_alias.py).
_NORMALIZED_INDEX = {_normalize_key(k): v for k, v in PROVINCE_ALIAS_MAP.items()}
_NORMALIZED_INDEX.update(_EXTRA_ALIASES)


def resolve_province_alias(raw_name: str) -> str:
    """Chuẩn hoá 1 tên tỉnh thô -> tên tỉnh MỚI đúng chuẩn 34 đơn vị hiện
    hành (hoặc "Khác"/"Remote").

    Nhận: tên CŨ (trước sáp nhập) hoặc MỚI; có/không dấu; hoa/thường tuỳ
    ý; có tiền tố "TP."/"Tp "/"Thành phố"/"Tỉnh"; dấu gạch nối/khoảng
    trắng lệch (vd "Bà Rịa-Vũng Tàu"); và vài viết tắt/tên tiếng Anh phổ
    biến (HCM, TPHCM, Sài Gòn, Ho Chi Minh City, HN, Thừa Thiên Huế...).

    Trả "" nếu rỗng hoặc không nhận diện được (để nơi gọi tự quyết định
    fallback, thường là "Khác" — xem db.get_province_id())."""
    if not raw_name:
        return ""
    return _NORMALIZED_INDEX.get(_normalize_key(raw_name), "")
