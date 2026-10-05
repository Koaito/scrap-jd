"""
Cùng MÃ JOB nhưng URL đổi (VietnamWorks: nhà tuyển dụng sửa tiêu đề) — pipeline
cập nhật job đã lưu thay vì tạo job trùng (pipeline._find_job_by_job_code /
_update_job_by_job_code).

Mock module db, KHÔNG cần database, KHÔNG cần internet. Phần chạy trên Postgres
thật nằm ở tests/test_pg_job_code_update.py.
"""
import logging
import os
import sys
from dataclasses import asdict
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import normalize
import pipeline
from adapters.base import BaseAdapter
from field_stats import EmptyFieldCounter
from models import RawJobRecord
from pipeline_stats import PipelineStats

NEW_URL = "https://www.vietnamworks.com/data-engineer-senior-7-jv"

DETAIL = {
    "work_type": "Toàn thời gian", "deadline_text": "05/09/2026",
    "job_description": "mô tả đầy đủ", "requirements": "yêu cầu đầy đủ", "perks": "",
    "required_skills": ["SQL"],
}


class CodeAdapter(BaseAdapter):
    """Có mã job ổn định trong URL, như VietnamWorks."""

    source_name = "Fake"

    def __init__(self, detail=None):
        super().__init__()
        self.detail = DETAIL if detail is None else detail

    def fetch_jobs(self, category_key, max_pages):
        yield from ()

    def fetch_job_full_detail(self, source_url):
        return dict(self.detail)

    def job_code_url_regex(self, source_url):
        return "-7-jv([/?#]|$)"


class NoCodeAdapter(CodeAdapter):
    """Giống TopCV/CareerViet: dùng hook mặc định của BaseAdapter (None)."""

    job_code_url_regex = BaseAdapter.job_code_url_regex


def _raw(title="Data Engineer (Senior)", **kw):
    base = dict(job_title=title, company_name="ACME", source_url=NEW_URL, source_name="Fake",
                salary_text="15 - 25 triệu", province_text="Hà Nội", experience_text="3 năm")
    base.update(kw)
    return RawJobRecord(**base)


def _row(job_id="old-1", title="Data Engineer", status="OPEN", updated_by=None):
    return (job_id, title, status, updated_by, "2026-01-01")


@pytest.fixture
def fake_db(monkeypatch):
    fdb = MagicMock()
    fdb.find_jobs_by_source_url_regex.return_value = []
    fdb.get_level_id.return_value = 5
    fdb.update_job_from_recrawl.return_value = True
    fdb.get_or_create_province.return_value = 7
    fdb.find_company_probe.return_value = None
    fdb.probe_needs_enrichment.return_value = False
    fdb.get_or_create_company_by_profile.return_value = "company-1"
    fdb.find_manual_job_duplicate.return_value = None
    monkeypatch.setattr(pipeline, "db", fdb)
    return fdb


def _run(adapter, raw=None):
    conn, stats = MagicMock(), PipelineStats()
    pipeline._import_new_job(adapter, conn, raw or _raw(), stats, EmptyFieldCounter())
    return conn, stats


# ---------------------------------------------------------------- cập nhật

def test_similar_title_updates_the_old_job_instead_of_inserting(fake_db):
    fake_db.find_jobs_by_source_url_regex.return_value = [_row()]

    conn, stats = _run(CodeAdapter())

    fake_db.insert_job.assert_not_called()
    # Nhánh này không cần tỉnh/công ty nên không được tạo thừa.
    fake_db.get_or_create_province.assert_not_called()
    fake_db.get_or_create_company_by_profile.assert_not_called()

    fake_db.find_jobs_by_source_url_regex.assert_called_once_with(
        conn, source_name="Fake", url_regex="-7-jv([/?#]|$)")

    link = fake_db.link_repost_source.call_args
    assert link.args == (conn, "old-1")
    assert link.kwargs["source_url"] == NEW_URL and link.kwargs["source_name"] == "Fake"

    update = fake_db.update_job_from_recrawl.call_args
    assert update.args == (conn, "old-1")
    kw = update.kwargs
    assert kw["job_title"] == "Data Engineer (Senior)"
    assert kw["level_id"] == 5
    # level kèm căn cứ + phiên bản quy tắc (tiêu đề có "Senior" -> căn cứ "title")
    assert kw["level_source"] == "title"
    assert kw["level_rule_version"] == normalize.LEVEL_RULE_VERSION
    fake_db.get_level_id.assert_called_once_with(
        conn, normalize.infer_level("3 năm", "Data Engineer (Senior)", ""))
    assert kw["work_type"] == normalize.normalize_work_type("Toàn thời gian")
    assert kw["parsed_content"]["job_description"] == "mô tả đầy đủ"
    assert kw["salary"] == asdict(normalize.normalize_salary("15 - 25 triệu"))

    fake_db.extend_job_deadline.assert_called_once_with(
        conn, "old-1", normalize.normalize_deadline("05/09/2026"))
    conn.commit.assert_called_once()
    assert stats.updated_by_job_code == 1
    assert stats.linked_by_job_code_only == 0 and stats.job_code_title_mismatch == 0


def test_blank_salary_text_keeps_the_old_salary(fake_db):
    """Chuỗi lương rỗng = ẩn lương hoặc không lấy được, không phân biệt được nên
    không được ghi đè lương cũ bằng "thoả thuận"."""
    fake_db.find_jobs_by_source_url_regex.return_value = [_row()]

    _run(CodeAdapter(), _raw(salary_text=""))

    assert fake_db.update_job_from_recrawl.call_args.kwargs["salary"] is None
    assert fake_db.update_job_from_recrawl.call_args.kwargs["job_title"] == "Data Engineer (Senior)"


def test_truncated_jd_does_not_overwrite_jd_or_level(fake_db):
    """JD kết thúc bằng "..." là bản bị API search cắt: không đè lên JD đầy đủ đã
    lưu, và level suy từ JD đó cũng không đáng tin. Phần lấy từ danh sách tìm kiếm
    (tiêu đề, lương, hình thức làm việc, hạn nộp) vẫn cập nhật."""
    fake_db.find_jobs_by_source_url_regex.return_value = [_row()]
    truncated = dict(DETAIL, requirements="Có kinh nghiệm SQL, Python và ...")

    conn, stats = _run(CodeAdapter(detail=truncated))

    kw = fake_db.update_job_from_recrawl.call_args.kwargs
    assert kw["parsed_content"] is None and kw["level_id"] is None
    fake_db.get_level_id.assert_not_called()
    assert kw["job_title"] == "Data Engineer (Senior)"
    assert kw["salary"] is not None and kw["work_type"] is not None
    fake_db.extend_job_deadline.assert_called_once()
    assert stats.updated_by_job_code == 1


def test_manually_edited_job_only_gets_the_new_url_linked(fake_db):
    fake_db.find_jobs_by_source_url_regex.return_value = [_row(updated_by="user-1")]

    conn, stats = _run(CodeAdapter())

    fake_db.link_repost_source.assert_called_once()
    fake_db.update_job_from_recrawl.assert_not_called()
    fake_db.extend_job_deadline.assert_not_called()
    fake_db.insert_job.assert_not_called()
    conn.commit.assert_called_once()
    assert stats.linked_by_job_code_only == 1 and stats.updated_by_job_code == 0


def test_job_changed_during_processing_counts_as_link_only(fake_db):
    """update_job_from_recrawl trả False khi job vừa bị sửa tay/đóng giữa lúc đọc và
    ghi: không đếm là đã cập nhật, không dời hạn nộp."""
    fake_db.find_jobs_by_source_url_regex.return_value = [_row()]
    fake_db.update_job_from_recrawl.return_value = False

    conn, stats = _run(CodeAdapter())

    fake_db.extend_job_deadline.assert_not_called()
    conn.commit.assert_called_once()
    assert stats.linked_by_job_code_only == 1 and stats.updated_by_job_code == 0


# ---------------------------------------------------------------- không khớp

def test_very_different_title_creates_a_new_job_and_warns(fake_db, caplog):
    fake_db.find_jobs_by_source_url_regex.return_value = [_row(title="Account Manager")]

    with caplog.at_level(logging.WARNING, logger="pipeline"):
        conn, stats = _run(CodeAdapter(), _raw(title="Sales Assistant"))

    fake_db.update_job_from_recrawl.assert_not_called()
    fake_db.link_repost_source.assert_not_called()
    fake_db.insert_job.assert_called_once()
    assert stats.job_code_title_mismatch == 1 and stats.inserted == 1
    warning = " ".join(r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    assert "Account Manager" in warning and "Sales Assistant" in warning and NEW_URL in warning


def test_closed_job_is_not_matched_and_not_counted_as_mismatch(fake_db):
    fake_db.find_jobs_by_source_url_regex.return_value = [_row(status="CLOSED")]

    _, stats = _run(CodeAdapter())

    fake_db.update_job_from_recrawl.assert_not_called()
    fake_db.insert_job.assert_called_once()
    assert stats.job_code_title_mismatch == 0 and stats.inserted == 1


def test_no_job_with_that_code_inserts_as_before(fake_db):
    _, stats = _run(CodeAdapter())

    fake_db.insert_job.assert_called_once()
    fake_db.update_job_from_recrawl.assert_not_called()
    assert stats.inserted == 1
    assert stats.job_code_title_mismatch == 0 and stats.updated_by_job_code == 0


def test_open_job_is_picked_even_when_a_closed_one_has_the_same_code(fake_db):
    fake_db.find_jobs_by_source_url_regex.return_value = [
        _row("closed-1", status="CLOSED"), _row("open-1"),
    ]

    _run(CodeAdapter())

    assert fake_db.update_job_from_recrawl.call_args.args[1] == "open-1"


# ---------------------------------------------------------------- chọn dòng

def test_picks_the_most_similar_title_even_if_it_is_newer(fake_db):
    fake_db.find_jobs_by_source_url_regex.return_value = [
        _row("older-less-similar", title="Data Analyst Engineer"),  # 2/3 từ trùng
        _row("newer-most-similar", title="Data Engineer"),          # 2/2 từ trùng
    ]

    _run(CodeAdapter(), _raw(title="Data Engineer Python Developer"))

    assert fake_db.update_job_from_recrawl.call_args.args[1] == "newer-most-similar"


def test_tie_goes_to_the_earliest_created_row(fake_db):
    # db trả cũ nhất trước; hai dòng cùng điểm thì lấy dòng đầu.
    fake_db.find_jobs_by_source_url_regex.return_value = [
        _row("first", title="Data Engineer"), _row("second", title="Data Engineer"),
    ]

    _run(CodeAdapter())

    assert fake_db.update_job_from_recrawl.call_args.args[1] == "first"


# ---------------------------------------------------------------- nguồn khác

def test_source_without_job_code_never_queries_by_code(fake_db):
    _, stats = _run(NoCodeAdapter())

    fake_db.find_jobs_by_source_url_regex.assert_not_called()
    fake_db.insert_job.assert_called_once()
    assert stats.inserted == 1


def test_adapter_that_does_not_inherit_base_adapter_still_works(fake_db):
    class PlainAdapter:
        def fetch_job_full_detail(self, source_url):
            return dict(DETAIL)

    _, stats = _run(PlainAdapter())

    fake_db.find_jobs_by_source_url_regex.assert_not_called()
    assert stats.inserted == 1


# ---------------------------------------------------------------- helper nhỏ

@pytest.mark.parametrize("parsed,expected", [
    (None, False),
    ({}, False),
    ({"job_description": "đầy đủ", "requirements": "đầy đủ."}, False),
    ({"job_description": "bị cắt...", "requirements": "đủ"}, True),
    ({"job_description": "đủ", "requirements": "bị cắt ...  "}, True),
    ({"perks": "quyền lợi..."}, False),  # chỉ xét mô tả và yêu cầu, như backfill
])
def test_jd_looks_truncated(parsed, expected):
    assert pipeline._jd_looks_truncated(parsed) is expected


def test_other_sources_have_no_job_code_hook_behaviour():
    """TopCV cấp job_id mới mỗi lần làm mới tin và CareerViet không có mã ổn định
    trong URL: dùng hook mặc định (None), nên luồng của chúng không đổi."""
    from adapters.careerviet import CareerVietAdapter
    from adapters.topcv import TopCVAdapter

    for adapter in (TopCVAdapter(), CareerVietAdapter()):
        assert adapter.job_code_url_regex("https://example.com/viec-lam/abc-123") is None
