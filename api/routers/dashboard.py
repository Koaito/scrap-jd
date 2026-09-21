"""
Dashboard insights — 8 khối tính toán của trang /dashboard, chuyển từ
Python phía Flask sang SQL phía backend (Phần 5 mục 8 của plan Next.js).
Chia 3 endpoint theo đúng 3 tab có dữ liệu insight (tab "Tổng quan" không
có khối nào trong 8 khối này — nó chỉ dùng GET /stats):

    GET /dashboard/insights/students   tab "Gợi ý học viên"
    GET /dashboard/insights/companies  tab "Doanh nghiệp"
    GET /dashboard/insights/monthly    tab "Báo cáo tháng"

Tách 3 endpoint (không gộp 1) để Next.js chỉ tải đúng tab đang mở. Toàn
bộ yêu cầu ss_team trở lên — trang /dashboard vốn staff-only, và tab
"Doanh nghiệp" chứa tên/ngày liên hệ của contact (dữ liệu nhạy cảm).
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import db as db_module
from api import error_codes
from api.deps import get_db, require_role
from api.rate_limit import get_user_id_or_ip, limiter
from api.schemas import CompanyInsightsOut, MonthlyInsightsOut, StudentInsightsOut

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

# Whitelist cố định cho ô chọn "Contact cần follow-up" (giống
# FOLLOWUP_DAYS_OPTIONS bên Flask): tránh giá trị vô lý (0, âm, quá lớn)
# làm bảng rỗng/vô nghĩa. Khác Flask ở 1 điểm: giá trị ngoài whitelist trả
# 400 rõ ràng thay vì lặng lẽ rơi về mặc định (API không nên nuốt lỗi —
# Next.js tự validate trước theo plan, nên gặp 400 này là lỗi lập trình).
FOLLOWUP_DAYS_OPTIONS = (7, 14, 30)
FOLLOWUP_DAYS_DEFAULT = 14


def _pct_change(current: int, previous: int):
    """% thay đổi so với kỳ trước, làm tròn bằng round() của Python (làm
    tròn về số chẵn khi đúng .5 — GIỮ NGUYÊN như _pct_change bên Flask để
    2 bản ra cùng con số). previous rỗng/0 -> None (không chia được)."""
    if not previous:
        return None
    return round((current - previous) / previous * 100)


@router.get("/insights/students", response_model=StudentInsightsOut)
@limiter.limit("30/minute", key_func=get_user_id_or_ip)
def get_student_insights(
    request: Request,
    user: dict = Depends(require_role("ss_team")),
    conn=Depends(get_db),
):
    """Tab "Gợi ý học viên": JD sắp hết hạn cần đẩy (deadline còn 7-14
    ngày, chưa ai lưu/ứng tuyển), JD "ế" (thu thập >= 30 ngày, chưa ai
    quan tâm), top 10 kỹ năng 30 ngày gần đây, lương trung bình theo
    ngành/level. Ngày tính theo giờ Việt Nam."""
    return {
        "jd_needing_push": db_module.get_jobs_needing_push(conn),
        "jd_stale": db_module.get_stale_jobs(conn),
        "top_skills": db_module.get_top_skills(conn),
        "salary_ranges": db_module.get_salary_ranges(conn),
    }


@router.get("/insights/companies", response_model=CompanyInsightsOut)
@limiter.limit("30/minute", key_func=get_user_id_or_ip)
def get_company_insights(
    request: Request,
    followup_days: int = Query(
        FOLLOWUP_DAYS_DEFAULT,
        description="Ngưỡng số ngày im lặng cho contacts_needing_followup — chỉ nhận "
                    "7 | 14 | 30 (mặc định 14), giá trị khác trả 400.",
    ),
    user: dict = Depends(require_role("ss_team")),
    conn=Depends(get_db),
):
    """Tab "Doanh nghiệp": công ty tiềm năng cao thiếu/nguội contact,
    contact cần follow-up (im lặng >= followup_days), công ty đang "nở rộ"
    (>= 2 job trong 30 ngày) và công ty "im ắng" (job mới nhất > 75 ngày
    trước)."""
    if followup_days not in FOLLOWUP_DAYS_OPTIONS:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": error_codes.DASHBOARD_FOLLOWUP_DAYS_INVALID,
                "message": f"followup_days chỉ nhận một trong {list(FOLLOWUP_DAYS_OPTIONS)}.",
                "params": {"value": followup_days},
            },
        )
    activity = db_module.get_company_job_activity(conn)
    return {
        "followup_days": followup_days,
        "companies_no_contact": db_module.get_high_potential_companies_without_contact(conn),
        "contacts_needing_followup": db_module.get_contacts_needing_followup(
            conn, quiet_days=followup_days
        ),
        "companies_expanding": activity["expanding"],
        "companies_quiet": activity["quiet"],
    }


@router.get("/insights/monthly", response_model=MonthlyInsightsOut)
@limiter.limit("30/minute", key_func=get_user_id_or_ip)
def get_monthly_insights(
    request: Request,
    user: dict = Depends(require_role("ss_team")),
    conn=Depends(get_db),
):
    """Tab "Báo cáo tháng": số job/công ty mới + % so với tháng trước, job
    hết hạn trong tháng, top 3 ngành + top 5 công ty tuyển nhiều nhất
    tháng này, số ứng tuyển/lưu job + %. "Tháng" theo lịch, giờ Việt Nam
    (cùng cách GET /stats/engagement tính ranh giới tháng)."""
    counts = db_module.get_monthly_recap_counts(conn)
    engagement = db_module.get_monthly_engagement_stats(conn)
    applications = engagement.get("applications") or {}
    saved_jobs = engagement.get("saved_jobs") or {}
    apps_this = applications.get("this_month", 0) or 0
    saved_this = saved_jobs.get("this_month", 0) or 0
    return {
        "this_month_start": counts["this_month_start"],
        "last_month_start": counts["last_month_start"],
        "jobs_new": counts["jobs_this"],
        "jobs_new_pct": _pct_change(counts["jobs_this"], counts["jobs_last"]),
        "jobs_expired": counts["jobs_expired"],
        "companies_new": counts["companies_this"],
        "companies_new_pct": _pct_change(counts["companies_this"], counts["companies_last"]),
        "top_industries": [
            {
                "industry": r["industry"],
                "count": r["this_count"],
                "pct_change": _pct_change(r["this_count"], r["last_count"]),
            }
            for r in counts["top_industries"]
        ],
        "top_companies": counts["top_companies"],
        "applications_this_month": apps_this,
        "applications_pct": _pct_change(apps_this, applications.get("last_month", 0) or 0),
        "saved_jobs_this_month": saved_this,
        "saved_jobs_pct": _pct_change(saved_this, saved_jobs.get("last_month", 0) or 0),
    }
