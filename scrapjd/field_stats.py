"""
Thống kê TỶ LỆ TRƯỜNG RỖNG của dữ liệu adapter trả về (đợt 2, 10/2026).

Vì sao cần: selector/JSON-LD của từng nguồn hỏng âm thầm rất khó thấy —
adapter vẫn trả record, pipeline vẫn insert, chỉ có 1 field (vd deadline,
experience) bắt đầu rỗng hàng loạt sau khi site đổi giao diện. Docstring
scrapjd/adapters/careerviet.py còn ghi "CHƯA XÁC NHẬN: có phải MỌI job đều có đủ
validThrough/monthsOfExperience" và cần "audit sau khi crawl thật vài trăm
job" — module này cho con số đó ngay trong stats của từng lượt crawl, không
cần chạy script audit riêng.

Đo ở 2 nhóm, mỗi nhóm có mẫu số riêng:
  - "listing": mọi RawJobRecord pipeline nhận được từ adapter.
  - "detail" : mọi dict fetch_job_full_detail() trả về thành công (job mới
    và job cũ đang được vá). Fetch thất bại (None) KHÔNG tính vào đây — đó
    là lỗi mạng/bị chặn, đã có stats["skipped_fetch_failed"].

"Rỗng" = None, chuỗi chỉ có khoảng trắng, hoặc list/tuple/set/dict rỗng.
Số 0 và False KHÔNG tính là rỗng.

Kết quả chỉ mang tính QUAN SÁT: không đổi hành vi crawl, không chặn insert.
Một số nguồn rỗng ở vài field là bình thường (vd job không ghi lương) nên
con số cần đọc theo từng nguồn, không có ngưỡng đúng chung cho mọi nguồn;
WARN_RATE chỉ để đánh dấu "đáng nhìn lại" trong log.
"""

import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# Field đo ở nhóm "listing" (RawJobRecord). Cố ý KHÔNG đo work_type_text/
# deadline_text ở đây: nhiều nguồn (vd TopCV) chỉ có 2 field này ở trang
# chi tiết nên luôn rỗng ở listing, sẽ làm nhiễu số liệu — chúng được đo ở
# nhóm "detail" bên dưới.
LISTING_FIELDS = (
    "job_title", "company_name", "salary_text", "province_text",
    "experience_text", "posted_text", "company_url", "raw_tags",
)

# Field đo ở nhóm "detail" (dict của fetch_job_full_detail()).
DETAIL_FIELDS = (
    "work_type", "deadline_text", "job_description", "requirements",
    "perks", "required_skills",
)

# Tỷ lệ rỗng từ mức này trở lên (và đủ mẫu, xem WARN_MIN_SAMPLES) thì log
# WARNING để người xem log live để ý.
WARN_RATE = 0.5
WARN_MIN_SAMPLES = 10

# Field BẮT BUỘC phải có nội dung (đợt 3, 10/2026): 1 tin tuyển dụng thật luôn
# có tiêu đề + tên công ty + nội dung mô tả. Nếu những field này rỗng gần hết
# thì gần như chắc chắn selector/cấu trúc trang đã đổi, KHÁC các field rỗng
# hợp lệ (job không ghi lương, không có phúc lợi...) nên cố ý không đưa
# salary_text/perks/... vào đây. Dùng cho degraded_reasons().
CRITICAL_FIELDS = {
    "listing": ("job_title", "company_name"),
    "detail": ("job_description",),
}

# Giá trị placeholder adapter tự điền khi không tìm thấy dữ liệu thật — tính
# như RỖNG để thống kê không bị che. Vd TopCV điền "Chưa xác định" khi không
# bắt được tên công ty (scrapjd/adapters/topcv.py), nếu không loại trừ thì
# company_name không bao giờ hiện là rỗng dù parser đã hỏng.
PLACEHOLDER_VALUES = frozenset({"chưa xác định"})


def is_empty(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        text = value.strip()
        return not text or text.lower() in PLACEHOLDER_VALUES
    if isinstance(value, (list, tuple, set, dict)):
        return len(value) == 0
    return False


def _read(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


class EmptyFieldCounter:
    """Đếm số lần mỗi field bị rỗng, theo nhóm. Dùng 1 instance cho 1 lượt
    run_pipeline(); không thread-safe (pipeline chạy tuần tự)."""

    def __init__(self) -> None:
        # group -> {"total": int, "empty": {field: int}}
        self._groups: dict = {}

    def record(self, group: str, obj: Any, fields: Iterable[str]) -> None:
        data = self._groups.setdefault(group, {"total": 0, "empty": {}})
        data["total"] += 1
        for name in fields:
            counts = data["empty"]
            counts.setdefault(name, 0)
            if is_empty(_read(obj, name)):
                counts[name] += 1

    def record_listing(self, raw: Any) -> None:
        self.record("listing", raw, LISTING_FIELDS)

    def record_detail(self, detail: Optional[dict]) -> None:
        """detail=None (fetch thất bại) bị bỏ qua có chủ đích, xem docstring
        module."""
        if detail is None:
            return
        self.record("detail", detail, DETAIL_FIELDS)

    def summary(self) -> dict:
        """JSON-serializable, gọn, để nhét thẳng vào crawl_runs.stats:

            {"listing": {"total": 40,
                         "fields": {"salary_text": {"empty": 12, "rate": 0.3}, ...}},
             "detail":  {...}}

        Trả {} nếu chưa ghi nhận record nào (nhóm không có mẫu bị bỏ qua)."""
        result = {}
        for group, data in self._groups.items():
            total = data["total"]
            if total <= 0:
                continue
            result[group] = {
                "total": total,
                "fields": {
                    name: {"empty": empty, "rate": round(empty / total, 3)}
                    for name, empty in data["empty"].items()
                },
            }
        return result

    def log_summary(self) -> None:
        """Ghi 1 dòng INFO mỗi nhóm + WARNING cho field rỗng nhiều. Đi qua
        logger chuẩn nên tự xuất hiện trong khu "Xem log live" ở /crawl."""
        for group, info in self.summary().items():
            total = info["total"]
            parts = [
                f"{name}={f['empty']}/{total}"
                for name, f in info["fields"].items() if f["empty"]
            ]
            logger.info(
                "Trường rỗng (%s, %d record): %s",
                group, total, ", ".join(parts) if parts else "không có",
            )
            if total < WARN_MIN_SAMPLES:
                continue
            for name, f in info["fields"].items():
                if f["rate"] >= WARN_RATE:
                    logger.warning(
                        "Trường '%s' (%s) rỗng %d/%d (%.0f%%) — nếu bất thường "
                        "so với các lần crawl trước, kiểm tra lại selector/"
                        "cấu trúc trang của nguồn này.",
                        name, group, f["empty"], total, f["rate"] * 100,
                    )


def degraded_reasons(summary: dict, *, rate_threshold: float, min_samples: int = WARN_MIN_SAMPLES) -> list:
    """Danh sách field BẮT BUỘC (CRITICAL_FIELDS) có tỷ lệ rỗng >= rate_threshold
    trong summary() — rỗng nghĩa là lượt crawl vẫn 'done' nhưng dữ liệu nhiều
    khả năng sai (selector hỏng âm thầm), cần người xem lại.

    Chỉ xét nhóm đủ min_samples record: mẫu quá nhỏ (vd crawl thử 3 job) làm
    tỷ lệ vô nghĩa và dễ báo nhầm. Mỗi phần tử:
        {"type": "field_empty", "group": "detail", "field": "job_description",
         "empty": 38, "total": 40, "rate": 0.95}
    """
    reasons = []
    for group, fields in CRITICAL_FIELDS.items():
        info = summary.get(group)
        if not info or info["total"] < min_samples:
            continue
        for name in fields:
            f = info["fields"].get(name)
            if f is None:
                continue
            if f["rate"] >= rate_threshold:
                reasons.append({
                    "type": "field_empty", "group": group, "field": name,
                    "empty": f["empty"], "total": info["total"], "rate": f["rate"],
                })
    return reasons
