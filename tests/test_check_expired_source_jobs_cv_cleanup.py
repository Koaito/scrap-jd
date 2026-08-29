"""
Test cho check_expired_source_jobs.cleanup_cvs_of_closed_jobs() — dọn CV
của application thuộc job đã CLOSED, thêm 08/2026 (xem việc_chưa_làm.txt
mục "chưa có hệ thống tự động dọn CV của học viên khi đã ứng tuyển
những Job hết hạn").

Mock ở 2 điểm: db.list_closed_job_applications_with_cv/clear_application_cv
(không cần Postgres thật) và cv_storage.delete_cv (không cần Supabase
thật) — giống cách tests/test_api_import_export.py mock db_module ở
router. conn truyền vào là mock_conn (xem tests/conftest.py).

Chạy: pytest tests/test_check_expired_source_jobs_cv_cleanup.py -v
"""
from unittest.mock import patch

import check_expired_source_jobs as script


def _fake_applications(n):
    return [
        {"application_id": f"app-{i}", "cv_url": f"cv-files/user-{i}/app-{i}.pdf"}
        for i in range(n)
    ]


def test_no_applications_to_clean(mock_conn):
    with patch.object(script.db, "list_closed_job_applications_with_cv", return_value=[]):
        stats = script.cleanup_cvs_of_closed_jobs(mock_conn)
    assert stats == {"cv_cleaned": 0, "cv_cleanup_errors": 0}
    mock_conn.commit.assert_not_called()


def test_cleans_each_application_and_commits(mock_conn):
    apps = _fake_applications(3)
    with patch.object(script.db, "list_closed_job_applications_with_cv", return_value=apps), \
         patch.object(script.db, "clear_application_cv") as mock_clear, \
         patch.object(script.cv_storage, "delete_cv") as mock_delete:
        stats = script.cleanup_cvs_of_closed_jobs(mock_conn)

    assert stats == {"cv_cleaned": 3, "cv_cleanup_errors": 0}
    assert mock_delete.call_count == 3
    assert mock_clear.call_count == 3
    # Mỗi application phải được gọi ĐÚNG cv_url/application_id của nó
    # (không lẫn lộn giữa các vòng lặp).
    mock_delete.assert_any_call("cv-files/user-0/app-0.pdf")
    mock_clear.assert_any_call(mock_conn, "app-0")
    assert mock_conn.commit.call_count == 3


def test_dry_run_does_not_touch_storage_or_db(mock_conn):
    """dry_run=True -> chỉ ĐẾM, KHÔNG gọi delete_cv/clear_application_cv/
    commit — đúng tinh thần dry-run sẵn có của script (xem --dry-run ở
    docstring đầu file)."""
    apps = _fake_applications(2)
    with patch.object(script.db, "list_closed_job_applications_with_cv", return_value=apps), \
         patch.object(script.db, "clear_application_cv") as mock_clear, \
         patch.object(script.cv_storage, "delete_cv") as mock_delete:
        stats = script.cleanup_cvs_of_closed_jobs(mock_conn, dry_run=True)

    assert stats == {"cv_cleaned": 2, "cv_cleanup_errors": 0}
    mock_delete.assert_not_called()
    mock_clear.assert_not_called()
    mock_conn.commit.assert_not_called()


def test_error_on_one_application_does_not_stop_the_rest(mock_conn):
    """1 application lỗi (vd DB tạm gián đoạn) -> KHÔNG được chặn các
    application còn lại trong cùng lượt dọn, và phải rollback đúng
    application lỗi đó."""
    apps = _fake_applications(3)

    def clear_side_effect(conn, application_id):
        if application_id == "app-1":
            raise RuntimeError("DB tạm lỗi")

    with patch.object(script.db, "list_closed_job_applications_with_cv", return_value=apps), \
         patch.object(script.db, "clear_application_cv", side_effect=clear_side_effect), \
         patch.object(script.cv_storage, "delete_cv"):
        stats = script.cleanup_cvs_of_closed_jobs(mock_conn)

    assert stats == {"cv_cleaned": 2, "cv_cleanup_errors": 1}
    mock_conn.rollback.assert_called_once()


def test_run_calls_cleanup_by_default(mock_conn):
    """run() phải tự gọi cleanup_cvs_of_closed_jobs() SAU vòng đóng job,
    kể cả khi không truyền skip_cv_cleanup (mặc định BẬT SẴN — xem
    docstring mục DỌN CV)."""
    with patch.object(script.db, "get_connection", return_value=mock_conn), \
         patch.object(script.db, "get_open_jobs_with_source_url", return_value=[]), \
         patch.object(script, "cleanup_cvs_of_closed_jobs", return_value={
             "cv_cleaned": 5, "cv_cleanup_errors": 0,
         }) as mock_cleanup:
        stats = script.run()

    mock_cleanup.assert_called_once_with(mock_conn, dry_run=False)
    assert stats["cv_cleaned"] == 5
    assert stats["cv_cleanup_errors"] == 0


def test_run_skips_cleanup_when_flag_set(mock_conn):
    with patch.object(script.db, "get_connection", return_value=mock_conn), \
         patch.object(script.db, "get_open_jobs_with_source_url", return_value=[]), \
         patch.object(script, "cleanup_cvs_of_closed_jobs") as mock_cleanup:
        stats = script.run(skip_cv_cleanup=True)

    mock_cleanup.assert_not_called()
    assert stats["cv_cleaned"] == 0
    assert stats["cv_cleanup_errors"] == 0
