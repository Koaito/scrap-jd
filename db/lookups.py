"""
db.lookups — tách từ db.py (God module) theo domain.
"""

import logging
from typing import Optional

import psycopg2
from province_alias import resolve_province_alias

logger = logging.getLogger(__name__)


def get_province_id(conn, province_name: str) -> Optional[int]:
    """Tra cứu province_id THEO TÊN, KHÔNG BAO GIỜ insert dòng mới.

    `provinces` là bảng CỨNG (fixed lookup table) — chỉ được nạp dữ liệu
    qua migration (sql/migration_update_provinces_2025.sql, đúng 34
    tỉnh/thành sau sáp nhập + 2 giá trị đặc biệt "Khác"/"Remote"), không
    phải bảng để ứng dụng tự thêm dòng lúc chạy.

    BUG ĐÃ SỬA (08/2026, phát hiện qua đối chiếu dữ liệu thật đã crawl):
    hàm cũ (get_or_create_province()) INSERT thẳng bất kỳ chuỗi nào
    không khớp tên có sẵn thành 1 dòng "province" MỚI trong bảng cứng
    này. Các lớp lọc ở adapter (vd
    VietnamWorksAdapter._looks_like_province_name() trong
    adapters/vietnamworks.py) chỉ loại được chuỗi rõ ràng KHÔNG PHẢI tên
    tỉnh (có số/dấu phẩy/quá dài) — vẫn để lọt bất kỳ chuỗi "trông giống"
    tên tỉnh (không số, không dấu phẩy, ngắn) dù không thật sự nằm trong
    danh sách 34 tỉnh/thành hợp lệ, ví dụ tên phòng ban, biến thể viết
    tắt, hoặc tên tỉnh cũ đã sáp nhập. Mọi giá trị như vậy trước đây đều
    biến thành rác trong bảng provinces. Endpoint API cho phép client
    truyền province_name tuỳ ý (api/routers/companies.py,
    api/routers/jobs.py) cũng đi qua cùng 1 hàm này nên cùng bị lỗi.

    Sửa bằng cách bỏ hẳn nhánh INSERT: nếu tên tỉnh không khớp trực tiếp,
    THỬ QUY ĐỔI qua province_alias.resolve_province_alias() trước khi bỏ
    cuộc — vì nhiều doanh nghiệp đăng tin trên TopCV/VietnamWorks VẪN
    ghi địa chỉ theo tên tỉnh CŨ (trước sáp nhập 07/2025, vd "Bình
    Dương", "Hòa Bình", "Bắc Giang"...), không phải lỗi hay rác, chỉ là
    nguồn dữ liệu chưa cập nhật theo địa giới hành chính mới -> map đúng
    về tỉnh mới đã sáp nhập thay vì gộp hết vào "Khác" (mất thông tin
    không cần thiết). Chỉ khi CẢ 2 bước đều không khớp -> map về "Khác"
    (dòng có sẵn, cố định); nếu ngay cả "Khác" cũng không có trong DB
    (schema chưa migrate) -> trả None thay vì tạo bừa, để lỗi lộ ra rõ
    ràng ở tầng insert_job() (cột province_id vốn nullable) thay vì âm
    thầm phình bảng cứng."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT province_id FROM provinces WHERE province_name = %s",
            (province_name or "Khác",),
        )
        row = cur.fetchone()
        if row:
            return row[0]

        aliased_name = resolve_province_alias(province_name)
        if aliased_name and aliased_name != province_name:
            cur.execute(
                "SELECT province_id FROM provinces WHERE province_name = %s",
                (aliased_name,),
            )
            row = cur.fetchone()
            if row:
                logger.info(
                    "get_province_id(): %r là tên tỉnh CŨ (trước sáp "
                    "nhập) -> quy đổi về tỉnh mới %r.",
                    province_name, aliased_name,
                )
                return row[0]

        if province_name:
            logger.warning(
                "get_province_id(): %r không khớp tỉnh/thành nào có sẵn "
                "(kể cả sau khi thử quy đổi tên tỉnh cũ -> mới) trong "
                "bảng provinces (bảng cứng, không tự tạo dòng mới) -> "
                "fallback về 'Khác'.", province_name,
            )
        cur.execute(
            "SELECT province_id FROM provinces WHERE province_name = 'Khác'"
        )
        row = cur.fetchone()
        return row[0] if row else None


get_or_create_province = get_province_id


def get_level_id(conn, level_code: str) -> Optional[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT level_id FROM levels WHERE level_code = %s", (level_code,))
        row = cur.fetchone()
        return row[0] if row else None
