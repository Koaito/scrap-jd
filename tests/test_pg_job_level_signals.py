"""
job_postings.level_signals trên POSTGRES THẬT (10/2026, xem
sql/migration_add_job_level_signals.sql). Cách chạy giống
tests/test_pg_job_level_source.py: đặt TEST_DATABASE_URL trỏ tới database dùng
riêng cho test (tên chứa "test"); không đặt thì cả file được bỏ qua.

Chứng minh trên SQL thật:
  - pipeline crawl lưu đúng hai chuỗi derive_level đã đọc, và tính lại từ tín hiệu
    đã lưu ra đúng level đang lưu (không có logic song song);
  - các đường ghi level TỰ ĐỘNG ghi tín hiệu cùng level, không có tín hiệu thì NULL
    (không giữ tín hiệu cũ nói dối), và không bao giờ đè dòng 'manual';
  - đường SỬA TAY không đụng tới tín hiệu;
  - CHECK chặn giá trị không phải object; migration idempotent, không UPDATE dòng
    nào (updated_at/content_hash không đổi), cho ra đúng constraint như schema.sql.
"""
import os
import uuid
from urllib.parse import urlparse

import psycopg2
import pytest

import db
from scrapjd import normalize
import pipeline
from adapters.base import BaseAdapter
from scrapjd.models import RawJobRecord

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
_MIGRATION_PATH = os.path.join(_ROOT, "sql", "migration_add_job_level_signals.sql")
VERSION = normalize.LEVEL_RULE_VERSION
SIG = {"experience_text": "3 năm", "level_hint": ""}


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


# ------------------------------------------------------------------ helpers
def _one(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def _lv(conn, code):
    return db.get_level_id(conn, code)


def _company(conn):
    cid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)",
                    (cid, f"Công ty Signals {uuid.uuid4().hex[:8]}"))
    return cid


def _signals(conn, job_id):
    return _one(conn, "SELECT level_signals FROM job_postings WHERE job_id = %s", (job_id,))[0]


def _job(conn, *, level=None, source=None, version=None, signals=None):
    job_id = db.insert_job(
        conn, company_id=_company(conn), job_title="Data Analyst", matching_industry="Data",
        level_id=_lv(conn, level) if level else None, province_id=None, work_type=None,
        currency="VNĐ", salary_min=None, salary_max=None, salary_type="NEGOTIABLE",
        source_url=f"https://example.com/{uuid.uuid4()}", source_name="Fake",
        level_source=source, level_rule_version=version, level_signals=signals,
    )
    conn.commit()
    return job_id


# ------------------------------------------------------------------ insert_job
def test_insert_job_stores_signals_as_given(pg_conn):
    with_text = _job(pg_conn, level="Middle", source="years", version=VERSION, signals=SIG)
    empty = _job(pg_conn, level="Junior", source="default", version=VERSION,
                 signals={"experience_text": "", "level_hint": ""})
    unknown = _job(pg_conn, level="Junior")
    assert _signals(pg_conn, with_text) == SIG
    # {} rỗng (đã lưu, nguồn không có gì) KHÁC NULL (chưa từng lưu).
    assert _signals(pg_conn, empty) == {"experience_text": "", "level_hint": ""}
    assert _signals(pg_conn, unknown) is None


def test_insert_job_rejects_signals_without_a_derived_source(pg_conn):
    with pytest.raises(ValueError):
        _job(pg_conn, level="Middle", signals=SIG)
    with pytest.raises(ValueError):
        _job(pg_conn, level="Middle", source="manual", signals=SIG)
    pg_conn.rollback()


def test_database_check_rejects_non_object_signals(pg_conn):
    job_id = _job(pg_conn, level="Senior")
    for bad in ('[]', '"3 năm"', '3', 'null'):
        with pytest.raises(psycopg2.errors.CheckViolation):
            with pg_conn.cursor() as cur:
                cur.execute("UPDATE job_postings SET level_signals = %s::jsonb WHERE job_id = %s",
                            (bad, job_id))
        pg_conn.rollback()


# ------------------------------------------------------------------ update_job
def test_update_job_automatic_path_writes_signals_and_manual_path_leaves_them(pg_conn):
    job_id = _job(pg_conn, level="Junior", source="default", version=VERSION)
    db.update_job(pg_conn, job_id, level_id=_lv(pg_conn, "Middle"), level_source="years",
                  level_rule_version=VERSION, level_signals=SIG)
    pg_conn.commit()
    assert _signals(pg_conn, job_id) == SIG

    # Ghi tự động lần nữa nhưng không biết tín hiệu: thành NULL, không giữ tín hiệu cũ.
    db.update_job(pg_conn, job_id, level_id=_lv(pg_conn, "Senior"), level_source="title",
                  level_rule_version=VERSION)
    pg_conn.commit()
    assert _signals(pg_conn, job_id) is None

    # Đường sửa tay: đổi level -> manual, tín hiệu (dữ kiện về nguồn) giữ nguyên.
    db.update_job(pg_conn, job_id, level_id=_lv(pg_conn, "Senior"), level_source="years",
                  level_rule_version=VERSION, level_signals=SIG)
    db.update_job(pg_conn, job_id, level_id=_lv(pg_conn, "Lead"))
    pg_conn.commit()
    assert _signals(pg_conn, job_id) == SIG
    assert _one(pg_conn, "SELECT level_source FROM job_postings WHERE job_id = %s",
                (job_id,))[0] == "manual"


def test_update_job_rejects_signals_on_manual_path_or_without_level(pg_conn):
    job_id = _job(pg_conn, level="Junior")
    with pytest.raises(ValueError):
        db.update_job(pg_conn, job_id, level_id=_lv(pg_conn, "Middle"), level_signals=SIG)
    with pytest.raises(ValueError):
        db.update_job(pg_conn, job_id, level_source="years", level_rule_version=VERSION,
                      level_signals=SIG)
    pg_conn.rollback()


def test_automatic_write_never_overwrites_manual_signals(pg_conn):
    manual = _job(pg_conn, level="Lead", source="manual")
    db.update_job(pg_conn, manual, level_id=_lv(pg_conn, "Junior"), level_source="years",
                  level_rule_version=VERSION, level_signals=SIG)
    pg_conn.commit()
    assert _signals(pg_conn, manual) is None      # không bị ghi vào dòng manual
    assert _one(pg_conn, "SELECT l.level_code, jp.level_source FROM job_postings jp "
                         "JOIN levels l USING (level_id) WHERE jp.job_id = %s",
                (manual,)) == ("Lead", "manual")


# ------------------------------------------------------------------ update_job_from_recrawl
def test_update_job_from_recrawl_writes_signals_with_level_only(pg_conn):
    job_id = _job(pg_conn, level="Junior", source="default", version=VERSION)
    db.update_job_from_recrawl(pg_conn, job_id, job_title="Mới", level_id=_lv(pg_conn, "Middle"),
                               level_source="years", level_rule_version=VERSION,
                               level_signals=SIG)
    pg_conn.commit()
    assert _signals(pg_conn, job_id) == SIG

    # Không truyền level (JD bị cắt): level và tín hiệu đều giữ nguyên.
    db.update_job_from_recrawl(pg_conn, job_id, job_title="Mới 2")
    pg_conn.commit()
    assert _signals(pg_conn, job_id) == SIG

    with pytest.raises(ValueError):
        db.update_job_from_recrawl(pg_conn, job_id, job_title="x", level_signals=SIG)
    pg_conn.rollback()


# ------------------------------------------------------------------ pipeline thật
class _Adapter(BaseAdapter):
    source_name = "Fake"

    def __init__(self, company, title, experience, level_hint=""):
        super().__init__()
        self.url = f"https://example.com/{uuid.uuid4()}"
        self.record = RawJobRecord(
            job_title=title, company_name=company, source_url=self.url, source_name="Fake",
            salary_text="15 - 25 triệu", province_text="Hà Nội", experience_text=experience,
            level_hint=level_hint,
        )

    def fetch_jobs(self, category_key, max_pages):
        yield self.record

    def fetch_job_full_detail(self, source_url):
        return {"work_type": "Toàn thời gian", "deadline_text": "05/09/2026",
                "job_description": "mô tả", "requirements": "yêu cầu", "perks": "",
                "required_skills": ["SQL"]}


@pytest.mark.parametrize("title,experience,hint", [
    ("Senior Data Engineer", "1 năm", ""),
    ("Data Analyst", "3 năm", ""),
    ("Data Analyst", "Trên 5 năm", ""),
    ("Data Analyst", "", "Manager"),
    ("Data Analyst", "", ""),
    ("Data Engineer (Senior/Leader)", "", ""),
])
def test_pipeline_insert_stores_signals_and_recompute_matches_stored_level(
        pg_conn, title, experience, hint):
    company = f"Công ty Pipeline {uuid.uuid4().hex[:8]}"
    stats = pipeline.run_pipeline(_Adapter(company, title, experience, hint), pg_conn,
                                  "data-analyst", 1)
    assert stats["inserted"] == 1
    job_id = _one(pg_conn, "SELECT jp.job_id FROM job_postings jp JOIN companies c USING "
                           "(company_id) WHERE c.company_name = %s", (company,))[0]
    stored = _signals(pg_conn, job_id)
    assert stored == {"experience_text": experience, "level_hint": hint}

    level, source = _one(
        pg_conn, "SELECT l.level_code, jp.level_source FROM job_postings jp "
                 "LEFT JOIN levels l USING (level_id) WHERE jp.job_id = %s", (job_id,))
    recomputed = normalize.derive_level_from_signals(title, stored)
    assert (recomputed.level, recomputed.source) == (level, source)


# ------------------------------------------------------------------ migration
def test_migration_matches_schema_adds_null_column_and_is_idempotent(pg_conn):
    def defs():
        return _one(pg_conn,
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = "
                    "'job_postings'::regclass AND conname = 'chk_job_postings_level_signals'")

    expected = defs()
    assert expected is not None

    # Dựng lại trạng thái TRƯỚC migration (bỏ cột, kéo theo CHECK), có sẵn vài job cũ.
    with pg_conn.cursor() as cur:
        cur.execute("ALTER TABLE job_postings DROP COLUMN level_signals")
    pg_conn.commit()

    def old_job(level, source=None, version=None):
        # INSERT thẳng (insert_job đã ghi cột level_signals, mà cột này đang bị bỏ).
        job_id = str(uuid.uuid4())
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO job_postings (job_id, company_id, job_title, level_id, level_source, "
                "level_rule_version, source_url) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (job_id, _company(pg_conn), "Job cũ", _lv(pg_conn, level), source, version,
                 f"https://example.com/{job_id}"))
        pg_conn.commit()
        return job_id

    old_a = old_job("Junior")
    old_b = old_job("Senior", "years", VERSION)
    before = {j: _one(pg_conn, "SELECT updated_at, content_hash FROM job_postings WHERE job_id = %s",
                      (j,)) for j in (old_a, old_b)}

    for _ in range(2):          # idempotent: chạy lần hai không lỗi, không đổi kết quả
        with open(_MIGRATION_PATH, encoding="utf-8") as fh, pg_conn.cursor() as cur:
            cur.execute(fh.read())
        pg_conn.commit()

    assert defs() == expected                       # đúng constraint như schema.sql
    for j in (old_a, old_b):
        assert _signals(pg_conn, j) is None         # không bịa tín hiệu cho job cũ
        assert _one(pg_conn, "SELECT updated_at, content_hash FROM job_postings WHERE job_id = %s",
                    (j,)) == before[j]              # không UPDATE dòng nào
