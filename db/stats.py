"""
db.stats — tách từ db.py (God module) theo domain, xem README/kế hoạch refactor.
"""

import logging

import psycopg2.extras
import psycopg2

logger = logging.getLogger(__name__)


def get_stats_summary(conn) -> dict:
    """Số liệu tổng quan cho dashboard — GET /stats."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT count(*) AS total_jobs FROM job_postings")
        total_jobs = cur.fetchone()["total_jobs"]

        cur.execute("SELECT count(*) AS total_companies FROM companies")
        total_companies = cur.fetchone()["total_companies"]

        cur.execute(
            "SELECT count(*) AS n FROM companies "
            "WHERE fanpage_url IS NOT NULL OR linkedin_url IS NOT NULL"
        )
        companies_with_social = cur.fetchone()["n"]

        cur.execute(
            "SELECT matching_industry, count(*) AS n FROM job_postings "
            "WHERE matching_industry IS NOT NULL "
            "GROUP BY matching_industry ORDER BY n DESC"
        )
        by_industry = cur.fetchall()

        cur.execute(
            "SELECT source_name, count(*) AS n FROM job_sources_log "
            "GROUP BY source_name ORDER BY n DESC"
        )
        by_source = cur.fetchall()

        # Thêm 08/2026 — dashboard frontend cần tổng số đơn ứng tuyển
        # toàn hệ thống, trước đây không có cách nào lấy được ngoại trừ
        # cộng dồn list_applications_for_job() cho từng job (tốn N lượt
        # gọi API). Đếm thẳng 1 lần ở đây rẻ hơn nhiều so với thêm 1
        # endpoint /stats/applications riêng.
        cur.execute("SELECT count(*) AS n FROM job_applications")
        total_applications = cur.fetchone()["n"]

        # Thêm 08/2026 cùng lúc với việc cho staff xem saved_jobs (xem
        # db.list_saved_jobs_for_job()) — để dashboard hiện cân xứng với
        # total_applications ở trên, cùng cách đếm thẳng 1 lần.
        cur.execute("SELECT count(*) AS n FROM saved_jobs")
        total_saved_jobs = cur.fetchone()["n"]

    return {
        "total_jobs": total_jobs,
        "total_companies": total_companies,
        "companies_with_social": companies_with_social,
        "by_industry": by_industry,
        "by_source": by_source,
        "total_applications": total_applications,
        "total_saved_jobs": total_saved_jobs,
    }


def get_job_engagement_counts(conn) -> list[dict]:
    """Đếm số lượt lưu + ứng tuyển của TỪNG job đang OPEN, gộp sẵn 1
    lần cho toàn bộ hệ thống — dùng cho dashboard frontend (nhóm "JD
    ế": job đăng lâu nhưng 0 lượt quan tâm). Trước đây không có cách
    lấy số này ngoại trừ gọi GET /jobs/{id}/applications +
    /jobs/{id}/saved-jobs CHO TỪNG job (N+1, ~200 job/lần là ~400
    request) — hàm này gộp bằng 2 GROUP BY rồi JOIN, luôn đúng 2 query
    bất kể có bao nhiêu job.

    Chỉ lấy job job_status = 'OPEN' (job đã đóng/hết hạn không còn ý
    nghĩa để "đẩy" cho học viên, frontend cũng không cần biết lượt
    quan tâm của job đã đóng)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                jp.job_id,
                jp.job_title,
                jp.deadline,
                jp.created_at,
                COALESCE(app_counts.n, 0) AS application_count,
                COALESCE(saved_counts.n, 0) AS saved_count
            FROM job_postings jp
            LEFT JOIN (
                SELECT job_id, count(*) AS n
                FROM job_applications
                GROUP BY job_id
            ) app_counts ON app_counts.job_id = jp.job_id
            LEFT JOIN (
                SELECT job_id, count(*) AS n
                FROM saved_jobs
                GROUP BY job_id
            ) saved_counts ON saved_counts.job_id = jp.job_id
            WHERE jp.job_status = 'OPEN'
            ORDER BY jp.created_at DESC
            """
        )
        return cur.fetchall()


def get_monthly_engagement_stats(conn) -> dict:
    """So sánh số ứng tuyển/lưu job THÁNG NÀY vs THÁNG TRƯỚC (theo
    calendar month, dùng applied_at/created_at thật của từng dòng) —
    dùng cho tab "Báo cáo tháng" bên frontend. GET /stats hiện có chỉ
    trả TỔNG DỒN (total_applications/total_saved_jobs), không có
    breakdown theo tháng nên không tính được % tăng/giảm.

    date_trunc('month', now()) lấy đúng đầu tháng hiện tại theo
    UTC (cột applied_at/created_at đều TIMESTAMPTZ) — nhất quán với
    cách _jobs_by_month() bên frontend (mindx-jobs/app.py) đang nhóm
    job theo tháng, không lệch múi giờ giữa 2 phía."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                count(*) FILTER (
                    WHERE applied_at >= date_trunc('month', now())
                ) AS this_month,
                count(*) FILTER (
                    WHERE applied_at >= date_trunc('month', now()) - interval '1 month'
                      AND applied_at < date_trunc('month', now())
                ) AS last_month
            FROM job_applications
            """
        )
        applications = cur.fetchone()

        cur.execute(
            """
            SELECT
                count(*) FILTER (
                    WHERE created_at >= date_trunc('month', now())
                ) AS this_month,
                count(*) FILTER (
                    WHERE created_at >= date_trunc('month', now()) - interval '1 month'
                      AND created_at < date_trunc('month', now())
                ) AS last_month
            FROM saved_jobs
            """
        )
        saved_jobs = cur.fetchone()

    return {
        "applications": dict(applications),
        "saved_jobs": dict(saved_jobs),
    }
