"""
Test cho api/routers/dashboard.py (GET /dashboard/insights/students,
/companies, /monthly) — Phần 5 mục 8 của plan Next.js.

CHỈ test tầng route + schema (mock db_module, không có DB thật): SQL bên
trong db/dashboard.py cần chạy thử trên Postgres thật (xem checklist ở
commit message) — không kiểm chứng được bằng mock.

Test coverage:
- _pct_change(): None khi kỳ trước = 0, làm tròn round() của Python (12.5
  -> 12, giống bản Flask), số âm.
- /companies: followup_days ngoài whitelist -> 400 + error_code riêng;
  giá trị hợp lệ được truyền xuống db_module.get_contacts_needing_followup.
- /monthly: ghép đúng phần đếm job/công ty với phần ứng tuyển/lưu job từ
  get_monthly_engagement_stats(), tính % đúng.
- response_model validate được đúng shape thật db_module trả về.

Cùng convention với tests/test_stats.py — gọi hàm route trực tiếp, luôn
truyền TƯỜNG MINH mọi tham số (kể cả Query có default, vì gọi thẳng hàm
thì default là object Query(...) chứ không phải giá trị).

Chạy: pytest tests/test_api_dashboard.py -v
"""
import os
import sys
from datetime import date, datetime
from unittest.mock import patch

import pytest
from fastapi import HTTPException

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api import error_codes
from api.routers import dashboard as dashboard_router
from api.routers.dashboard import (
    _pct_change,
    get_company_insights,
    get_monthly_insights,
    get_student_insights,
)
from api.schemas import CompanyInsightsOut, MonthlyInsightsOut, StudentInsightsOut

_CID = "11111111-1111-1111-1111-111111111111"
_JID = "22222222-2222-2222-2222-222222222222"


def test_pct_change_none_when_previous_is_zero():
    assert _pct_change(5, 0) is None
    assert _pct_change(0, 0) is None


def test_pct_change_uses_python_round_half_even():
    # (9-8)/8*100 = 12.5 -> round() về số chẵn = 12 (giống Flask)
    assert _pct_change(9, 8) == 12
    assert _pct_change(6, 8) == -25
    assert _pct_change(10, 10) == 0


def test_companies_rejects_followup_days_outside_whitelist(fake_request, ss_team_user, mock_conn):
    with patch("api.routers.dashboard.db_module"):
        with pytest.raises(HTTPException) as exc:
            get_company_insights(
                request=fake_request, followup_days=21, user=ss_team_user, conn=mock_conn
            )
    assert exc.value.status_code == 400
    assert exc.value.detail["error_code"] == error_codes.DASHBOARD_FOLLOWUP_DAYS_INVALID
    assert exc.value.detail["params"] == {"value": 21}


@pytest.mark.parametrize("days", dashboard_router.FOLLOWUP_DAYS_OPTIONS)
def test_companies_passes_followup_days_to_db(days, fake_request, ss_team_user, mock_conn):
    with patch("api.routers.dashboard.db_module") as mock_db:
        mock_db.get_company_job_activity.return_value = {"expanding": [], "quiet": []}
        mock_db.get_high_potential_companies_without_contact.return_value = []
        mock_db.get_contacts_needing_followup.return_value = []
        result = get_company_insights(
            request=fake_request, followup_days=days, user=ss_team_user, conn=mock_conn
        )
    mock_db.get_contacts_needing_followup.assert_called_once_with(mock_conn, quiet_days=days)
    assert result["followup_days"] == days


def test_companies_response_model_accepts_real_db_shapes(fake_request, ss_team_user, mock_conn):
    with patch("api.routers.dashboard.db_module") as mock_db:
        mock_db.get_company_job_activity.return_value = {
            "expanding": [{"company_id": _CID, "company_name": "A", "city": None,
                           "recent_job_count": 3, "recent_jobs": ["Dev", "QA"]}],
            "quiet": [{"company_id": _CID, "company_name": "B", "city": "Hà Nội",
                       "quiet_days": 80, "last_job_date": date(2026, 6, 1)}],
        }
        mock_db.get_high_potential_companies_without_contact.return_value = [
            {"company_id": _CID, "company_name": "A", "city": None,
             "last_contacted": None, "reason": "no_contact"},
        ]
        mock_db.get_contacts_needing_followup.return_value = [
            {"contact_id": _JID, "contact_name": "X", "job_title": None,
             "contact_status": "EMAIL_SENT", "company_id": _CID, "company_name": "A",
             "last_contacted_date": None, "collected_date": date(2026, 8, 1),
             "quiet_days": 51, "never_contacted": True},
        ]
        result = get_company_insights(
            request=fake_request, followup_days=14, user=ss_team_user, conn=mock_conn
        )
    out = CompanyInsightsOut.model_validate(result)
    assert out.companies_no_contact[0].reason == "no_contact"
    assert out.contacts_needing_followup[0].never_contacted is True


def test_students_delegates_and_validates(fake_request, ss_team_user, mock_conn):
    with patch("api.routers.dashboard.db_module") as mock_db:
        mock_db.get_jobs_needing_push.return_value = [
            {"job_id": _JID, "job_title": "Dev", "company_id": _CID, "company_name": "A",
             "deadline": date(2026, 9, 30), "created_at": datetime(2026, 9, 1, 3, 0),
             "days_left": 9},
        ]
        mock_db.get_stale_jobs.return_value = [
            {"job_id": _JID, "job_title": "QA", "company_id": _CID, "company_name": "A",
             "created_at": datetime(2026, 7, 1, 3, 0), "age_days": 82},
        ]
        mock_db.get_top_skills.return_value = [{"skill": "Python", "count": 12}]
        mock_db.get_salary_ranges.return_value = [
            {"industry": "Data", "level": "Junior", "avg_min": 10_000_000.0,
             "avg_max": None, "sample_size": 4},
        ]
        result = get_student_insights(request=fake_request, user=ss_team_user, conn=mock_conn)
    out = StudentInsightsOut.model_validate(result)
    assert out.jd_needing_push[0].days_left == 9
    assert out.salary_ranges[0].avg_max is None


def test_monthly_merges_counts_with_engagement(fake_request, ss_team_user, mock_conn):
    with patch("api.routers.dashboard.db_module") as mock_db:
        mock_db.get_monthly_recap_counts.return_value = {
            "this_month_start": date(2026, 9, 1), "last_month_start": date(2026, 8, 1),
            "jobs_this": 30, "jobs_last": 20, "jobs_expired": 4,
            "companies_this": 5, "companies_last": 0,
            "top_industries": [{"industry": "Data", "this_count": 9, "last_count": 0}],
            "top_companies": [{"company_id": _CID, "company_name": "A", "count": 3}],
        }
        mock_db.get_monthly_engagement_stats.return_value = {
            "applications": {"this_month": 9, "last_month": 8},
            "saved_jobs": {"this_month": 0, "last_month": 0},
        }
        result = get_monthly_insights(request=fake_request, user=ss_team_user, conn=mock_conn)
    out = MonthlyInsightsOut.model_validate(result)
    assert out.jobs_new == 30 and out.jobs_new_pct == 50
    assert out.companies_new_pct is None          # tháng trước = 0
    assert out.top_industries[0].pct_change is None
    assert out.applications_pct == 12             # 12.5 -> round half even
    assert out.saved_jobs_this_month == 0 and out.saved_jobs_pct is None
