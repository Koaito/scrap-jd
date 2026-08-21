"""
"Khuôn dữ liệu chung" (contract) mà MỌI adapter (TopCV, ITviec, ...) đều
phải trả về. Pipeline lõi chỉ biết làm việc với RawJobRecord, không quan
tâm dữ liệu đến từ nguồn nào.

Thêm nguồn crawl mới trong tương lai = viết thêm 1 adapter mới trả về
list[RawJobRecord], KHÔNG cần sửa gì ở normalize.py / db.py / pipeline.py.
"""

from dataclasses import dataclass, field


@dataclass
class RawJobRecord:
    # --- Bắt buộc ---
    job_title: str
    company_name: str
    source_url: str          # link JD gốc, dùng làm khóa chống trùng theo nguồn
    source_name: str         # "TopCV", "ITviec", ...

    # --- Optional, text thô chưa parse (normalize.py sẽ xử lý) ---
    salary_text: str = ""        # "15 - 30 triệu", "Thoả thuận", "Tới 3,000 USD"
    province_text: str = ""      # "Hà Nội", "Hồ Chí Minh (mới)"
    experience_text: str = ""    # "2 năm", "Không yêu cầu", "Dưới 1 năm"
    work_type_text: str = ""     # "Toàn thời gian", "Thực tập", ...
    posted_text: str = ""        # "Đăng 2 ngày trước"
    deadline_text: str = ""      # "30/08/2026" (nhãn "Hạn ứng tuyển" trên trang chi tiết JD)
    matching_industry: str = ""  # gán sẵn từ config theo category đang crawl

    # --- Optional, thông tin bổ sung ---
    company_url: str = ""
    raw_tags: list = field(default_factory=list)  # các tag phụ tìm thấy trên card
