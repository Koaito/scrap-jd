"""
Bảo vệ mặt tiền `import db` sau khi tách db/jobs.py và db/companies.py
(10/2026): mọi tên trong db.__all__ phải còn truy cập được, và không file
db/*.py nào được phình lại thành "God module".

Test đọc mã nguồn/import, không cần DB.
"""

import inspect
from pathlib import Path

from scrapjd import db

DB_DIR = Path(__file__).resolve().parent.parent / "scrapjd" / "db"

# Ngưỡng cảnh báo, không phải mục tiêu. File vượt ngưỡng nên tách theo
# domain (xem cách db/jobs.py -> job_queries.py, job_health.py) thay vì nới số này.
MAX_LINES_PER_DB_MODULE = 700


def test_every_name_in_all_resolves():
    missing = [name for name in db.__all__ if not hasattr(db, name)]
    assert not missing, f"db.__all__ liệt kê tên không tồn tại: {missing}"


def test_all_has_no_duplicates():
    dupes = sorted({n for n in db.__all__ if db.__all__.count(n) > 1})
    assert not dupes, f"db.__all__ trùng tên: {dupes}"


def test_split_modules_are_wired_through_facade():
    # Mỗi hàm sau phải được định nghĩa ở đúng module con sau khi tách.
    expected = {
        "insert_job": "scrapjd.db.jobs",
        "update_job": "scrapjd.db.jobs",
        "list_jobs": "scrapjd.db.job_queries",
        "get_job_by_id": "scrapjd.db.job_queries",
        "get_jobs_by_company_id": "scrapjd.db.job_queries",
        "get_job_data_health": "scrapjd.db.job_health",
        "merge_companies": "scrapjd.db.companies",
        "soft_delete_company": "scrapjd.db.companies",
        "list_companies": "scrapjd.db.company_queries",
        "get_company_by_id": "scrapjd.db.company_queries",
        "get_companies_needing_web_lookup": "scrapjd.db.company_enrichment",
        "update_company_social_links": "scrapjd.db.company_enrichment",
        "get_partnership_signals": "scrapjd.db.company_analytics",
        "get_company_data_health": "scrapjd.db.company_analytics",
        "list_duplicate_job_rows": "scrapjd.db.job_duplicates",
        "list_merge_job_details": "scrapjd.db.job_merge",
        "list_multi_source_job_logs": "scrapjd.db.job_reposts",
        "lock_job_dedup_key": "scrapjd.db.job_dedup_lock",
    }
    wrong = {
        name: inspect.getmodule(getattr(db, name)).__name__
        for name, module in expected.items()
        if inspect.getmodule(getattr(db, name)).__name__ != module
    }
    assert not wrong, f"hàm nằm sai module (tên: module thực tế): {wrong}"


def test_job_unset_sentinel_is_shared():
    # import_executor dùng db.JOB_UNSET; update_job so sánh với _UNSET trong
    # db.jobs — phải là CÙNG một object, nếu không "không gửi field" sẽ
    # bị hiểu thành "xoá lương".
    from scrapjd.db import jobs as db_jobs

    assert db.JOB_UNSET is db_jobs._UNSET


def test_no_db_module_grows_back_into_god_module():
    too_long = {}
    for path in sorted(DB_DIR.glob("*.py")):
        n = len(path.read_text(encoding="utf-8").splitlines())
        if n > MAX_LINES_PER_DB_MODULE:
            too_long[path.name] = n
    assert not too_long, (
        f"db/*.py vượt {MAX_LINES_PER_DB_MODULE} dòng, hãy tách theo domain: {too_long}"
    )
