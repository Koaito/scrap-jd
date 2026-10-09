"""
Snapshot HTML/JSON gốc (đợt 3, 10/2026) — KHÔNG cần database, KHÔNG cần internet.
Phủ: SnapshotRecorder (chính sách giữ), hook trong 3 adapter, db.save_snapshots
(không bao giờ raise, nén gzip, dọn bản cũ).
"""

import gzip
import json
import os
import sys
from unittest.mock import MagicMock
from urllib.parse import urljoin

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scrapjd.adapters.base import BaseAdapter
from scrapjd.adapters.careerviet import CareerVietAdapter
from scrapjd.adapters.topcv import TopCVAdapter
from scrapjd.adapters.vietnamworks import VietnamWorksAdapter
from scrapjd.db.crawl_snapshots import save_snapshots
from scrapjd.snapshots import MAX_PER_ANOMALY, SnapshotRecorder
from vnw_page_builder import build_detail_html

HERE = os.path.dirname(__file__)


def _read(name):
    with open(os.path.join(HERE, name), encoding="utf-8") as f:
        return f.read()


# ----------------------------------------------------------------------
# SnapshotRecorder
# ----------------------------------------------------------------------
def test_sample_kept_once_per_kind():
    rec = SnapshotRecorder(max_items=10)
    assert rec.offer("listing", "u1", "<html>1</html>") is True
    assert rec.offer("listing", "u2", "<html>2</html>") is False   # kind đã có mẫu
    assert rec.offer("detail", "u3", "<html>3</html>") is True
    assert [i.url for i in rec.items] == ["u1", "u3"]


def test_anomaly_capped_per_kind_and_reason():
    rec = SnapshotRecorder(max_items=50)
    kept = [rec.offer("detail", f"u{i}", "<html/>", reason="detail_blank") for i in range(5)]
    assert kept.count(True) == MAX_PER_ANOMALY
    # reason khác vẫn được giữ
    assert rec.offer("detail", "x", "<html/>", reason="detail_unparsable") is True


def test_total_cap_per_run():
    rec = SnapshotRecorder(max_items=2)
    assert rec.offer("listing", "a", "x")
    assert rec.offer("detail", "b", "x")
    assert rec.offer("detail", "c", "x", reason="detail_blank") is False
    assert len(rec.items) == 2


def test_empty_or_non_string_body_ignored():
    rec = SnapshotRecorder()
    assert rec.offer("listing", "u", "") is False
    assert rec.offer("listing", "u", None) is False
    assert rec.offer("listing", "u", b"bytes") is False
    assert rec.items == []


def test_long_body_truncated_and_flagged():
    rec = SnapshotRecorder(max_chars=1000)
    rec.offer("listing", "u", "a" * 5000)
    item = rec.items[0]
    assert len(item.body) == 1000 and item.truncated is True


# ----------------------------------------------------------------------
# BaseAdapter._snapshot / _detail_is_blank
# ----------------------------------------------------------------------
class Dummy(BaseAdapter):
    source_name = "Dummy"

    def fetch_jobs(self, category_key, max_pages):
        return iter(())


def test_snapshot_is_noop_without_recorder():
    Dummy()._snapshot("listing", "u", "<html/>")   # không raise


def test_snapshot_swallows_recorder_errors():
    adapter = Dummy()
    bad = MagicMock()
    bad.offer.side_effect = RuntimeError("boom")
    adapter.set_snapshot_recorder(bad)
    adapter._snapshot("listing", "u", "<html/>")   # không raise


@pytest.mark.parametrize("detail, blank", [
    (None, True),
    ({}, True),
    ({"job_description": "  ", "requirements": "", "perks": "", "required_skills": []}, True),
    ({"job_description": "có nội dung", "requirements": "", "perks": ""}, False),
    ({"job_description": "", "requirements": "", "perks": "", "required_skills": ["SQL"]}, False),
])
def test_detail_is_blank(detail, blank):
    assert BaseAdapter._detail_is_blank(detail) is blank


# ----------------------------------------------------------------------
# Hook trong adapter
# ----------------------------------------------------------------------
def _stub_fetch(adapter, mapping):
    adapter._fetch_html = lambda url, max_retries=3: mapping.get(url)


def test_careerviet_snapshots_first_listing_and_detail_sample():
    listing = _read("fixture_careerviet_listing.html")
    detail = _read("fixture_careerviet_job_detail.html")
    adapter = CareerVietAdapter()
    rec = SnapshotRecorder()
    adapter.set_snapshot_recorder(rec)

    urls = CareerVietAdapter._extract_job_detail_urls(listing)
    search = "https://careerviet.vn/viec-lam/data-analyst-k-vi.html"
    mapping = {search: listing, **{u: detail for u in urls}}
    _stub_fetch(adapter, mapping)

    records = list(adapter.fetch_jobs("data-analyst", 1))
    assert records
    kinds = [(i.kind, i.reason) for i in rec.items]
    assert ("listing", "sample") in kinds
    assert ("detail", "sample") in kinds
    assert adapter.listing_anomalies == []


def test_careerviet_listing_with_no_job_urls_flags_anomaly():
    adapter = CareerVietAdapter()
    rec = SnapshotRecorder()
    adapter.set_snapshot_recorder(rec)
    search = "https://careerviet.vn/viec-lam/data-analyst-k-vi.html"
    _stub_fetch(adapter, {search: "<html><body>Trang challenge</body></html>"})

    assert list(adapter.fetch_jobs("data-analyst", 1)) == []
    assert adapter.listing_anomalies == ["first_page_no_jobs"]
    assert [(i.kind, i.reason) for i in rec.items] == [("listing", "listing_empty")]


def test_careerviet_blank_detail_page_snapshotted_as_anomaly():
    adapter = CareerVietAdapter()
    rec = SnapshotRecorder()
    adapter.set_snapshot_recorder(rec)
    adapter._snapshot_detail("u", "<html>x</html>", None)
    assert [(i.kind, i.reason) for i in rec.items] == [("detail", "detail_unparsable")]


def test_topcv_empty_first_page_flags_anomaly_and_snapshots():
    adapter = TopCVAdapter()
    rec = SnapshotRecorder()
    adapter.set_snapshot_recorder(rec)
    _stub_fetch_all = lambda url, max_retries=3: "<html><body>không có job</body></html>"
    adapter._fetch_html = _stub_fetch_all

    assert list(adapter.fetch_jobs("data-analyst", 1)) == []
    assert adapter.listing_anomalies == ["first_page_no_jobs"]
    assert [i.reason for i in rec.items] == ["listing_empty"]


def test_vietnamworks_empty_first_page_flags_anomaly_with_json_snapshot():
    adapter = VietnamWorksAdapter()
    rec = SnapshotRecorder()
    adapter.set_snapshot_recorder(rec)
    adapter._post_json = lambda url, body, max_retries=3: {"meta": {"nbPages": 0}, "data": []}

    assert list(adapter.fetch_jobs("data-analyst", 2)) == []
    assert adapter.listing_anomalies == ["first_page_no_jobs"]
    assert rec.items[0].reason == "listing_empty"
    assert json.loads(rec.items[0].body)["meta"]["nbPages"] == 0


def test_vietnamworks_first_page_sample_is_json():
    adapter = VietnamWorksAdapter()
    rec = SnapshotRecorder()
    adapter.set_snapshot_recorder(rec)
    payload = json.loads(_read("fixture_vietnamworks_search.json"))
    adapter._post_json = lambda url, body, max_retries=3: payload
    # Từ 10/2026 mỗi job mới còn tải trang chi tiết (giả lập, không đụng mạng).
    pages = {}
    for j in payload["data"]:
        pages.setdefault(urljoin("https://www.vietnamworks.com", j["jobUrl"]), build_detail_html(j))
    adapter._fetch_html = lambda url, max_retries=3: pages[url]

    assert list(adapter.fetch_jobs("data-analyst", 1))
    assert [(i.kind, i.reason) for i in rec.items] == [("listing", "sample"), ("detail", "sample")]
    json.loads(rec.items[0].body)   # snapshot listing là JSON hợp lệ


# ----------------------------------------------------------------------
# db.save_snapshots
# ----------------------------------------------------------------------
def _fake_conn():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


def test_save_snapshots_gzips_body_and_cleans_old_rows():
    rec = SnapshotRecorder()
    rec.offer("listing", "https://x/1", "<html>xin chào</html>")
    conn, cur = _fake_conn()

    saved = save_snapshots(conn, "run-1", "topcv", rec.items, retention_days=7)

    assert saved == 1
    insert_args = cur.execute.call_args_list[0].args[1]
    assert insert_args[0:5] == ("run-1", "topcv", "listing", "sample", "https://x/1")
    # psycopg2.Binary(...) thật là 1 object bọc bytes (thuộc tính .adapted),
    # KHÔNG phải bytes — lấy .adapted ra rồi mới giải nén.
    payload = getattr(insert_args[5], "adapted", insert_args[5])
    assert gzip.decompress(payload).decode("utf-8") == "<html>xin chào</html>"
    assert insert_args[6] == len("<html>xin chào</html>".encode("utf-8"))
    delete_sql, delete_params = cur.execute.call_args_list[1].args
    assert "DELETE FROM crawl_snapshots" in delete_sql and delete_params == (7,)
    conn.commit.assert_called_once()


def test_save_snapshots_nothing_to_save_touches_no_db():
    conn, cur = _fake_conn()
    assert save_snapshots(conn, "run-1", "topcv", []) == 0
    conn.cursor.assert_not_called()


def test_save_snapshots_never_raises_and_rolls_back():
    """Migration chưa chạy / DB lỗi: lưu snapshot (chỉ để debug) không được
    làm đổi kết quả lượt crawl."""
    rec = SnapshotRecorder()
    rec.offer("listing", "u", "<html/>")
    conn, cur = _fake_conn()
    cur.execute.side_effect = RuntimeError('relation "crawl_snapshots" does not exist')

    assert save_snapshots(conn, "run-1", "topcv", rec.items) == 0
    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()
