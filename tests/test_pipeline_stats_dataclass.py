"""
Đợt B1: PipelineStats (pipeline_stats.py) thay dict `stats` trong pipeline.py.

Mục tiêu chính là CHỨNG MINH KHÔNG ĐỔI HÀNH VI: dict run_pipeline() trả về và
exc.stats vẫn đúng tập khoá/giá trị như bản dùng dict (giá trị mong đợi bên
dưới lấy bằng cách chạy bản pipeline.py cũ trên cùng kịch bản). Không cần DB,
không cần internet.
"""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db as real_db
import pipeline
from adapters.base import CrawlBlockedError
from pipeline_stats import PipelineStats
from test_pipeline_blocked_degraded import BLANK_DETAIL, GOOD_DETAIL, ScriptedAdapter, _raw

# Tập khoá của stats dict TRƯỚC khi có PipelineStats. Thêm counter mới thì phải
# sửa cả chỗ này (cố ý: frontend/crawl_runs.stats đọc theo các khoá này).
COUNTER_KEYS = {
    "fetched", "inserted", "skipped_duplicate", "skipped_duplicate_repost",
    "repost_deadline_extended", "updated_existing", "skipped_fetch_failed",
    "errors", "skipped_anonymous_employer", "skipped_known_url",
}


# ----------------------------------------------------------------------
# Bản thân dataclass
# ----------------------------------------------------------------------
def test_repost_reopen_counters_are_optional_keys_only_present_when_positive():
    # Hợp đồng ra ngoài: lượt chạy bình thường giữ nguyên tập khoá cũ (COUNTER_KEYS).
    assert "repost_reopened" not in PipelineStats().to_dict()
    assert "repost_kept_closed" not in PipelineStats().to_dict()
    data = PipelineStats(repost_reopened=2, repost_kept_closed=1).to_dict()
    assert data["repost_reopened"] == 2 and data["repost_kept_closed"] == 1


def test_default_to_dict_has_exactly_the_counter_keys_all_zero():
    assert PipelineStats().to_dict() == {key: 0 for key in COUNTER_KEYS}


def test_optional_keys_only_present_when_set():
    stats = PipelineStats()
    stats.field_empty = {"listing": {"total": 1}}
    stats.degraded = {"reasons": [{"type": "x"}]}
    stats.blocked = True
    data = stats.to_dict()
    assert set(data) == COUNTER_KEYS | {"field_empty", "degraded", "blocked"}
    assert data["blocked"] is True
    assert data["degraded"] == {"reasons": [{"type": "x"}]}


def test_job_code_counters_only_present_when_nonzero():
    """3 bộ đếm theo mã job là khoá tuỳ chọn: lượt crawl bình thường giữ nguyên tập
    khoá cũ (frontend đọc theo các khoá đó)."""
    keys = {"updated_by_job_code", "linked_by_job_code_only", "job_code_title_mismatch"}
    assert not keys & set(PipelineStats().to_dict())

    data = PipelineStats(updated_by_job_code=2, job_code_title_mismatch=1).to_dict()
    assert data["updated_by_job_code"] == 2
    assert data["job_code_title_mismatch"] == 1
    assert "linked_by_job_code_only" not in data


def test_empty_optional_values_are_omitted_like_the_old_dict():
    stats = PipelineStats(field_empty={}, degraded=None, blocked=False)
    assert set(stats.to_dict()) == COUNTER_KEYS


def test_typo_in_counter_name_raises_instead_of_creating_new_attribute():
    stats = PipelineStats()
    with pytest.raises(AttributeError):
        stats.insterted += 1  # noqa: B018 - cố ý gõ sai


def test_progress_returns_fresh_small_dict():
    stats = PipelineStats(fetched=5, inserted=2, errors=9)
    progress = stats.progress()
    assert progress == {"fetched": 5, "inserted": 2}
    progress["fetched"] = 999
    assert stats.fetched == 5


def test_to_dict_is_json_serializable():
    json.dumps(PipelineStats(fetched=1, field_empty={"a": 1}).to_dict())


# ----------------------------------------------------------------------
# Hành vi của run_pipeline() trên kịch bản chạm mọi counter
# ----------------------------------------------------------------------
@pytest.fixture
def scenario_db(monkeypatch):
    fdb = MagicMock()
    probes = {
        "u-1": ("job-1", "Toàn thời gian", "2026-09-05", {"a": 1}, None),  # đã đủ field
        "u-2": ("job-2", None, None, None, None),  # thiếu field -> vá
    }
    fdb.get_job_probe_by_source_url.side_effect = lambda conn, url: probes.get(url)
    fdb.job_needs_detail_enrichment.side_effect = real_db.job_needs_detail_enrichment
    fdb.find_company_probe.return_value = None
    fdb.probe_needs_enrichment.return_value = False
    fdb.get_or_create_company_by_profile.return_value = "company-1"
    fdb.find_repost_candidate.side_effect = (
        lambda conn, **kw: {"job_id": "dup-job", "job_status": "OPEN", "level_id": None,
                            "deadline": None, "closed_reason": None}
        if kw["job_title"] == "Job 4" else None
    )
    fdb.extend_job_deadline.return_value = True

    def _insert(*args, **kwargs):
        if kwargs["job_title"] == "Job 6":
            raise RuntimeError("boom")

    fdb.insert_job.side_effect = _insert
    monkeypatch.setattr(pipeline, "db", fdb)
    return fdb


def _detail_for(i):
    return {3: None, 5: BLANK_DETAIL}.get(i, GOOD_DETAIL)


def _raw_for(i):
    if i == 7:
        return _raw(i, company_name="Vietnamworks' Client")  # nhà tuyển dụng ẩn danh
    return _raw(i)


def test_full_scenario_counters_match_the_old_dict_behaviour(scenario_db):
    """9 job: u-1 đủ field (trùng URL), u-2 vá được, Job 3 fetch lỗi, Job 4 đăng
    lại, Job 5 detail rỗng, Job 6 lỗi insert, Job 7 ẩn danh, còn lại insert."""
    progress_calls = []
    stats = pipeline.run_pipeline(
        ScriptedAdapter(9, detail_for=_detail_for, raw_for=_raw_for),
        MagicMock(), "data-analyst", 3, on_progress=progress_calls.append,
    )

    assert type(stats) is dict
    counters = {k: v for k, v in stats.items() if k in COUNTER_KEYS}
    assert set(counters) == COUNTER_KEYS
    assert counters == {
        "fetched": 9, "inserted": 3, "skipped_duplicate": 2,
        "skipped_duplicate_repost": 1, "repost_deadline_extended": 1,
        "updated_existing": 1, "skipped_fetch_failed": 1, "errors": 1,
        "skipped_anonymous_employer": 1, "skipped_known_url": 0,
    }
    assert "blocked" not in stats
    assert "field_empty" in stats  # đã ghi nhận record nên có thống kê trường rỗng
    json.dumps(stats)  # ghi được vào cột JSONB crawl_runs.stats

    # Heartbeat sau mỗi job (9 lần), mỗi lần là dict độc lập 2 khoá.
    assert len(progress_calls) == 9
    assert all(set(p) == {"fetched", "inserted"} for p in progress_calls)
    assert progress_calls[-1] == {"fetched": 9, "inserted": 3}


def test_blocked_run_attaches_plain_dict_with_blocked_flag(scenario_db):
    blocked = CrawlBlockedError("bị chặn")
    adapter = ScriptedAdapter(
        9, detail_for=lambda i: blocked if i == 5 else _detail_for(i), raw_for=_raw_for,
    )
    with pytest.raises(CrawlBlockedError) as exc_info:
        pipeline.run_pipeline(adapter, MagicMock(), "data-analyst", 3)

    stats = exc_info.value.stats
    assert type(stats) is dict
    assert stats["blocked"] is True
    assert {k: stats[k] for k in COUNTER_KEYS} == {
        "fetched": 6, "inserted": 1, "skipped_duplicate": 2,
        "skipped_duplicate_repost": 1, "repost_deadline_extended": 1,
        "updated_existing": 1, "skipped_fetch_failed": 1, "errors": 0,
        "skipped_anonymous_employer": 0, "skipped_known_url": 0,
    }
    json.dumps(stats)


def test_max_jobs_limits_fetched_counter(scenario_db):
    stats = pipeline.run_pipeline(
        ScriptedAdapter(9, detail_for=_detail_for, raw_for=_raw_for),
        MagicMock(), "data-analyst", 3, max_jobs=4,
    )
    assert stats["fetched"] == 4
