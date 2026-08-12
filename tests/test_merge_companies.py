"""
Test logic merge_companies() / update_company_profile_with_merge() bằng
mock connection — KHÔNG cần Postgres thật, chỉ verify đúng THỨ TỰ và
NỘI DUNG các câu lệnh SQL được gọi khi phát hiện tax_id trùng.

Chạy: python tests/test_merge_companies.py
"""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db


class FakeCursor:
    def __init__(self, log, fetchone_queue):
        self.log = log
        self._fetchone_queue = fetchone_queue
        self._last_query = ""

    def execute(self, query, params=None):
        self._last_query = query
        self.log.append((" ".join(query.split()), params))

    def fetchone(self):
        if self._fetchone_queue:
            return self._fetchone_queue.pop(0)
        return None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    def __init__(self, fetchone_queue):
        self.log = []
        self._fetchone_queue = fetchone_queue

    def cursor(self):
        return FakeCursor(self.log, self._fetchone_queue)


def test_no_conflict_updates_normally() -> bool:
    """Không có company nào khác có tax_id này -> update thẳng vào
    company_id truyền vào, KHÔNG gọi merge."""
    print("--- Test: không trùng tax_id -> update bình thường ---")
    ok = True

    # find_company_by_tax_id() -> fetchone() trả None (không ai có tax_id này)
    conn = FakeConn(fetchone_queue=[None])

    result_id = db.update_company_profile_with_merge(
        conn, "company-A", tax_id="0312345678", website="https://abc.com"
    )

    if result_id != "company-A":
        ok = False
        print(f"  !! SAI: kỳ vọng company_id không đổi, được {result_id!r}")

    queries = [q for q, _ in conn.log]
    if any("DELETE FROM companies" in q for q in queries):
        ok = False
        print("  !! SAI: không nên có DELETE khi không trùng tax_id")
    if not any("UPDATE companies SET" in q and "tax_id" in q for q in queries):
        ok = False
        print("  !! SAI: phải có UPDATE ghi tax_id vào company-A")

    print(f"  Các query đã gọi: {queries}")
    print()
    return ok


def test_conflict_triggers_merge() -> bool:
    """Company khác (company-EXISTING) đã có sẵn tax_id này -> phải
    MERGE company-A vào company-EXISTING (chuyển job/contact, xoá
    company-A), KHÔNG được update thẳng tax_id vào company-A (sẽ vi
    phạm unique constraint nếu chạy thật)."""
    print("--- Test: trùng tax_id với company khác -> tự gộp ---")
    ok = True

    # find_company_by_tax_id() -> fetchone() trả về company-EXISTING
    conn = FakeConn(fetchone_queue=[("company-EXISTING",)])

    result_id = db.update_company_profile_with_merge(
        conn, "company-A", tax_id="0312345678", website="https://abc.com"
    )

    if result_id != "company-EXISTING":
        ok = False
        print(f"  !! SAI: kỳ vọng trả về company-EXISTING (company đích sau merge), được {result_id!r}")

    queries_with_params = conn.log
    queries = [q for q, _ in queries_with_params]

    # Phải có đúng chuỗi: chuyển job_postings, company_contacts, rồi DELETE company-A
    if not any("UPDATE job_postings SET company_id" in q for q in queries):
        ok = False
        print("  !! SAI: thiếu bước chuyển job_postings sang company đích")
    if not any("UPDATE company_contacts SET company_id" in q for q in queries):
        ok = False
        print("  !! SAI: thiếu bước chuyển company_contacts sang company đích")
    if not any("DELETE FROM companies" in q for q in queries):
        ok = False
        print("  !! SAI: thiếu bước xoá company-A (source) sau khi gộp")

    # Không được UPDATE tax_id vào company-A nữa (đã merge, company-A sắp bị xoá)
    update_company_queries = [
        (q, p) for q, p in queries_with_params
        if q.startswith("UPDATE companies SET") and "tax_id" in q
    ]
    if update_company_queries:
        ok = False
        print(f"  !! SAI: không nên UPDATE tax_id riêng sau khi đã merge, nhưng thấy: {update_company_queries}")

    # website vẫn phải được update vào company đích (company-EXISTING),
    # vì merge chỉ xử lý tax_id, các field khác vẫn cần vá bình thường
    website_update = [
        (q, p) for q, p in queries_with_params
        if q.startswith("UPDATE companies SET") and "website" in q
    ]
    if not website_update:
        ok = False
        print("  !! SAI: website vẫn phải được update vào company đích sau khi merge")
    elif website_update[0][1][-1] != "company-EXISTING":
        ok = False
        print(f"  !! SAI: UPDATE website phải nhắm vào company-EXISTING, nhưng params={website_update[0][1]}")

    print(f"  Các query đã gọi (theo thứ tự):")
    for q, p in queries_with_params:
        print(f"    {q}  params={p}")
    print()
    return ok


def test_merge_companies_noop_when_same_id() -> bool:
    """merge_companies() với source == target -> không làm gì cả (tránh
    tự xoá chính mình nếu lỡ gọi nhầm)."""
    print("--- Test: merge_companies(A, A) -> no-op ---")
    ok = True
    conn = FakeConn(fetchone_queue=[])
    db.merge_companies(conn, "company-A", "company-A")
    if conn.log:
        ok = False
        print(f"  !! SAI: kỳ vọng không có query nào chạy, nhưng có: {conn.log}")
    print()
    return ok


def main():
    ok = True
    ok = test_no_conflict_updates_normally() and ok
    ok = test_conflict_triggers_merge() and ok
    ok = test_merge_companies_noop_when_same_id() and ok

    print("=" * 50)
    print("KẾT QUẢ: " + ("✅ PASS" if ok else "❌ FAIL"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
