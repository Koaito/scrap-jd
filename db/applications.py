"""
db.applications — tách từ db.py (God module) theo domain, xem README/kế hoạch refactor.
"""

import logging
from typing import Optional

import psycopg2.extras
import psycopg2

logger = logging.getLogger(__name__)


def create_job_application(conn, *, ss_user_id: str, job_id: str, note: Optional[str] = None, cv_url: Optional[str] = None) -> str:
    """Raise psycopg2.errors.UniqueViolation nếu user đã ứng tuyển job
    này rồi (uq_job_applications_user_job) — router bắt lỗi này để trả
    409 thay vì để lộ traceback 500."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO job_applications (ss_user_id, job_id, note, cv_url)
            VALUES (%s, %s, %s, %s)
            RETURNING application_id
            """,
            (ss_user_id, job_id, note, cv_url),
        )
        return str(cur.fetchone()[0])


def list_applications_for_user(conn, ss_user_id: str):
    """Đơn ứng tuyển của 1 học viên — join thêm job_title/company_name
    để hiển thị trực tiếp, không cần frontend gọi thêm GET /jobs/{id}
    cho từng dòng."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT a.application_id, a.ss_user_id, a.job_id, a.note, a.applied_at,
                   a.cv_url,
                   j.job_title, j.job_status, c.company_name
            FROM job_applications a
            JOIN job_postings j ON j.job_id = a.job_id
            JOIN companies c ON c.company_id = j.company_id
            WHERE a.ss_user_id = %s
            ORDER BY a.applied_at DESC
            """,
            (ss_user_id,),
        )
        return cur.fetchall()


def list_applications_for_job(conn, job_id: str):
    """Ai đã ứng tuyển 1 job — staff (ss_team+) dùng để chủ động gửi hồ
    sơ cho HR. Join thêm full_name/email/phone từ app_users (bảng dùng
    chung cho mọi role, xem migration_add_role_hierarchy.sql) để staff
    khỏi phải tra riêng. phone thêm 08/2026 (xem
    migration_add_phone_track.sql) — có thể NULL nếu học viên đăng ký
    trước khi cột này tồn tại, hoặc bỏ trống lúc đăng ký (không bắt buộc)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT a.application_id, a.ss_user_id, a.job_id, a.note, a.applied_at,
                   a.cv_url,
                   u.full_name, u.email, u.phone
            FROM job_applications a
            JOIN app_users u ON u.ss_user_id = a.ss_user_id
            WHERE a.job_id = %s
            ORDER BY a.applied_at DESC
            """,
            (job_id,),
        )
        return cur.fetchall()


def delete_job_application(conn, *, ss_user_id: str, job_id: str) -> bool:
    """Huỷ ứng tuyển — DELETE thật (08/2026, đổi ý so với thiết kế ban
    đầu coi ứng tuyển là "sự kiện lịch sử không sửa/xoá" — xem lịch sử
    trao đổi: học viên cần rút lại được nếu bấm nhầm/đổi ý). Không cần
    is_active/soft-delete kiểu company_contacts — application không có
    giá trị tra cứu lịch sử như HR contact, xoá thật đơn giản hơn và
    học viên có thể ứng tuyển lại (uq_job_applications_user_job không
    còn chặn vì record cũ đã mất).

    Trả False nếu chưa từng ứng tuyển job này (không có gì để xoá) — route
    dùng để trả 404 đúng lúc."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM job_applications WHERE ss_user_id = %s AND job_id = %s",
            (ss_user_id, job_id),
        )
        return cur.rowcount > 0


def create_saved_job(conn, *, ss_user_id: str, job_id: str) -> str:
    """Raise psycopg2.errors.UniqueViolation nếu job đã được lưu rồi
    (uq_saved_jobs_user_job) — router bắt lỗi này để trả 409."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO saved_jobs (ss_user_id, job_id)
            VALUES (%s, %s)
            RETURNING saved_job_id
            """,
            (ss_user_id, job_id),
        )
        return str(cur.fetchone()[0])


def list_saved_jobs_for_user(conn, ss_user_id: str):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT s.saved_job_id, s.ss_user_id, s.job_id, s.created_at,
                   j.job_title, j.job_status, c.company_name
            FROM saved_jobs s
            JOIN job_postings j ON j.job_id = s.job_id
            JOIN companies c ON c.company_id = j.company_id
            WHERE s.ss_user_id = %s
            ORDER BY s.created_at DESC
            """,
            (ss_user_id,),
        )
        return cur.fetchall()


def delete_saved_job(conn, *, ss_user_id: str, job_id: str) -> bool:
    """Bỏ lưu — DELETE thật (không soft-delete, đây chỉ là bookmark,
    không cần giữ lịch sử như company_contacts)."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM saved_jobs WHERE ss_user_id = %s AND job_id = %s",
            (ss_user_id, job_id),
        )
        return cur.rowcount > 0


def list_saved_jobs_for_job(conn, job_id: str):
    """Ai đã lưu 1 job — staff (ss_team+) dùng để biết job nào đang
    được học viên quan tâm nhiều (kể cả chưa ứng tuyển), từ đó chủ động
    nhắc/hỗ trợ. Mirror ĐÚNG list_applications_for_job() ở trên — join
    thêm full_name/email/phone từ app_users để staff khỏi tra riêng.
    Thêm 08/2026 cùng lúc với việc đảo ngược quyết định riêng tư saved
    jobs (xem comment đầu khối "Saved jobs" phía trên)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT s.saved_job_id, s.ss_user_id, s.job_id, s.created_at,
                   u.full_name, u.email, u.phone
            FROM saved_jobs s
            JOIN app_users u ON u.ss_user_id = s.ss_user_id
            WHERE s.job_id = %s
            ORDER BY s.created_at DESC
            """,
            (job_id,),
        )
        return cur.fetchall()
