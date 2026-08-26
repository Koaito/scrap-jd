from abc import ABC, abstractmethod
from typing import Iterator, Optional

from models import RawJobRecord


class CrawlBlockedError(Exception):
    """Raise khi TRANG ĐẦU TIÊN (page 1, hoặc page 0 với adapter đánh số
    từ 0 như VietnamWorks) của 1 lượt crawl không lấy được HTML/response
    sau khi đã hết retry (403/429/lỗi kết nối liên tục) — đây là tín hiệu
    RẤT CÓ THỂ bị chặn (WAF/rate-limit), KHÁC HẲN với "chạy xong nhưng
    đúng là hết job" (0 job mới nhưng có lấy được HTML).

    QUAN TRỌNG: chỉ raise ở TRANG ĐẦU. Các trang SAU (page >= 2/1) thất
    bại vẫn giữ nguyên hành vi cũ (break, coi là hết trang/tạm dừng bình
    thường) — không phải mọi lần fetch thất bại giữa chừng đều là bị
    chặn, có thể chỉ là đã hết dữ liệu thật.

    KHÔNG bị pipeline.run_pipeline() nuốt mất (nó raise ra ngoài vòng
    for, không nằm trong try/except bọc từng job) — lan thẳng lên
    api/crawl_runner.py::execute(), nơi bắt bằng except Exception chung
    và ghi status='error' + error message thay vì 'done' với stats toàn
    số 0, để UI (bảng Lịch sử crawl) phân biệt được 2 tình huống: "bị
    chặn ngay từ đầu" vs "chạy xong, đúng là 0 job mới"."""


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
