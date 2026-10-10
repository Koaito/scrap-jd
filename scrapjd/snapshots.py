"""
Ghi nhận snapshot HTML/JSON GỐC của 1 lượt crawl để debug khi parser hỏng
(đợt 3, 10/2026).

Vì sao cần: khi site đổi giao diện, adapter thường KHÔNG báo lỗi — chỉ trả
0 record hoặc field rỗng hàng loạt. Không có HTML gốc của đúng thời điểm đó
thì không thể biết selector hỏng ở đâu, cũng không thể thay fixture tổng hợp
bằng fixture thật (xem tests/fixture_*.html ghi rõ là dữ liệu dựng tay).

Cách hoạt động:
  - Adapter gọi BaseAdapter._snapshot(kind, url, body, reason) tại các điểm
    đáng giữ. Nếu không có recorder (CLI main.py, test) thì lệnh đó là no-op.
  - scrapjd/api/crawl_runner.py::_execute_one() gắn 1 SnapshotRecorder vào adapter
    trước khi chạy, và SAU KHI chạy xong (thành công hay lỗi) lưu các mục
    đã giữ xuống bảng crawl_snapshots (scrapjd/db/crawl_snapshots.py).
  - Recorder chỉ giữ trong RAM nên không thêm round-trip DB nào trong lúc
    crawl; đổi lại nếu process bị kill cứng thì snapshot của lượt đó mất.

Chính sách giữ (để chặn dung lượng):
  - reason="sample": MỖI loại (kind) chỉ giữ mẫu ĐẦU TIÊN của lượt — vd trang
    listing đầu và trang chi tiết đầu, đủ làm fixture thật.
  - reason khác (anomaly, vd "listing_empty", "detail_blank"): mỗi cặp
    (kind, reason) giữ tối đa MAX_PER_ANOMALY mục — đây là thứ đáng giữ nhất
    vì chính là trang đã làm parser trả rỗng.
  - Tổng không quá max_items mục/lượt; body cắt ở max_chars ký tự.
"""

import logging
from dataclasses import dataclass
from typing import Any, List, Optional

from scrapjd.config import SNAPSHOT_MAX_CHARS, SNAPSHOT_MAX_PER_RUN

logger = logging.getLogger(__name__)

SAMPLE_REASON = "sample"
MAX_PER_ANOMALY = 2


@dataclass
class Snapshot:
    kind: str       # "listing" | "detail" | "company" ...
    reason: str     # "sample" hoặc mã bất thường
    url: str
    body: str       # HTML hoặc JSON dạng text, đã cắt theo max_chars
    truncated: bool = False


class SnapshotRecorder:
    """1 instance cho 1 lượt crawl. Không thread-safe (pipeline chạy tuần tự)."""

    def __init__(self, max_items: Optional[int] = None, max_chars: Optional[int] = None):
        self.max_items = SNAPSHOT_MAX_PER_RUN if max_items is None else max_items
        self.max_chars = SNAPSHOT_MAX_CHARS if max_chars is None else max_chars
        self.items: List[Snapshot] = []
        self._sampled_kinds: set = set()
        self._anomaly_counts: dict = {}

    def offer(self, kind: str, url: str, body: Any, reason: str = SAMPLE_REASON) -> bool:
        """Đề nghị giữ 1 snapshot. Trả True nếu được giữ. Không bao giờ raise."""
        try:
            if not body or not isinstance(body, str):
                return False
            if len(self.items) >= self.max_items:
                return False

            if reason == SAMPLE_REASON:
                if kind in self._sampled_kinds:
                    return False
                self._sampled_kinds.add(kind)
            else:
                key = (kind, reason)
                if self._anomaly_counts.get(key, 0) >= MAX_PER_ANOMALY:
                    return False
                self._anomaly_counts[key] = self._anomaly_counts.get(key, 0) + 1

            truncated = len(body) > self.max_chars
            self.items.append(Snapshot(
                kind=kind, reason=reason, url=url or "",
                body=body[: self.max_chars] if truncated else body,
                truncated=truncated,
            ))
            return True
        except Exception:  # noqa: BLE001 - ghi snapshot không được làm hỏng crawl
            logger.exception("SnapshotRecorder.offer lỗi, bỏ qua")
            return False
