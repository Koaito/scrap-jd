"""
Thống kê của 1 lượt chạy pipeline (đợt B1, 10/2026).

Trước đây `stats` là 1 dict dựng ngay đầu run_pipeline() rồi truyền qua nhiều
hàm: gõ sai tên khoá ở 1 nhánh ít chạy thì chỉ lộ ra khi nhánh đó chạy thật,
và không có chỗ nào liệt kê đủ các khoá một cách chính thức. PipelineStats gom
tất cả vào 1 chỗ, có kiểu rõ ràng; `slots=True` khiến gán nhầm tên counter
(`stats.insterted += 1`) báo AttributeError ngay thay vì âm thầm tạo thuộc tính
mới.

Hợp đồng ra bên ngoài KHÔNG đổi: run_pipeline() vẫn trả dict, CrawlBlockedError.stats
vẫn là dict, crawl_runs.stats vẫn là cùng tập khoá JSON như trước. Chuyển đổi
chỉ xảy ra ở 1 chỗ là PipelineStats.to_dict().
"""

from dataclasses import dataclass, fields

# Khoá chỉ xuất hiện trong to_dict() khi có giá trị (khác rỗng/0/False).
_OPTIONAL_KEYS = frozenset({"skipped_detail_unavailable", "field_empty", "degraded", "blocked"})


@dataclass(slots=True)
class PipelineStats:
    # Tổng số raw record nhận từ adapter (đếm trước khi biết có insert hay không).
    fetched: int = 0
    inserted: int = 0
    # Trùng theo source_url (job đã crawl trước đó).
    skipped_duplicate: int = 0
    # Trùng theo (company_id, job_title, level_id, province_id) — KHÁC
    # skipped_duplicate ở trên. Là tin ĐĂNG LẠI dưới URL mới (vd TopCV cấp
    # job_id mới mỗi lần nhà tuyển dụng "làm mới" tin) nhưng nội dung/vị trí
    # thực chất là 1 job. Tách riêng để audit dễ, không lẫn với trùng URL.
    skipped_duplicate_repost: int = 0
    # Số tin đăng lại làm deadline của job cũ được dời ra sau.
    repost_deadline_extended: int = 0
    # Job cũ được vá thêm work_type/deadline/parsed_content.
    updated_existing: int = 0
    skipped_fetch_failed: int = 0
    errors: int = 0
    # Job có company_name khớp normalize.is_anonymous_employer_name() (vd
    # "Vietnamworks' Client"), bỏ hẳn không insert.
    skipped_anonymous_employer: int = 0
    # URL đã có trong DB và đủ field nên adapter bỏ qua từ trước khi fetch chi
    # tiết. Điền cuối lượt từ adapter.skipped_known_count (xem finalize).
    skipped_known_url: int = 0

    # Job adapter tự bỏ TRONG fetch_jobs() vì không tải/giải mã được trang chi
    # tiết (CareerViet, VietnamWorks). Không tới pipeline nên không nằm trong
    # fetched/skipped_fetch_failed. Điền cuối lượt từ
    # adapter.skipped_detail_unavailable_count. Giống 3 mục dưới: CHỈ xuất hiện
    # trong to_dict() khi > 0 (giữ nguyên tập khoá cũ của các lượt bình thường).
    skipped_detail_unavailable: int = 0

    # Mục dưới CHỈ xuất hiện trong to_dict() khi có giá trị (giống hành vi cũ:
    # dict không có khoá này nếu không có gì để báo), để frontend phân biệt
    # "không có" với "có nhưng rỗng".
    field_empty: dict | None = None  # tỷ lệ trường rỗng, xem field_stats.py
    degraded: dict | None = None  # {"reasons": [...]}, xem pipeline._finalize_stats
    blocked: bool = False  # bị chặn (trang đầu thất bại hoặc ngắt mạch)

    def progress(self) -> dict:
        """Dict nhỏ cho heartbeat on_progress. Mỗi lần gọi trả bản mới, người
        nhận sửa thoải mái cũng không ảnh hưởng stats."""
        return {"fetched": self.fetched, "inserted": self.inserted}

    def to_dict(self) -> dict:
        """Dict JSON-serializable, cùng tập khoá với stats dict trước đây."""
        data = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if f.name in _OPTIONAL_KEYS and not value:
                continue
            data[f.name] = value
        return data
