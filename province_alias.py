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


def resolve_province_alias(raw_name: str) -> str:
    """Chuẩn hoá 1 tên tỉnh thô (có thể là tên CŨ, tên MỚI, hoặc có tiền
    tố "TP."/"Tp. ") -> tên tỉnh MỚI đúng chuẩn 34 đơn vị hiện hành.

    Trả "" nếu rỗng hoặc không nhận diện được (để nơi gọi tự quyết định
    fallback, thường là "Khác" — xem db.get_province_id())."""
    if not raw_name:
        return ""
    name = re.sub(r"^\s*(TP\.|Tp\.)\s*", "", raw_name.strip()).strip()
    return PROVINCE_ALIAS_MAP.get(name, "")
