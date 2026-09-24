"""
Test cho việc XOÁ giá trị job qua PATCH /jobs/{id} (thêm 09/2026, phục vụ
JobForm trang sửa job bên Next.js — xem JobUpdate docstring,
db.jobs.JOB_CLEARABLE_FIELD_TO_COLUMN):

- 4 field deadline/level_code/province_name/work_type gửi RÕ `null` = xoá
  (NULL), không gửi = giữ nguyên.
- db.update_job() có tham số riêng `clear_fields`; mọi nơi gọi cũ (nhất là
  import_executor, vốn truyền None với nghĩa "bỏ qua") KHÔNG bị ảnh hưởng.

Cùng convention với tests/test_api_contacts.py: gọi thẳng hàm route, mock
`api.routers.jobs.db_module`, không qua HTTP/DB thật.
"""
import re
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from api.schemas import JobUpdate
from db.audit_logs import diff_changed_fields
from db.jobs import JOB_CLEARABLE_FIELD_TO_COLUMN, update_job


# ------------------------------------------------------------------
# db.update_job(clear_fields=...)
# ------------------------------------------------------------------


def _conn_and_cursor():
    cur = MagicMock()
    cur.rowcount = 1
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    return conn, cur


def _executed_sql(cur) -> str:
    return cur.execute.call_args[0][0]


def test_update_job_clear_fields_sets_columns_to_null():
    conn, cur = _conn_and_cursor()

    ok = update_job(
        conn, str(uuid.uuid4()),
        clear_fields={"deadline", "level_id", "province_id", "work_type"},
    )

    assert ok is True
    sql = _executed_sql(cur)
    for col in ("deadline", "level_id", "province_id", "work_type"):
        assert f"{col} = NULL" in sql


def test_update_job_clear_fields_can_be_combined_with_other_updates():
    conn, cur = _conn_and_cursor()

    update_job(conn, str(uuid.uuid4()), job_title="Mới", clear_fields={"deadline"})

    sql = _executed_sql(cur)
    assert "job_title = %s" in sql
    assert "deadline = NULL" in sql
    # tham số truyền cho %s chỉ gồm job_title + job_id, KHÔNG có giá trị
    # nào cho cột bị xoá (NULL viết thẳng vào câu SQL)
    assert cur.execute.call_args[0][1][0] == "Mới"
    assert len(cur.execute.call_args[0][1]) == 2


def test_update_job_default_none_args_do_not_touch_clearable_columns():
    """REGRESSION cho luồng import (import_executor::_update_row): nơi
    đó truyền level_id/province_id/work_type/deadline = None với nghĩa
    'ô trống -> giữ nguyên'. Không có clear_fields thì TUYỆT ĐỐI không
    được chạm 4 cột này."""
    conn, cur = _conn_and_cursor()

    update_job(
        conn, str(uuid.uuid4()),
        job_title="X", level_id=None, province_id=None,
        work_type=None, deadline=None,
    )

    sql = _executed_sql(cur)
    for col in ("deadline", "level_id", "province_id", "work_type"):
        assert col not in sql


def test_update_job_rejects_column_outside_whitelist():
    conn, _ = _conn_and_cursor()

    with pytest.raises(ValueError):
        update_job(conn, str(uuid.uuid4()), clear_fields={"job_title"})
    with pytest.raises(ValueError):
        update_job(conn, str(uuid.uuid4()), clear_fields={"deadline; DROP TABLE x"})


def test_update_job_rejects_set_and_clear_same_column():
    conn, _ = _conn_and_cursor()

    with pytest.raises(ValueError):
        update_job(conn, str(uuid.uuid4()), level_id=3, clear_fields={"level_id"})


def test_clearable_columns_are_nullable_in_schema():
    """Whitelist chỉ được chứa cột mà DB cho phép NULL — nếu ai đó thêm
    NOT NULL vào 1 trong 4 cột này (hoặc thêm cột NOT NULL vào whitelist)
    thì chức năng xoá sẽ 500 ở runtime; bắt ngay ở test."""
    root = Path(__file__).resolve().parent.parent
    sql = (root / "sql" / "schema.sql").read_text(encoding="utf-8")
    start = sql.index("CREATE TABLE IF NOT EXISTS job_postings")
    block = sql[start:sql.index(");", start)]
    for col in JOB_CLEARABLE_FIELD_TO_COLUMN.values():
        line = re.search(rf"^\s*{col}\s+.*$", block, re.MULTILINE)
        assert line, f"không thấy cột {col} trong job_postings"
        assert "NOT NULL" not in line.group(0)


# ------------------------------------------------------------------
# PATCH /jobs/{id} (api.routers.jobs.patch_job)
# ------------------------------------------------------------------


def _existing_job(job_id: str) -> dict:
    return {
        "job_id": job_id,
        "job_title": "Data Analyst",
        "company_id": str(uuid.uuid4()),
        "level_code": "Junior",
        "province_name": "Hà Nội",
        "work_type": "FULL_TIME",
        "deadline": "2026-12-31",
        "job_status": "OPEN",
    }


def _call_patch(mock_conn, ss_team_user, body: dict):
    """Gọi patch_job với body JSON THÔ (model_validate) — đúng cách FastAPI
    dựng payload từ request, nên model_fields_set phản ánh đúng field nào
    client thực sự gửi."""
    from api.routers.jobs import patch_job

    job_id = str(uuid.uuid4())
    with patch("api.routers.jobs.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        mock_db.get_job_by_id.return_value = _existing_job(job_id)
        mock_db.update_job.return_value = True
        mock_db.get_level_id.return_value = 3
        mock_db.get_or_create_province.return_value = 7
        mock_db.diff_changed_fields.side_effect = diff_changed_fields

        patch_job(
            job_id=job_id,
            payload=JobUpdate.model_validate(body),
            conn=mock_conn,
            user=ss_team_user,
        )
        return mock_db


def test_patch_job_explicit_null_clears_the_four_clearable_fields(mock_conn, ss_team_user):
    mock_db = _call_patch(
        mock_conn, ss_team_user,
        {"deadline": None, "level_code": None, "province_name": None, "work_type": None},
    )

    kwargs = mock_db.update_job.call_args.kwargs
    assert kwargs["clear_fields"] == {"deadline", "level_id", "province_id", "work_type"}
    # không có giá trị mới để tra -> không gọi hàm tra level/tỉnh
    mock_db.get_level_id.assert_not_called()
    mock_db.get_or_create_province.assert_not_called()
    assert kwargs["level_id"] is None and kwargs["province_id"] is None


def test_patch_job_omitted_fields_are_not_cleared(mock_conn, ss_team_user):
    mock_db = _call_patch(mock_conn, ss_team_user, {"job_title": "Đổi tên"})

    assert mock_db.update_job.call_args.kwargs["clear_fields"] == set()


def test_patch_job_new_value_is_not_treated_as_clear(mock_conn, ss_team_user):
    mock_db = _call_patch(
        mock_conn, ss_team_user,
        {"level_code": "Senior", "province_name": "Đà Nẵng", "deadline": "2027-01-15"},
    )

    kwargs = mock_db.update_job.call_args.kwargs
    assert kwargs["clear_fields"] == set()
    assert kwargs["level_id"] == 3 and kwargs["province_id"] == 7


def test_patch_job_null_on_non_clearable_field_is_still_ignored(mock_conn, ss_team_user):
    """job_title/currency/salary_type/... gửi null vẫn bị bỏ qua như trước
    (không đưa vào clear_fields) — chỉ 4 field trong whitelist mới xoá được."""
    mock_db = _call_patch(
        mock_conn, ss_team_user,
        {"job_title": None, "currency": None, "salary_type": None, "job_status": None},
    )

    assert mock_db.update_job.call_args.kwargs["clear_fields"] == set()


def test_patch_job_clearing_is_recorded_in_audit_log(mock_conn, ss_team_user):
    mock_db = _call_patch(
        mock_conn, ss_team_user,
        {"level_code": None, "province_name": None, "deadline": None},
    )

    mock_db.log_action.assert_called_once()
    changes = mock_db.log_action.call_args.kwargs["changes"]
    assert changes["level_code"] == {"old": "Junior", "new": None}
    assert changes["province_name"] == {"old": "Hà Nội", "new": None}
    assert changes["deadline"] == {"old": "2026-12-31", "new": None}


def test_patch_job_clearing_already_empty_field_logs_nothing(mock_conn, ss_team_user):
    """Xoá 1 field vốn đã trống không phải thay đổi thật -> không ghi log."""
    from api.routers.jobs import patch_job

    job_id = str(uuid.uuid4())
    existing = _existing_job(job_id)
    existing["deadline"] = None
    with patch("api.routers.jobs.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        mock_db.get_job_by_id.return_value = existing
        mock_db.update_job.return_value = True
        mock_db.diff_changed_fields.side_effect = diff_changed_fields

        patch_job(
            job_id=job_id,
            payload=JobUpdate.model_validate({"deadline": None}),
            conn=mock_conn,
            user=ss_team_user,
        )

    mock_db.log_action.assert_not_called()
