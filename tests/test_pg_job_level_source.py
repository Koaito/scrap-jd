"""
job_postings.level_source / level_rule_version trên POSTGRES THẬT (10/2026,
xem sql/migration_add_job_level_source.sql).

Cách chạy giống tests/test_pg_integration.py: đặt TEST_DATABASE_URL trỏ tới một
database dùng riêng cho test (tên phải chứa "test", fixture DROP SCHEMA public).
Không đặt biến này -> cả file được bỏ qua.

Chứng minh trên SQL thật:
  - CHECK ở DB chặn tổ hợp sai và chấp nhận đúng danh sách normalize.LEVEL_SOURCES;
  - pipeline crawl đóng dấu level (căn cứ + phiên bản quy tắc) khi insert / cập nhật;
  - sửa tay (update_job mặc định) đóng dấu 'manual' CHỈ khi level thật sự đổi;
  - ghi tự động không bao giờ đè dòng 'manual';
  - migration điền 'manual' đúng tập job chắc chắn do người đặt level, idempotent,
    không làm nhảy updated_at, và cho ra đúng constraint như schema.sql.
"""
import os
import uuid
from urllib.parse import urlparse

import psycopg2
import pytest

import db
import normalize
import pipeline
from adapters.base import BaseAdapter
from models import RawJobRecord

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
_MIGRATION_PATH = os.path.join(_ROOT, "sql", "migration_add_job_level_source.sql")
VERSION = normalize.LEVEL_RULE_VERSION


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


def _stamp(conn, job_id):
    """(level_code, level_source, level_rule_version) của job."""
    return _one(
        conn,
        "SELECT l.level_code, jp.level_source, jp.level_rule_version "
        "FROM job_postings jp LEFT JOIN levels l USING (level_id) WHERE jp.job_id = %s",
        (job_id,))


def _company(conn):
    cid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)",
                    (cid, f"Công ty Level {uuid.uuid4().hex[:8]}"))
    return cid


def _user(conn):
    uid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO app_users (ss_user_id, full_name, email) VALUES (%s, %s, %s)",
                    (uid, "Người sửa", f"{uid}@example.com"))
    conn.commit()
    return uid


def _job(conn, *, level=None, source=None, version=None, created_by=None, title="Data Engineer"):
    job_id = db.insert_job(
        conn, company_id=_company(conn), job_title=title, matching_industry="Data",
        level_id=_lv(conn, level) if level else None, province_id=None, work_type=None,
        currency="VNĐ", salary_min=None, salary_max=None, salary_type="NEGOTIABLE",
        source_url=f"https://example.com/{uuid.uuid4()}", source_name="Fake",
        created_by=created_by, level_source=source, level_rule_version=version,
    )
    conn.commit()
    return job_id


# ---------------------------------------------------------- luật đóng dấu (thuần)
@pytest.mark.parametrize("level_id,source,version", [
    (1, "bogus", 1),             # nguồn không có trong LEVEL_SOURCES
    (1, "years", None),          # nguồn do máy suy thiếu phiên bản
    (None, "years", 1),          # nguồn do máy suy mà không có level
    (1, "manual", 1),            # manual không có phiên bản
    (1, None, 1),                # phiên bản mà không có nguồn
])
def test_check_level_stamp_rejects_bad_combinations(level_id, source, version):
    with pytest.raises(ValueError):
        db.jobs._check_level_stamp(level_id, source, version)


@pytest.mark.parametrize("level_id,source,version,expected", [
    (1, None, None, (None, None)),
    (None, None, None, (None, None)),
    (1, "manual", None, ("manual", None)),
    (None, "manual", None, ("manual", None)),   # người chủ động xoá level
    (3, "years", 1, ("years", 1)),
])
def test_check_level_stamp_accepts_valid_combinations(level_id, source, version, expected):
    assert db.jobs._check_level_stamp(level_id, source, version) == expected


# ------------------------------------------------------------------ CHECK ở DB
def _raw_update(conn, job_id, level_source, version, level_id_sql="level_id"):
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE job_postings SET level_source = %s, level_rule_version = %s, "
            f"level_id = {level_id_sql} WHERE job_id = %s",
            (level_source, version, job_id))


@pytest.mark.parametrize("source,version,level", [
    ("bogus", 1, "Senior"),
    ("years", None, "Senior"),
    (None, 1, "Senior"),
    ("manual", 1, "Senior"),
    ("years", 1, None),          # nguồn máy suy nhưng không có level
])
def test_database_check_rejects_bad_stamp(pg_conn, source, version, level):
    job_id = _job(pg_conn, level="Senior" if level else None)
    with pytest.raises(psycopg2.errors.CheckViolation):
        _raw_update(pg_conn, job_id, source, version,
                    level_id_sql="NULL" if level is None else "level_id")
    pg_conn.rollback()


def test_database_check_accepts_every_normalize_level_source(pg_conn):
    """Danh sách trong CHECK (SQL) phải khớp normalize.LEVEL_SOURCES (Python)."""
    for source in normalize.LEVEL_SOURCES:
        job_id = _job(pg_conn, level="Senior")
        version = None if source == "manual" else VERSION
        _raw_update(pg_conn, job_id, source, version)
        pg_conn.commit()
        assert _stamp(pg_conn, job_id) == ("Senior", source, version)


# ------------------------------------------------------------------ insert_job
def test_insert_job_stores_stamp(pg_conn):
    derived = _job(pg_conn, level="Middle", source="years", version=VERSION)
    manual = _job(pg_conn, level="Lead", source="manual")
    unknown = _job(pg_conn, level="Junior")
    no_level = _job(pg_conn)
    assert _stamp(pg_conn, derived) == ("Middle", "years", VERSION)
    assert _stamp(pg_conn, manual) == ("Lead", "manual", None)
    assert _stamp(pg_conn, unknown) == ("Junior", None, None)
    assert _stamp(pg_conn, no_level) == (None, None, None)


def test_create_manual_job_stamps_manual_only_when_level_given(pg_conn):
    cid = _company(pg_conn)
    with_level = db.create_manual_job(pg_conn, job_title="Dev A", company_id=cid,
                                      level_id=_lv(pg_conn, "Senior"))
    without = db.create_manual_job(pg_conn, job_title="Dev B", company_id=cid)
    pg_conn.commit()
    assert _stamp(pg_conn, with_level) == ("Senior", "manual", None)
    assert _stamp(pg_conn, without) == (None, None, None)


# ------------------------------------------------------------------ update_job
def test_update_job_marks_manual_only_when_level_changes(pg_conn):
    job_id = _job(pg_conn, level="Junior", source="years", version=VERSION)

    # Form gửi lại đúng level đang có (sửa field khác): KHÔNG phải người sửa level.
    db.update_job(pg_conn, job_id, level_id=_lv(pg_conn, "Junior"), ss_team_notes="ghi chú")
    pg_conn.commit()
    assert _stamp(pg_conn, job_id) == ("Junior", "years", VERSION)

    # Không đụng level: dấu giữ nguyên.
    db.update_job(pg_conn, job_id, ss_team_notes="ghi chú 2")
    pg_conn.commit()
    assert _stamp(pg_conn, job_id) == ("Junior", "years", VERSION)

    # Đổi level thật: manual, bỏ phiên bản.
    db.update_job(pg_conn, job_id, level_id=_lv(pg_conn, "Senior"))
    pg_conn.commit()
    assert _stamp(pg_conn, job_id) == ("Senior", "manual", None)

    # Đã manual, gửi lại cùng level: vẫn manual.
    db.update_job(pg_conn, job_id, level_id=_lv(pg_conn, "Senior"))
    pg_conn.commit()
    assert _stamp(pg_conn, job_id) == ("Senior", "manual", None)


def test_update_job_marks_manual_when_level_is_cleared(pg_conn):
    job_id = _job(pg_conn, level="Middle", source="title", version=VERSION)
    db.update_job(pg_conn, job_id, clear_fields={"level_id"})
    pg_conn.commit()
    assert _stamp(pg_conn, job_id) == (None, "manual", None)

    # Level vốn đã trống: xoá nữa không tạo dấu 'manual' từ hư không.
    empty = _job(pg_conn)
    db.update_job(pg_conn, empty, clear_fields={"level_id"})
    pg_conn.commit()
    assert _stamp(pg_conn, empty) == (None, None, None)


def test_update_job_with_explicit_stamp_writes_derived_level(pg_conn):
    job_id = _job(pg_conn, level="Junior")
    db.update_job(pg_conn, job_id, level_id=_lv(pg_conn, "Lead"),
                  level_source="label", level_rule_version=VERSION)
    pg_conn.commit()
    assert _stamp(pg_conn, job_id) == ("Lead", "label", VERSION)

    # "Chưa biết" tường minh (script cũ dùng suy từ chữ, không có căn cứ chuẩn).
    db.update_job(pg_conn, job_id, level_id=_lv(pg_conn, "Senior"), level_source=None)
    pg_conn.commit()
    assert _stamp(pg_conn, job_id) == ("Senior", None, None)


def test_automatic_write_never_overwrites_manual_level(pg_conn):
    job_id = _job(pg_conn, level="Senior", source="manual")
    db.update_job(pg_conn, job_id, level_id=_lv(pg_conn, "Junior"),
                  level_source="years", level_rule_version=VERSION)
    pg_conn.commit()
    assert _stamp(pg_conn, job_id) == ("Senior", "manual", None)


def test_update_job_rejects_stamp_without_level(pg_conn):
    job_id = _job(pg_conn, level="Senior")
    with pytest.raises(ValueError):
        db.update_job(pg_conn, job_id, level_source="years", level_rule_version=VERSION)
    with pytest.raises(ValueError):
        db.update_job(pg_conn, job_id, level_id=_lv(pg_conn, "Junior"), level_source="years")


# ------------------------------------------------------- update_job_from_recrawl
def test_update_job_from_recrawl_stamps_level_and_respects_manual(pg_conn):
    job_id = _job(pg_conn, level="Junior", source="default", version=VERSION)
    assert db.update_job_from_recrawl(
        pg_conn, job_id, job_title="Mới", level_id=_lv(pg_conn, "Senior"),
        level_source="title", level_rule_version=VERSION) is True
    pg_conn.commit()
    assert _stamp(pg_conn, job_id) == ("Senior", "title", VERSION)

    # Không truyền level: dấu cũ giữ nguyên.
    db.update_job_from_recrawl(pg_conn, job_id, job_title="Mới 2")
    pg_conn.commit()
    assert _stamp(pg_conn, job_id) == ("Senior", "title", VERSION)

    # Dòng 'manual' (kể cả khi updated_by còn NULL): level không bị đè, field khác vẫn ghi.
    manual = _job(pg_conn, level="Lead", source="manual")
    assert db.update_job_from_recrawl(
        pg_conn, manual, job_title="Tiêu đề mới", level_id=_lv(pg_conn, "Junior"),
        level_source="years", level_rule_version=VERSION) is True
    pg_conn.commit()
    assert _stamp(pg_conn, manual) == ("Lead", "manual", None)
    assert _one(pg_conn, "SELECT job_title FROM job_postings WHERE job_id = %s",
                (manual,)) == ("Tiêu đề mới",)

    with pytest.raises(ValueError):
        db.update_job_from_recrawl(pg_conn, job_id, job_title="x", level_source="years",
                                   level_rule_version=VERSION)


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


def _crawl(conn, adapter):
    return pipeline.run_pipeline(adapter, conn, "data-analyst", 1)


@pytest.mark.parametrize("title,experience,hint,level,source", [
    ("Senior Data Engineer", "1 năm", "", "Senior", "title"),
    ("Data Analyst", "3 năm", "", "Middle", "years"),
    ("Data Analyst", "Trên 5 năm", "", "Lead", "label"),
    ("Data Analyst", "", "Manager", "Manager", "hint"),
    ("Data Analyst", "", "", "Junior", "default"),
    ("Data Engineer (Senior/Leader)", "", "", "Senior", "title_range"),
])
def test_pipeline_insert_stamps_level_source_and_rule_version(pg_conn, title, experience,
                                                              hint, level, source):
    company = f"Công ty Pipeline {uuid.uuid4().hex[:8]}"
    assert _crawl(pg_conn, _Adapter(company, title, experience, hint))["inserted"] == 1
    job_id = _one(pg_conn, "SELECT jp.job_id FROM job_postings jp JOIN companies c USING "
                           "(company_id) WHERE c.company_name = %s", (company,))[0]
    assert _stamp(pg_conn, job_id) == (level, source, VERSION)


def test_job_matches_derive_level_for_the_same_input():
    """Thứ pipeline lưu chính là thứ derive_level trả (không có logic song song)."""
    d = normalize.derive_level("3 năm", "Data Analyst", "")
    assert (d.level, d.source) == ("Middle", "years")


# ------------------------------------------------------------------ migration
def _constraint_defs(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'job_postings'::regclass AND conname IN ('chk_job_postings_level_source', 'chk_job_postings_level_stamp') "
            "ORDER BY conname")
        return cur.fetchall()


def _run_migration(conn):
    with open(_MIGRATION_PATH, encoding="utf-8") as fh:
        sql = fh.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def test_migration_matches_schema_and_marks_only_certain_manual_jobs(pg_conn):
    expected_defs = _constraint_defs(pg_conn)
    assert len(expected_defs) == 2

    # Dựng lại trạng thái TRƯỚC migration: bỏ hai cột (kéo theo hai CHECK).
    with pg_conn.cursor() as cur:
        cur.execute("ALTER TABLE job_postings DROP COLUMN level_source, "
                    "DROP COLUMN level_rule_version")
    pg_conn.commit()

    user = _user(pg_conn)

    def add(level, created_by=None, updated_by=None, title="Job"):
        cid = _company(pg_conn)
        job_id = str(uuid.uuid4())
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO job_postings (job_id, company_id, job_title, level_id, created_by, "
                "updated_by, source_url) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (job_id, cid, title, _lv(pg_conn, level) if level else None, created_by,
                 updated_by, f"https://example.com/{job_id}"))
        pg_conn.commit()
        return job_id

    crawled = add("Junior")                                  # job crawl: chưa biết
    created = add("Senior", created_by=user)                 # nhập tay/import: manual
    edited_level = add("Lead")                               # audit có đổi level_code: manual
    edited_other = add("Middle", updated_by=user)            # sửa field khác: chưa biết
    no_level_created = add(None, created_by=user)            # không có level: không có gì để đóng dấu
    other_audit = add("Junior")                              # audit đổi field khác: chưa biết
    wrong_entity = add("Junior")                             # audit level_code nhưng của COMPANY

    def audit(entity_id, changes, entity_type="JOB", action="UPDATE_JOB"):
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO audit_logs (actor_id, action_type, entity_type, entity_id, changes, "
                "is_manual_log) VALUES (%s, %s, %s, %s, %s::jsonb, false)",
                (user, action, entity_type, entity_id, changes))
        pg_conn.commit()

    audit(edited_level, '{"level_code": {"old": "Junior", "new": "Lead"}}')
    audit(other_audit, '{"salary_min": {"old": 1, "new": 2}}')
    audit(wrong_entity, '{"level_code": {"old": "A", "new": "B"}}', entity_type="COMPANY",
          action="UPDATE_COMPANY")

    stamps_before = {
        j: _one(pg_conn, "SELECT updated_at FROM job_postings WHERE job_id = %s", (j,))[0]
        for j in (crawled, created, edited_level)
    }

    _run_migration(pg_conn)
    _run_migration(pg_conn)          # idempotent: chạy lần hai không lỗi, không đổi kết quả

    def src(job_id):
        return _one(pg_conn, "SELECT level_source, level_rule_version FROM job_postings "
                             "WHERE job_id = %s", (job_id,))

    assert src(crawled) == (None, None)
    assert src(created) == ("manual", None)
    assert src(edited_level) == ("manual", None)
    assert src(edited_other) == (None, None)
    assert src(no_level_created) == (None, None)
    assert src(other_audit) == (None, None)
    assert src(wrong_entity) == (None, None)

    # Di trú không làm "cập nhật lần cuối" của job nhảy.
    for job_id, before in stamps_before.items():
        assert _one(pg_conn, "SELECT updated_at FROM job_postings WHERE job_id = %s",
                    (job_id,))[0] == before
    # Trigger updated_at được bật lại sau di trú.
    assert _one(pg_conn, "SELECT tgenabled FROM pg_trigger WHERE tgname = "
                         "'set_updated_at_job_postings'") == ("O",)
    db.update_job(pg_conn, crawled, ss_team_notes="x")
    pg_conn.commit()
    assert _one(pg_conn, "SELECT updated_at FROM job_postings WHERE job_id = %s",
                (crawled,))[0] > stamps_before[crawled]

    # Constraint sau di trú giống hệt constraint dựng từ schema.sql.
    assert _constraint_defs(pg_conn) == expected_defs
