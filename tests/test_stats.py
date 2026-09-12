"""
Test cho api/routers/meta.py::get_stats()/get_engagement_stats()/
get_sources()/get_enums() (GET /stats, GET /stats/engagement,
GET /sources, GET /enums) — 4 route đọc-only, KHÔNG có route nào yêu
cầu role hay bị rate-limit (không có tham số `user`/`request` trong
signature, không có @limiter.limit), nên gọi hàm route trực tiếp là
đủ, không cần MagicMock(spec=Request) như các test khác trong bộ này.

Test coverage:
- get_stats()/get_engagement_stats() delegate ĐÚNG xuống db_module,
  KHÔNG tự xử lý/biến đổi gì thêm — trả thẳng dict backend tính sẵn.
- response_model=StatsOut/EngagementStatsOut validate đúng qua đủ
  field thật (9 field của StatsOut, xem api/schemas/stats.py — KHÔNG
  phải 8 field như 1 bản kế hoạch cũ (plan_nextjs.md) từng ghi nhầm),
  kể cả 2 field mới nhất jobs_by_status/total_students (09/2026) mà
  Next.js dashboard đang đọc trực tiếp (xem app/actions/dashboard.ts).
- get_sources()/get_enums() không đọc DB (không có tham số conn) —
  trả thẳng dict tính từ CATEGORIES_BY_SOURCE/constants.py, test bằng
  cách so khớp đúng nguồn sự thật đó thay vì hard-code lại kỳ vọng,
  để không bị lệch nếu registry/constants đổi sau này.

Cùng convention với tests/test_api_data_health.py — gọi hàm route trực
tiếp (không qua TestClient/HTTP thật), mock db_module bằng
unittest.mock.patch.

Chạy: pytest tests/test_stats.py -v
"""
import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.schemas import EngagementStatsOut, StatsOut


# ---------------------------------------------------------------------------
# GET /stats
# ---------------------------------------------------------------------------

# Khớp ĐÚNG shape thật db_module.get_stats_summary() trả về (db/stats.py)
# — đủ cả 9 field của StatsOut, kể cả 2 field mới 09/2026.
_FAKE_STATS = {
    "total_jobs": 120,
    "total_companies": 40,
    "companies_with_social": 25,
    "by_industry": [{"matching_industry": "IT - Software", "n": 80}],
    "by_source": [{"source_name": "TopCV", "n": 100}],
    "total_applications": 55,
    "total_saved_jobs": 70,
    "jobs_by_status": {"OPEN": 90, "CLOSED": 30},
    "total_students": 300,
}


def test_get_stats_delegates_to_db_module(mock_conn):
    """Route KHÔNG tự tính gì — chỉ gọi thẳng
    db_module.get_stats_summary(conn) và trả nguyên kết quả."""
    with patch("api.routers.meta.db_module") as mock_db:
        mock_db.get_stats_summary.return_value = _FAKE_STATS

        from api.routers.meta import get_stats

        result = get_stats(conn=mock_conn)

        mock_db.get_stats_summary.assert_called_once_with(mock_conn)
        assert result == _FAKE_STATS


def test_get_stats_response_matches_stats_out_schema():
    """response_model=StatsOut (khai báo trên route) phải validate được
    đúng shape thật của get_stats_summary() — bắt hồi quy nếu ai đó sửa
    db/stats.py thêm/bớt field mà quên đồng bộ StatsOut, hoặc ngược lại.
    Test KHÔNG hard-code lại field theo trí nhớ mà validate thẳng bằng
    chính class StatsOut import từ schema thật."""
    validated = StatsOut(**_FAKE_STATS)

    # 2 field mới 09/2026 mà Next.js dashboard đọc trực tiếp (xem
    # app/actions/dashboard.ts) — quan trọng nhất để không hồi quy về
    # bug "dashboard hiện 0 và —" đã từng gặp khi backend production
    # chưa deploy 2 field này.
    assert validated.jobs_by_status == {"OPEN": 90, "CLOSED": 30}
    assert validated.total_students == 300
    # Field còn lại vẫn có mặt đủ (tổng 9 field, không thiếu/thừa so
    # với StatsOut thật).
    assert validated.model_dump().keys() == _FAKE_STATS.keys()


def test_get_stats_has_no_auth_or_role_dependency():
    """GET /stats là public (không require_role, không JWT) — dashboard
    Next.js gọi ngay sau login, không phải đợi token. Kiểm tra signature
    CHỈ có tham số `conn`, tránh ai đó lỡ thêm require_role()/user vào
    đây làm dashboard vỡ."""
    import inspect
    from api.routers.meta import get_stats

    sig = inspect.signature(get_stats)
    assert set(sig.parameters.keys()) == {"conn"}


# ---------------------------------------------------------------------------
# GET /stats/engagement
# ---------------------------------------------------------------------------

_FAKE_ENGAGEMENT_JOBS = [
    {
        "job_id": "job-1",
        "job_title": "Backend Developer",
        "deadline": None,
        "created_at": None,
        "application_count": 0,
        "saved_count": 0,
    }
]
_FAKE_MONTHLY = {
    "applications": {"this_month": 10, "last_month": 8},
    "saved_jobs": {"this_month": 15, "last_month": 20},
}


def test_get_engagement_stats_delegates_to_db_module(mock_conn):
    """Route gộp 2 lời gọi db_module (get_job_engagement_counts +
    get_monthly_engagement_stats) thành 1 response — kiểm tra CẢ 2 đều
    được gọi đúng với conn, và kết quả gộp đúng đúng shape
    EngagementStatsOut mong đợi (2 key: jobs, monthly)."""
    with patch("api.routers.meta.db_module") as mock_db:
        mock_db.get_job_engagement_counts.return_value = _FAKE_ENGAGEMENT_JOBS
        mock_db.get_monthly_engagement_stats.return_value = _FAKE_MONTHLY

        from api.routers.meta import get_engagement_stats

        result = get_engagement_stats(conn=mock_conn)

        mock_db.get_job_engagement_counts.assert_called_once_with(mock_conn)
        mock_db.get_monthly_engagement_stats.assert_called_once_with(mock_conn)
        assert result == {"jobs": _FAKE_ENGAGEMENT_JOBS, "monthly": _FAKE_MONTHLY}


def test_get_engagement_stats_response_matches_schema():
    """response_model=EngagementStatsOut validate đúng shape gộp — bắt
    hồi quy nếu JobEngagementOut/MonthlyEngagementOut (api/schemas/stats.py)
    đổi field mà route/db layer không cập nhật theo."""
    validated = EngagementStatsOut(
        jobs=_FAKE_ENGAGEMENT_JOBS, monthly=_FAKE_MONTHLY
    )
    assert len(validated.jobs) == 1
    assert validated.jobs[0].application_count == 0
    assert validated.monthly.applications.this_month == 10
    assert validated.monthly.saved_jobs.last_month == 20


# ---------------------------------------------------------------------------
# GET /sources — không đọc DB, tính thẳng từ CATEGORIES_BY_SOURCE
# ---------------------------------------------------------------------------


def test_get_sources_matches_categories_registry():
    """get_sources() phải LUÔN khớp đúng sources_registry.CATEGORIES_BY_SOURCE
    (nguồn sự thật duy nhất) — test bằng cách so trực tiếp với registry
    thật thay vì hard-code lại danh sách nguồn/category theo trí nhớ,
    để không tự lỗi thời nếu có nguồn mới (TopCV/VietnamWorks/CareerViet...)
    được thêm sau này (đúng bug lịch sử mà docstring get_sources() nhắc
    tới: từng quên hardcode CareerViet ở đây)."""
    from sources_registry import CATEGORIES_BY_SOURCE
    from api.routers.meta import get_sources

    result = get_sources()

    assert set(result.keys()) == set(CATEGORIES_BY_SOURCE.keys())
    for source, categories in CATEGORIES_BY_SOURCE.items():
        expected_labels = {key: cfg["label"] for key, cfg in categories.items()}
        assert result[source] == expected_labels


def test_get_sources_has_no_parameters():
    """Route KHÔNG có tham số conn/request/user nào — hoàn toàn tính
    tĩnh từ registry, không chạm DB."""
    import inspect
    from api.routers.meta import get_sources

    assert inspect.signature(get_sources).parameters == {}


# ---------------------------------------------------------------------------
# GET /enums — không đọc DB, tính thẳng từ constants.py
# ---------------------------------------------------------------------------


def test_get_enums_matches_constants_module():
    """get_enums() phải LUÔN khớp đúng constants.py (nguồn sự thật duy
    nhất cho enum values) — lý do endpoint này tồn tại (xem docstring):
    tránh lệch giữa backend và frontend mỗi khi đổi enum (như
    EXPIRED -> CLOSED trong JOB_STATUS_VALUES). Test so trực tiếp với
    module constants thật, không hard-code lại từng giá trị enum theo
    trí nhớ (dễ lỗi thời nếu ai đó thêm/sửa enum value sau này mà quên
    đồng bộ 2 nơi)."""
    import constants
    from api.routers.meta import get_enums

    result = get_enums()

    expected = {
        "job_status": constants.JOB_STATUS_VALUES,
        "work_type": constants.WORK_TYPE_VALUES,
        "salary_type": constants.SALARY_TYPE_VALUES,
        "salary_period": constants.SALARY_PERIOD_VALUES,
        "level_code": constants.LEVEL_CODE_VALUES,
        "currency": constants.CURRENCY_VALUES,
        "contact_status": constants.CONTACT_STATUS_VALUES,
        "partnership_potential": constants.PARTNERSHIP_POTENTIAL_VALUES,
        "user_role": constants.USER_ROLE_VALUES,
        "entity_type": constants.ENTITY_TYPE_VALUES,
        "action_type": constants.ACTION_TYPE_VALUES,
    }
    assert result == expected


def test_get_enums_job_status_and_level_code_needed_by_job_form():
    """Regression cụ thể cho nhu cầu Next.js JobForm.tsx (đợt nối
    /enums thay hard-code, xem job-posting): 2 field frontend sẽ đọc
    trực tiếp để build dropdown là job_status và level_code — phải
    luôn là list (không phải dict/None) và không rỗng, nếu không
    dropdown phía frontend sẽ render trống hoàn toàn."""
    from api.routers.meta import get_enums

    result = get_enums()

    assert isinstance(result["job_status"], list) and len(result["job_status"]) > 0
    assert isinstance(result["level_code"], list) and len(result["level_code"]) > 0
    assert isinstance(result["work_type"], list) and len(result["work_type"]) > 0
    assert isinstance(result["salary_type"], list) and len(result["salary_type"]) > 0
    assert isinstance(result["salary_period"], list) and len(result["salary_period"]) > 0


def test_get_enums_has_no_parameters():
    """Route KHÔNG có tham số conn/request/user nào — hoàn toàn tính
    tĩnh từ constants.py, không chạm DB."""
    import inspect
    from api.routers.meta import get_enums

    assert inspect.signature(get_enums).parameters == {}
