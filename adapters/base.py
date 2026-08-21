from abc import ABC, abstractmethod
from typing import Iterator, Optional

from models import RawJobRecord


class BaseAdapter(ABC):
    """
    Mọi adapter nguồn (TopCV, ITviec, VietnamWorks, ...) phải kế thừa class
    này và implement fetch_jobs(). Pipeline lõi (pipeline.py) chỉ gọi qua
    interface này -> không cần biết chi tiết bên trong từng nguồn.
    """

    source_name: str = "unknown"

    @abstractmethod
    def fetch_jobs(self, category_key: str, max_pages: int) -> Iterator[RawJobRecord]:
        """Trả về (yield) từng RawJobRecord tìm được cho category_key."""
        raise NotImplementedError

    def fetch_company_profile(self, company_url: str) -> dict:
        """Optional: crawl sâu vào trang hồ sơ công ty để lấy thêm website
        thật, địa chỉ, quy mô, lĩnh vực. Mặc định trả dict rỗng (nguồn nào
        không hỗ trợ thì bỏ qua, pipeline vẫn chạy bình thường)."""
        return {}

    def fetch_job_full_detail(self, source_url: str) -> Optional[dict]:
        """Optional: crawl sâu vào trang chi tiết job để lấy thêm các field
        chỉ hiển thị ở đó (không có trên trang listing) — vd 'work_type'
        (Loại hình làm việc), 'deadline_text' (Hạn ứng tuyển), và nội dung
        mô tả đầy đủ ('job_description', 'requirements', 'perks',
        'required_skills').

        Trả dict (có thể có field rỗng "" / [] nếu trang không có đủ mọi
        khối — không coi là lỗi) khi fetch THÀNH CÔNG.
        Trả None khi fetch THẤT BẠI thật sự (network error, bị chặn...)
        — pipeline dùng tín hiệu None này để quyết định bỏ hẳn job đó
        thay vì insert với dữ liệu thiếu một cách âm thầm.
        Mặc định trả dict rỗng-an-toàn (nguồn nào không hỗ trợ tính năng
        này thì coi như luôn "thành công" với dữ liệu rỗng, pipeline vẫn
        chạy bình thường, không bị hiểu nhầm là "fetch thất bại")."""
        return {
            "work_type": "", "deadline_text": "", "job_description": "",
            "requirements": "", "perks": "", "required_skills": [],
        }
