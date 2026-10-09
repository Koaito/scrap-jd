"""
merge_companies() chạy trên POSTGRES THẬT (10/2026).

tests/test_merge_companies.py chỉ dùng mock nên chỉ kiểm thứ tự câu lệnh SQL,
không bắt được ràng buộc khoá ngoại. Bảng audit_logs có company_id REFERENCES
companies(company_id): trước khi sửa, công ty nào từng có dòng audit (do staff
sửa) bị gộp sẽ làm DELETE FROM companies vỡ khoá ngoại.

Cách chạy giống tests/test_pg_integration.py: đặt TEST_DATABASE_URL trỏ tới một
database dùng riêng cho test (tên phải chứa "test", fixture DROP SCHEMA public).
Không đặt biến này -> cả file được bỏ qua.
"""
import os
from urllib.parse import urlparse

import psycopg2
import pytest

from scrapjd import db

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
        pytest.fail(f"Từ chối chạy: database '{dbname}' không chứa 'test' trong tên.")
    conn = psycopg2.connect(TEST_DATABASE_URL)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    conn.commit()
    db.apply_schema(conn, _SCHEMA_PATH)
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture(autouse=True)
def _clean(pg_conn):
    """Mỗi test bắt đầu từ bảng rỗng, không phụ thuộc thứ tự chạy."""
    yield
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM audit_logs")
        cur.execute("DELETE FROM company_contacts")
        cur.execute("DELETE FROM job_sources_log")
        cur.execute("DELETE FROM job_postings")
        cur.execute("DELETE FROM companies")
    pg_conn.commit()


def _company(conn, name, **cols):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO companies (company_name, tax_id) VALUES (%s, %s) RETURNING company_id",
            (name, cols.get("tax_id")),
        )
        return str(cur.fetchone()[0])


def _audit(conn, company_id, label="log"):
    """Một dòng audit mà staff sinh ra khi sửa công ty (entity_id = chính công ty)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO audit_logs (action_type, entity_type, entity_id, entity_label,
                                    company_id, is_manual_log)
            VALUES ('UPDATE_COMPANY', 'company', %s, %s, %s, false)
            RETURNING log_id
            """,
            (company_id, label, company_id),
        )
        return str(cur.fetchone()[0])


def test_merge_succeeds_when_source_has_audit_logs(pg_conn):
    source = _company(pg_conn, "ABC Việt Nam")
    target = _company(pg_conn, "Công ty ABC", tax_id="0312345678")
    log_id = _audit(pg_conn, source)
    pg_conn.commit()

    db.merge_companies(pg_conn, source_company_id=source, target_company_id=target)
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM companies WHERE company_id = %s", (source,))
        assert cur.fetchone() is None, "công ty nguồn phải bị xoá sau khi gộp"
        # Lịch sử không mất: dòng audit chuyển sang công ty đích, entity_id giữ nguyên
        cur.execute("SELECT company_id, entity_id FROM audit_logs WHERE log_id = %s", (log_id,))
        row = cur.fetchone()
    assert row is not None, "dòng audit không được biến mất"
    assert str(row[0]) == target
    assert str(row[1]) == source


def test_failed_merge_leaves_everything_untouched(pg_conn):
    """Gộp lỗi giữa chừng phải hoàn tác hết (nơi gọi rollback): không có
    job/audit nào bị chuyển nửa vời."""
    source = _company(pg_conn, "ABC Việt Nam")
    log_id = _audit(pg_conn, source)
    pg_conn.commit()

    with pytest.raises(psycopg2.Error):
        # target không tồn tại -> UPDATE audit_logs vỡ khoá ngoại
        db.merge_companies(
            pg_conn, source_company_id=source,
            target_company_id="00000000-0000-0000-0000-000000000000",
        )
    pg_conn.rollback()

    with pg_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM companies WHERE company_id = %s", (source,))
        assert cur.fetchone() is not None
        cur.execute("SELECT company_id FROM audit_logs WHERE log_id = %s", (log_id,))
        assert str(cur.fetchone()[0]) == source


def test_merge_with_tax_id_conflict_end_to_end(pg_conn):
    """Đường thật mà scrapjd/maintenance/enrich_company_web_info.py đi: tax_id tra được trùng một
    công ty khác đã có, công ty nguồn có lịch sử audit."""
    source = _company(pg_conn, "ABC Việt Nam")
    target = _company(pg_conn, "Công ty ABC", tax_id="0312345678")
    _audit(pg_conn, source)
    pg_conn.commit()

    final_id = db.update_company_profile_with_merge(
        pg_conn, source, tax_id="0312345678", website="https://abc.vn",
    )
    pg_conn.commit()

    assert final_id == target
    with pg_conn.cursor() as cur:
        cur.execute("SELECT website FROM companies WHERE company_id = %s", (target,))
        assert cur.fetchone()[0] == "https://abc.vn"


# ---------------------------------------------------------------------------
# Khớp công ty theo tên khi có nhiều dòng trùng tên: kết quả phải cố định.
# ---------------------------------------------------------------------------
def _named(conn, name, *, is_active=True, created_at="2026-01-01"):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO companies (company_name, is_active, created_at) "
            "VALUES (%s, %s, %s) RETURNING company_id",
            (name, is_active, created_at),
        )
        return str(cur.fetchone()[0])


def test_name_match_prefers_active_then_oldest(pg_conn):
    inactive_old = _named(pg_conn, "Trùng Tên", is_active=False, created_at="2025-01-01")
    active_new = _named(pg_conn, "trùng tên", created_at="2026-06-01")
    active_old = _named(pg_conn, "TRÙNG TÊN", created_at="2026-02-01")
    pg_conn.commit()

    assert inactive_old != active_old != active_new
    assert str(db.find_company_probe(pg_conn, "Trùng Tên")[0]) == active_old
    # get_or_create dùng cùng quy tắc, và không tạo thêm bản sao
    assert db.get_or_create_company_by_profile(pg_conn, "Trùng Tên", None) == active_old
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM companies WHERE lower(company_name) = 'trùng tên'")
        assert cur.fetchone()[0] == 3


def test_name_match_falls_back_to_inactive_instead_of_duplicating(pg_conn):
    only_inactive = _named(pg_conn, "Đã Xoá Mềm", is_active=False)
    pg_conn.commit()

    assert db.get_or_create_company_by_profile(pg_conn, "Đã Xoá Mềm", None) == only_inactive
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM companies WHERE company_name = 'Đã Xoá Mềm'")
        assert cur.fetchone()[0] == 1
