"""
Test cho db.get_company_data_health() / db.get_job_data_health()
(db/companies.py, db/jobs.py — thêm 08/2026, thay thế cách cũ frontend
tự đếm/group bằng Python trên list_all_companies()/
list_all_jobs(include_content=True)).

KHÔNG test đúng-sai của câu SQL (cần Postgres thật — xem
tests/test_migrations.py cho loại test đó) — CHỈ test phần XỬ LÝ PYTHON
sau khi có kết quả SQL: tính pct_missing, giữ đúng thứ tự field, gộp
nhóm/sort, map "" -> "Không rõ nguồn"... — đây chính là phần dễ có bug
off-by-one/chia 0/sai thứ tự nhất, và trước giờ CHƯA có test nào che
phủ 2 hàm này (thêm cùng đợt "rà codebase — độ linh hoạt/mở rộng").

Dùng FakeCursor/FakeConn tự viết (không phải Postgres thật) — cùng kiểu
với tests/test_merge_companies.py, nhưng viết theo style pytest thật
(assert, không phải hàm trả bool tự in log) để pytest chạy + báo lỗi
đúng chuẩn.

Chạy: pytest tests/test_db_data_health.py -v
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db


class FakeCursor:
    """Giả lập psycopg2 cursor — nhận sẵn 1 HÀNG QUEUE cho fetchone()
    (pop từng lần gọi) và 1 HÀNG QUEUE cho fetchall() (pop từng lần
    gọi) — khớp đúng cách get_job_data_health() gọi NHIỀU execute()
    nối tiếp trên CÙNG 1 cursor (1 fetchone rồi 3 fetchall, xem
    db/jobs.py). Không quan tâm nội dung SQL thật (không assert query
    string) — chỉ test phần xử lý Python sau khi có kết quả."""

    def __init__(self, fetchone_queue=None, fetchall_queue=None):
        self._fetchone_queue = list(fetchone_queue or [])
        self._fetchall_queue = list(fetchall_queue or [])
        self.queries = []

    def execute(self, query, params=None):
        self.queries.append((" ".join(query.split()), params))

    def fetchone(self):
        return self._fetchone_queue.pop(0) if self._fetchone_queue else None

    def fetchall(self):
        return self._fetchall_queue.pop(0) if self._fetchall_queue else []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, fetchone_queue=None, fetchall_queue=None):
        self._fetchone_queue = fetchone_queue
        self._fetchall_queue = fetchall_queue

    def cursor(self, cursor_factory=None):
        # cursor_factory (RealDictCursor thật) bị bỏ qua — FakeCursor
        # trả thẳng dict Python, đủ để test logic vì code chỉ đọc
        # row["ten_cot"], không quan tâm class thật của row.
        return FakeCursor(self._fetchone_queue, self._fetchall_queue)


# ---------------------------------------------------------------------------
# get_company_data_health()
# ---------------------------------------------------------------------------


def test_company_data_health_field_order_and_labels():
    """company_health_rows PHẢI đúng thứ tự _COMPANY_HEALTH_FIELDS —
    template bên Flask render theo thứ tự list trả về (không tự sort),
    lệch thứ tự = hiển thị sai vị trí cột trên UI mà không hề có lỗi gì
    để báo."""
    conn = FakeConn(fetchone_queue=[{
        "total": 200,
        "missing_tax_id": 30,
        "missing_website": 20,
        "missing_industry": 0,
        "missing_address": 10,
        "missing_company_size": 50,
        "missing_fanpage": 100,
        "missing_linkedin_company": 199,
        "missing_contact": 45,
    }])

    result = db.get_company_data_health(conn)

    fields_in_order = [row["field"] for row in result["company_health_rows"]]
    assert fields_in_order == [
        "tax_id", "website", "industry", "address",
        "company_size", "fanpage", "linkedin_company",
    ]
    labels_in_order = [row["label"] for row in result["company_health_rows"]]
    assert labels_in_order == [
        "Mã số thuế", "Website", "Ngành", "Địa chỉ",
        "Quy mô", "Fanpage", "LinkedIn",
    ]


def test_company_data_health_pct_missing_rounding():
    """pct_missing = round(missing/total*100) — kiểm tra vài mốc làm
    tròn cụ thể (30/200=15% tròn khớp, 1/3 phải làm tròn đúng chuẩn
    round-half-to-even của Python chứ không phải luôn làm tròn lên)."""
    conn = FakeConn(fetchone_queue=[{
        "total": 3,
        "missing_tax_id": 1,       # 1/3 = 33.33% -> 33
        "missing_website": 2,      # 2/3 = 66.67% -> 67
        "missing_industry": 0,     # 0%
        "missing_address": 3,      # 100%
        "missing_company_size": 0,
        "missing_fanpage": 0,
        "missing_linkedin_company": 0,
        "missing_contact": 0,
    }])

    result = db.get_company_data_health(conn)
    by_field = {row["field"]: row["pct_missing"] for row in result["company_health_rows"]}
    assert by_field["tax_id"] == 33
    assert by_field["website"] == 67
    assert by_field["industry"] == 0
    assert by_field["address"] == 100


def test_company_data_health_zero_total_no_division_error():
    """total=0 (hệ thống chưa có company active nào) -> pct_missing=0
    cho MỌI field, KHÔNG raise ZeroDivisionError."""
    conn = FakeConn(fetchone_queue=[{
        "total": 0,
        "missing_tax_id": 0, "missing_website": 0, "missing_industry": 0,
        "missing_address": 0, "missing_company_size": 0, "missing_fanpage": 0,
        "missing_linkedin_company": 0, "missing_contact": 0,
    }])

    result = db.get_company_data_health(conn)
    assert result["company_health_total"] == 0
    assert all(row["pct_missing"] == 0 for row in result["company_health_rows"])
    assert result["company_no_contact_total"] == 0
    assert result["company_no_contact_missing"] == 0


def test_company_data_health_no_contact_passthrough():
    """company_no_contact_missing/total lấy đúng từ cột missing_contact
    và total của cùng 1 hàng SQL — KHÔNG tính lại/gộp nhầm với field
    health."""
    conn = FakeConn(fetchone_queue=[{
        "total": 88,
        "missing_tax_id": 0, "missing_website": 0, "missing_industry": 0,
        "missing_address": 0, "missing_company_size": 0, "missing_fanpage": 0,
        "missing_linkedin_company": 0, "missing_contact": 12,
    }])

    result = db.get_company_data_health(conn)
    assert result["company_no_contact_missing"] == 12
    assert result["company_no_contact_total"] == 88


# ---------------------------------------------------------------------------
# get_job_data_health()
# ---------------------------------------------------------------------------

_ZERO_JOB_COUNTS = {
    "total": 0, "missing_skills": 0, "missing_requirements": 0,
    "missing_benefits": 0, "missing_description": 0, "missing_deadline": 0,
}


def _job_counts(total, **missing):
    row = dict(_ZERO_JOB_COUNTS)
    row["total"] = total
    row.update({f"missing_{k}": v for k, v in missing.items()})
    return row


def test_job_data_health_field_order_and_total():
    conn = FakeConn(
        fetchone_queue=[_job_counts(800, skills=120, requirements=40, deadline=200)],
        fetchall_queue=[[], [], []],  # source breakdown, expired, duplicate — rỗng
    )

    result = db.get_job_data_health(conn)

    fields_in_order = [row["field"] for row in result["job_health_rows"]]
    assert fields_in_order == ["skills", "requirements", "benefits", "description", "deadline"]
    assert result["job_health_total"] == 800
    by_field = {row["field"]: row["missing"] for row in result["job_health_rows"]}
    assert by_field["skills"] == 120
    assert by_field["deadline"] == 200


def test_job_data_health_source_breakdown_unknown_source_label():
    """source_name = "" (job chưa từng có log nguồn) PHẢI hiển thị
    "Không rõ nguồn" — khớp hành vi cũ (job_health_by_source() bên
    Flask trước đây dùng đúng fallback này)."""
    conn = FakeConn(
        fetchone_queue=[_job_counts(500)],
        fetchall_queue=[
            [  # source breakdown
                _job_counts(300, skills=10) | {"source_name": "TopCV"},
                _job_counts(150, skills=5) | {"source_name": ""},
            ],
            [],  # expired
            [],  # duplicate
        ],
    )

    result = db.get_job_data_health(conn)
    sources = {g["source"]: g["total"] for g in result["job_health_by_source"]}
    assert sources["TopCV"] == 300
    assert sources["Không rõ nguồn"] == 150
    # Sort theo total giảm dần — TopCV (300) phải đứng trước "Không rõ nguồn" (150)
    assert [g["source"] for g in result["job_health_by_source"]][0] == "TopCV"


def test_job_data_health_expired_jobs_shape():
    conn = FakeConn(
        fetchone_queue=[_job_counts(10)],
        fetchall_queue=[
            [],
            [
                {"job_id": "j1", "job_title": "Backend Fresher", "company_name": "ABC",
                 "deadline": "2026-08-01", "source_name": "TopCV"},
            ],
            [],
        ],
    )

    result = db.get_job_data_health(conn)
    assert len(result["expired_open_jobs"]) == 1
    job = result["expired_open_jobs"][0]
    assert job == {
        "id": "j1", "position": "Backend Fresher", "company": "ABC",
        "deadline": "2026-08-01", "source": "TopCV",
    }


def test_job_data_health_duplicate_groups_sorted_by_size_desc():
    """3 job cùng company+position phải gộp thành 1 group (jobs=3), 2
    job khác thành 1 group riêng (jobs=2) — group 3 job phải đứng TRƯỚC
    group 2 job (sort giảm dần theo số job trong nhóm)."""
    conn = FakeConn(
        fetchone_queue=[_job_counts(5)],
        fetchall_queue=[
            [],  # source breakdown
            [],  # expired
            [   # duplicate rows — nhóm A (company-1, "fresher backend") có 3, nhóm B (company-2, "intern qa") có 2
                {"job_id": "a1", "job_title": "Fresher Backend", "company_id": "c1",
                 "company_name": "A", "deadline": None, "source_name": "TopCV",
                 "position_key": "fresher backend"},
                {"job_id": "a2", "job_title": "Fresher Backend", "company_id": "c1",
                 "company_name": "A", "deadline": None, "source_name": "TopCV",
                 "position_key": "fresher backend"},
                {"job_id": "a3", "job_title": "fresher backend", "company_id": "c1",
                 "company_name": "A", "deadline": None, "source_name": "VietnamWorks",
                 "position_key": "fresher backend"},
                {"job_id": "b1", "job_title": "Intern QA", "company_id": "c2",
                 "company_name": "B", "deadline": None, "source_name": "TopCV",
                 "position_key": "intern qa"},
                {"job_id": "b2", "job_title": "Intern QA", "company_id": "c2",
                 "company_name": "B", "deadline": None, "source_name": "TopCV",
                 "position_key": "intern qa"},
            ],
        ],
    )

    result = db.get_job_data_health(conn)
    groups = result["duplicate_job_groups"]
    assert len(groups) == 2
    assert len(groups[0]["jobs"]) == 3  # nhóm A (3 job) đứng đầu
    assert len(groups[1]["jobs"]) == 2  # nhóm B (2 job) đứng sau
    assert groups[0]["company"] == "A"
    assert groups[1]["company"] == "B"


def test_job_data_health_zero_total_no_division_error():
    conn = FakeConn(
        fetchone_queue=[_job_counts(0)],
        fetchall_queue=[[], [], []],
    )
    result = db.get_job_data_health(conn)
    assert result["job_health_total"] == 0
    assert all(row["pct_missing"] == 0 for row in result["job_health_rows"])
    assert result["expired_open_jobs"] == []
    assert result["duplicate_job_groups"] == []


if __name__ == "__main__":
    import pytest as _pytest
    raise SystemExit(_pytest.main([__file__, "-v"]))
