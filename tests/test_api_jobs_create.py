"""
Test cho POST /jobs trả `was_existing` (thêm 09/2026, phục vụ tab "Job" của
trang /them-moi bên Next.js — cùng dạng với POST /companies, Phần 5 mục 16
của plan migrate).

create_job() là IDEMPOTENT: trùng (company_id, job_title, level, tỉnh) thì
trả job CŨ và bỏ mọi dữ liệu vừa gửi. Trước đây client không có cách nào
phân biệt "vừa tạo" với "trả lại job cũ" nên UI báo "đã tạo" sai sự thật.

Cùng convention với tests/test_api_jobs_clear_fields.py: gọi thẳng hàm
route, mock `api.routers.jobs.db_module`, không qua HTTP/DB thật.
"""
import uuid
from unittest.mock import patch

from api.schemas import JobCreate, JobCreateResult, JobDetailOut


def _job_row(job_id: str, company_id: str) -> dict:
    return {
        "job_id": job_id,
        "job_title": "Data Analyst",
        "company_id": company_id,
        "level_code": "Junior",
        "province_name": "Hà Nội",
        "work_type": "FULL_TIME",
        "deadline": "2026-12-31",
        "job_status": "OPEN",
    }


def _call_create(mock_conn, ss_team_user, *, duplicate: bool):
    from api.routers.jobs import create_job

    company_id = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    with patch("api.routers.jobs.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        mock_db.get_company_by_id.return_value = {"company_id": company_id}
        mock_db.get_level_id.return_value = 3
        mock_db.get_or_create_province.return_value = 7
        mock_db.find_manual_job_duplicate.return_value = (
            {"job_id": job_id} if duplicate else None
        )
        mock_db.create_manual_job.return_value = job_id
        mock_db.get_job_by_id.return_value = _job_row(job_id, company_id)

        result = create_job(
            payload=JobCreate(
                job_title="Data Analyst", company_id=company_id,
                level_code="Junior", province_name="Hà Nội",
            ),
            conn=mock_conn,
            user=ss_team_user,
        )
        return result, mock_db


def test_create_job_new_returns_was_existing_false(mock_conn, ss_team_user):
    result, mock_db = _call_create(mock_conn, ss_team_user, duplicate=False)

    assert result["was_existing"] is False
    assert result["job_title"] == "Data Analyst"
    mock_db.log_action.assert_called_once()
    assert mock_db.log_action.call_args.kwargs["action_type"] == "CREATE_JOB"
    mock_conn.commit.assert_called_once()


def test_create_job_duplicate_returns_was_existing_true_and_no_audit_log(
    mock_conn, ss_team_user
):
    result, mock_db = _call_create(mock_conn, ss_team_user, duplicate=True)

    assert result["was_existing"] is True
    # job cũ được tái sử dụng -> KHÔNG ghi CREATE_JOB (hành vi có sẵn)
    mock_db.log_action.assert_not_called()


def test_job_create_result_extends_job_detail_out_only_for_post():
    """was_existing chỉ có ở JobCreateResult, KHÔNG rò sang JobDetailOut
    (GET /jobs/{id}, PATCH /jobs/{id} dùng chung schema này)."""
    assert issubclass(JobCreateResult, JobDetailOut)
    assert "was_existing" in JobCreateResult.model_fields
    assert "was_existing" not in JobDetailOut.model_fields
