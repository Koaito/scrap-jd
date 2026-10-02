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
        ("Thừa Thiên Huế", "Huế"),                # tên cũ của Huế
        ("TP.HCM", "Hồ Chí Minh"),                # viết tắt
        ("Ho Chi Minh", "Hồ Chí Minh"),           # không dấu
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


# ----------------------------------------------------------------------
# Crawl watchdog: db.reconcile_stale_runs() tính theo tiến độ gần nhất
# (10/2026). Cần Postgres thật vì logic nằm ở câu SQL (regex + COALESCE +
# make_interval), mock conn không bắt được.
# ----------------------------------------------------------------------
@pytest.fixture
def clean_crawl_runs(pg_conn):
    # sql/schema.sql KHÔNG có bảng crawl_runs (chỉ có trong file migration),
    # nên tự áp dụng 2 migration liên quan. Cả 2 đều idempotent.
    sql_dir = os.path.join(os.path.dirname(__file__), "..", "sql")
    for name in ("migration_add_crawl_runs.sql", "migration_add_crawl_progress_logs.sql"):
        with open(os.path.join(sql_dir, name), encoding="utf-8") as f:
            with pg_conn.cursor() as cur:
                cur.execute(f.read())
        pg_conn.commit()

    def _wipe():
        pg_conn.rollback()
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM crawl_runs")
        pg_conn.commit()

    _wipe()
    yield
    _wipe()


def _insert_run(pg_conn, source, status, *, started_min_ago, last_update=None):
    """last_update: None (progress NULL), str (ghi nguyên văn, kể cả giá trị
    rác), hoặc số phút trước (float/int -> ISO datetime UTC)."""
    import json
    from datetime import datetime, timedelta, timezone

    progress = None
    if isinstance(last_update, (int, float)):
        ts = datetime.now(timezone.utc) - timedelta(minutes=last_update)
        progress = {"fetched": 1, "inserted": 0, "last_update": ts.isoformat()}
    elif isinstance(last_update, str):
        progress = {"fetched": 1, "inserted": 0, "last_update": last_update}

    with pg_conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO crawl_runs (source, category, pages, status, started_at, progress)
            VALUES (%s, 'data-analyst', 1, %s::crawl_status_enum,
                    now() - make_interval(mins => %s), %s)
            RETURNING run_id
            """,
            (source, status, started_min_ago,
             json.dumps(progress) if progress is not None else None),
        )
        run_id = str(cur.fetchone()[0])
    pg_conn.commit()
    return run_id


def _status_of(pg_conn, run_id):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT status::text FROM crawl_runs WHERE run_id = %s", (run_id,))
        return cur.fetchone()[0]


def test_reconcile_stale_runs_uses_last_progress_not_started_at(pg_conn, clean_crawl_runs):
    from datetime import datetime, timezone

    z_fresh = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")  # dạng 'Z'
    cases = {
        # (status, started_min_ago, last_update) -> trạng thái sau khi quét
        "long_but_progressing": (("running", 300, 1), "running"),
        "long_progress_z_suffix": (("running", 300, z_fresh), "running"),
        "silent_after_progress": (("running", 40, 35), "error"),
        "no_progress_old": (("running", 40, None), "error"),
        "no_progress_recent": (("running", 5, None), "running"),
        "garbage_last_update_old": (("running", 300, "không phải ngày"), "error"),
        "garbage_last_update_recent": (("running", 5, "không phải ngày"), "running"),
        # queued: so với started_at + ngưỡng timeout (120), không bị ngưỡng 30 phút
        # của 'running' áp vào (có thể đang xếp hàng chờ GLOBAL_JOB_SEMAPHORE).
        "queued_waiting_50": (("queued", 50, None), "queued"),
        "queued_stuck_130": (("queued", 130, None), "error"),
        "done_untouched": (("done", 500, 400), "done"),
    }
    ids = {
        name: _insert_run(
            pg_conn, f"wd_{name}", status, started_min_ago=started, last_update=last
        )
        for name, ((status, started, last), _) in cases.items()
    }

    count = db.reconcile_stale_crawl_runs(pg_conn, 120, no_progress_minutes=30)

    for name, (_, expected) in cases.items():
        assert _status_of(pg_conn, ids[name]) == expected, name
    assert count == sum(1 for _, exp in cases.values() if exp == "error")

    # dòng bị đánh dấu có finished_at + thông báo lỗi
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT error, finished_at FROM crawl_runs WHERE run_id = %s",
            (ids["silent_after_progress"],),
        )
        error, finished_at = cur.fetchone()
    assert "30 phút" in error and finished_at is not None


def test_mark_running_writes_first_heartbeat(pg_conn, clean_crawl_runs):
    """Run xếp hàng lâu ở 'queued' rồi mới chạy: mark_running() phải ghi
    heartbeat cùng lúc, nếu không watchdog so với started_at (mốc lúc tạo)
    sẽ huỷ nhầm ngay lượt quét sau."""
    run_id = _insert_run(pg_conn, "wd_waited_long", "queued", started_min_ago=100)

    db.mark_crawl_run_running(pg_conn, run_id)

    assert _status_of(pg_conn, run_id) == "running"
    assert db.reconcile_stale_crawl_runs(pg_conn, 120, no_progress_minutes=30) == 0
    assert _status_of(pg_conn, run_id) == "running"


# ----------------------------------------------------------------------
# Đợt 3 (10/2026): lượt bị chặn + snapshot. Cần Postgres thật vì logic nằm
# ở câu SQL (stats->>'blocked', make_interval, bytea gzip, ON DELETE CASCADE).
# ----------------------------------------------------------------------
@pytest.fixture
def clean_crawl_snapshots(pg_conn, clean_crawl_runs):
    sql_dir = os.path.join(os.path.dirname(__file__), "..", "sql")
    with open(os.path.join(sql_dir, "migration_add_crawl_snapshots.sql"), encoding="utf-8") as f:
        with pg_conn.cursor() as cur:
            cur.execute(f.read())
    pg_conn.commit()
    yield


def test_get_recent_blocked_run_only_matches_blocked_errors_in_window(pg_conn, clean_crawl_runs):
    blocked = _insert_run(pg_conn, "blk_src", "running", started_min_ago=10, last_update=1)
    db.mark_crawl_run_error(pg_conn, blocked, "bị chặn", stats={"blocked": True, "inserted": 3})
    plain = _insert_run(pg_conn, "plain_src", "running", started_min_ago=10, last_update=1)
    db.mark_crawl_run_error(pg_conn, plain, "lỗi thường")

    found = db.get_recent_blocked_crawl_run(pg_conn, "blk_src", 60)
    assert found is not None and found["run_id"] == blocked
    assert db.get_recent_blocked_crawl_run(pg_conn, "plain_src", 60) is None   # lỗi thường
    assert db.get_recent_blocked_crawl_run(pg_conn, "other_src", 60) is None   # nguồn khác

    with pg_conn.cursor() as cur:   # đẩy finished_at ra ngoài cửa sổ
        cur.execute("UPDATE crawl_runs SET finished_at = now() - interval '3 hours' WHERE run_id = %s",
                    (blocked,))
    pg_conn.commit()
    assert db.get_recent_blocked_crawl_run(pg_conn, "blk_src", 60) is None


def test_snapshot_roundtrip_retention_and_cascade(pg_conn, clean_crawl_snapshots):
    from snapshots import SnapshotRecorder

    run_id = _insert_run(pg_conn, "snap_src", "running", started_min_ago=1, last_update=0)
    rec = SnapshotRecorder()
    rec.offer("listing", "https://x/1", "<html>xin chào</html>")
    rec.offer("detail", "https://x/2", "<html>chi tiết</html>", reason="detail_blank")

    assert db.save_crawl_snapshots(pg_conn, run_id, "snap_src", rec.items) == 2

    rows = db.list_crawl_snapshots(pg_conn, run_id=run_id)
    assert {(r["kind"], r["reason"]) for r in rows} == {("listing", "sample"), ("detail", "detail_blank")}
    snap = db.get_crawl_snapshot(pg_conn, rows[0]["id"])
    assert snap["body"] in ("<html>xin chào</html>", "<html>chi tiết</html>")

    # bản cũ hơn retention bị xoá ở lần lưu kế tiếp
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE crawl_snapshots SET created_at = now() - interval '30 days' WHERE run_id = %s",
                    (run_id,))
    pg_conn.commit()
    rec2 = SnapshotRecorder()
    rec2.offer("listing", "https://x/3", "<html>mới</html>")
    db.save_crawl_snapshots(pg_conn, run_id, "snap_src", rec2.items)
    assert len(db.list_crawl_snapshots(pg_conn, run_id=run_id)) == 1

    # xoá run -> snapshot đi theo (ON DELETE CASCADE)
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM crawl_runs WHERE run_id = %s", (run_id,))
    pg_conn.commit()
    assert db.list_crawl_snapshots(pg_conn, run_id=run_id) == []
