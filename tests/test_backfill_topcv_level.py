"""
Test scripts/backfill/backfill_topcv_level.py (vá level Senior -> Lead cho job TopCV nhãn
"Trên 5 năm"), normalize.level_from_title / infer_level_from_requirements và
TopCVAdapter.parse_experience_label() — KHÔNG cần database, KHÔNG cần internet.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts.backfill import backfill_topcv_level as bf
import normalize
from adapters.base import CrawlBlockedError
from adapters.topcv import TopCVAdapter

HERE = os.path.dirname(__file__)
URL = "https://www.topcv.vn/viec-lam/data-analyst/123.html"
LEVEL_IDS = {code: i for i, code in enumerate(normalize.LEVEL_ORDER, 1)}


def _row(**over) -> dict:
    row = {
        "job_id": "job-1", "job_title": "Chuyên Gia Dữ Liệu", "level_code": "Senior",
        "source_url": URL,
        "parsed_content": {"requirements": "Có trên 5 năm kinh nghiệm phân tích dữ liệu."},
    }
    row.update(over)
    return row


# ----------------------------------------------------------------------
# normalize
# ----------------------------------------------------------------------
class TestLevelFromTitle:
    @pytest.mark.parametrize("title,expected", [
        ("Senior Backend Engineer", "Senior"),
        ("Head of Data Team Lead", "Lead"),
        ("Sales Manager", "Manager"),
        ("Thực tập sinh Marketing", "Intern"),
        ("Fresher Java", "Fresher"),
        ("Chuyên Gia Khoa Học Dữ Liệu", None),
        ("Mobile Developer", None),
        ("Trợ lý giám đốc", None),
    ])
    def test_cases(self, title, expected):
        assert normalize.level_from_title(title) == expected

    def test_infer_level_unchanged_behaviour(self):
        # Tiêu đề vẫn thắng số năm; không có từ khoá thì vẫn đi theo nhãn.
        assert normalize.infer_level("2 năm", "Senior Dev") == "Senior"
        assert normalize.infer_level("Trên 5 năm", "Data Analyst") == "Lead"
        assert normalize.infer_level("5 năm", "Data Analyst") == "Senior"
        assert normalize.infer_level("", "Data Analyst") == "Junior"


class TestInferLevelFromRequirements:
    @pytest.mark.parametrize("text", [
        "Có trên 5 năm kinh nghiệm phân tích dữ liệu.",
        "Hơn 5 năm kinh nghiệm ở vị trí tương đương.",
        "Over 5 years of experience in data engineering.",
        "More than 7 years experience in sales",
        "Tối thiểu 8 năm kinh nghiệm quản lý.",
        "Ít nhất 6 năm kinh nghiệm backend.",
    ])
    def test_lead(self, text):
        assert normalize.infer_level_from_requirements(text) == "Lead"

    @pytest.mark.parametrize("text", [
        "Từ 5 năm kinh nghiệm backend.",
        "Tối thiểu 5 năm kinh nghiệm.",
        "5+ năm kinh nghiệm Java.",
        "3-5 năm kinh nghiệm.",
        "Có kinh nghiệm làm việc với nhiều phòng ban.",
        "Không yêu cầu kinh nghiệm.",
        "",
        None,
    ])
    def test_no_basis_returns_none(self, text):
        assert normalize.infer_level_from_requirements(text) is None

    def test_first_mention_decides(self):
        # Đoạn đầu nói 3 năm; "trên 5 năm" chỉ là ưu tiên ở phía sau -> không đổi.
        text = "Tối thiểu 3 năm kinh nghiệm. Ưu tiên trên 5 năm kinh nghiệm quản lý."
        assert normalize.infer_level_from_requirements(text) is None

    def test_over_five_not_near_experience_word_ignored(self):
        assert normalize.infer_level_from_requirements("Công ty hoạt động trên 5 năm.") is None


# ----------------------------------------------------------------------
# Adapter: đọc nhãn kinh nghiệm
# ----------------------------------------------------------------------
class TestParseExperienceLabel:
    def test_brandpro_fixture(self):
        html = open(os.path.join(HERE, "fixture_topcv_job_detail_brandpro.html"), encoding="utf-8").read()
        assert TopCVAdapter.parse_experience_label(html) == "2 năm"

    def test_normal_page_pair(self):
        html = ('<div><div class="title">Kinh nghiệm</div><div class="value">Trên 5 năm</div></div>'
                '<div class="box-job-information-detail-item__text">Có kinh nghiệm</div>')
        assert TopCVAdapter.parse_experience_label(html) == "Trên 5 năm"

    def test_word_inside_description_not_mistaken(self):
        html = "<ul><li>Kinh nghiệm</li><li>làm việc nhóm tốt</li></ul>"
        assert TopCVAdapter.parse_experience_label(html) is None

    def test_no_label(self):
        html = open(os.path.join(HERE, "fixture_topcv_job_detail.html"), encoding="utf-8").read()
        assert TopCVAdapter.parse_experience_label(html) is None
        assert TopCVAdapter.parse_experience_label("") is None

    def test_fetch_statuses(self):
        adapter = TopCVAdapter.__new__(TopCVAdapter)
        adapter._fetch_html = MagicMock(return_value=None)
        assert adapter.fetch_experience_label(URL) == (adapter.REFRESH_UNAVAILABLE, None)
        adapter._fetch_html = MagicMock(return_value="<p>không có</p>")
        assert adapter.fetch_experience_label(URL) == (adapter.REFRESH_NO_LABEL, None)
        adapter._fetch_html = MagicMock(
            return_value='<div>Kinh nghiệm</div><div>Trên 5 năm</div>')
        assert adapter.fetch_experience_label(URL) == (adapter.REFRESH_OK, "Trên 5 năm")
        assert adapter.fetch_experience_label("") == (adapter.REFRESH_UNAVAILABLE, None)


# ----------------------------------------------------------------------
# plan_level
# ----------------------------------------------------------------------
class TestPlanLevel:
    def test_label_over_five_becomes_lead(self):
        p = bf.plan_level(_row(), "Trên 5 năm")
        assert (p["new_level"], p["basis"], p["level_changed"], p["confirmed"]) == (
            "Lead", bf.BASIS_LABEL, True, True)

    def test_label_five_years_confirms_senior(self):
        p = bf.plan_level(_row(), "5 năm")
        assert (p["new_level"], p["level_changed"], p["confirmed"]) == ("Senior", False, True)

    def test_no_label_falls_back_to_text(self):
        p = bf.plan_level(_row(), None)
        assert (p["new_level"], p["basis"], p["level_changed"], p["confirmed"]) == (
            "Lead", bf.BASIS_TEXT, True, False)

    def test_no_label_no_text_basis_keeps_level(self):
        p = bf.plan_level(_row(parsed_content={"requirements": "Có kinh nghiệm."}), None)
        assert (p["new_level"], p["level_changed"], p["confirmed"]) == ("Senior", False, False)

    def test_missing_parsed_content(self):
        p = bf.plan_level(_row(parsed_content=None), None)
        assert p["level_changed"] is False


# ----------------------------------------------------------------------
# process_job / run
# ----------------------------------------------------------------------
def _adapter(status, label=None):
    a = MagicMock()
    a.REFRESH_OK, a.REFRESH_NO_LABEL, a.REFRESH_UNAVAILABLE = "ok", "no_label", "unavailable"
    a.fetch_experience_label.return_value = (status, label)
    return a


@pytest.fixture
def update_job(monkeypatch):
    m = MagicMock()
    monkeypatch.setattr(bf.db, "update_job", m)
    return m


class TestProcessJob:
    def test_dry_run_does_not_write(self, update_job):
        conn, s = MagicMock(), bf.Summary()
        done = bf.process_job(conn, _adapter("ok", "Trên 5 năm"), _row(), apply=False,
                              level_ids=LEVEL_IDS, summary=s)
        assert done is False
        update_job.assert_not_called()
        conn.commit.assert_not_called()
        assert s.changed_by_label == 1

    def test_apply_writes_level_and_commits(self, update_job):
        conn, s = MagicMock(), bf.Summary()
        done = bf.process_job(conn, _adapter("ok", "Trên 5 năm"), _row(), apply=True,
                              level_ids=LEVEL_IDS, summary=s)
        assert done is True
        update_job.assert_called_once_with(
            conn, "job-1", level_id=LEVEL_IDS["Lead"], level_source="label",
            level_rule_version=bf.normalize.LEVEL_RULE_VERSION,
            level_signals={"experience_text": "Trên 5 năm", "level_hint": ""})
        conn.commit.assert_called_once()

    def test_apply_text_basis_stamps_unknown_source(self, update_job):
        """Suy từ chữ (không có nhãn trang) không phải một nhánh của derive_level:
        ghi level nhưng để căn cứ None ("chưa biết"), lệnh tính lại xử lý sau."""
        conn = MagicMock()
        done = bf.process_job(conn, _adapter("no_label"), _row(), apply=True,
                              level_ids=LEVEL_IDS, summary=bf.Summary())
        assert done is True
        update_job.assert_called_once_with(
            conn, "job-1", level_id=LEVEL_IDS["Lead"], level_source=None,
            level_rule_version=None, level_signals=None)

    def test_apply_rolls_back_on_db_error(self, update_job):
        update_job.side_effect = RuntimeError("db")
        conn = MagicMock()
        with pytest.raises(RuntimeError):
            bf.process_job(conn, _adapter("ok", "Trên 5 năm"), _row(), apply=True,
                           level_ids=LEVEL_IDS, summary=bf.Summary())
        conn.rollback.assert_called_once()

    def test_title_decided_skips_fetch(self, update_job):
        adapter, s = _adapter("ok", "Trên 5 năm"), bf.Summary()
        done = bf.process_job(MagicMock(), adapter, _row(job_title="Senior Data Analyst"),
                              apply=True, level_ids=LEVEL_IDS, summary=s)
        assert done is True
        adapter.fetch_experience_label.assert_not_called()
        update_job.assert_not_called()
        assert s.title_decided == 1

    def test_unavailable_page_uses_text_and_is_not_marked_done_when_unchanged(self, update_job):
        s = bf.Summary()
        row = _row(parsed_content={"requirements": "Có kinh nghiệm."})
        done = bf.process_job(MagicMock(), _adapter("unavailable"), row, apply=True,
                              level_ids=LEVEL_IDS, summary=s)
        assert done is False  # chưa xác nhận bằng nhãn trang -> lần sau thử lại
        assert s.unavailable == 1
        update_job.assert_not_called()

    def test_unavailable_page_text_basis_still_changes(self, update_job):
        s = bf.Summary()
        done = bf.process_job(MagicMock(), _adapter("unavailable"), _row(), apply=True,
                              level_ids=LEVEL_IDS, summary=s)
        assert done is True
        assert s.changed_by_text == 1
        update_job.assert_called_once()

    def test_label_confirms_senior_marks_done_without_write(self, update_job):
        done = bf.process_job(MagicMock(), _adapter("ok", "5 năm"), _row(), apply=True,
                              level_ids=LEVEL_IDS, summary=bf.Summary())
        assert done is True
        update_job.assert_not_called()

    def test_no_fetch_mode_uses_text_only(self, update_job):
        s = bf.Summary()
        done = bf.process_job(MagicMock(), None, _row(), apply=True,
                              level_ids=LEVEL_IDS, summary=s)
        assert done is True
        assert s.changed_by_text == 1 and s.fetched_ok == 0

    def test_missing_level_id_raises(self, update_job):
        with pytest.raises(RuntimeError):
            bf.process_job(MagicMock(), _adapter("ok", "Trên 5 năm"), _row(), apply=True,
                           level_ids={}, summary=bf.Summary())


class TestRun:
    def test_blocked_stops_and_error_continues(self, update_job):
        adapter = MagicMock()
        adapter.REFRESH_OK, adapter.REFRESH_NO_LABEL, adapter.REFRESH_UNAVAILABLE = "ok", "no_label", "unavailable"
        adapter.fetch_experience_label.side_effect = [
            RuntimeError("boom"),
            CrawlBlockedError("chặn"),
            ("ok", "Trên 5 năm"),
        ]
        rows = [_row(job_id="a"), _row(job_id="b"), _row(job_id="c")]
        done_ids = []
        s = bf.run(MagicMock(), adapter, rows, apply=True, level_ids=LEVEL_IDS,
                   on_done=done_ids.append)
        assert s.errors == 1 and s.blocked is True
        assert done_ids == []
        assert adapter.fetch_experience_label.call_count == 2

    def test_on_done_called_for_finished_jobs(self, update_job):
        done_ids = []
        bf.run(MagicMock(), _adapter("ok", "Trên 5 năm"), [_row(job_id="a"), _row(job_id="b")],
               apply=True, level_ids=LEVEL_IDS, on_done=done_ids.append)
        assert done_ids == ["a", "b"]


def test_load_done(tmp_path):
    p = tmp_path / "done"
    assert bf.load_done(str(p)) == set()
    p.write_text("a\nb\n\n", encoding="utf-8")
    assert bf.load_done(str(p)) == {"a", "b"}
