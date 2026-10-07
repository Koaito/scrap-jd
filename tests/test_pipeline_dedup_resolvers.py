"""
Cơ chế "danh sách resolver chống trùng theo thứ tự" (B1): adapter khai báo, pipeline chạy đúng
theo khai báo, khai báo sai thì lỗi ngay. Thứ tự các bước và hành vi từng nhánh nằm ở
tests/test_pipeline_dedup_order.py (đặc tả viết trước refactor). Mock module db.
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pipeline
from adapters.base import DEFAULT_DEDUP_RESOLVERS
from adapters.careerviet import CareerVietAdapter
from adapters.topcv import TopCVAdapter
from adapters.vietnamworks import VietnamWorksAdapter
from field_stats import EmptyFieldCounter
from pipeline_stats import PipelineStats

from test_pipeline_dedup_order import DETAIL, CodeAdapter, _order, _raw, fake_db  # noqa: F401

VNW_URL = "https://www.vietnamworks.com/data-engineer-1234567-jv"


def _adapter(declared=None, base=CodeAdapter):
    """CodeAdapter (có job_code_url_regex) khai báo `declared`; None = không khai báo gì."""
    if declared is None:
        return base()
    return type("Declaring", (base,), {"dedup_resolvers": lambda self: declared})()


def _run(adapter):
    conn, stats = MagicMock(), PipelineStats()
    pipeline._import_new_job(adapter, conn, _raw(), stats, EmptyFieldCounter())
    return conn, stats


# ------------------------------------------------------------ khai báo của adapter thật

def test_registered_resolver_names_and_stages():
    assert list(pipeline.DEDUP_RESOLVERS) == ["job_code", "repost"]
    assert pipeline.DEDUP_RESOLVERS["job_code"].stage == pipeline.STAGE_BEFORE_COMPANY
    assert pipeline.DEDUP_RESOLVERS["repost"].stage == pipeline.STAGE_AFTER_COMPANY


def test_base_default_keeps_the_pre_b1_behaviour():
    assert DEFAULT_DEDUP_RESOLVERS == ("job_code", "repost")
    assert CodeAdapter().dedup_resolvers() == ("job_code", "repost")


@pytest.mark.parametrize("cls, expected", [
    (TopCVAdapter, ("repost",)),
    (CareerVietAdapter, ("repost",)),
    (VietnamWorksAdapter, ("job_code", "repost")),
])
def test_real_adapters_declare_their_resolvers(cls, expected):
    adapter = cls()
    assert tuple(adapter.dedup_resolvers()) == expected
    # Khai báo hợp lệ: mọi tên đều đăng ký, không lặp.
    assert [r.name for r in pipeline._resolvers_for(adapter)] == list(expected)


@pytest.mark.parametrize("cls", [TopCVAdapter, CareerVietAdapter, VietnamWorksAdapter])
def test_job_code_is_declared_exactly_when_the_adapter_has_a_job_code_hook(cls):
    """Khai báo "job_code" mà adapter không có regex mã job (hoặc ngược lại) là cấu hình lệch."""
    adapter = cls()
    declares_job_code = "job_code" in adapter.dedup_resolvers()
    has_hook = adapter.job_code_url_regex(VNW_URL) is not None
    assert declares_job_code == has_hook


# ------------------------------------------------------------ _resolvers_for

def test_unknown_resolver_name_raises_value_error_naming_the_adapter():
    with pytest.raises(ValueError, match=r"Declaring.*'nope'"):
        pipeline._resolvers_for(_adapter(("job_code", "nope")))


def test_duplicate_resolver_name_raises_value_error():
    with pytest.raises(ValueError, match="lặp"):
        pipeline._resolvers_for(_adapter(("repost", "repost")))


def test_adapter_without_the_hook_uses_the_default():
    class Plain:  # không kế thừa BaseAdapter
        pass

    assert [r.name for r in pipeline._resolvers_for(Plain())] == ["job_code", "repost"]


def test_mock_adapter_falls_back_to_the_default():
    assert [r.name for r in pipeline._resolvers_for(MagicMock())] == ["job_code", "repost"]


def test_list_declaration_is_accepted():
    assert [r.name for r in pipeline._resolvers_for(_adapter(["repost"]))] == ["repost"]


# ------------------------------------------------------------ pipeline chạy đúng khai báo

def test_adapter_that_declares_only_repost_never_queries_by_job_code(fake_db):  # noqa: F811
    """Dù adapter có job_code_url_regex, không khai báo "job_code" thì không tra mã job."""
    fake_db.find_jobs_by_source_url_regex.return_value = [("old-1", "Data Engineer", "OPEN", None, "x")]

    _, stats = _run(_adapter(("repost",)))

    fake_db.find_jobs_by_source_url_regex.assert_not_called()
    assert stats.updated_by_job_code == 0
    assert stats.inserted == 1


def test_adapter_that_declares_only_job_code_skips_the_repost_lock(fake_db):  # noqa: F811
    _, stats = _run(_adapter(("job_code",)))

    fake_db.lock_job_dedup_key.assert_not_called()
    fake_db.find_repost_candidate.assert_not_called()
    assert stats.inserted == 1


def test_stage_decides_order_not_the_declared_order(fake_db):  # noqa: F811
    """Khai báo ("repost", "job_code") vẫn tra mã job TRƯỚC khi tạo tỉnh/công ty."""
    _run(_adapter(("repost", "job_code")))

    assert _order(fake_db) == [
        "find_jobs_by_source_url_regex", "get_or_create_province", "get_level_id",
        "get_or_create_company_by_profile", "lock_job_dedup_key", "find_repost_candidate",
        "insert_job",
    ]


def test_new_resolver_plugs_in_without_touching_import_new_job(fake_db, monkeypatch):  # noqa: F811
    seen = []

    def matching(ctx):
        seen.append((ctx.company_id, ctx.province_id, ctx.level_id))
        return True

    custom = pipeline._DedupResolver("custom", pipeline.STAGE_AFTER_COMPANY, matching)
    monkeypatch.setitem(pipeline.DEDUP_RESOLVERS, "custom", custom)

    _, stats = _run(_adapter(("custom", "repost")))

    # Resolver sau stage-after chạy theo thứ tự khai báo: "custom" khớp trước "repost".
    assert seen == [("company-1", 7, 5)]
    fake_db.lock_job_dedup_key.assert_not_called()
    fake_db.insert_job.assert_not_called()
    assert stats.inserted == 0


def test_first_matching_resolver_in_a_stage_short_circuits_the_rest(monkeypatch):
    calls = []
    first = pipeline._DedupResolver("a", pipeline.STAGE_AFTER_COMPANY, lambda ctx: calls.append("a") or True)
    second = pipeline._DedupResolver("b", pipeline.STAGE_AFTER_COMPANY, lambda ctx: calls.append("b") or True)

    assert pipeline._run_dedup_stage([first, second], pipeline.STAGE_AFTER_COMPANY, object()) is True
    assert calls == ["a"]
    assert pipeline._run_dedup_stage([first, second], pipeline.STAGE_BEFORE_COMPANY, object()) is False
    assert calls == ["a"]


# ------------------------------------------------------------ run_pipeline fail-fast

def test_run_pipeline_rejects_a_bad_declaration_before_fetching_anything(fake_db):  # noqa: F811
    adapter = _adapter(("nope",))
    adapter.fetch_jobs = MagicMock(return_value=iter(()))

    with pytest.raises(ValueError, match="nope"):
        pipeline.run_pipeline(adapter, MagicMock(), "cat", 1)

    adapter.fetch_jobs.assert_not_called()
