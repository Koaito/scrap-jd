"""
Một nguồn sự thật DUY NHẤT cho việc đăng ký "nguồn crawl" (TopCV/
VietnamWorks/CareerViet/...).

08/2026 — TRƯỚC ĐÂY danh sách này bị khai báo LẶP LẠI thủ công độc lập ở
4 nơi khác nhau trong backend:
  - main.py::SOURCES
  - api/crawl_runner.py::_SOURCE_ADAPTERS
  - api/routers/crawl.py::_CATEGORIES_BY_SOURCE
  - api/routers/meta.py::get_sources() (hardcode từng nguồn)

Hậu quả thực tế đã xảy ra: adapter CareerViet đã viết xong và chạy được
qua CLI (main.py) từ trước, nhưng "biến mất" ở 3 nơi kia do bị quên đăng
ký thủ công — kết quả là trang web /crawl không hiện CareerViet dù
backend đã crawl được qua CLI (xem lịch sử sửa bug 08/2026). Đây không
phải lỗi cá biệt mà là hệ quả tất yếu của việc có 4 "bản sao" cùng 1
danh sách phải tự tay giữ đồng bộ.

Giờ CẢ 4 nơi trên đều import từ ĐÚNG 1 chỗ này (SOURCES / SOURCE_ADAPTERS
/ CATEGORIES_BY_SOURCE bên dưới). Thêm 1 nguồn crawl mới (vd ITviec) từ
nay chỉ cần:
  1. Viết adapter mới (kế thừa BaseAdapter, xem adapters/base.py).
  2. Thêm CATEGORIES tương ứng vào config.py.
  3. Thêm đúng 1 entry vào dict SOURCES bên dưới.
KHÔNG cần sửa main.py / api/crawl_runner.py / api/routers/crawl.py /
api/routers/meta.py nữa — cả 4 file đó tự động thấy nguồn mới.

Ngoại lệ còn lại: frontend (mindx-jobs, repo riêng biệt) vẫn cần tự khai
báo nhãn hiển thị ở blueprints/crawl.py (_SOURCE_LABELS) — không tránh
được hoàn toàn vì đây là 2 repo tách biệt, nhưng ít nhất phía backend đã
gộp về 1 nguồn sự thật duy nhất.
"""

from adapters.topcv import TopCVAdapter
from adapters.vietnamworks import VietnamWorksAdapter
from adapters.careerviet import CareerVietAdapter
from config import TOPCV_CATEGORIES, VIETNAMWORKS_CATEGORIES, CAREERVIET_CATEGORIES

# Đăng ký nguồn crawl ở đây — thêm nguồn mới sau này (ITviec...) chỉ cần
# thêm 1 dòng vào dict này (xem hướng dẫn ở docstring đầu file).
SOURCES = {
    "topcv": {"adapter_cls": TopCVAdapter, "categories": TOPCV_CATEGORIES},
    "vietnamworks": {"adapter_cls": VietnamWorksAdapter, "categories": VIETNAMWORKS_CATEGORIES},
    "careerviet": {"adapter_cls": CareerVietAdapter, "categories": CAREERVIET_CATEGORIES},
}

DEFAULT_SOURCE = "topcv"

# 2 "view" phái sinh từ SOURCES — giữ NGUYÊN tên biến mà api/crawl_runner.py
# và api/routers/crawl.py đã dùng trước đây (_SOURCE_ADAPTERS /
# _CATEGORIES_BY_SOURCE), để 2 nơi đó chỉ cần đổi CÂU IMPORT, không cần
# sửa phần logic còn lại đang tham chiếu tới tên biến này.
SOURCE_ADAPTERS = {key: cfg["adapter_cls"] for key, cfg in SOURCES.items()}
CATEGORIES_BY_SOURCE = {key: cfg["categories"] for key, cfg in SOURCES.items()}
