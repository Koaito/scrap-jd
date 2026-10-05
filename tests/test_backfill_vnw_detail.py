"""
Test backfill_vnw_detail.py (vá job VietnamWorks đã lưu) và
VietnamWorksAdapter.fetch_refreshed_job() — KHÔNG cần database, KHÔNG cần internet.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import backfill_vnw_detail as bf
from adapters.base import CrawlBlockedError
from adapters.vietnamworks import VietnamWorksAdapter
from vnw_page_builder import build_detail_html

URL = "https://www.vietnamworks.com/data-engineer-2109772-jv"
TRUNCATED_REQ = "- Bachelor degree. - 3+ years of experience in Data Eng..."


def _page_job(**over) -> dict:
    job = {
        "jobId": 2109772, "jobTitle": "Data Engineer", "yearsOfExperience": 3,
        "jobLevel": "Experienced (non-manager)",
        "jobDescription": "<p>Thiết kế và vận hành data pipeline.</p>",
        "jobRequirement": "<p>- Có 3+ năm kinh nghiệm Data Engineering. </p><p>- Python, SQL.</p>",
        "skills": [{"skillName": "PYTHON", "skillId": 1}],
        "benefits": [{"benefitNameVI": "Đào tạo", "benefitValue": "Học tập"}],
    }
    job.update(over)
    return job


def _row(**over) -> dict:
    row = {
        "job_id": "job-1", "job_title": "Data Engineer", "level_code": "Junior",
        "source_url": URL,
        "parsed_content": {"job_description": "Thiết kế...", "requirements": TRUNCATED_REQ,
                            "perks": "", "required_skills": ["PYTHON"]},
    }
    row.update(over)
    return row


def _refreshed(**over) -> dict:
    data = {
        "detail": {"job_description": "Mô tả đầy đủ", "requirements": "Có 3+ năm kinh nghiệm Data",
                    "perks": "Đào tạo", "required_skills": ["PYTHON", "SQL"]},
        "experience_text": "3 năm", "level_hint": "",
    }
    data.update(over)
    return data


# ----------------------------------------------------------------------
# VietnamWorksAdapter.fetch_refreshed_job
# ----------------------------------------------------------------------
def _adapter_with_page(html):
    adapter = VietnamWorksAdapter()
    adapter._fetch_html = lambda url, max_retries=3: html
    return adapter


def test_refresh_ok_returns_full_detail_and_years():
    adapter = _adapter_with_page(build_detail_html(_page_job()))
    status, data = adapter.fetch_refreshed_job(URL)

    assert status == adapter.REFRESH_OK
    assert data["experience_text"] == "3 năm"
    assert data["page_title"] == "Data Engineer"
    assert "3+ năm kinh nghiệm" in data["detail"]["requirements"]
    assert data["detail"]["required_skills"] == ["PYTHON"]


def test_refresh_page_not_fetched_is_unavailable():
    adapter = _adapter_with_page(None)
    assert adapter.fetch_refreshed_job(URL) == (adapter.REFRESH_UNAVAILABLE, None)


def test_refresh_gone_page_is_reported_as_gone():
    """Job đã gỡ: VietnamWorks trả HTTP 200 + chuyển hướng /410 (file thật job 2089555),
    không phải trang đổi cấu trúc."""
    with open(os.path.join(os.path.dirname(__file__), "fixture_vietnamworks_gone.html"),
              encoding="utf-8") as f:
        adapter = _adapter_with_page(f.read())
    assert adapter.fetch_refreshed_job("https://www.vietnamworks.com/brand-manager-upto-45-trieu-2089555-jv") \
        == (adapter.REFRESH_GONE, None)


def _redirect_stub(target: str) -> str:
    """Trang rỗng kiểu VietnamWorks: không có dữ liệu job, chỉ có lệnh chuyển hướng
    phía client (đúng định dạng đọc từ file thật)."""
    return ('<html><body><script>self.__next_f.push([1,"b:E{\\"digest\\":\\"NEXT_REDIRECT;replace;'
            + target + ';307;\\"}\\n"])</script></body></html>')


OLD_SLUG = "https://www.vietnamworks.com/school-sales-managersupervisor-2104572-jv"
NEW_SLUG = "https://www.vietnamworks.com/school-sales-supervisor-2104572-jv"


def _adapter_with_pages(pages: dict):
    adapter = VietnamWorksAdapter()
    adapter.fetched = []

    def fake_fetch(url, max_retries=3):
        adapter.fetched.append(url)
        return pages.get(url)

    adapter._fetch_html = fake_fetch
    return adapter


def test_refresh_follows_slug_change_to_same_job():
    """Nhà tuyển dụng sửa tiêu đề -> slug đổi, URL cũ chuyển hướng sang slug mới
    của CÙNG job. Phải đi theo và vá bình thường (dữ liệu thật: job 2104572)."""
    adapter = _adapter_with_pages({
        OLD_SLUG: _redirect_stub(NEW_SLUG),
        NEW_SLUG: build_detail_html(_page_job(jobId=2104572)),
    })
    status, data = adapter.fetch_refreshed_job(OLD_SLUG)

    assert status == adapter.REFRESH_OK
    assert data["experience_text"] == "3 năm"
    assert adapter.fetched == [OLD_SLUG, NEW_SLUG]


def test_refresh_does_not_follow_redirect_to_a_different_job():
    other = "https://www.vietnamworks.com/another-job-999-jv"
    adapter = _adapter_with_pages({OLD_SLUG: _redirect_stub(other),
                                   other: build_detail_html(_page_job(jobId=999))})
    assert adapter.fetch_refreshed_job(OLD_SLUG)[0] == adapter.REFRESH_UNPARSABLE
    assert adapter.fetched == [OLD_SLUG]


def test_refresh_does_not_follow_redirect_to_another_host():
    evil = "https://evil.example.com/school-sales-supervisor-2104572-jv"
    adapter = _adapter_with_pages({OLD_SLUG: _redirect_stub(evil)})
    assert adapter.fetch_refreshed_job(OLD_SLUG)[0] == adapter.REFRESH_UNPARSABLE
    assert adapter.fetched == [OLD_SLUG]


def test_refresh_follows_only_one_hop():
    hop2 = "https://www.vietnamworks.com/school-sales-supervisor-v3-2104572-jv"
    adapter = _adapter_with_pages({OLD_SLUG: _redirect_stub(NEW_SLUG),
                                   NEW_SLUG: _redirect_stub(hop2)})
    assert adapter.fetch_refreshed_job(OLD_SLUG)[0] == adapter.REFRESH_UNPARSABLE
    assert adapter.fetched == [OLD_SLUG, NEW_SLUG]


def test_refresh_redirect_chain_ending_in_410_is_gone():
    adapter = _adapter_with_pages({OLD_SLUG: _redirect_stub(NEW_SLUG),
                                   NEW_SLUG: _redirect_stub("https://www.vietnamworks.com/410")})
    assert adapter.fetch_refreshed_job(OLD_SLUG)[0] == adapter.REFRESH_GONE


def test_refresh_new_slug_unreachable_is_unavailable():
    adapter = _adapter_with_pages({OLD_SLUG: _redirect_stub(NEW_SLUG)})   # NEW_SLUG -> None
    assert adapter.fetch_refreshed_job(OLD_SLUG)[0] == adapter.REFRESH_UNAVAILABLE


def test_refresh_redirect_to_itself_is_not_followed():
    adapter = _adapter_with_pages({OLD_SLUG: _redirect_stub(OLD_SLUG)})
    assert adapter.fetch_refreshed_job(OLD_SLUG)[0] == adapter.REFRESH_UNPARSABLE
    assert adapter.fetched == [OLD_SLUG]


def test_refresh_garbage_page_is_unparsable():
    adapter = _adapter_with_page("<html>không phải trang job</html>")
    assert adapter.fetch_refreshed_job(URL) == (adapter.REFRESH_UNPARSABLE, None)


def test_refresh_page_of_another_job_is_unparsable():
    """Trang trả job khác với jobId trong URL thì không dùng, tránh ghi JD sai job."""
    adapter = _adapter_with_page(build_detail_html(_page_job(jobId=999)))
    assert adapter.fetch_refreshed_job(URL)[0] == adapter.REFRESH_UNPARSABLE


def test_refresh_lets_circuit_breaker_propagate():
    adapter = VietnamWorksAdapter()

    def blocked(url, max_retries=3):
        raise CrawlBlockedError("bị chặn")

    adapter._fetch_html = blocked
    with pytest.raises(CrawlBlockedError):
        adapter.fetch_refreshed_job(URL)


# ----------------------------------------------------------------------
# plan_job (logic thuần)
# ----------------------------------------------------------------------
def test_plan_replaces_truncated_jd_and_fixes_level():
    plan = bf.plan_job(_row(), _refreshed())

    assert plan["new_level"] == "Middle"         # 3 năm
    assert plan["level_changed"] is True
    assert plan["jd_changed"] is True
    assert plan["parsed_content"]["requirements"] == "Có 3+ năm kinh nghiệm Data"
    assert not plan["parsed_content"]["requirements"].endswith("...")
    assert "Có 3+ năm kinh nghiệm Data" in plan["raw_jd"]


def test_plan_empty_fields_from_page_keep_old_values():
    refreshed = _refreshed(detail={"job_description": "", "requirements": "Yêu cầu mới",
                                    "perks": "", "required_skills": []})
    plan = bf.plan_job(_row(), refreshed)

    pc = plan["parsed_content"]
    assert pc["job_description"] == "Thiết kế..."     # giữ cũ, không ghi đè bằng rỗng
    assert pc["required_skills"] == ["PYTHON"]
    assert pc["requirements"] == "Yêu cầu mới"


def test_plan_nothing_new_changes_nothing():
    row = _row(level_code="Middle", parsed_content={
        "job_description": "Mô tả đầy đủ", "requirements": "Có 3+ năm kinh nghiệm Data",
        "perks": "Đào tạo", "required_skills": ["PYTHON", "SQL"]})
    plan = bf.plan_job(row, _refreshed())

    assert plan["level_changed"] is False
    assert plan["jd_changed"] is False


def test_plan_title_keyword_still_beats_years():
    plan = bf.plan_job(_row(job_title="Senior Data Engineer", level_code="Junior"), _refreshed())
    assert plan["new_level"] == "Senior"


def test_plan_no_years_falls_back_to_level_hint():
    plan = bf.plan_job(_row(), _refreshed(experience_text="", level_hint="Manager"))
    assert plan["new_level"] == "Manager"


def test_plan_job_without_parsed_content():
    plan = bf.plan_job(_row(parsed_content=None), _refreshed())
    assert plan["jd_changed"] is True
    assert plan["parsed_content"]["job_description"] == "Mô tả đầy đủ"


# ----------------------------------------------------------------------
# process_job / run
# ----------------------------------------------------------------------
LEVEL_IDS = {"Intern": 1, "Fresher": 2, "Junior": 3, "Middle": 4, "Senior": 5, "Lead": 6, "Manager": 7}


class FakeAdapter:
    REFRESH_OK = VietnamWorksAdapter.REFRESH_OK
    REFRESH_UNAVAILABLE = VietnamWorksAdapter.REFRESH_UNAVAILABLE
    REFRESH_GONE = VietnamWorksAdapter.REFRESH_GONE
    REFRESH_UNPARSABLE = VietnamWorksAdapter.REFRESH_UNPARSABLE

    def __init__(self, results):
        self.results = results       # url -> (status, data) hoặc Exception
        self.requested = []

    def fetch_refreshed_job(self, url):
        self.requested.append(url)
        result = self.results[url]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def writes(monkeypatch):
    calls = []
    monkeypatch.setattr(bf.db, "update_job", lambda conn, job_id, **kw: calls.append(("update_job", job_id, kw)))
    monkeypatch.setattr(bf.db, "update_job_fields",
                        lambda conn, job_id, **kw: calls.append(("update_job_fields", job_id, kw)))
    monkeypatch.setattr(bf.db, "mark_source_detail_checked",
                        lambda conn, url: calls.append(("mark_checked", url)))
    monkeypatch.setattr(bf, "_update_raw_jd",
                        lambda conn, job_id, url, raw: calls.append(("raw_jd", job_id, url)))
    return calls


def _rows(n=1):
    return [_row(job_id=f"job-{i}", source_url=f"https://x/{i}-jv") for i in range(n)]


def test_dry_run_writes_nothing_but_reports(writes):
    conn = MagicMock()
    rows = _rows(2)
    adapter = FakeAdapter({r["source_url"]: (FakeAdapter.REFRESH_OK, _refreshed()) for r in rows})
    done = []

    summary = bf.run(conn, adapter, rows, apply=False, level_ids=LEVEL_IDS, on_done=done.append)

    assert writes == [] and done == []
    conn.commit.assert_not_called()
    assert summary.level_changed == 2 and summary.jd_changed == 2
    assert summary.transitions[("Junior", "Middle")] == 2


def test_apply_writes_each_job_in_its_own_transaction(writes):
    conn = MagicMock()
    rows = _rows(2)
    adapter = FakeAdapter({r["source_url"]: (FakeAdapter.REFRESH_OK, _refreshed()) for r in rows})
    done = []

    bf.run(conn, adapter, rows, apply=True, level_ids=LEVEL_IDS, on_done=done.append)

    assert conn.commit.call_count == 2
    assert done == ["job-0", "job-1"]
    # level tự động được đóng dấu căn cứ + phiên bản quy tắc (3 năm -> Middle theo "years")
    assert ("update_job", "job-0", {"level_id": 4, "level_source": "years",
                                    "level_rule_version": bf.normalize.LEVEL_RULE_VERSION}) in writes
    assert any(c[0] == "update_job_fields" and c[1] == "job-0" for c in writes)
    assert ("raw_jd", "job-0", "https://x/0-jv") in writes
    assert ("mark_checked", "https://x/0-jv") in writes


def test_apply_skips_level_write_when_level_same(writes):
    conn = MagicMock()
    row = _row(level_code="Middle")
    adapter = FakeAdapter({row["source_url"]: (FakeAdapter.REFRESH_OK, _refreshed())})

    bf.run(conn, adapter, [row], apply=True, level_ids=LEVEL_IDS)

    assert not any(c[0] == "update_job" for c in writes)
    assert any(c[0] == "update_job_fields" for c in writes)


def test_job_turned_into_another_position_is_skipped_not_written(writes):
    conn = MagicMock()
    row = _row(job_title="Account Manager")
    adapter = FakeAdapter({row["source_url"]: (FakeAdapter.REFRESH_OK, _refreshed(page_title="Sales Assistant"))})
    done = []

    summary = bf.run(conn, adapter, [row], apply=True, level_ids=LEVEL_IDS, on_done=done.append)

    assert summary.title_mismatch == 1 and summary.ok == 0 and summary.errors == 0
    assert writes == [] and done == []
    conn.commit.assert_not_called()


def test_same_position_with_edited_title_is_still_patched(writes):
    conn = MagicMock()
    row = _row(job_title="Kỹ Sư Dữ Liệu - Data Engineer")
    adapter = FakeAdapter({row["source_url"]: (FakeAdapter.REFRESH_OK, _refreshed(page_title="Kỹ Sư Dữ Liệu (DE)"))})

    summary = bf.run(conn, adapter, [row], apply=True, level_ids=LEVEL_IDS)

    assert summary.title_mismatch == 0 and summary.ok == 1
    assert any(c[0] == "update_job_fields" for c in writes)


def test_unavailable_and_unparsable_keep_job_untouched(writes):
    conn = MagicMock()
    rows = _rows(2)
    adapter = FakeAdapter({
        rows[0]["source_url"]: (FakeAdapter.REFRESH_UNAVAILABLE, None),
        rows[1]["source_url"]: (FakeAdapter.REFRESH_UNPARSABLE, None),
    })
    done = []

    summary = bf.run(conn, adapter, rows, apply=True, level_ids=LEVEL_IDS, on_done=done.append)

    assert writes == [] and done == []
    conn.commit.assert_not_called()
    assert summary.unavailable == 1 and summary.unparsable == 1 and summary.errors == 0


def test_gone_job_is_counted_separately_and_never_written(writes):
    conn = MagicMock()
    row = _row()
    adapter = FakeAdapter({row["source_url"]: (FakeAdapter.REFRESH_GONE, None)})
    done = []

    summary = bf.run(conn, adapter, [row], apply=True, level_ids=LEVEL_IDS, on_done=done.append)

    assert summary.gone == 1 and summary.unparsable == 0 and summary.errors == 0
    assert writes == [] and done == []          # không đóng job, không ghi tiến độ
    conn.commit.assert_not_called()


def test_error_in_one_job_rolls_back_and_continues(writes, monkeypatch):
    conn = MagicMock()
    rows = _rows(2)
    adapter = FakeAdapter({r["source_url"]: (FakeAdapter.REFRESH_OK, _refreshed()) for r in rows})

    def flaky(conn_, job_id, **kw):
        if job_id == "job-0":
            raise RuntimeError("boom")
        writes.append(("update_job", job_id, kw))

    monkeypatch.setattr(bf.db, "update_job", flaky)
    done = []

    summary = bf.run(conn, adapter, rows, apply=True, level_ids=LEVEL_IDS, on_done=done.append)

    assert summary.errors == 1
    conn.rollback.assert_called_once()
    assert done == ["job-1"]              # job lỗi không được ghi vào file tiến độ
    assert conn.commit.call_count == 1


def test_blocked_stops_the_whole_run(writes):
    conn = MagicMock()
    rows = _rows(3)
    adapter = FakeAdapter({
        rows[0]["source_url"]: (FakeAdapter.REFRESH_OK, _refreshed()),
        rows[1]["source_url"]: CrawlBlockedError("bị chặn"),
        rows[2]["source_url"]: (FakeAdapter.REFRESH_OK, _refreshed()),
    })

    summary = bf.run(conn, adapter, rows, apply=True, level_ids=LEVEL_IDS)

    assert summary.blocked is True
    assert adapter.requested == [rows[0]["source_url"], rows[1]["source_url"]]


def test_missing_level_id_is_counted_as_error_not_written(writes):
    conn = MagicMock()
    row = _row()
    adapter = FakeAdapter({row["source_url"]: (FakeAdapter.REFRESH_OK, _refreshed())})

    summary = bf.run(conn, adapter, [row], apply=True, level_ids={})

    assert summary.errors == 1
    assert writes == []
    conn.commit.assert_not_called()


# ----------------------------------------------------------------------
# select_jobs + file tiến độ
# ----------------------------------------------------------------------
def test_select_jobs_passes_filters_and_closes_read_transaction():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [("job-1", "Data Engineer", "Junior", {"a": 1}, None, URL)]

    rows = bf.select_jobs(conn, all_jobs=False, include_closed=False, limit=20)

    params = cur.execute.call_args[0][1]
    assert params == {"source": "VietnamWorks", "all_jobs": False, "include_closed": False,
                      "limit": 20, "before": None}
    assert rows[0]["job_id"] == "job-1" and rows[0]["source_url"] == URL
    conn.rollback.assert_called_once()


def test_select_jobs_passes_created_before():
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = []

    bf.select_jobs(conn, all_jobs=False, include_closed=False, limit=None, before="2026-10-01")

    assert cur.execute.call_args[0][1]["before"] == "2026-10-01"


def test_select_sql_orders_oldest_first_and_filters_by_date():
    """Job cũ mới cần vá: --limit nhỏ phải rơi vào nhóm cũ, không phải job vừa crawl."""
    assert "ORDER BY picked.created_at ASC" in bf._SELECT_SQL
    assert "jp.created_at < %(before)s::date" in bf._SELECT_SQL


def test_select_sql_never_touches_manually_edited_jobs():
    assert "jp.updated_by IS NULL" in bf._SELECT_SQL


def test_load_done_reads_ids_and_tolerates_missing_file(tmp_path):
    assert bf.load_done(str(tmp_path / "none.done")) == set()
    f = tmp_path / "x.done"
    f.write_text("a\nb\n\n", encoding="utf-8")
    assert bf.load_done(str(f)) == {"a", "b"}
