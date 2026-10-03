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

import db as real_db
import pipeline
from adapters.base import BaseAdapter
from models import RawJobRecord

DETAIL = {
    "work_type": "Toàn thời gian", "deadline_text": "05/09/2026",
    "job_description": "mô tả công việc", "requirements": "", "perks": "",
    "required_skills": [],
}


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


@pytest.fixture
def fake_db(monkeypatch):
    fdb = MagicMock()
    fdb.get_job_probe_by_source_url.return_value = None
    fdb.job_needs_detail_enrichment.side_effect = real_db.job_needs_detail_enrichment
    fdb.find_company_probe.return_value = None
    fdb.probe_needs_enrichment.return_value = False
    fdb.get_or_create_company_by_profile.return_value = "company-1"
    fdb.find_manual_job_duplicate.return_value = None
    fdb.insert_job.return_value = "new-job"
    fdb.extend_job_deadline.return_value = False
    monkeypatch.setattr(pipeline, "db", fdb)
    return fdb


def test_repost_links_source_to_existing_job_and_commits(fake_db):
    fake_db.find_manual_job_duplicate.return_value = "job-orig"
    conn = MagicMock()

    stats = pipeline.run_pipeline(FakeAdapter(["https://x/new-url"]), conn, "data-analyst", 1)

    assert stats["skipped_duplicate_repost"] == 1
    assert stats["inserted"] == 0
    fake_db.insert_job.assert_not_called()

    fake_db.link_repost_source.assert_called_once()
    args, kwargs = fake_db.link_repost_source.call_args
    assert args == (conn, "job-orig")
    assert kwargs["source_name"] == "Fake"
    assert kwargs["source_url"] == "https://x/new-url"
    assert "mô tả công việc" in kwargs["raw_jd_content"]  # giữ bằng chứng gốc
    assert kwargs["salary_raw_text"] == "15-20 triệu"
    conn.commit.assert_called()


def test_non_duplicate_does_not_link(fake_db):
    stats = pipeline.run_pipeline(FakeAdapter(["https://x/u1"]), MagicMock(), "data-analyst", 1)

    assert stats["inserted"] == 1
    fake_db.link_repost_source.assert_not_called()


def test_link_failure_counts_error_rolls_back_and_continues(fake_db):
    # Tin 1 là đăng lại nhưng ghi nguồn phụ lỗi; tin 2 là tin mới bình thường.
    fake_db.find_manual_job_duplicate.side_effect = ["job-orig", None]
    fake_db.link_repost_source.side_effect = RuntimeError("DB mất kết nối")
    conn = MagicMock()

    stats = pipeline.run_pipeline(
        FakeAdapter(["https://x/repost", "https://x/fresh"]), conn, "data-analyst", 1
    )

    assert stats["errors"] == 1
    assert stats["inserted"] == 1  # lỗi một job không kéo sập cả lượt
    conn.rollback.assert_called()


def test_repost_extends_deadline_of_existing_job(fake_db):
    from datetime import date

    fake_db.find_manual_job_duplicate.return_value = "job-orig"
    fake_db.extend_job_deadline.return_value = True
    conn = MagicMock()

    stats = pipeline.run_pipeline(FakeAdapter(["https://x/new-url"]), conn, "data-analyst", 1)

    fake_db.extend_job_deadline.assert_called_once_with(conn, "job-orig", date(2026, 9, 5))
    assert stats["repost_deadline_extended"] == 1
    assert stats["skipped_duplicate_repost"] == 1


def test_repost_deadline_not_counted_when_db_keeps_later_deadline(fake_db):
    fake_db.find_manual_job_duplicate.return_value = "job-orig"
    fake_db.extend_job_deadline.return_value = False  # job cũ đã có hạn muộn hơn

    stats = pipeline.run_pipeline(FakeAdapter(["https://x/new-url"]), MagicMock(), "data-analyst", 1)

    assert stats["repost_deadline_extended"] == 0
    assert stats["skipped_duplicate_repost"] == 1
