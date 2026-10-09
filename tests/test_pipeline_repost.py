"""
Nhánh "tin đăng lại dưới URL khác" của pipeline.run_pipeline() (10/2026):
trùng company/title/level/province với job đã có -> KHÔNG insert job mới,
nhưng GHI source_url mới làm nguồn phụ (db.link_repost_source) và commit.

Trước đây nhánh này không có test nào, và URL bị bỏ mà không ghi gì nên mỗi
lượt crawl sau lại fetch chi tiết rồi bỏ lại. Mock module db, không cần DB hay
mạng. Bản chạy trên Postgres thật nằm ở tests/test_pg_repost_link.py.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pipeline
from adapters.base import BaseAdapter
from db.job_recrawl import RepostLink
from scrapjd.models import RawJobRecord

DETAIL = {
    "work_type": "Toàn thời gian", "deadline_text": "05/09/2026",
    "job_description": "mô tả công việc", "requirements": "", "perks": "",
    "required_skills": [],
}


def _candidate(job_id, status="OPEN", closed_reason=None):
    """Kết quả db.find_repost_candidate() cho test (mock db). Job CLOSED mặc định do hết hạn tự đóng."""
    if status == "CLOSED" and closed_reason is None:
        closed_reason = "expired_auto"
    return {"job_id": job_id, "job_status": status, "level_id": None, "deadline": None,
            "closed_reason": closed_reason}


class FakeAdapter(BaseAdapter):
    source_name = "Fake"

    def __init__(self, urls):
        super().__init__()
        self.urls = urls

    def fetch_jobs(self, category_key, max_pages):
        for url in self.urls:
            yield RawJobRecord(
                job_title="Data Analyst", company_name="ACME", source_url=url,
                source_name="Fake", salary_text="15-20 triệu",
                province_text="Hà Nội", experience_text="2 năm",
            )

    def fetch_job_full_detail(self, source_url):
        return dict(DETAIL)



def test_repost_links_source_to_existing_job_and_commits(pipeline_db):
    pipeline_db.find_repost_candidate.return_value = _candidate("job-orig")
    conn = MagicMock()

    stats = pipeline.run_pipeline(FakeAdapter(["https://x/new-url"]), conn, "data-analyst", 1)

    assert stats["skipped_duplicate_repost"] == 1
    assert stats["inserted"] == 0
    pipeline_db.insert_job.assert_not_called()

    pipeline_db.link_repost_source.assert_called_once()
    args, kwargs = pipeline_db.link_repost_source.call_args
    assert args == (conn, "job-orig")
    assert kwargs["source_name"] == "Fake"
    assert kwargs["source_url"] == "https://x/new-url"
    assert "mô tả công việc" in kwargs["raw_jd_content"]  # giữ bằng chứng gốc
    assert kwargs["salary_raw_text"] == "15-20 triệu"
    # hạn của chính tin đăng lại đi vào listing (C1), không chỉ vào job
    from datetime import date
    assert kwargs["deadline"] == date(2026, 9, 5)
    conn.commit.assert_called()


def test_non_duplicate_does_not_link(pipeline_db):
    stats = pipeline.run_pipeline(FakeAdapter(["https://x/u1"]), MagicMock(), "data-analyst", 1)

    assert stats["inserted"] == 1
    pipeline_db.link_repost_source.assert_not_called()


def test_link_failure_counts_error_rolls_back_and_continues(pipeline_db):
    # Tin 1 là đăng lại nhưng ghi nguồn phụ lỗi; tin 2 là tin mới bình thường.
    pipeline_db.find_repost_candidate.side_effect = [_candidate("job-orig"), None]
    pipeline_db.link_repost_source.side_effect = RuntimeError("DB mất kết nối")
    conn = MagicMock()

    stats = pipeline.run_pipeline(
        FakeAdapter(["https://x/repost", "https://x/fresh"]), conn, "data-analyst", 1
    )

    assert stats["errors"] == 1
    assert stats["inserted"] == 1  # lỗi một job không kéo sập cả lượt
    conn.rollback.assert_called()


def test_repost_extends_deadline_of_existing_job(pipeline_db):
    from datetime import date

    pipeline_db.find_repost_candidate.return_value = _candidate("job-orig")
    pipeline_db.link_repost_source.return_value = RepostLink(inserted=True, deadline_extended=True)
    conn = MagicMock()

    stats = pipeline.run_pipeline(FakeAdapter(["https://x/new-url"]), conn, "data-analyst", 1)

    # C4 phần 2/3: hạn dời ngay trong link_repost_source (listing mới mang hạn 05/09/2026), pipeline chỉ đếm
    assert pipeline_db.link_repost_source.call_args.kwargs["deadline"] == date(2026, 9, 5)
    assert stats["repost_deadline_extended"] == 1
    assert stats["skipped_duplicate_repost"] == 1


def test_repost_deadline_not_counted_when_db_keeps_later_deadline(pipeline_db):
    pipeline_db.find_repost_candidate.return_value = _candidate("job-orig")
    # mặc định link_repost_source trả deadline_extended=False: job cũ đã có hạn muộn hơn

    stats = pipeline.run_pipeline(FakeAdapter(["https://x/new-url"]), MagicMock(), "data-analyst", 1)

    assert stats["repost_deadline_extended"] == 0
    assert stats["skipped_duplicate_repost"] == 1


# ----------------------------------------------------------------------
# Phần 3c: job đã CLOSED + khoá tra không xét level (mock db)
# ----------------------------------------------------------------------
def test_pipeline_uses_the_new_lookup_not_the_manual_one(pipeline_db):
    conn = MagicMock()
    stats = pipeline.run_pipeline(FakeAdapter(["https://x/u1"]), conn, "data-analyst", 1)

    # POST /jobs vẫn dùng find_manual_job_duplicate, crawler thì không: hàm đó không nằm trong
    # PipelineDB nên Fake không có nó (gọi là AttributeError, job bị đếm vào stats.errors).
    assert not hasattr(pipeline_db, "find_manual_job_duplicate")
    assert stats["errors"] == 0
    pipeline_db.find_repost_candidate.assert_called_once()
    kwargs = pipeline_db.find_repost_candidate.call_args.kwargs
    assert kwargs["company_id"] == "company-1" and kwargs["job_title"] == "Data Analyst"
    assert "level_id" in kwargs and "province_id" in kwargs    # level chỉ để xếp hạng, không lọc


def test_repost_of_closed_job_reopens_it_with_new_url_and_deadline(pipeline_db):
    from datetime import date

    pipeline_db.find_repost_candidate.return_value = _candidate("job-orig", status="CLOSED")
    pipeline_db.link_repost_source.return_value = RepostLink(inserted=True, reopened=True)   # C4: link tự mở lại
    conn = MagicMock()

    stats = pipeline.run_pipeline(FakeAdapter(["https://x/new-url"]), conn, "data-analyst", 1)

    pipeline_db.link_repost_source.assert_called_once()
    args, kwargs = pipeline_db.link_repost_source.call_args
    assert args == (conn, "job-orig")
    assert kwargs["source_url"] == "https://x/new-url" and kwargs["deadline"] == date(2026, 9, 5)
    pipeline_db.insert_job.assert_not_called()
    assert stats["repost_reopened"] == 1 and stats["skipped_duplicate_repost"] == 1
    assert "repost_kept_closed" not in stats
    assert stats["repost_deadline_extended"] == 0                      # job CLOSED không đếm dời hạn
    conn.commit.assert_called()


@pytest.mark.parametrize("reason", ["staff", "unknown", "merged"])
def test_repost_of_job_not_closed_by_expiry_is_not_reopened(pipeline_db, reason):
    # staff: nhân viên đóng; unknown: không rõ nên coi như nhân viên đóng; merged: dự phòng.
    pipeline_db.find_repost_candidate.return_value = _candidate("job-orig", status="CLOSED", closed_reason=reason)

    stats = pipeline.run_pipeline(FakeAdapter(["https://x/new-url"]), MagicMock(), "data-analyst", 1)

    pipeline_db.link_repost_source.assert_called_once()              # vẫn ghi URL mới làm nguồn phụ
    assert stats["repost_kept_closed"] == 1 and stats["skipped_duplicate_repost"] == 1
    assert "repost_reopened" not in stats


def test_repost_not_reopened_by_db_counts_as_kept_closed(pipeline_db):
    pipeline_db.find_repost_candidate.return_value = _candidate("job-orig", status="CLOSED")
    # mặc định link_repost_source trả reopened=False: hạn mới đã qua (listing sinh ra CLOSED) hoặc luồng khác mở trước

    stats = pipeline.run_pipeline(FakeAdapter(["https://x/new-url"]), MagicMock(), "data-analyst", 1)

    assert stats["repost_kept_closed"] == 1 and "repost_reopened" not in stats
    assert stats["skipped_duplicate_repost"] == 1


def test_repost_of_open_job_is_never_counted_as_reopened(pipeline_db):
    pipeline_db.find_repost_candidate.return_value = _candidate("job-orig", status="OPEN")

    stats = pipeline.run_pipeline(FakeAdapter(["https://x/new-url"]), MagicMock(), "data-analyst", 1)

    assert "repost_reopened" not in stats and "repost_kept_closed" not in stats


def test_reopen_failure_counts_error_rolls_back_and_continues(pipeline_db):
    pipeline_db.find_repost_candidate.side_effect = [_candidate("job-orig", status="CLOSED"), None]
    pipeline_db.link_repost_source.side_effect = RuntimeError("DB mất kết nối")
    conn = MagicMock()

    stats = pipeline.run_pipeline(FakeAdapter(["https://x/repost", "https://x/fresh"]), conn, "data-analyst", 1)

    assert stats["errors"] == 1 and stats["inserted"] == 1
    assert "repost_reopened" not in stats and "repost_kept_closed" not in stats
    conn.rollback.assert_called()
