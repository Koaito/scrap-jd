"""
Lệnh tính lại level (Phần 2b) trên POSTGRES THẬT: cờ phiên app.skip_updated_at
(sql/migration_add_skip_updated_at_flag.sql) và `recompute_levels.run`. Cách chạy
giống tests/test_pg_job_level_signals.py: đặt TEST_DATABASE_URL trỏ tới database
dùng riêng cho test (tên chứa "test"); không đặt thì cả file được bỏ qua.

Chứng minh trên SQL thật:
  - trigger trg_set_updated_at: có cờ thì updated_at giữ nguyên, KHÔNG cờ thì nhảy như cũ
    (cho cả job_postings và một bảng khác dùng chung hàm), cờ chỉ sống trong transaction;
  - migration idempotent và cho ra đúng hàm như schema.sql;
  - chọn job theo dấu (không đụng 'manual'), bỏ qua job có updated_by chưa rõ level;
  - chạy thử không ghi gì; --apply ghi đúng, giữ updated_at, đổi content_hash, không đụng
    job cũ không có căn cứ title; chạy lại không ghi thêm gì;
  - ghi kiểu so-sánh-rồi-ghi không đè dữ liệu đã đổi giữa chừng (kể cả chuyển sang 'manual');
  - từ chối --apply khi hàm trigger chưa biết cờ.
"""
import os
import uuid
from urllib.parse import urlparse

import psycopg2
import pytest

from scrapjd import db
from scrapjd import normalize
from scrapjd.cli import recompute_levels as rl

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
_MIGRATION_PATH = os.path.join(_ROOT, "sql", "migration_add_skip_updated_at_flag.sql")
VERSION = normalize.LEVEL_RULE_VERSION
SIG_3Y = {"experience_text": "3 năm", "level_hint": ""}
PAST = "2020-01-01 00:00:00"

# Hàm trigger như TRƯỚC khi có cờ (để dựng lại DB cũ).
_OLD_TRIGGER_FN = """
CREATE OR REPLACE FUNCTION trg_set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


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
    db.baseline_migrations(conn)  # như `init-db`: schema.sql đã chứa kết quả mọi migration
    yield conn
    conn.rollback()
    conn.close()


@pytest.fixture(autouse=True)
def clean_jobs(pg_conn):
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE job_postings CASCADE")
    pg_conn.commit()
    yield
    pg_conn.rollback()


# ------------------------------------------------------------------ helpers
def _one(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    conn.rollback()
    return row


def _lv(conn, code):
    return db.get_level_id(conn, code)


def _company(conn):
    cid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)",
                    (cid, f"Công ty Recompute {uuid.uuid4().hex[:8]}"))
    conn.commit()
    return cid


def _user(conn):
    uid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO app_users (ss_user_id, full_name, email) VALUES (%s, %s, %s)",
                    (uid, "Người sửa", f"{uid}@example.com"))
    conn.commit()
    return uid


def _job(conn, title, *, level=None, source=None, version=None, signals=None,
         company_id=None, updated_by=None):
    job_id = db.insert_job(
        conn, company_id=company_id or _company(conn), job_title=title, matching_industry="Data",
        level_id=_lv(conn, level) if level else None, province_id=None, work_type=None,
        currency="VNĐ", salary_min=None, salary_max=None, salary_type="NEGOTIABLE",
        source_url=f"https://example.com/{uuid.uuid4()}", source_name="Fake",
        level_source=source, level_rule_version=version, level_signals=signals,
    )
    with conn.cursor() as cur:
        # Đặt updated_at về quá khứ: có cờ nên trigger không ghi đè giá trị này.
        cur.execute("SELECT set_config('app.skip_updated_at', 'on', true)")
        cur.execute("UPDATE job_postings SET updated_at = %s, updated_by = %s WHERE job_id = %s",
                    (PAST, updated_by, job_id))
    conn.commit()
    return job_id


def _state(conn, job_id):
    """(level_code, level_source, level_rule_version, level_signals, updated_at, content_hash)."""
    return _one(conn, """
        SELECT l.level_code, jp.level_source, jp.level_rule_version, jp.level_signals,
               jp.updated_at::text, jp.content_hash
        FROM job_postings jp LEFT JOIN levels l ON l.level_id = jp.level_id WHERE jp.job_id = %s
    """, (job_id,))


def _fn_def(conn):
    return _one(conn, "SELECT pg_get_functiondef('trg_set_updated_at'::regproc)")[0]


# ------------------------------------------------------------------ cờ updated_at
def _updated_at(conn, table, key_col, key):
    return _one(conn, f"SELECT updated_at FROM {table} WHERE {key_col} = %s", (key,))[0]


@pytest.mark.parametrize("table,key_col,column,get_key", [
    ("job_postings", "job_id", "ss_team_notes", lambda c: _job(c, "Data Analyst", level="Junior")),
    ("companies", "company_id", "company_name", lambda c: _company(c)),
])
def test_flag_keeps_updated_at_without_flag_it_jumps_and_flag_is_transaction_local(
        pg_conn, table, key_col, column, get_key):
    key = get_key(pg_conn)
    t0 = _updated_at(pg_conn, table, key_col, key)

    def update(flag):
        with pg_conn.cursor() as cur:
            if flag:
                cur.execute("SELECT set_config('app.skip_updated_at', 'on', true)")
            cur.execute(f"UPDATE {table} SET {column} = %s WHERE {key_col} = %s", (f"x-{uuid.uuid4()}", key))
        pg_conn.commit()
        return _updated_at(pg_conn, table, key_col, key)

    t1 = update(flag=False)
    assert t1 > t0, "không có cờ: trigger phải đặt updated_at như cũ"
    t2 = update(flag=True)
    assert t2 == t1, "có cờ: updated_at phải giữ nguyên"
    t3 = update(flag=False)
    assert t3 > t2, "cờ chỉ có hiệu lực trong transaction đặt nó, transaction sau lại nhảy bình thường"


def test_flag_off_values_do_not_skip(pg_conn):
    job_id = _job(pg_conn, "Data Analyst", level="Junior")
    t0 = _updated_at(pg_conn, "job_postings", "job_id", job_id)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT set_config('app.skip_updated_at', 'off', true)")
        cur.execute("UPDATE job_postings SET ss_team_notes = 'n' WHERE job_id = %s", (job_id,))
    pg_conn.commit()
    assert _updated_at(pg_conn, "job_postings", "job_id", job_id) > t0


def test_migration_is_idempotent_and_matches_schema(pg_conn):
    schema_def = _fn_def(pg_conn)
    assert db.skip_updated_at_supported(pg_conn)

    with pg_conn.cursor() as cur:
        cur.execute(_OLD_TRIGGER_FN)
    pg_conn.commit()
    assert not db.skip_updated_at_supported(pg_conn)

    sql = open(_MIGRATION_PATH, encoding="utf-8").read()
    for _ in range(2):
        with pg_conn.cursor() as cur:
            cur.execute(sql)
        pg_conn.commit()
    assert _fn_def(pg_conn) == schema_def
    assert db.skip_updated_at_supported(pg_conn)


# ------------------------------------------------------------------ chọn job
def test_candidates_are_chosen_by_stamp_and_manual_is_never_chosen(pg_conn):
    user = _user(pg_conn)
    unstamped = _job(pg_conn, "Data Analyst", level="Senior")
    old_version = _job(pg_conn, "Data Analyst", level="Junior", source="years", version=VERSION - 1)
    current = _job(pg_conn, "Data Analyst", level="Junior", source="years", version=VERSION)
    manual = _job(pg_conn, "Data Analyst", level="Junior", source="manual")
    edited = _job(pg_conn, "Data Analyst", level="Middle", updated_by=user)

    rows = db.list_level_recompute_candidates(pg_conn, VERSION)
    by_id = {str(r["job_id"]): r for r in rows}
    assert set(by_id) == {unstamped, old_version, edited}
    assert current not in by_id and manual not in by_id
    assert by_id[edited]["has_editor"] is True and by_id[unstamped]["has_editor"] is False
    assert by_id[unstamped]["level_code"] == "Senior" and by_id[unstamped]["level_signals"] is None

    assert len(db.list_level_recompute_candidates(pg_conn, VERSION, limit=1)) == 1
    # Tăng phiên bản quy tắc thì job đang ở bản hiện tại thành "cũ".
    assert str(current) in {str(r["job_id"]) for r in db.list_level_recompute_candidates(pg_conn, VERSION + 1)}


# ------------------------------------------------------------------ chạy lệnh
def _scenario(conn):
    """Các loại job đại diện (xem recompute_levels.plan_job)."""
    user = _user(conn)
    shared = _company(conn)
    return {
        # Job cũ, tiêu đề nêu rõ Senior, đang Middle -> đổi.
        "old_title": _job(conn, "Senior Data Engineer", level="Middle", company_id=shared),
        # Job khác cùng công ty + tiêu đề + level Senior: old_title đổi xong sẽ NHẬP nhóm trùng với nó.
        "twin": _job(conn, "Senior Data Engineer", level="Senior", company_id=shared,
                     source="title", version=VERSION),
        # Job cũ, tiêu đề không có căn cứ: giữ nguyên level, dấu NULL.
        "old_notitle": _job(conn, "Data Analyst", level="Senior"),
        # Job cũ, tiêu đề cùng ý level đang có: chỉ đóng dấu.
        "old_same": _job(conn, "Senior Backend Developer", level="Senior"),
        # Job có người sửa, level chưa rõ: bỏ qua dù tiêu đề rõ.
        "edited": _job(conn, "Senior Data Scientist", level="Middle", updated_by=user),
        # Job có tín hiệu, dấu của quy tắc cũ: tính lại đầy đủ.
        "with_signals": _job(conn, "Data Analyst", level="Junior", source="default",
                             version=VERSION - 1, signals=SIG_3Y),
        # Người đã đặt level: không bao giờ đụng.
        "manual": _job(conn, "Senior Data Engineer", level="Junior", source="manual"),
    }


def _snapshot(conn, ids):
    return {name: _state(conn, job_id) for name, job_id in ids.items()}


def test_dry_run_writes_nothing_and_reports(pg_conn, capsys):
    ids = _scenario(pg_conn)
    before = _snapshot(pg_conn, ids)
    assert rl.run(pg_conn, apply=False) == 0
    assert _snapshot(pg_conn, ids) == before
    out = capsys.readouterr().out
    assert "CHẠY THỬ" in out and "--apply" in out
    assert "Middle -> Senior" in out


def test_duplicate_effects_name_the_job_joining_an_existing_group(pg_conn):
    ids = _scenario(pg_conn)
    level_ids = {code: _lv(pg_conn, code) for code in normalize.LEVEL_ORDER}
    plans = rl.build_plans(db.list_level_recompute_candidates(pg_conn, VERSION), VERSION)
    effects = rl.analyze_duplicate_effects(pg_conn, plans, level_ids)
    joined = {e["job_id"]: e for e in effects["joined"]}
    assert set(joined) == {ids["old_title"]}
    assert joined[ids["old_title"]]["others"] == [ids["twin"]]
    assert effects["left"] == []


def test_apply_writes_expected_rows_keeps_updated_at_and_changes_hash(pg_conn, capsys):
    ids = _scenario(pg_conn)
    before = _snapshot(pg_conn, ids)
    # View theo dedup_key (không level): old_title và twin đã cùng khoá từ trước khi đổi level.
    dup_before = db.count_duplicate_job_groups(pg_conn)
    assert dup_before == 1

    assert rl.run(pg_conn, apply=True) == 0
    after = _snapshot(pg_conn, ids)
    out = capsys.readouterr().out
    assert "ĐÃ GHI DB" in out and "Đã ghi: 3 job" in out

    # Job cũ có căn cứ title: đổi level + dấu, tín hiệu vẫn NULL, hash đổi, updated_at giữ nguyên.
    lvl, src, ver, sig, upd, h = after["old_title"]
    assert (lvl, src, ver, sig) == ("Senior", "title", VERSION, None)
    assert upd == before["old_title"][4] and upd.startswith("2020-01-01")
    assert h != before["old_title"][5]

    # Job cũ không có căn cứ title: không đổi gì cả.
    assert after["old_notitle"] == before["old_notitle"]
    # Job cũ, tiêu đề đồng ý với level đang có: level giữ, thêm dấu, hash không đổi.
    lvl, src, ver, sig, upd, h = after["old_same"]
    assert (lvl, src, ver, sig) == ("Senior", "title", VERSION, None)
    assert h == before["old_same"][5]
    # Job có người sửa và job manual: không đụng.
    assert after["edited"] == before["edited"]
    assert after["manual"] == before["manual"]
    # Job có tín hiệu: tính lại đầy đủ từ tín hiệu đã lưu, tín hiệu giữ nguyên.
    lvl, src, ver, sig, upd, h = after["with_signals"]
    assert (lvl, src, ver, sig) == ("Middle", "years", VERSION, SIG_3Y)
    assert upd.startswith("2020-01-01")
    # Mọi updated_at đều không nhảy.
    assert all(s[4].startswith("2020-01-01") for s in after.values())
    # old_title nhập nhóm cùng content_hash với twin (khoá cũ), và báo cáo có nêu. Nhóm trùng theo
    # dedup_key thì KHÔNG đổi vì đổi level không đổi khoá.
    assert after["old_title"][5] == after["twin"][5]
    assert db.count_duplicate_job_groups(pg_conn) == dup_before == 1
    assert "Job NHẬP vào nhóm trùng mới: 1" in out

    # Chạy lại: không còn gì để ghi (job giữ nguyên/bỏ qua vẫn được xét nhưng không ghi).
    snapshot = _snapshot(pg_conn, ids)
    assert rl.run(pg_conn, apply=True) == 0
    assert "Đã ghi: 0 job" in capsys.readouterr().out
    assert _snapshot(pg_conn, ids) == snapshot


def test_apply_in_small_batches_writes_everything(pg_conn):
    ids = _scenario(pg_conn)
    assert rl.run(pg_conn, apply=True, batch_size=1) == 0
    assert _state(pg_conn, ids["old_title"])[:3] == ("Senior", "title", VERSION)
    assert _state(pg_conn, ids["with_signals"])[:3] == ("Middle", "years", VERSION)


def test_apply_refused_when_trigger_does_not_know_the_flag(pg_conn, capsys):
    ids = _scenario(pg_conn)
    before = _snapshot(pg_conn, ids)
    with pg_conn.cursor() as cur:
        cur.execute(_OLD_TRIGGER_FN)
    pg_conn.commit()
    try:
        assert rl.run(pg_conn, apply=True) == 1
        assert "migration_add_skip_updated_at_flag.sql" in capsys.readouterr().out
        assert _snapshot(pg_conn, ids) == before
        # Chạy thử vẫn dùng được (không ghi nên không cần cờ).
        assert rl.run(pg_conn, apply=False) == 0
    finally:
        with pg_conn.cursor() as cur:
            cur.execute(open(_MIGRATION_PATH, encoding="utf-8").read())
        pg_conn.commit()


def test_run_refused_when_required_migration_is_pending(pg_conn, capsys):
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM schema_migrations WHERE filename = 'migration_add_job_level_signals.sql'")
    pg_conn.commit()
    try:
        assert rl.run(pg_conn, apply=False) == 1
        assert "migration_add_job_level_signals.sql" in capsys.readouterr().out
    finally:
        db.baseline_migrations(pg_conn)


# ------------------------------------------------------------------ so-sánh-rồi-ghi
def _change_for(conn, job_id):
    row = next(r for r in db.list_level_recompute_candidates(conn, VERSION) if str(r["job_id"]) == job_id)
    plan = rl.plan_job(row, VERSION)
    level_ids = {code: _lv(conn, code) for code in normalize.LEVEL_ORDER}
    return rl.to_change(row, plan, level_ids, VERSION)


def test_write_skips_job_changed_between_select_and_write(pg_conn):
    job_id = _job(pg_conn, "Senior Data Engineer", level="Middle")
    change = _change_for(pg_conn, job_id)
    # Giữa chừng crawl đóng dấu lại job này (vd tái crawl).
    db.update_job(pg_conn, job_id, level_id=_lv(pg_conn, "Lead"), level_source="years",
                  level_rule_version=VERSION, level_signals=SIG_3Y)
    pg_conn.commit()
    written, stale = db.write_recomputed_levels(pg_conn, [change])
    pg_conn.commit()
    assert (written, stale) == (0, [job_id])
    assert _state(pg_conn, job_id)[:4] == ("Lead", "years", VERSION, SIG_3Y)


def test_write_never_overwrites_a_job_that_became_manual(pg_conn):
    job_id = _job(pg_conn, "Senior Data Engineer", level="Middle")
    change = _change_for(pg_conn, job_id)
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE job_postings SET level_source = 'manual' WHERE job_id = %s", (job_id,))
    pg_conn.commit()
    written, stale = db.write_recomputed_levels(pg_conn, [change])
    pg_conn.commit()
    assert written == 0 and stale == [job_id]
    assert _state(pg_conn, job_id)[:3] == ("Middle", "manual", None)


def test_write_skips_job_whose_title_changed(pg_conn):
    job_id = _job(pg_conn, "Senior Data Engineer", level="Middle")
    change = _change_for(pg_conn, job_id)
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE job_postings SET job_title = 'Data Engineer' WHERE job_id = %s", (job_id,))
    pg_conn.commit()
    assert db.write_recomputed_levels(pg_conn, [change]) == (0, [job_id])
    pg_conn.rollback()
    assert _state(pg_conn, job_id)[:2] == ("Middle", None)


def test_write_with_no_changes_is_noop(pg_conn):
    assert db.write_recomputed_levels(pg_conn, []) == (0, [])
