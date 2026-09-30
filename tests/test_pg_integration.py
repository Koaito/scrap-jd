"""
Test tích hợp CHẠY TRÊN POSTGRES THẬT (thêm 09/2026).

Vì sao cần file này: toàn bộ test còn lại mock DB (xem tests/conftest.py
::mock_conn), nên KHÔNG bắt được lỗi nằm ở chính câu SQL. Đã có 2 lỗi
thật lọt qua như vậy:

- `col_uuid = ANY(%s)` với list[str]: psycopg2 gửi `text[]`, Postgres báo
  `operator does not exist: uuid = text`  -> GET /companies/partnership-signals
  và GET /jobs?ids= trả 500 "Internal Server Error".
- `col_enum = ANY(%s)` tương tự: `contact_status_enum = text`.
- sql/schema.sql có dòng `DO $$ BEGIN` bị lặp (commit 4ec74cb) -> apply_schema()
  báo syntax error ngay ở lần chạy đầu trên DB trống.

Cách chạy: đặt TEST_DATABASE_URL trỏ tới 1 database TRỐNG dùng riêng cho
test, ví dụ
    TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/scrapjd_test pytest tests/test_pg_integration.py
Không đặt biến này -> toàn bộ file được bỏ qua (pytest thường vẫn xanh).

AN TOÀN: fixture DROP SCHEMA public CASCADE để dựng lại từ đầu, nên từ chối
chạy nếu tên database không chứa "test" (tránh trỏ nhầm vào DB thật).
"""
import os
import uuid
from urllib.parse import urlparse

import psycopg2
import pytest

import db

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
    yield conn
    conn.rollback()
    conn.close()


def _insert_company(cur, name):
    cid = str(uuid.uuid4())
    cur.execute(
        "INSERT INTO companies (company_id, company_name) VALUES (%s, %s)", (cid, name)
    )
    return cid


def _insert_job(cur, company_id, title, *, level_code, status="OPEN", industry=None):
    jid = str(uuid.uuid4())
    cur.execute(
        """
        INSERT INTO job_postings
            (job_id, company_id, job_title, level_id, job_status, matching_industry)
        VALUES (%s, %s, %s, (SELECT level_id FROM levels WHERE level_code = %s),
                %s::job_status_enum, %s)
        """,
        (jid, company_id, title, level_code, status, industry),
    )
    return jid


def _insert_contact(cur, company_id, name, status):
    cur.execute(
        """
        INSERT INTO company_contacts (company_id, contact_name, contact_status)
        VALUES (%s, %s, %s::contact_status_enum)
        """,
        (company_id, name, status),
    )


def test_apply_schema_idempotent(pg_conn):
    """schema.sql chạy được trên DB trống VÀ chạy lại lần 2 không lỗi
    (docstring apply_schema hứa idempotent)."""
    db.apply_schema(pg_conn, _SCHEMA_PATH)
    db.apply_schema(pg_conn, _SCHEMA_PATH)


@pytest.fixture(scope="module")
def seeded(pg_conn):
    with pg_conn.cursor() as cur:
        c_full = _insert_company(cur, "A - đủ 3 tín hiệu")
        _insert_job(cur, c_full, "Intern Python", level_code="Intern",
                    industry="Code")
        _insert_contact(cur, c_full, "Người đã phản hồi", "RESPONDED")

        c_industry_only = _insert_company(cur, "B - chỉ ngành")
        _insert_job(cur, c_industry_only, "Senior Data", level_code="Senior",
                    industry="Data Analysis")

        c_closed_entry = _insert_company(cur, "C - job entry đã đóng")
        _insert_job(cur, c_closed_entry, "Fresher đã đóng", level_code="Fresher",
                    status="CLOSED", industry="Kế toán")

        c_empty = _insert_company(cur, "D - không job, không contact")
        j_ids = [
            _insert_job(cur, c_full, "Job thêm 1", level_code="Junior"),
            _insert_job(cur, c_full, "Job thêm 2", level_code="Middle"),
        ]
    pg_conn.commit()
    return {
        "full": c_full, "industry_only": c_industry_only,
        "closed_entry": c_closed_entry, "empty": c_empty, "job_ids": j_ids,
    }


def test_partnership_signals_filtered_by_company_ids(pg_conn, seeded):
    """Đúng đường gọi của trang /companies: truyền list company_id (str)."""
    ids = [seeded["full"], seeded["industry_only"], seeded["closed_entry"],
           seeded["empty"]]
    result = db.get_partnership_signals(pg_conn, company_ids=ids)

    assert result[seeded["full"]] == {
        "has_open_entry_job": True,
        "matches_target_industry": True,
        "has_responded": True,
    }
    assert result[seeded["industry_only"]] == {
        "has_open_entry_job": False,
        "matches_target_industry": True,
        "has_responded": False,
    }
    # Job entry nhưng CLOSED, ngành ngoài danh sách -> cả 3 đều False.
    assert result[seeded["closed_entry"]] == {
        "has_open_entry_job": False,
        "matches_target_industry": False,
        "has_responded": False,
    }
    # Công ty không job/contact -> không xuất hiện trong dict (docstring hàm).
    assert seeded["empty"] not in result


def test_partnership_signals_without_filter(pg_conn, seeded):
    """company_ids=None -> tính cho toàn bộ công ty (nhánh không có ANY())."""
    result = db.get_partnership_signals(pg_conn, company_ids=None)
    assert seeded["full"] in result
    assert seeded["industry_only"] in result


def test_partnership_signals_only_selected_companies(pg_conn, seeded):
    result = db.get_partnership_signals(pg_conn, company_ids=[seeded["industry_only"]])
    assert list(result) == [seeded["industry_only"]]


def test_list_jobs_ids_filter(pg_conn, seeded):
    """GET /jobs?ids=... -> `job_id = ANY(%s::uuid[])` phải chạy được với
    list[str] và chỉ trả đúng các job được yêu cầu."""
    wanted = seeded["job_ids"]
    rows, total, _ = db.list_jobs(pg_conn, ids=wanted, limit=50)
    assert total == len(wanted)
    assert {str(r["job_id"]) for r in rows} == set(wanted)


def test_list_jobs_ids_combined_with_status(pg_conn, seeded):
    """ids kết hợp AND với filter khác (docstring list_jobs)."""
    rows, total, _ = db.list_jobs(
        pg_conn, ids=seeded["job_ids"], job_status="CLOSED", limit=50
    )
    assert total == 0
    assert rows == []


# ---------------------------------------------------------------------------
# Tỉnh/thành (thêm 09/2026)
#
# Lỗi thật đã gặp: dropdown lọc /companies bên Next.js dùng 63 tên tỉnh cũ,
# trong khi list_companies() so sánh BẰNG (`p.province_name = %s`) với bảng
# `provinces` chỉ có 34 tỉnh sau sáp nhập -> chọn "TP. Hồ Chí Minh" luôn ra
# 0 công ty. Các test dưới khoá lại hành vi bên backend để danh sách tỉnh
# phía Next.js (lib/constants.ts::CITIES_VN) có mốc đối chiếu rõ ràng.
# ---------------------------------------------------------------------------

# 34 đơn vị hành chính cấp tỉnh hiện hành. PHẢI khớp CITIES_VN trong
# lib/constants.ts bên Next.js (nếu sửa ở đây thì sửa cả bên đó).
_PROVINCES_2025 = {
    "Hà Nội", "Hồ Chí Minh", "Đà Nẵng", "Hải Phòng", "Cần Thơ", "Huế",
    "An Giang", "Bắc Ninh", "Cà Mau", "Cao Bằng", "Đắk Lắk", "Điện Biên",
    "Đồng Nai", "Đồng Tháp", "Gia Lai", "Hà Tĩnh", "Hưng Yên", "Khánh Hòa",
    "Lai Châu", "Lâm Đồng", "Lạng Sơn", "Lào Cai", "Nghệ An", "Ninh Bình",
    "Phú Thọ", "Quảng Ngãi", "Quảng Ninh", "Quảng Trị", "Sơn La", "Tây Ninh",
    "Thái Nguyên", "Thanh Hóa", "Tuyên Quang", "Vĩnh Long",
}


def _province_id(cur, name):
    cur.execute("SELECT province_id FROM provinces WHERE province_name = %s", (name,))
    row = cur.fetchone()
    return row[0] if row else None


def test_provinces_seed_is_34_plus_special(pg_conn):
    """schema.sql seed đúng 34 tỉnh mới + 2 giá trị đặc biệt, không còn tên
    cũ/biến thể ("TP. Hồ Chí Minh", "Thừa Thiên Huế", "Bình Dương"...)."""
    with pg_conn.cursor() as cur:
        cur.execute("SELECT province_name FROM provinces")
        names = {r[0] for r in cur.fetchall()}
    assert names == _PROVINCES_2025 | {"Khác", "Remote"}
    assert len(_PROVINCES_2025) == 34


def test_list_companies_province_filter_is_exact(pg_conn):
    """Lọc theo tỉnh là so sánh BẰNG: đúng tên trong bảng thì ra, thêm
    "TP." thì ra 0 (đúng lỗi từng gặp ở dropdown Next.js)."""
    with pg_conn.cursor() as cur:
        cid = _insert_company(cur, "Công ty test lọc tỉnh HCM")
        cur.execute(
            "UPDATE companies SET province_id = %s WHERE company_id = %s",
            (_province_id(cur, "Hồ Chí Minh"), cid),
        )
    pg_conn.commit()

    rows, total = db.list_companies(pg_conn, province_name="Hồ Chí Minh", limit=50)
    assert cid in {str(r["company_id"]) for r in rows}

    rows, total = db.list_companies(pg_conn, province_name="TP. Hồ Chí Minh", limit=50)
    assert total == 0 and rows == []


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Hồ Chí Minh", "Hồ Chí Minh"),           # tên mới, khớp thẳng
        ("TP. Hồ Chí Minh", "Hồ Chí Minh"),       # bỏ tiền tố "TP."
        ("Bình Dương", "Hồ Chí Minh"),            # tên cũ đã sáp nhập
        ("Bà Rịa - Vũng Tàu", "Hồ Chí Minh"),
        ("Long An", "Tây Ninh"),
        ("Hà Giang", "Tuyên Quang"),
        ("Huế", "Huế"),
        ("", "Khác"),                             # rỗng -> Khác
        ("Tỉnh không tồn tại", "Khác"),           # lạ -> Khác, KHÔNG tạo dòng mới
    ],
)
def test_get_province_id_maps_old_names_to_new(pg_conn, raw, expected):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM provinces")
        before = cur.fetchone()[0]
        expected_id = _province_id(cur, expected)

    assert db.get_province_id(pg_conn, raw) == expected_id

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM provinces")
        assert cur.fetchone()[0] == before  # bảng cứng, không bị INSERT thêm
