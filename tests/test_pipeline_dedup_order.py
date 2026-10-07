"""
Đặc tả THỨ TỰ các bước chống trùng của một job MỚI trong pipeline (B1).

Viết TRƯỚC khi refactor "danh sách resolver theo thứ tự" và phải đỏ-xanh y như nhau trước
và sau refactor: B1 chỉ đổi cách tổ chức code, không đổi hành vi. Các test dưới đây chốt:

  1. Thứ tự: tra mã job -> tỉnh -> cấp bậc -> công ty -> khoá advisory -> tra tin đăng lại -> insert.
  2. Hễ một bước chống trùng khớp thì các bước SAU nó không chạy (không khoá, không tra, không insert).
  3. Bước tra mã job chạy TRƯỚC khi tạo tỉnh/công ty (nhánh khớp không được tạo thừa).
  4. Khoá advisory được giành TRƯỚC câu tra tin đăng lại, với đúng khoá (công ty, tiêu đề, tỉnh).
  5. Không bước nào khớp thì khoá vẫn giữ tới lúc insert (commit sau insert).

Mock module db, không cần DB hay mạng.
"""
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pipeline
from adapters.base import BaseAdapter
from field_stats import EmptyFieldCounter
from models import RawJobRecord
from pipeline_stats import PipelineStats

URL = "https://www.vietnamworks.com/data-engineer-senior-7-jv"
DETAIL = {
    "work_type": "Toàn thời gian", "deadline_text": "05/09/2026",
    "job_description": "mô tả", "requirements": "yêu cầu", "perks": "", "required_skills": [],
}


class CodeAdapter(BaseAdapter):
    source_name = "Fake"

    def fetch_jobs(self, category_key, max_pages):
        yield from ()

    def fetch_job_full_detail(self, source_url):
        return dict(DETAIL)

    def job_code_url_regex(self, source_url):
        return "-7-jv([/?#]|$)"


class NoCodeAdapter(CodeAdapter):
    job_code_url_regex = BaseAdapter.job_code_url_regex


def _raw():
    return RawJobRecord(
        job_title="Data Engineer", company_name="ACME", source_url=URL, source_name="Fake",
        salary_text="15 - 25 triệu", province_text="Hà Nội", experience_text="3 năm",
    )


@pytest.fixture
def fake_db(monkeypatch):
    fdb = MagicMock()
    fdb.find_jobs_by_source_url_regex.return_value = []
    fdb.get_level_id.return_value = 5
    fdb.get_or_create_province.return_value = 7
    fdb.find_company_probe.return_value = None
    fdb.probe_needs_enrichment.return_value = False
    fdb.get_or_create_company_by_profile.return_value = "company-1"
    fdb.find_repost_candidate.return_value = None
    fdb.update_job_from_recrawl.return_value = True
    fdb.extend_job_deadline.return_value = False
    fdb.reopen_job_for_repost.return_value = True
    monkeypatch.setattr(pipeline, "db", fdb)
    return fdb


def _run(adapter):
    conn, stats = MagicMock(), PipelineStats()
    pipeline._import_new_job(adapter, conn, _raw(), stats, EmptyFieldCounter())
    return conn, stats


def _order(fake_db, conn=None):
    """Tên các hàm db được gọi theo thứ tự (chỉ những hàm liên quan thứ tự chống trùng)."""
    wanted = {
        "find_jobs_by_source_url_regex", "get_or_create_province", "get_level_id",
        "get_or_create_company_by_profile", "lock_job_dedup_key", "find_repost_candidate",
        "insert_job", "link_repost_source", "update_job_from_recrawl",
    }
    return [name for name, _, _ in fake_db.mock_calls if name in wanted]


def test_no_match_runs_every_step_in_order_then_inserts(fake_db):
    conn, stats = _run(CodeAdapter())

    assert _order(fake_db) == [
        "find_jobs_by_source_url_regex", "get_or_create_province", "get_level_id",
        "get_or_create_company_by_profile", "lock_job_dedup_key", "find_repost_candidate",
        "insert_job",
    ]
    assert stats.inserted == 1
    conn.commit.assert_called_once()


def test_lock_is_taken_before_repost_lookup_with_the_dedup_key(fake_db):
    conn, _ = _run(CodeAdapter())

    lock = fake_db.lock_job_dedup_key.call_args
    assert lock.args == (conn,)
    assert lock.kwargs == {"company_id": "company-1", "job_title": "Data Engineer", "province_id": 7}
    find = fake_db.find_repost_candidate.call_args
    assert find.args == (conn,)
    assert find.kwargs == {
        "company_id": "company-1", "job_title": "Data Engineer", "province_id": 7, "level_id": 5,
    }


def test_job_code_match_stops_everything_after_it(fake_db):
    fake_db.find_jobs_by_source_url_regex.return_value = [("old-1", "Data Engineer", "OPEN", None, "2026-01-01")]

    conn, stats = _run(CodeAdapter())

    assert _order(fake_db) == [
        "find_jobs_by_source_url_regex", "link_repost_source", "get_level_id", "update_job_from_recrawl",
    ]
    fake_db.get_or_create_province.assert_not_called()
    fake_db.get_or_create_company_by_profile.assert_not_called()
    fake_db.lock_job_dedup_key.assert_not_called()
    fake_db.find_repost_candidate.assert_not_called()
    fake_db.insert_job.assert_not_called()
    assert stats.updated_by_job_code == 1
    conn.commit.assert_called_once()


def test_job_code_mismatch_falls_through_to_repost_then_insert(fake_db):
    # Cùng mã nhưng tiêu đề khác hẳn: không đụng job cũ, chạy tiếp như job mới.
    fake_db.find_jobs_by_source_url_regex.return_value = [("old-1", "Kế toán trưởng", "OPEN", None, "2026-01-01")]

    _, stats = _run(CodeAdapter())

    assert _order(fake_db) == [
        "find_jobs_by_source_url_regex", "get_or_create_province", "get_level_id",
        "get_or_create_company_by_profile", "lock_job_dedup_key", "find_repost_candidate",
        "insert_job",
    ]
    assert stats.job_code_title_mismatch == 1
    assert stats.inserted == 1


def test_repost_match_stops_before_insert(fake_db):
    fake_db.find_repost_candidate.return_value = {
        "job_id": "old-2", "job_status": "OPEN", "level_id": None, "deadline": None, "closed_reason": None,
    }

    conn, stats = _run(CodeAdapter())

    assert _order(fake_db) == [
        "find_jobs_by_source_url_regex", "get_or_create_province", "get_level_id",
        "get_or_create_company_by_profile", "lock_job_dedup_key", "find_repost_candidate",
        "link_repost_source",
    ]
    fake_db.insert_job.assert_not_called()
    assert stats.skipped_duplicate_repost == 1
    assert stats.inserted == 0
    conn.commit.assert_called_once()


def test_source_without_job_code_skips_straight_to_company_and_repost(fake_db):
    _run(NoCodeAdapter())

    fake_db.find_jobs_by_source_url_regex.assert_not_called()
    assert _order(fake_db) == [
        "get_or_create_province", "get_level_id", "get_or_create_company_by_profile",
        "lock_job_dedup_key", "find_repost_candidate", "insert_job",
    ]


def test_job_code_wins_over_repost_when_both_would_match(fake_db):
    fake_db.find_jobs_by_source_url_regex.return_value = [("old-1", "Data Engineer", "OPEN", None, "2026-01-01")]
    fake_db.find_repost_candidate.return_value = {
        "job_id": "old-2", "job_status": "OPEN", "level_id": None, "deadline": None, "closed_reason": None,
    }

    _, stats = _run(CodeAdapter())

    assert stats.updated_by_job_code == 1
    assert stats.skipped_duplicate_repost == 0
    assert fake_db.link_repost_source.call_args.args[1] == "old-1"
