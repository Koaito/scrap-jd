"""
A4 nửa 1/2 trên POSTGRES THẬT: khoá advisory chống trùng khi nhiều nơi ghi chạy song song.

Phần 1, db.lock_job_dedup_key (hai connection thật, hai thread):
  - cùng khoá chống trùng thì bên sau phải chờ bên trước commit/rollback, rồi mới giành được;
  - tiêu đề lệch hoa/thường và khoảng trắng vẫn là cùng khoá; khác tỉnh/khác công ty/khác tiêu đề
    thì KHÔNG chặn nhau;
  - hết thời gian chờ thì raise JobDedupLockTimeout, và lock_timeout của transaction được trả về
    giá trị cũ sau khi giành được khoá;
  - kết nối autocommit bị từ chối (khoá sẽ vô tác dụng).

Phần 2, pipeline.run_pipeline thật chạy hai lượt crawl khác nguồn SONG SONG với cùng một tin:
  - có khoá: đúng MỘT job, tin còn lại được coi là đăng lại;
  - nhóm đối chứng (tắt khoá): cùng kịch bản sinh HAI job, chứng minh test dựng đúng cuộc đua.

Cách chạy giống tests/test_pg_pipeline_repost_reopen.py: đặt TEST_DATABASE_URL trỏ tới database
dùng riêng cho test (tên chứa "test"); không đặt thì cả file được bỏ qua.
"""
import os
import threading
import uuid
from urllib.parse import urlparse

import psycopg2
import pytest

import db
import pipeline
from scrapjd.adapters.base import BaseAdapter
from scrapjd.models import RawJobRecord

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "..", "sql", "schema.sql")
FUTURE = "31/12/2099"


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
    db.baseline_migrations(conn)
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture(autouse=True)
def clean(pg_conn):
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE job_postings, audit_logs CASCADE")
    pg_conn.commit()
    yield
    pg_conn.rollback()


@pytest.fixture
def conns():
    """Hai kết nối riêng (mỗi bên một transaction), đóng hết sau test."""
    opened = [psycopg2.connect(TEST_DATABASE_URL) for _ in range(2)]
    for c in opened:
        c.autocommit = False
    yield opened
    for c in opened:
        c.rollback()
        c.close()


def _in_thread(fn):
    """Chạy fn() ở thread riêng; trả (thread, kết quả dict) để nơi gọi join rồi đọc."""
    out = {}

    def run():
        try:
            out["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - gom lại để test tự khẳng định
            out["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


COMPANY = str(uuid.uuid4())


def _lock(conn, title="Data Analyst", province_id=1, company=COMPANY, timeout_ms=10_000):
    db.lock_job_dedup_key(conn, company_id=company, job_title=title, province_id=province_id,
                          timeout_ms=timeout_ms)


# ------------------------------------------------------------------ phần 1: hàm khoá
def test_second_session_waits_for_the_first_to_commit(conns):
    a, b = conns
    _lock(a)
    got = threading.Event()

    def second():
        _lock(b)
        got.set()

    t, out = _in_thread(second)
    assert not got.wait(0.5), "bên sau không được giành khoá khi bên trước chưa commit"
    a.commit()                                    # nhả khoá
    assert got.wait(5), "bên sau phải giành được khoá ngay khi bên trước commit"
    t.join(5)
    assert "error" not in out, out


def test_rollback_also_releases_the_lock(conns):
    a, b = conns
    _lock(a)
    a.rollback()
    _lock(b, timeout_ms=500)                      # không chờ, không lỗi


@pytest.mark.parametrize("title,province,company", [
    ("  data   ANALYST ", 1, COMPANY),             # cùng khoá: lệch hoa/thường, khoảng trắng
])
def test_same_dedup_key_in_another_spelling_blocks(conns, title, province, company):
    a, b = conns
    _lock(a)
    with pytest.raises(db.JobDedupLockTimeout):
        _lock(b, title=title, province_id=province, company=company, timeout_ms=300)


@pytest.mark.parametrize("title,province,company", [
    ("Data Engineer", 1, COMPANY),                # khác tiêu đề
    ("Data Analyst", 2, COMPANY),                 # khác tỉnh
    ("Data Analyst", None, COMPANY),              # thiếu tỉnh là một giá trị riêng
    ("Data Analyst", 1, str(uuid.uuid4())),       # khác công ty
])
def test_different_dedup_key_does_not_block(conns, title, province, company):
    a, b = conns
    _lock(a)
    _lock(b, title=title, province_id=province, company=company, timeout_ms=300)


def test_timeout_raises_a_clear_error_and_the_caller_must_roll_back(conns):
    a, b = conns
    _lock(a)
    with pytest.raises(db.JobDedupLockTimeout):
        _lock(b, timeout_ms=200)
    b.rollback()                                  # như vòng lặp job của pipeline
    a.commit()
    _lock(b, timeout_ms=500)                      # sau khi bên giữ khoá nhả, giành lại được


def test_lock_timeout_setting_is_restored_after_acquiring(conns):
    a, _ = conns
    with a.cursor() as cur:
        cur.execute("SHOW lock_timeout")
        before = cur.fetchone()[0]
    _lock(a, timeout_ms=1234)
    with a.cursor() as cur:
        cur.execute("SHOW lock_timeout")
        assert cur.fetchone()[0] == before        # chỉ đổi riêng cho câu giành khoá


def test_autocommit_connection_is_refused(conns):
    a, _ = conns
    a.rollback()
    a.autocommit = True
    with pytest.raises(RuntimeError):
        _lock(a)


# ------------------------------------------------------------------ phần 2: hai lượt crawl song song
def _detail():
    return {
        "work_type": "Toàn thời gian", "deadline_text": FUTURE,
        "job_description": "mô tả công việc", "requirements": "yêu cầu", "perks": "",
        "required_skills": ["SQL"],
    }


class FakeAdapter(BaseAdapter):
    def __init__(self, source_name, url, *, company, title):
        super().__init__()
        self.source_name, self.url, self.company, self.title = source_name, url, company, title

    def fetch_jobs(self, category_key, max_pages):
        yield RawJobRecord(
            job_title=self.title, company_name=self.company, source_url=self.url,
            source_name=self.source_name, salary_text="", province_text="Hà Nội",
            experience_text="2 năm",
        )

    def fetch_job_full_detail(self, source_url):
        return _detail()


def _count_jobs(conn, title):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM job_postings WHERE job_title = %s", (title,))
        n = cur.fetchone()[0]
    conn.rollback()
    return n


def _race(pg_conn, monkeypatch, *, title):
    """Hai lượt crawl (TopCV và VietnamWorks) cùng một tin, mỗi bên một connection và một thread.

    Ép cuộc đua xảy ra: câu tra trùng của MỖI bên chạy xong rồi chờ bên kia tới cùng chỗ (tối đa
    2 giây) trước khi đi tiếp insert. Không có khoá thì cả hai đều tra thấy "chưa có". Có khoá thì
    bên sau còn đang chờ khoá (chưa tới câu tra) nên bên trước hết 2 giây chờ rồi đi tiếp, insert và
    commit, bên sau tra lại và thấy job."""
    company = f"Công ty Song Song {uuid.uuid4().hex[:8]}"
    # Có sẵn công ty (và tỉnh): chỉ cuộc đua giữa hai job được kiểm, không phải cuộc đua tạo công ty.
    seed = FakeAdapter("Seed", f"https://seed/{uuid.uuid4()}", company=company, title="Công việc khởi tạo")
    pipeline.run_pipeline(seed, pg_conn, "data-analyst", 1)
    pg_conn.rollback()

    barrier = threading.Barrier(2)
    real_find = db.find_repost_candidate

    def find_then_wait(conn, **kw):
        result = real_find(conn, **kw)
        try:
            barrier.wait(timeout=2)
        except threading.BrokenBarrierError:
            pass
        return result

    monkeypatch.setattr(db, "find_repost_candidate", find_then_wait)

    results = {}
    workers = []
    for source in ("TopCV", "VietnamWorks"):
        conn = psycopg2.connect(TEST_DATABASE_URL)
        conn.autocommit = False
        adapter = FakeAdapter(source, f"https://{source.lower()}/{uuid.uuid4()}",
                              company=company, title=title)
        t, out = _in_thread(lambda a=adapter, c=conn: pipeline.run_pipeline(a, c, "data-analyst", 1))
        workers.append((t, out, conn, source))
    for t, out, conn, source in workers:
        t.join(30)
        assert not t.is_alive(), f"{source} chưa chạy xong"
        assert "error" not in out, out
        results[source] = out["value"]
        conn.rollback()
        conn.close()
    return results


def test_two_crawls_of_the_same_listing_create_exactly_one_job(pg_conn, monkeypatch):
    title = f"Backend Developer {uuid.uuid4().hex[:6]}"
    results = _race(pg_conn, monkeypatch, title=title)

    assert _count_jobs(pg_conn, title) == 1
    assert sum(r.get("inserted", 0) for r in results.values()) == 1
    assert sum(r.get("skipped_duplicate_repost", 0) for r in results.values()) == 1
    assert sum(r.get("errors", 0) for r in results.values()) == 0


def test_control_without_the_lock_the_same_race_creates_two_jobs(pg_conn, monkeypatch):
    """Đối chứng: tắt khoá thì đúng kịch bản trên sinh hai job cùng dedup_key. Test này không bảo vệ
    điều gì của code; nó chứng minh test ở trên dựng đúng cuộc đua nên việc nó qua là có ý nghĩa."""
    monkeypatch.setattr(db, "lock_job_dedup_key", lambda *a, **k: None)
    title = f"Backend Developer {uuid.uuid4().hex[:6]}"
    _race(pg_conn, monkeypatch, title=title)

    assert _count_jobs(pg_conn, title) == 2
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(DISTINCT dedup_key) FROM job_postings WHERE job_title = %s", (title,))
        assert cur.fetchone()[0] == 1
    pg_conn.rollback()
