"""
Test hành vi cho 3 route đụng bảng job_applications trên Postgres THẬT:

  - GET    /jobs/applications/{id}/cv-url   (scrapjd.api.routers.jobs.get_cv_signed_url)
  - POST   /me/applications                 (scrapjd.api.routers.me.apply_to_job)
  - DELETE /me/applications/{job_id}        (scrapjd.api.routers.me.withdraw_application)

Viết ra để chốt hành vi TRƯỚC khi đưa SQL thô ở các route này xuống scrapjd/db/
(refactor không đổi hành vi: bộ test này chạy giống hệt trước và sau).
Gọi thẳng hàm route (cùng convention tests/test_api_contacts.py), chỉ giả
lập scrapjd.api.storage (không gọi Supabase thật).

Cách chạy: đặt TEST_DATABASE_URL trỏ tới 1 DB RIÊNG có chữ "test" trong tên
(xem tests/test_pg_integration.py — fixture ở đây cũng DROP SCHEMA public).
Không đặt thì cả file bị bỏ qua.
"""

import io
import os
import sys
import uuid
from unittest.mock import patch
from urllib.parse import urlparse

import psycopg2
import psycopg2.extras
import pytest
from fastapi import HTTPException, UploadFile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scrapjd import db
from scrapjd.api import error_codes
from scrapjd.api.routers import jobs as jobs_router
from scrapjd.api.routers import me as me_router

from conftest import make_fake_request

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")


@pytest.fixture(scope="module")
def pg_conn():
    dbname = urlparse(TEST_DATABASE_URL).path.lstrip("/")
    if "test" not in dbname.lower():
        pytest.fail(
            f"Từ chối chạy: database '{dbname}' không chứa 'test' trong tên "
            "(fixture này DROP SCHEMA public CASCADE)."
        )
    conn = psycopg2.connect(TEST_DATABASE_URL)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    conn.commit()
    db.apply_schema(conn, _SCHEMA_PATH)
    # 2 thay đổi dưới đây thuộc về migration_add_cv_url.sql và
    # migration_add_application_audit_log.sql; cả 2 idempotent nên chạy lại
    # an toàn dù schema.sql đã gộp sẵn hay chưa (fixture không phụ thuộc vào
    # việc schema.sql đã theo kịp migration). ALTER TYPE ... ADD VALUE không
    # được dùng giá trị mới trong cùng transaction -> chạy ở chế độ autocommit.
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE job_applications ADD COLUMN IF NOT EXISTS cv_url TEXT")
        for value in ("APPLY_JOB", "WITHDRAW_JOB_APPLICATION"):
            cur.execute(f"ALTER TYPE audit_action_enum ADD VALUE IF NOT EXISTS '{value}'")
    conn.autocommit = False
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture(autouse=True)
def _clean(pg_conn):
    """Mỗi test bắt đầu từ trạng thái sạch (và kết thúc không để transaction dở)."""
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM audit_logs")
        cur.execute("DELETE FROM job_applications")
    pg_conn.commit()
    yield
    pg_conn.rollback()


def _make_user(conn, role="user"):
    uid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_users (ss_user_id, full_name, email, role) VALUES (%s, %s, %s, %s)",
            (uid, "Học viên Test", f"{uid}@test.local", role),
        )
    conn.commit()
    return {"sub": uid, "email": f"{uid}@test.local", "role": role}


def _make_job(conn, *, status="OPEN", title="Data Analyst Intern"):
    cid, jid = str(uuid.uuid4()), str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)",
                    (cid, f"Công ty {cid[:8]}"))
        cur.execute(
            """INSERT INTO job_postings (job_id, company_id, job_title, job_status)
               VALUES (%s, %s, %s, %s::job_status_enum)""",
            (jid, cid, title, status),
        )
    conn.commit()
    return jid, cid


def _make_application(conn, user_id, job_id, cv_url=None, note=None):
    aid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO job_applications (application_id, ss_user_id, job_id, cv_url, note)
               VALUES (%s, %s, %s, %s, %s)""",
            (aid, user_id, job_id, cv_url, note),
        )
    conn.commit()
    return aid


def _fetch(conn, sql, params=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _pdf(name="cv.pdf", content=b"%PDF-1.4 test"):
    return UploadFile(file=io.BytesIO(content), filename=name)


def _http_error(exc_info):
    return exc_info.value.status_code, exc_info.value.detail["error_code"]


# ---------------------------------------------------------------------
# GET /jobs/applications/{id}/cv-url
# ---------------------------------------------------------------------

def _call_cv_url(conn, application_id, user=None):
    return jobs_router.get_cv_signed_url(
        request=make_fake_request(), application_id=application_id,
        user=user or {"sub": str(uuid.uuid4()), "role": "ss_team"}, conn=conn,
    )


def test_cv_url_invalid_uuid_is_400(pg_conn):
    with pytest.raises(HTTPException) as e:
        _call_cv_url(pg_conn, "không-phải-uuid")
    assert _http_error(e) == (400, error_codes.PROFILE_INVALID)


def test_cv_url_unknown_application_is_404(pg_conn):
    with pytest.raises(HTTPException) as e:
        _call_cv_url(pg_conn, str(uuid.uuid4()))
    assert _http_error(e) == (404, error_codes.PROFILE_CV_NOT_SUBMITTED)


def test_cv_url_application_without_cv_is_404(pg_conn):
    student = _make_user(pg_conn)
    jid, _ = _make_job(pg_conn)
    aid = _make_application(pg_conn, student["sub"], jid, cv_url=None)
    with pytest.raises(HTTPException) as e:
        _call_cv_url(pg_conn, aid)
    assert _http_error(e) == (404, error_codes.PROFILE_CV_NOT_SUBMITTED)


def test_cv_url_returns_signed_url_for_stored_path(pg_conn):
    student = _make_user(pg_conn)
    jid, _ = _make_job(pg_conn)
    aid = _make_application(pg_conn, student["sub"], jid, cv_url="cv-files/u/a.pdf")
    with patch.object(jobs_router.cv_storage, "get_signed_url",
                      return_value="https://signed.example/x") as signed:
        out = _call_cv_url(pg_conn, aid)
    assert out == {"signed_url": "https://signed.example/x"}
    signed.assert_called_once_with("cv-files/u/a.pdf")


def test_cv_url_storage_failure_is_500(pg_conn):
    student = _make_user(pg_conn)
    jid, _ = _make_job(pg_conn)
    aid = _make_application(pg_conn, student["sub"], jid, cv_url="cv-files/u/a.pdf")
    with patch.object(jobs_router.cv_storage, "get_signed_url", return_value=None):
        with pytest.raises(HTTPException) as e:
            _call_cv_url(pg_conn, aid)
    assert _http_error(e) == (500, error_codes.PROFILE_CANNOT_CREATE)


# ---------------------------------------------------------------------
# POST /me/applications
# ---------------------------------------------------------------------

def _call_apply(conn, user, job_id, cv_file=None, note="Em muốn ứng tuyển"):
    return me_router.apply_to_job(
        request=make_fake_request(), job_id=job_id, note=note,
        cv_file=cv_file or _pdf(), user=user, conn=conn,
    )


def test_apply_success_stores_cv_path_and_returns_application(pg_conn):
    student = _make_user(pg_conn)
    jid, _ = _make_job(pg_conn)

    def fake_upload(*, file_bytes, user_id, application_id):
        assert file_bytes.startswith(b"%PDF")
        assert user_id == student["sub"]
        return f"cv-files/{user_id}/{application_id}.pdf"

    with patch.object(me_router.cv_storage, "upload_cv", side_effect=fake_upload):
        out = _call_apply(pg_conn, student, jid)

    rows = _fetch(pg_conn, "SELECT * FROM job_applications")
    assert len(rows) == 1
    assert rows[0]["cv_url"] == f"cv-files/{student['sub']}/{rows[0]['application_id']}.pdf"
    assert rows[0]["note"] == "Em muốn ứng tuyển"
    assert str(out["application_id"]) == str(rows[0]["application_id"])
    assert out["cv_url"] == rows[0]["cv_url"]
    logs = _fetch(pg_conn, "SELECT action_type, entity_id FROM audit_logs")
    assert [(r["action_type"], str(r["entity_id"])) for r in logs] == \
           [("APPLY_JOB", str(rows[0]["application_id"]))]


def test_apply_upload_failure_rolls_back_the_application(pg_conn):
    """Upload CV lỗi -> 500 và KHÔNG để lại đơn ứng tuyển mồ côi (đơn tạo ở
    bước 2 cùng transaction với UPDATE cv_url, rollback ở bước 3)."""
    student = _make_user(pg_conn)
    jid, _ = _make_job(pg_conn)
    with patch.object(me_router.cv_storage, "upload_cv", side_effect=RuntimeError("storage sập")):
        with pytest.raises(HTTPException) as e:
            _call_apply(pg_conn, student, jid)
    assert _http_error(e) == (500, error_codes.PROFILE_CV_UPLOAD_FAILED)
    assert _fetch(pg_conn, "SELECT 1 FROM job_applications") == []


def test_apply_twice_is_409_and_keeps_first_application(pg_conn):
    student = _make_user(pg_conn)
    jid, _ = _make_job(pg_conn)
    with patch.object(me_router.cv_storage, "upload_cv", return_value="cv-files/x.pdf"):
        _call_apply(pg_conn, student, jid)
        with pytest.raises(HTTPException) as e:
            _call_apply(pg_conn, student, jid)
    assert _http_error(e) == (409, error_codes.PROFILE_ALREADY_APPLIED)
    assert len(_fetch(pg_conn, "SELECT 1 FROM job_applications")) == 1


def test_apply_to_closed_job_is_400(pg_conn):
    student = _make_user(pg_conn)
    jid, _ = _make_job(pg_conn, status="CLOSED")
    with pytest.raises(HTTPException) as e:
        _call_apply(pg_conn, student, jid)
    assert _http_error(e) == (400, error_codes.PROFILE_JOB_STATUS_NOT_APPLICABLE)


def test_apply_with_non_pdf_is_400_and_creates_nothing(pg_conn):
    student = _make_user(pg_conn)
    jid, _ = _make_job(pg_conn)
    with pytest.raises(HTTPException) as e:
        _call_apply(pg_conn, student, jid, cv_file=_pdf("cv.docx"))
    assert _http_error(e) == (400, error_codes.PROFILE_CV_FORMAT_INVALID)
    assert _fetch(pg_conn, "SELECT 1 FROM job_applications") == []


def test_apply_with_missing_filename_is_400_not_a_crash(pg_conn):
    # UploadFile.filename có kiểu Optional[str]. Thiếu tên file phải bị từ chối như file sai định dạng (400),
    # không được văng AttributeError (500) ở bước kiểm đuôi .pdf.
    student = _make_user(pg_conn)
    jid, _ = _make_job(pg_conn)
    with pytest.raises(HTTPException) as e:
        _call_apply(pg_conn, student, jid, cv_file=_pdf(name=None))
    assert _http_error(e) == (400, error_codes.PROFILE_CV_FORMAT_INVALID)
    assert _fetch(pg_conn, "SELECT 1 FROM job_applications") == []


# ---------------------------------------------------------------------
# DELETE /me/applications/{job_id}
# ---------------------------------------------------------------------

def _call_withdraw(conn, user, job_id, note=None):
    return me_router.withdraw_application(job_id=job_id, note=note, user=user, conn=conn)


def test_withdraw_deletes_application_logs_audit_and_removes_cv(pg_conn):
    student = _make_user(pg_conn)
    jid, cid = _make_job(pg_conn, title="Senior BA")
    aid = _make_application(pg_conn, student["sub"], jid, cv_url="cv-files/u/a.pdf")

    with patch.object(me_router.cv_storage, "delete_cv") as delete_cv:
        out = _call_withdraw(pg_conn, student, jid, note="Đã nhận offer khác")

    assert out is None
    assert _fetch(pg_conn, "SELECT 1 FROM job_applications") == []
    delete_cv.assert_called_once_with("cv-files/u/a.pdf")
    logs = _fetch(pg_conn, "SELECT * FROM audit_logs")
    assert len(logs) == 1
    assert logs[0]["action_type"] == "WITHDRAW_JOB_APPLICATION"
    assert str(logs[0]["entity_id"]) == aid
    assert logs[0]["entity_label"] == "Senior BA"
    assert str(logs[0]["company_id"]) == cid
    assert logs[0]["note"] == "Đã nhận offer khác"


def test_withdraw_without_cv_does_not_call_storage(pg_conn):
    student = _make_user(pg_conn)
    jid, _ = _make_job(pg_conn)
    _make_application(pg_conn, student["sub"], jid, cv_url=None)
    with patch.object(me_router.cv_storage, "delete_cv") as delete_cv:
        _call_withdraw(pg_conn, student, jid)
    delete_cv.assert_not_called()
    assert _fetch(pg_conn, "SELECT 1 FROM job_applications") == []


def test_withdraw_when_not_applied_is_404_and_writes_no_audit(pg_conn):
    student = _make_user(pg_conn)
    jid, _ = _make_job(pg_conn)
    with patch.object(me_router.cv_storage, "delete_cv") as delete_cv:
        with pytest.raises(HTTPException) as e:
            _call_withdraw(pg_conn, student, jid)
    assert _http_error(e) == (404, error_codes.PROFILE_NOT_APPLIED_YET)
    delete_cv.assert_not_called()
    assert _fetch(pg_conn, "SELECT 1 FROM audit_logs") == []


def test_withdraw_only_affects_own_application(pg_conn):
    alice, bob = _make_user(pg_conn), _make_user(pg_conn)
    jid, _ = _make_job(pg_conn)
    _make_application(pg_conn, alice["sub"], jid, cv_url="cv-files/alice.pdf")
    _make_application(pg_conn, bob["sub"], jid, cv_url="cv-files/bob.pdf")
    with patch.object(me_router.cv_storage, "delete_cv") as delete_cv:
        _call_withdraw(pg_conn, alice, jid)
    delete_cv.assert_called_once_with("cv-files/alice.pdf")
    left = _fetch(pg_conn, "SELECT ss_user_id FROM job_applications")
    assert [str(r["ss_user_id"]) for r in left] == [bob["sub"]]


def test_withdraw_invalid_uuid_is_400(pg_conn):
    student = _make_user(pg_conn)
    with pytest.raises(HTTPException) as e:
        _call_withdraw(pg_conn, student, "xxx")
    assert _http_error(e) == (400, error_codes.PROFILE_JOB_ID_INVALID_UUID)


# ---------------------------------------------------------------------
# Hàm db.* mới (get_application_cv_url / set_application_cv_url /
# get_application_with_job_info)
# ---------------------------------------------------------------------

def test_get_application_cv_url_none_for_unknown_and_for_missing_cv(pg_conn):
    student = _make_user(pg_conn)
    jid, _ = _make_job(pg_conn)
    aid = _make_application(pg_conn, student["sub"], jid, cv_url=None)
    assert db.get_application_cv_url(pg_conn, str(uuid.uuid4())) is None
    assert db.get_application_cv_url(pg_conn, aid) is None


def test_set_application_cv_url_does_not_commit(pg_conn):
    """Phải nằm chung transaction với tạo đơn + audit log: rollback thì mất."""
    student = _make_user(pg_conn)
    jid, _ = _make_job(pg_conn)
    aid = _make_application(pg_conn, student["sub"], jid, cv_url=None)

    db.set_application_cv_url(pg_conn, aid, "cv-files/new.pdf")
    assert db.get_application_cv_url(pg_conn, aid) == "cv-files/new.pdf"
    pg_conn.rollback()
    assert db.get_application_cv_url(pg_conn, aid) is None


def test_get_application_with_job_info_returns_row_or_none(pg_conn):
    student = _make_user(pg_conn)
    jid, cid = _make_job(pg_conn, title="Data Engineer")
    aid = _make_application(pg_conn, student["sub"], jid, cv_url="cv-files/a.pdf")

    row = db.get_application_with_job_info(pg_conn, ss_user_id=student["sub"], job_id=jid)
    assert str(row["application_id"]) == aid
    assert row["cv_url"] == "cv-files/a.pdf"
    assert row["job_title"] == "Data Engineer"
    assert str(row["company_id"]) == cid

    other = _make_user(pg_conn)
    assert db.get_application_with_job_info(pg_conn, ss_user_id=other["sub"], job_id=jid) is None
