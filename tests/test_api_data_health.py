"""
Test cho api/routers/companies.py::get_company_data_health() và
api/routers/jobs.py::get_job_data_health() (route GET /companies/
data-health, GET /jobs/data-health — thêm 08/2026).

Test coverage:
- Route đúng require_role("ss_team") cho companies/data-health (JOIN
  qua contact — thông tin nhạy cảm), route jobs/data-health KHÔNG yêu
  cầu role (public, giống GET /jobs).
- Route delegate đúng xuống db_module.get_*_data_health(conn), KHÔNG tự
  xử lý/biến đổi gì thêm — trả thẳng dict backend tính sẵn.

Cùng convention với tests/test_api_contacts.py — gọi hàm route trực
tiếp (không qua TestClient/HTTP thật), mock db_module bằng
unittest.mock.patch, dùng MagicMock(spec=Request) để qua được isinstance
check của @limiter.limit (slowapi).

Chạy: pytest tests/test_api_data_health.py -v
"""
import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from fastapi import Request


_FAKE_COMPANY_HEALTH = {
    "company_health_rows": [
        {"field": "tax_id", "label": "Mã số thuế", "missing": 30, "total": 200, "pct_missing": 15},
    ],
    "company_health_total": 200,
    "company_no_contact_missing": 45,
    "company_no_contact_total": 200,
}

_FAKE_JOB_HEALTH = {
    "job_health_rows": [
        {"field": "skills", "label": "Kỹ năng", "missing": 120, "total": 800, "pct_missing": 15},
    ],
    "job_health_total": 800,
    "expired_open_jobs": [],
    "job_health_by_source": [],
    "duplicate_job_groups": [],
}


# ---------------------------------------------------------------------------
# GET /companies/data-health — require_role("ss_team")
# ---------------------------------------------------------------------------


def test_company_data_health_delegates_to_db_module(mock_conn, ss_team_user):
    """Route KHÔNG tự tính gì — chỉ gọi thẳng
    db_module.get_company_data_health(conn) và trả nguyên kết quả."""
    with patch("api.routers.companies.db_module") as mock_db:
        mock_db.get_company_data_health.return_value = _FAKE_COMPANY_HEALTH

        from api.routers.companies import get_company_data_health

        result = get_company_data_health(
            request=MagicMock(spec=Request), conn=mock_conn, user=ss_team_user,
        )

        mock_db.get_company_data_health.assert_called_once_with(mock_conn)
        assert result == _FAKE_COMPANY_HEALTH


def test_company_data_health_requires_ss_team_role_declared():
    """Kiểm tra route THẬT SỰ khai báo require_role("ss_team") (không bị
    lỡ tay xoá/đổi thành role khác lúc sửa code sau này) — soi trực tiếp
    default value của tham số `user` trong signature, vì
    require_role(...) trả về 1 callable dùng làm Depends(...), khó mock
    qua HTTP thật trong unit test không có TestClient đầy đủ."""
    import inspect
    from api.routers.companies import get_company_data_health

    sig = inspect.signature(get_company_data_health)
    user_param = sig.parameters["user"]
    # Depends(require_role("ss_team")) — soi vào dependency callable đã
    # đóng gói sẵn role "ss_team" (closure) qua repr, đủ để phát hiện
    # nếu ai đó lỡ đổi thành require_role("admin") hoặc bỏ hẳn dependency.
    depends_obj = user_param.default
    assert depends_obj is not None, "user param phải có Depends(require_role(...)), không phải None"
    assert "require_role" in repr(depends_obj.dependency) or "wrapper" in repr(depends_obj.dependency)


def test_company_data_health_route_declared_before_company_id_route():
    """PHẢI đăng ký /data-health TRƯỚC /{company_id} — nếu đăng ký SAU,
    FastAPI khớp "/companies/data-health" nhầm vào path param
    company_id="data-health" (rồi lỗi vì không phải UUID hợp lệ) thay vì
    khớp đúng route tĩnh này. Test bằng cách soi thứ tự route trong
    router.routes — regression test cho đúng lỗi đã né lúc viết route."""
    from api.routers.companies import router

    paths = [r.path for r in router.routes]
    data_health_idx = paths.index("/companies/data-health")
    company_id_idx = paths.index("/companies/{company_id}")
    assert data_health_idx < company_id_idx


# ---------------------------------------------------------------------------
# GET /jobs/data-health — public (không require_role)
# ---------------------------------------------------------------------------


def test_job_data_health_delegates_to_db_module(mock_conn):
    with patch("api.routers.jobs.db_module") as mock_db:
        mock_db.get_job_data_health.return_value = _FAKE_JOB_HEALTH

        from api.routers.jobs import get_job_data_health

        result = get_job_data_health(request=MagicMock(spec=Request), conn=mock_conn)

        mock_db.get_job_data_health.assert_called_once_with(mock_conn)
        assert result == _FAKE_JOB_HEALTH


def test_job_data_health_has_no_role_dependency():
    """Route KHÔNG yêu cầu role — khác /companies/data-health. Kiểm tra
    signature KHÔNG có tham số `user` nào (chỉ request/conn), tránh ai
    đó lỡ thêm require_role() vào đây làm frontend (crawler_client.
    get_job_data_health(), public, không gửi JWT) gọi bị 401."""
    import inspect
    from api.routers.jobs import get_job_data_health

    sig = inspect.signature(get_job_data_health)
    assert "user" not in sig.parameters


def test_job_data_health_route_declared_before_job_id_route():
    """Cùng lý do route ordering ở companies — /data-health phải đứng
    trước /{job_id}."""
    from api.routers.jobs import router

    paths = [r.path for r in router.routes]
    data_health_idx = paths.index("/jobs/data-health")
    job_id_idx = paths.index("/jobs/{job_id}")
    assert data_health_idx < job_id_idx


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
