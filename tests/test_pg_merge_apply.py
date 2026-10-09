"""
Gộp job trùng, phần GHI (Phần 3b nửa 2/2) trên POSTGRES THẬT: migration MERGE_JOB,
db.merge_job_group và `merge_duplicates.run(apply=True)`. Cách chạy giống
tests/test_pg_merge_duplicates.py: đặt TEST_DATABASE_URL trỏ tới database dùng riêng cho test
(tên chứa "test"); không đặt thì cả file được bỏ qua.

Chứng minh trên SQL thật:
  - migration thêm MERGE_JOB idempotent, khớp schema.sql; kiểm tra enum/crawl đang chạy;
  - gộp một nhóm: job phụ bị xoá, trường hợp nhất lên job giữ (hồi sinh, lương, hạn, level
    manual + level_signals), dữ liệu con chuyển/bỏ đúng kế hoạch, lịch sử trao đổi dồn sang
    liên kết của job giữ, interaction_status theo luật, snapshot đủ để khôi phục trong audit_logs;
  - updated_at KHÔNG nhảy (job giữ lẫn dòng con) và cờ không rò sang transaction sau;
  - URL của job phụ vẫn tra ra job giữ (job_exists_by_source_url / get_job_probe_by_source_url);
  - lỗi giữa chừng (log_action lỗi, kế hoạch không khớp) => rollback sạch, không còn gì dở;
  - stale: dữ liệu đổi sau khi lập kế hoạch => không gộp; khoá bị giữ => hết hạn chờ và rollback;
  - run(apply=True): xác nhận, --yes, --limit, từ chối khi có crawl/bảo trì (--force bỏ qua),
    từ chối khi DB chưa sẵn sàng, nhóm stale/lỗi không chặn nhóm sau và exit code 2, chạy lại
    không còn gì để gộp.
"""
import dataclasses
import os
import uuid
from urllib.parse import urlparse

import psycopg2
import psycopg2.errors
import psycopg2.extras
import pytest

import db
import duplicate_report as dr
import merge_duplicates as md

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_SCHEMA_PATH = os.path.join(_ROOT, "sql", "schema.sql")
_MIGRATION = "migration_add_merge_job_audit_action.sql"
_SKIP_FLAG_MIGRATION = "migration_add_skip_updated_at_flag.sql"
PAST = "2020-01-01 00:00:00"

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
def clean(pg_conn):
    pg_conn.rollback()
    with pg_conn.cursor() as cur:
        cur.execute("TRUNCATE job_postings, company_contacts, audit_logs, crawl_runs, "
                    "maintenance_runs CASCADE")
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


def _all(conn, sql, params=()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    conn.rollback()
    return rows


def _count(conn, table):
    return _one(conn, f"SELECT count(*) FROM {table}")[0]


def _company(conn):
    cid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)",
                    (cid, f"Công ty Gộp {uuid.uuid4().hex[:8]}"))
    conn.commit()
    return cid


def _user(conn):
    uid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO app_users (ss_user_id, full_name, email) VALUES (%s, %s, %s)",
                    (uid, "Người dùng", f"{uid}@example.com"))
    conn.commit()
    return uid


def _contact(conn, company_id):
    cid = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute("INSERT INTO company_contacts (contact_id, company_id, contact_name) VALUES (%s, %s, %s)",
                    (cid, company_id, "HR"))
    conn.commit()
    return cid


def _job(conn, company_id, title, *, level="Junior", url=None, status="OPEN", deadline=None,
         salary=(None, None), notes=None, level_source=None, signals=None):
    job_id = db.insert_job(
        conn, company_id=company_id, job_title=title, matching_industry="Data",
        level_id=db.get_level_id(conn, level), province_id=None, work_type=None, currency="VNĐ",
        salary_min=salary[0], salary_max=salary[1], salary_type="RANGE" if salary[0] else "NEGOTIABLE",
        source_url=url or f"https://www.example.com/{uuid.uuid4()}", source_name="Fake",
    )
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.skip_updated_at', 'on', true)")
        cur.execute("UPDATE job_postings SET updated_at = %s, job_status = %s, deadline = %s, "
                    "ss_team_notes = %s WHERE job_id = %s", (PAST, status, deadline, notes, job_id))
        # C3c: listing của job theo kịp trạng thái/hạn vừa đặt (như dữ liệu thật sau backfill C1); nếu không,
        # luật suy ra sẽ thấy listing OPEN không hạn dưới một job "CLOSED" và gộp ra kết quả khác ý test.
        cur.execute("UPDATE job_sources_log SET listing_status = %s, deadline = %s, "
                    "closed_reason = CASE WHEN %s = 'CLOSED' THEN 'unknown' END, "
                    "closed_at = CASE WHEN %s = 'CLOSED' THEN now() END WHERE job_id = %s",
                    (status, deadline, status, status, job_id))
        if level_source == "manual":
            cur.execute("UPDATE job_postings SET level_source = 'manual', level_rule_version = NULL, "
                        "level_signals = %s::jsonb WHERE job_id = %s",
                        (psycopg2.extras.Json(signals) if signals else None, job_id))
    conn.commit()
    return job_id


def _save(conn, user, job):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO saved_jobs (ss_user_id, job_id) VALUES (%s, %s)", (user, job))
    conn.commit()


def _apply_for(conn, user, job, cv=None):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO job_applications (ss_user_id, job_id, cv_url) VALUES (%s, %s, %s)",
                    (user, job, cv))
    conn.commit()


def _link(conn, job, contact, status=None, interactions=0):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO job_contact_links (job_id, contact_id, interaction_status) VALUES (%s, %s, %s) "
                    "RETURNING link_id", (job, contact, status))
        link_id = str(cur.fetchone()[0])
        for i in range(interactions):
            cur.execute("INSERT INTO job_contact_interactions (link_id, interaction_type, note) "
                        "VALUES (%s, 'EMAIL', %s)", (link_id, f"lần {i}"))
        cur.execute("SELECT set_config('app.skip_updated_at', 'on', true)")   # không thì trigger ghi đè PAST
        cur.execute("UPDATE job_contact_links SET updated_at = %s WHERE link_id = %s", (PAST, link_id))
    conn.commit()
    return link_id


def _plan_for(conn, ids, keeper):
    """Kế hoạch gộp nhóm gồm `ids` với job giữ do người dùng chọn (như --only + cột 'x')."""
    groups = dr.build_groups(db.list_duplicate_job_rows(conn))
    only = md.OnlySpec(job_ids=set(ids), keepers={keeper})
    selected, skipped, _, _ = md.select_groups(groups, only)
    assert len(selected) == 1 and skipped == [], skipped
    plans, vanished = md.build_plans(conn, selected)
    assert vanished == [] and len(plans) == 1
    return plans[0]


def _merge(conn, plan, **kw):
    """Gọi db.merge_job_group đúng như vòng gộp trong merge_duplicates, rồi commit."""
    res = db.merge_job_group(
        conn, keeper_id=plan.keeper_id, donor_ids=plan.donor_ids, expected=plan.expected,
        changes=plan.changes, child=dataclasses.asdict(plan.child), conflicts=plan.conflicts,
        notes=plan.notes, derived_changes=plan.derived_changes, listing_actions=plan.listing_actions, **kw)
    conn.commit()
    return res


def _state(conn):
    """Ảnh chụp để chứng minh 'không đổi gì'."""
    return (
        _all(conn, "SELECT job_id::text, job_status::text, deadline::text, salary_min, source_url, "
                   "level_id, level_source, ss_team_notes, updated_at::text FROM job_postings ORDER BY job_id"),
        _all(conn, "SELECT log_id::text, job_id::text FROM job_sources_log ORDER BY log_id"),
        _all(conn, "SELECT saved_job_id::text, job_id::text FROM saved_jobs ORDER BY saved_job_id"),
        _all(conn, "SELECT application_id::text, job_id::text FROM job_applications ORDER BY application_id"),
        _all(conn, "SELECT link_id::text, job_id::text, interaction_status, updated_at::text "
                   "FROM job_contact_links ORDER BY link_id"),
        _all(conn, "SELECT interaction_id::text, link_id::text FROM job_contact_interactions ORDER BY 1"),
        _count(conn, "audit_logs"),
    )


def _audit(conn):
    rows = _all(conn, "SELECT entity_id::text, action_type::text, is_manual_log, note_required, actor_id, "
                      "company_id::text, entity_label, changes, note FROM audit_logs ORDER BY created_at, log_id")
    return [dict(zip(("entity_id", "action", "manual", "note_required", "actor", "company_id", "label",
                      "changes", "note"), r)) for r in rows]


def _simple_group(conn, title="Data Engineer", **keeper_kw):
    """Hai job trùng đơn giản (không dữ liệu con): (company, keeper, donor)."""
    c = _company(conn)
    keeper = _job(conn, c, title, deadline="2026-12-01", **keeper_kw)
    donor = _job(conn, c, title, deadline="2026-12-01")
    return c, keeper, donor


# ------------------------------------------------------------------ migration + kiểm tra sẵn sàng
def test_migration_is_idempotent_and_schema_has_merge_job(pg_conn):
    labels = lambda: [r[0] for r in _all(  # noqa: E731
        pg_conn, "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                 "WHERE t.typname = 'audit_action_enum'")]
    assert labels().count("MERGE_JOB") == 1                  # schema.sql đã có
    sql = open(os.path.join(_ROOT, "sql", _MIGRATION), encoding="utf-8").read()
    for _ in range(2):                                       # chạy lại không lỗi, không nhân đôi
        with pg_conn.cursor() as cur:
            cur.execute(sql)
        pg_conn.commit()
    assert labels().count("MERGE_JOB") == 1
    assert db.merge_job_enum_supported(pg_conn) is True


def test_enum_check_is_false_without_merge_job_label(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("ALTER TYPE audit_action_enum RENAME VALUE 'MERGE_JOB' TO 'MERGE_JOB_TMP'")
    assert db.merge_job_enum_supported(pg_conn) is False     # hàm rollback => đổi tên tự hoàn tác
    assert db.merge_job_enum_supported(pg_conn) is True


def test_list_active_runs_reports_only_queued_and_running(pg_conn):
    maint_type = _one(pg_conn, "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                               "WHERE t.typname = 'maintenance_job_type_enum' ORDER BY e.enumsortorder LIMIT 1")[0]
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO crawl_runs (source, category, pages, status) VALUES "
                    "('topcv', 'data-analyst', 1, 'running'), ('topcv', 'old', 1, 'done'), "
                    "('careerviet', 'x', 1, 'error')")
        cur.execute("INSERT INTO maintenance_runs (job_type, status) VALUES (%s, 'queued')", (maint_type,))
    pg_conn.commit()
    runs = db.list_active_runs(pg_conn)
    assert sorted((r["kind"], r["status"]) for r in runs) == [("bảo trì", "queued"), ("crawl", "running")]
    assert any(r["label"] == "topcv / data-analyst" for r in runs)
    assert all(isinstance(r["age_minutes"], int) for r in runs)


# ------------------------------------------------------------------ gộp một nhóm
def _rich_group(conn):
    """Nhóm nhiều dữ liệu: job giữ K (CLOSED, có lương, link đã gọi) + hai job phụ."""
    c = _company(conn)
    u1, u2, u3 = _user(conn), _user(conn), _user(conn)
    k1, k2 = _contact(conn, c), _contact(conn, c)
    keeper = _job(conn, c, "Data Engineer", status="CLOSED", deadline="2026-09-01",
                  salary=(20_000_000, 30_000_000), url="https://www.topcv.vn/old")
    d1 = _job(conn, c, "Data Engineer", status="OPEN", deadline="2026-11-01", url="https://www.topcv.vn/new")
    d2 = _job(conn, c, "Data Engineer", status="OPEN", deadline="2026-10-01", url="https://www.topcv.vn/new-2")
    _save(conn, u1, keeper)
    _save(conn, u1, d1)                                      # trùng người dùng -> bỏ
    _save(conn, u2, d1)                                      # chuyển
    _apply_for(conn, u1, keeper, cv="cv/keeper.pdf")
    _apply_for(conn, u1, d1, cv="cv/d1-dup.pdf")             # trùng người dùng, có CV -> bỏ, ghi đường dẫn vào audit
    _apply_for(conn, u3, d1)                                 # chuyển
    keeper_link = _link(conn, keeper, k1, status="Đã gọi", interactions=2)
    d1_link = _link(conn, d1, k1, status="Chờ phản hồi", interactions=1)   # dồn, trạng thái lệch
    d2_link = _link(conn, d2, k2, status="Mới")                            # chuyển
    with conn.cursor() as cur:
        cur.execute("UPDATE job_sources_log SET raw_jd_content = 'abcde' WHERE job_id = %s", (d2,))
    conn.commit()
    return dict(c=c, keeper=keeper, d1=d1, d2=d2, u=(u1, u2, u3), k=(k1, k2),
                links=(keeper_link, d1_link, d2_link))


def test_merge_group_moves_children_merges_fields_and_writes_snapshots(pg_conn):
    g = _rich_group(pg_conn)
    keeper, d1, d2 = g["keeper"], g["d1"], g["d2"]
    plan = _plan_for(pg_conn, [keeper, d1, d2], keeper)
    res = _merge(pg_conn, plan)

    assert res["status"] == "merged" and res["donors_deleted"] == 2
    assert [r[0] for r in _all(pg_conn, "SELECT job_id::text FROM job_postings")] == [keeper]

    # --- job giữ: hồi sinh; hạn = muộn nhất trong listing OPEN (11-01 của d1); source_url = listing OPEN MỚI NHẤT
    # (d2, tạo sau d1, dù hạn sớm hơn: luật suy ra C3c, khác luật cũ lấy URL của job có hạn muộn hơn); lương giữ
    # nguyên; updated_at KHÔNG nhảy
    row = _one(pg_conn, "SELECT job_status::text, deadline::text, source_url, salary_min, salary_max, updated_at::text "
                        "FROM job_postings WHERE job_id = %s", (keeper,))
    assert row[0] == "OPEN" and row[1] == "2026-11-01" and row[2] == "https://www.topcv.vn/new-2"
    assert (row[3], row[4]) == (20_000_000, 30_000_000)
    assert row[5][:19] == PAST   # [:19] bỏ hậu tố múi giờ nếu cột là TIMESTAMPTZ (D3)

    # --- dữ liệu con: không còn gì trỏ vào job phụ
    for table in ("job_sources_log", "saved_jobs", "job_applications", "job_contact_links"):
        assert _one(pg_conn, f"SELECT count(*) FROM {table} WHERE job_id = ANY(%s::uuid[])", ([d1, d2],))[0] == 0
    assert sorted(r[0] for r in _all(pg_conn, "SELECT ss_user_id::text FROM saved_jobs WHERE job_id = %s",
                                     (keeper,))) == sorted([g["u"][0], g["u"][1]])
    assert sorted(r[0] for r in _all(pg_conn, "SELECT ss_user_id::text FROM job_applications WHERE job_id = %s",
                                     (keeper,))) == sorted([g["u"][0], g["u"][2]])
    # đơn của u1 còn là bản của job giữ (CV của job giữ), bản trùng đã bị bỏ
    assert _one(pg_conn, "SELECT cv_url FROM job_applications WHERE job_id = %s AND ss_user_id = %s",
                (keeper, g["u"][0]))[0] == "cv/keeper.pdf"
    urls = sorted(r[0] for r in _all(pg_conn, "SELECT source_url FROM job_sources_log WHERE job_id = %s", (keeper,)))
    assert urls == ["https://www.topcv.vn/new", "https://www.topcv.vn/new-2",
                    "https://www.topcv.vn/old"]                              # mọi URL của job phụ dồn về job giữ

    # --- liên hệ: liên kết trùng dồn, liên kết khác chuyển; lịch sử trao đổi về một mối; trạng thái giữ của job giữ
    links = {r[1]: r for r in _all(pg_conn, "SELECT link_id::text, contact_id::text, interaction_status, "
                                            "updated_at::text FROM job_contact_links WHERE job_id = %s", (keeper,))}
    assert set(links) == set(g["k"])
    assert links[g["k"][0]][0] == g["links"][0] and links[g["k"][0]][2] == "Đã gọi"
    assert links[g["k"][1]][0] == g["links"][2]                                  # liên kết chuyển giữ nguyên link_id
    assert all(r[3][:19] == PAST for r in links.values())                             # updated_at dòng con cũng không nhảy
    assert _one(pg_conn, "SELECT count(*) FROM job_contact_interactions WHERE link_id = %s", (g["links"][0],))[0] == 3

    # --- audit: một dòng mỗi job phụ + một dòng job giữ; tự động, không người thực hiện
    logs = _audit(pg_conn)
    assert len(logs) == 3 and {x["action"] for x in logs} == {"MERGE_JOB"}
    assert all(x["manual"] is False and x["note_required"] is False and x["actor"] is None for x in logs)
    assert all(x["company_id"] == g["c"] and x["label"] == "Data Engineer" and x["note"] for x in logs)
    by_entity = {x["entity_id"]: x for x in logs}
    assert set(by_entity) == {keeper, d1, d2}

    s1 = by_entity[d1]["changes"]
    assert s1["merged_into"] == keeper
    snap = s1["snapshot"]
    assert snap["job"]["job_id"] == d1 and snap["job"]["job_title"] == "Data Engineer"   # nguyên dòng job phụ
    assert snap["job"]["deadline"] == "2026-11-01"
    assert [r["ss_user_id"] for r in snap["saved_jobs"]["dropped"]] == [g["u"][0]]
    assert len(snap["saved_jobs"]["moved"]) == 1
    assert [r["cv_url"] for r in snap["job_applications"]["dropped"]] == ["cv/d1-dup.pdf"]
    assert len(snap["job_applications"]["moved"]) == 1
    merged_link = snap["job_contact_links"]["merged"][0]
    assert merged_link["link"]["link_id"] == g["links"][1] and merged_link["merged_into_link_id"] == g["links"][0]
    assert len(merged_link["interactions_moved"]) == 1 and merged_link["link"]["interaction_status"] == "Chờ phản hồi"

    s2 = by_entity[d2]["changes"]["snapshot"]
    assert s2["job_contact_links"]["moved"] == [g["links"][2]]
    # Từ D2 một URL chỉ thuộc một job nên không còn log nào bị bỏ vì trùng URL với job giữ (nhánh bỏ log
    # vẫn còn trong code cho các lần gộp cũ và DB chưa có UNIQUE (source_url)). Cả hai log của job phụ
    # được chuyển, snapshot chỉ ghi log_id của chúng.
    assert s2["job_sources_log"]["dropped"] == [] and snap["job_sources_log"]["dropped"] == []
    assert len(s2["job_sources_log"]["moved"]) == 1 and len(snap["job_sources_log"]["moved"]) == 1
    assert res["cv_dropped"] == 1

    kc = by_entity[keeper]["changes"]
    assert kc["job_status"] == {"old": "CLOSED", "new": "OPEN"} and kc["deadline"]["new"] == "2026-11-01"
    assert sorted(kc["merged_from"]) == sorted([d1, d2]) and kc["notes"]
    assert kc["link_status_conflicts"] == [{"contact_id": g["k"][0], "kept": "Đã gọi", "other": "Chờ phản hồi",
                                             "kept_link_id": g["links"][0], "other_link_id": g["links"][1]}]
    # audit_logs.entity_id không có khoá ngoại tới job_postings nên log của job đã xoá vẫn còn
    assert _one(pg_conn, "SELECT count(*) FROM job_postings WHERE job_id = %s", (d1,))[0] == 0


def test_urls_of_merged_jobs_still_resolve_to_the_keeper(pg_conn):
    c = _company(pg_conn)
    keeper = _job(pg_conn, c, "Backend Dev", url="https://www.topcv.vn/keeper", deadline="2026-12-01")
    donor = _job(pg_conn, c, "Backend Dev", url="https://www.topcv.vn/donor", deadline="2026-12-01")
    _merge(pg_conn, _plan_for(pg_conn, [keeper, donor], keeper))
    # Lần crawl sau gặp lại URL của job phụ: phải nhận ra (không insert lại bản trùng) và trỏ về job giữ.
    assert db.job_exists_by_source_url(pg_conn, "https://www.topcv.vn/donor") is True
    assert str(db.get_job_probe_by_source_url(pg_conn, "https://www.topcv.vn/donor")[0]) == keeper
    assert db.job_exists_by_source_url(pg_conn, "https://www.topcv.vn/keeper") is True


def test_updated_at_flag_does_not_leak_into_the_next_transaction(pg_conn):
    _, keeper, donor = _simple_group(pg_conn, notes="n")
    _merge(pg_conn, _plan_for(pg_conn, [keeper, donor], keeper))
    assert _one(pg_conn, "SELECT current_setting('app.skip_updated_at', true)")[0] in (None, "")
    assert _one(pg_conn, "SELECT current_setting('lock_timeout')")[0] == "0"            # giới hạn chờ khoá cũng hết hiệu lực
    # Sau khi gộp, sửa job bình thường vẫn làm updated_at nhảy như cũ.
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE job_postings SET ss_team_notes = 'sửa tay' WHERE job_id = %s", (keeper,))
    pg_conn.commit()
    assert _one(pg_conn, "SELECT updated_at > %s FROM job_postings WHERE job_id = %s", (PAST, keeper))[0] is True


def test_manual_level_and_signals_are_copied_with_valid_stamp(pg_conn):
    c = _company(pg_conn)
    keeper = _job(pg_conn, c, "Data Analyst", level="Junior", deadline="2026-12-01")
    donor = _job(pg_conn, c, "Data Analyst", level="Junior", deadline="2026-12-01")
    # Cho job phụ có level do người đặt: đổi level + dấu 'manual' + tín hiệu thô, rồi lập lại kế hoạch.
    with pg_conn.cursor() as cur:
        cur.execute("SELECT set_config('app.skip_updated_at', 'on', true)")
        cur.execute("UPDATE job_postings SET level_id = %s, level_source = 'manual', level_rule_version = NULL, "
                    "level_signals = %s WHERE job_id = %s",
                    (db.get_level_id(pg_conn, "Senior"), psycopg2.extras.Json({"experience_text": "5 năm",
                                                                                "level_hint": ""}), donor))
    pg_conn.commit()
    plan = _plan_for(pg_conn, [keeper, donor], keeper)
    assert {"level_id", "level_source", "level_signals"} <= set(plan.changes)
    _merge(pg_conn, plan)
    row = _one(pg_conn, "SELECT level_id, level_source, level_rule_version, level_signals, content_hash, "
                        "generate_job_hash(company_id, job_title, level_id, province_id) "
                        "FROM job_postings WHERE job_id = %s", (keeper,))
    assert row[0] == db.get_level_id(pg_conn, "Senior") and row[1] == "manual" and row[2] is None
    assert row[3] == {"experience_text": "5 năm", "level_hint": ""}
    assert row[4] == row[5]                                  # trigger tính lại content_hash theo level mới


def test_link_status_rule_takes_donor_status_only_when_keeper_has_none(pg_conn):
    c = _company(pg_conn)
    keeper = _job(pg_conn, c, "QA Engineer", deadline="2026-12-01")
    donor = _job(pg_conn, c, "QA Engineer", deadline="2026-12-01")
    k_empty, k_same = _contact(pg_conn, c), _contact(pg_conn, c)
    keeper_empty = _link(pg_conn, keeper, k_empty, status="  ")
    _link(pg_conn, donor, k_empty, status="Đã gửi mail", interactions=2)
    _link(pg_conn, keeper, k_same, status="Đã gọi")
    _link(pg_conn, donor, k_same, status="Đã gọi")
    _merge(pg_conn, _plan_for(pg_conn, [keeper, donor], keeper))
    assert _one(pg_conn, "SELECT interaction_status FROM job_contact_links WHERE link_id = %s",
                (keeper_empty,))[0] == "Đã gửi mail"          # bên giữ trống -> lấy của bên bị dồn
    assert _count(pg_conn, "job_contact_interactions") == 2
    logs = {x["entity_id"]: x for x in _audit(pg_conn)}
    assert "link_status_conflicts" not in (logs.get(keeper, {}).get("changes") or {})   # không lệch thì không ghi


def test_second_donor_link_merges_into_link_moved_from_first_donor(pg_conn):
    c = _company(pg_conn)
    keeper = _job(pg_conn, c, "DevOps", deadline="2026-12-01")
    d1 = _job(pg_conn, c, "DevOps", deadline="2026-12-01")
    d2 = _job(pg_conn, c, "DevOps", deadline="2026-12-01")
    k = _contact(pg_conn, c)
    l1 = _link(pg_conn, d1, k, status="A", interactions=1)
    l2 = _link(pg_conn, d2, k, status="B", interactions=2)
    plan = _plan_for(pg_conn, [keeper, d1, d2], keeper)
    assert len(plan.child.links_move) == 1 and len(plan.child.links_merge) == 1
    _merge(pg_conn, plan)
    links = _all(pg_conn, "SELECT link_id::text, job_id::text, interaction_status FROM job_contact_links")
    assert len(links) == 1 and links[0][1] == keeper and links[0][0] in (l1, l2)
    assert _count(pg_conn, "job_contact_interactions") == 3


# ------------------------------------------------------------------ lỗi giữa chừng => rollback sạch
def test_failure_while_writing_audit_rolls_back_the_whole_group(pg_conn, monkeypatch):
    g = _rich_group(pg_conn)
    plan = _plan_for(pg_conn, [g["keeper"], g["d1"], g["d2"]], g["keeper"])
    before = _state(pg_conn)
    import db.job_merge as jm
    real = jm.log_action
    calls = []

    def flaky(conn, **kw):
        calls.append(kw["entity_id"])
        if len(calls) == 2:                                  # hỏng sau khi job phụ đã xoá và dòng con đã chuyển
            raise RuntimeError("ghi audit lỗi")
        return real(conn, **kw)

    monkeypatch.setattr(jm, "log_action", flaky)
    with pytest.raises(RuntimeError, match="ghi audit lỗi"):
        db.merge_job_group(pg_conn, keeper_id=plan.keeper_id, donor_ids=plan.donor_ids, expected=plan.expected,
                           changes=plan.changes, child=dataclasses.asdict(plan.child),
                           derived_changes=plan.derived_changes, listing_actions=plan.listing_actions)
    pg_conn.rollback()
    assert len(calls) == 2
    assert _state(pg_conn) == before                         # job phụ còn, dòng con còn nguyên chỗ, không audit, updated_at y nguyên


def test_plan_that_does_not_cover_children_is_refused_and_leaves_nothing(pg_conn):
    g = _rich_group(pg_conn)
    plan = _plan_for(pg_conn, [g["keeper"], g["d1"], g["d2"]], g["keeper"])
    child = dataclasses.asdict(plan.child)
    child["saved_move"] = []                                 # quên một dòng con => xoá job sẽ mất dữ liệu
    before = _state(pg_conn)
    with pytest.raises(db.MergeIntegrityError, match="saved_jobs"):
        db.merge_job_group(pg_conn, keeper_id=plan.keeper_id, donor_ids=plan.donor_ids, expected=plan.expected,
                           changes=plan.changes, child=child,
                           derived_changes=plan.derived_changes, listing_actions=plan.listing_actions)
    pg_conn.rollback()
    assert _state(pg_conn) == before


def test_unknown_column_in_changes_is_refused(pg_conn):
    _, keeper, donor = _simple_group(pg_conn)
    plan = _plan_for(pg_conn, [keeper, donor], keeper)
    with pytest.raises(db.MergeIntegrityError, match="không được phép"):
        db.merge_job_group(pg_conn, keeper_id=keeper, donor_ids=[donor], expected=plan.expected,
                           changes={"company_id": {"old": "x", "new": "y"}}, child=dataclasses.asdict(plan.child),
                           derived_changes={})
    pg_conn.rollback()
    assert _count(pg_conn, "job_postings") == 2


# ------------------------------------------------------------------ stale + khoá
@pytest.mark.parametrize("mutate", ["donor_saved", "keeper_closed", "donor_deleted", "donor_title"])
def test_group_changed_after_planning_is_stale_and_untouched(pg_conn, mutate):
    c, keeper, donor = _simple_group(pg_conn)
    u = _user(pg_conn)
    plan = _plan_for(pg_conn, [keeper, donor], keeper)
    with pg_conn.cursor() as cur:
        if mutate == "donor_saved":
            cur.execute("INSERT INTO saved_jobs (ss_user_id, job_id) VALUES (%s, %s)", (u, donor))
        elif mutate == "keeper_closed":
            cur.execute("UPDATE job_postings SET job_status = 'CLOSED' WHERE job_id = %s", (keeper,))
        elif mutate == "donor_deleted":
            cur.execute("DELETE FROM job_sources_log WHERE job_id = %s", (donor,))
            cur.execute("DELETE FROM job_postings WHERE job_id = %s", (donor,))
        else:
            cur.execute("UPDATE job_postings SET job_title = 'Tên khác' WHERE job_id = %s", (donor,))
    pg_conn.commit()
    before = _state(pg_conn)
    with pytest.raises(db.MergeStaleError):
        db.merge_job_group(pg_conn, keeper_id=keeper, donor_ids=[donor], expected=plan.expected,
                           changes=plan.changes, child=dataclasses.asdict(plan.child),
                           derived_changes=plan.derived_changes, listing_actions=plan.listing_actions)
    pg_conn.rollback()
    assert _state(pg_conn) == before


def test_row_lock_held_by_another_transaction_times_out_and_rolls_back(pg_conn):
    _, keeper, donor = _simple_group(pg_conn)
    plan = _plan_for(pg_conn, [keeper, donor], keeper)
    other = psycopg2.connect(TEST_DATABASE_URL)
    try:
        with other.cursor() as cur:
            cur.execute("SELECT job_id FROM job_postings WHERE job_id = %s FOR UPDATE", (donor,))   # giữ khoá
        before = _state(pg_conn)
        with pytest.raises(psycopg2.errors.LockNotAvailable):
            db.merge_job_group(pg_conn, keeper_id=keeper, donor_ids=[donor], expected=plan.expected,
                               changes=plan.changes, child=dataclasses.asdict(plan.child),
                               derived_changes=plan.derived_changes, listing_actions=plan.listing_actions,
                               lock_timeout_ms=200)
        pg_conn.rollback()
        assert _state(pg_conn) == before
    finally:
        other.rollback()
        other.close()
    # Hết giữ khoá thì gộp được bình thường.
    _merge(pg_conn, plan)
    assert _count(pg_conn, "job_postings") == 1


# ------------------------------------------------------------------ run(apply=True)
def _two_groups(conn):
    """Hai nhóm độ chắc 'cao' (mỗi nhóm một tin OPEN + một tin đã CLOSED)."""
    c1, c2 = _company(conn), _company(conn)
    a = _job(conn, c1, "Data Engineer", deadline="2026-12-01")
    a2 = _job(conn, c1, "Data Engineer", status="CLOSED", deadline="2026-12-01")
    b = _job(conn, c2, "Kế toán tổng hợp", deadline="2026-12-01")
    b2 = _job(conn, c2, "Kế toán tổng hợp", status="CLOSED", deadline="2026-12-01")
    return (a, a2), (b, b2)


def _run(conn, **kw):
    kw.setdefault("yes", True)
    return md.run(conn, apply=True, **kw)


def test_run_apply_merges_all_high_confidence_groups_and_is_repeatable(pg_conn, capsys):
    (a, a2), (b, b2) = _two_groups(pg_conn)
    jobs_before = _count(pg_conn, "job_postings")
    dup_before = db.count_duplicate_job_groups(pg_conn)
    pg_conn.rollback()
    assert _run(pg_conn) == md.EXIT_OK
    out = capsys.readouterr().out
    assert "KẾT QUẢ GỘP THẬT" in out and "đã gộp:                  2" in out
    assert f"trước = {jobs_before}, sau = {jobs_before - 2}" in out
    assert "--yes: bỏ qua bước hỏi xác nhận" in out
    assert _count(pg_conn, "job_postings") == 2
    assert db.count_duplicate_job_groups(pg_conn) == dup_before - 2
    pg_conn.rollback()
    assert sum(1 for x in _audit(pg_conn) if x["action"] == "MERGE_JOB") == 2   # không đổi trường => chỉ log job phụ

    # Chạy lại: không còn gì để gộp, không lỗi, không ghi thêm.
    assert _run(pg_conn) == md.EXIT_OK
    assert "Không có nhóm nào để gộp" in capsys.readouterr().out
    assert _count(pg_conn, "audit_logs") == 2


def test_run_apply_asks_for_confirmation(pg_conn, capsys):
    _two_groups(pg_conn)
    before = _state(pg_conn)
    assert md.run(pg_conn, apply=True, confirm=lambda prompt: "no") == md.EXIT_REFUSED
    assert "Đã huỷ" in capsys.readouterr().out and _state(pg_conn) == before

    def eof(prompt):
        raise EOFError

    assert md.run(pg_conn, apply=True, confirm=eof) == md.EXIT_REFUSED       # chạy tự động mà quên --yes
    assert _state(pg_conn) == before

    prompts = []
    assert md.run(pg_conn, apply=True, confirm=lambda p: prompts.append(p) or "yes") == md.EXIT_OK
    assert "xác nhận" in prompts[0] and _count(pg_conn, "job_postings") == 2


def test_run_apply_limit_merges_only_first_groups(pg_conn, capsys):
    _two_groups(pg_conn)
    assert _run(pg_conn, limit=1) == md.EXIT_OK
    out = capsys.readouterr().out
    assert "--limit 1: chỉ gộp 1 nhóm đầu" in out
    assert _count(pg_conn, "job_postings") == 3
    assert _run(pg_conn, limit=1) == md.EXIT_OK                               # lần sau gộp nốt nhóm còn lại
    assert _count(pg_conn, "job_postings") == 2


def test_run_apply_refuses_while_crawl_or_maintenance_is_active_and_force_overrides(pg_conn, capsys):
    _two_groups(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO crawl_runs (source, category, pages, status) VALUES ('topcv', 'data', 1, 'running')")
    pg_conn.commit()
    before = _state(pg_conn)
    assert _run(pg_conn) == md.EXIT_REFUSED
    out = capsys.readouterr().out
    assert "Đang có crawl/bảo trì chạy" in out and "topcv / data" in out and "--force" in out
    assert _state(pg_conn) == before

    assert _run(pg_conn, force=True) == md.EXIT_OK
    out = capsys.readouterr().out
    assert "--force: bỏ qua kiểm tra" in out and "KHÔNG dừng" in out
    assert _count(pg_conn, "job_postings") == 2
    # --force không đụng tới dòng crawl: không dừng, không sửa gì.
    assert _one(pg_conn, "SELECT status::text FROM crawl_runs")[0] == "running"


def test_run_apply_refuses_when_maintenance_run_is_queued(pg_conn, capsys):
    _two_groups(pg_conn)
    maint_type = _one(pg_conn, "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                               "WHERE t.typname = 'maintenance_job_type_enum' ORDER BY e.enumsortorder LIMIT 1")[0]
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO maintenance_runs (job_type, status) VALUES (%s, 'queued')", (maint_type,))
    pg_conn.commit()
    assert _run(pg_conn) == md.EXIT_REFUSED
    assert "bảo trì" in capsys.readouterr().out and _count(pg_conn, "job_postings") == 4


def test_dry_run_still_works_while_crawl_is_active(pg_conn, capsys):
    _two_groups(pg_conn)
    with pg_conn.cursor() as cur:
        cur.execute("INSERT INTO crawl_runs (source, category, pages, status) VALUES ('topcv', 'data', 1, 'running')")
    pg_conn.commit()
    before = _state(pg_conn)
    assert md.run(pg_conn) == md.EXIT_OK                                      # chạy thử chỉ đọc nên không bị chặn
    assert "CHẠY THỬ" in capsys.readouterr().out and _state(pg_conn) == before


def test_run_apply_refuses_when_database_is_not_ready(pg_conn, capsys, monkeypatch):
    _two_groups(pg_conn)
    before = _state(pg_conn)

    # 1) migration chưa áp dụng
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM schema_migrations WHERE filename = %s", (_MIGRATION,))
    pg_conn.commit()
    try:
        assert _run(pg_conn) == md.EXIT_REFUSED
        assert _MIGRATION in capsys.readouterr().out
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s) ON CONFLICT DO NOTHING", (_MIGRATION,))
        pg_conn.commit()

    # 2) enum chưa có MERGE_JOB (dù migration đã được ghi nhận)
    monkeypatch.setattr(md.db, "merge_job_enum_supported", lambda conn: False)
    assert _run(pg_conn) == md.EXIT_REFUSED
    assert "MERGE_JOB" in capsys.readouterr().out
    monkeypatch.undo()

    # 3) trigger chưa biết cờ app.skip_updated_at => updated_at sẽ nhảy, không được gộp
    with pg_conn.cursor() as cur:
        cur.execute(_OLD_TRIGGER_FN)
    pg_conn.commit()
    try:
        assert _run(pg_conn) == md.EXIT_REFUSED
        assert "app.skip_updated_at" in capsys.readouterr().out
    finally:
        with pg_conn.cursor() as cur:
            cur.execute(open(os.path.join(_ROOT, "sql", _SKIP_FLAG_MIGRATION), encoding="utf-8").read())
        pg_conn.commit()
    assert _state(pg_conn) == before


def test_run_apply_skips_stale_group_and_still_merges_the_rest(pg_conn, capsys, monkeypatch):
    (a, a2), (b, b2) = _two_groups(pg_conn)
    real = db.merge_job_group
    calls = []

    def sabotage_first(conn, **kw):
        calls.append(kw["keeper_id"])
        if len(calls) == 1:                                  # có người sửa job phụ ngay sau khi lập kế hoạch
            with conn.cursor() as cur:
                cur.execute("UPDATE job_postings SET ss_team_notes = 'sửa tay' WHERE job_id = ANY(%s::uuid[])",
                            (kw["donor_ids"],))
            conn.commit()
        return real(conn, **kw)

    monkeypatch.setattr(md.db, "merge_job_group", sabotage_first)
    assert _run(pg_conn) == md.EXIT_PARTIAL
    out = capsys.readouterr().out
    assert "BỎ QUA (stale)" in out and "đã gộp:                  1" in out
    assert "bỏ qua vì dữ liệu đã đổi (stale): 1" in out and "Chạy lại cùng lệnh" in out
    assert _count(pg_conn, "job_postings") == 3              # đúng một nhóm đã gộp
    monkeypatch.undo()
    assert _run(pg_conn) == md.EXIT_OK                       # chạy lại: nhóm stale được lập kế hoạch mới và gộp
    assert _count(pg_conn, "job_postings") == 2


def test_run_apply_group_error_is_rolled_back_and_does_not_block_others(pg_conn, capsys, monkeypatch):
    _two_groups(pg_conn)
    real = db.merge_job_group
    calls = []

    def fail_first(conn, **kw):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("lỗi giả lập")
        return real(conn, **kw)

    monkeypatch.setattr(md.db, "merge_job_group", fail_first)
    assert _run(pg_conn) == md.EXIT_PARTIAL
    out = capsys.readouterr().out
    assert "LỖI — RuntimeError: lỗi giả lập" in out and "lỗi (đã rollback nhóm đó):        1" in out
    assert _count(pg_conn, "job_postings") == 3 and _count(pg_conn, "audit_logs") == 1


def test_run_apply_with_only_file_merges_a_hand_approved_review_group(pg_conn):
    c = _company(pg_conn)
    # Hai tin cùng OPEN khác level: "cần xem", không tự gộp; chỉ gộp khi duyệt tay qua --only.
    a = _job(pg_conn, c, "Kế toán", level="Junior", deadline="2026-12-01")
    b = _job(pg_conn, c, "Kế toán", level="Senior", deadline="2026-12-01")
    assert _run(pg_conn) == md.EXIT_OK and _count(pg_conn, "job_postings") == 2
    only = md.OnlySpec(job_ids={a, b}, keepers={b})
    assert _run(pg_conn, only=only) == md.EXIT_OK
    assert [r[0] for r in _all(pg_conn, "SELECT job_id::text FROM job_postings")] == [b]


# ------------------------------------------------------------------ C3c: job giữ theo luật suy ra
# Từ C3c, trạng thái / lý do đóng / hạn / source_url của job giữ là giá trị SUY RA từ listing sau gộp
# (db.job_sync), ghi trong cùng transaction với việc chuyển listing và đối chiếu với dự đoán của kế hoạch.
# Ngoại lệ (bạn chốt 09/10, phương án a): job giữ NHẬP TAY (mọi listing là manual://) không bị luật suy ra đè.
def _manual_url():
    return f"manual://{uuid.uuid4()}"


def _job_row(conn, job_id):
    return _one(conn, "SELECT job_status::text, closed_reason, deadline::text, source_url, updated_at::text "
                      "FROM job_postings WHERE job_id = %s", (job_id,))


def _listings(conn, job_id):
    """[(source_url, listing_status, closed_reason, deadline)] của job, sắp theo URL."""
    return _all(conn, "SELECT source_url, listing_status, closed_reason, deadline::text FROM job_sources_log "
                      "WHERE job_id = %s ORDER BY source_url", (job_id,))


def _mismatches(conn, job_id):
    import check_listing_derivation as cld
    report = cld.build_report(db.list_jobs_with_listings(conn))
    return [(m.field, m.stored, m.derived) for m in report.mismatches if m.job_id == job_id]


def _keeper_audit(conn, keeper):
    return next(x for x in _audit(conn) if x["entity_id"] == keeper and "merged_from" in x["changes"])["changes"]


def test_merged_keeper_equals_derivation_of_its_listings_afterwards(pg_conn):
    g = _rich_group(pg_conn)
    plan = _plan_for(pg_conn, [g["keeper"], g["d1"], g["d2"]], g["keeper"])
    assert not plan.protected_manual and plan.derived_changes["job_status"] == {"old": "CLOSED", "new": "OPEN"}
    res = _merge(pg_conn, plan)
    assert res["derived_changes"] == sorted(plan.derived_changes) and res["listing_actions"] == {}
    assert _mismatches(pg_conn, g["keeper"]) == []                  # lệnh so lệch: job giữ khớp luật suy ra
    kc = _keeper_audit(pg_conn, g["keeper"])                        # audit ghi cả giá trị suy ra (old/new)
    assert kc["source_url"] == {"old": "https://www.topcv.vn/old", "new": "https://www.topcv.vn/new-2"}


def test_plan_mismatching_real_derivation_is_refused_and_leaves_nothing(pg_conn):
    g = _rich_group(pg_conn)
    plan = _plan_for(pg_conn, [g["keeper"], g["d1"], g["d2"]], g["keeper"])
    before = _state(pg_conn)
    with pytest.raises(db.MergeIntegrityError, match="đồng bộ job giữ"):
        db.merge_job_group(pg_conn, keeper_id=plan.keeper_id, donor_ids=plan.donor_ids, expected=plan.expected,
                           changes=plan.changes, child=dataclasses.asdict(plan.child),
                           derived_changes={})                      # kế hoạch "quên" việc hồi sinh
    pg_conn.rollback()
    assert _state(pg_conn) == before


def test_merge_job_group_requires_an_explicit_derived_changes_decision(pg_conn):
    _, keeper, donor = _simple_group(pg_conn)
    plan = _plan_for(pg_conn, [keeper, donor], keeper)
    with pytest.raises(TypeError):
        db.merge_job_group(pg_conn, keeper_id=keeper, donor_ids=[donor], expected=plan.expected,
                           changes=plan.changes, child=dataclasses.asdict(plan.child))
    pg_conn.rollback()
    with pytest.raises(db.MergeIntegrityError, match="không được phép"):
        db.merge_job_group(pg_conn, keeper_id=keeper, donor_ids=[donor], expected=plan.expected,
                           changes={}, child=dataclasses.asdict(plan.child),
                           derived_changes={"salary_min": {"old": 1, "new": 2}})
    pg_conn.rollback()
    with pytest.raises(db.MergeIntegrityError, match="hành động listing"):
        db.merge_job_group(pg_conn, keeper_id=keeper, donor_ids=[donor], expected=plan.expected,
                           changes={}, child=dataclasses.asdict(plan.child), derived_changes={},
                           listing_actions={"xoá_hết": 1})
    pg_conn.rollback()
    assert _count(pg_conn, "job_postings") == 2


def test_manual_open_keeper_keeps_status_deadline_and_url_and_stamps_deadline_into_open_listings(pg_conn):
    c = _company(pg_conn)
    manual_url, donor_url = _manual_url(), "https://www.topcv.vn/crawl"
    keeper = _job(pg_conn, c, "Kế toán trưởng", url=manual_url, deadline="2026-10-15")
    donor = _job(pg_conn, c, "Kế toán trưởng", url=donor_url, deadline="2026-12-01")
    plan = _plan_for(pg_conn, [keeper, donor], keeper)
    assert plan.protected_manual and plan.derived_changes is None
    assert plan.changes == {} and plan.listing_actions == {"stamp_deadline": plan.expected[keeper]["deadline"]}
    res = _merge(pg_conn, plan)

    assert res["listing_actions"] == {"deadline_stamped_listings": 2} and res["derived_changes"] == []
    row = _job_row(pg_conn, keeper)
    assert row[:4] == ("OPEN", None, "2026-10-15", manual_url)         # hạn crawl 12-01 KHÔNG đè hạn nhập tay
    assert row[4][:19] == PAST                                          # updated_at không nhảy
    assert _listings(pg_conn, keeper) == sorted([(manual_url, "OPEN", None, "2026-10-15"),
                                                 (donor_url, "OPEN", None, "2026-10-15")])
    assert _keeper_audit(pg_conn, keeper)["listing_actions"] == {"deadline_stamped_listings": 2}

    # Hạn và trạng thái được giữ LÂU DÀI (listing đã khớp nên các lần đồng bộ sau suy ra đúng chúng); riêng
    # source_url không có chỗ đánh dấu nên lần đồng bộ kế tiếp suy ra lại theo listing OPEN mới nhất.
    assert set(_mismatches(pg_conn, keeper)) == {("source_url", manual_url, donor_url)}
    changed = db.sync_job_from_listings(pg_conn, keeper)
    pg_conn.commit()
    assert changed == {"source_url": (manual_url, donor_url)}


def test_manual_keeper_that_staff_closed_stays_closed_and_the_donor_listing_closes_with_it(pg_conn):
    c = _company(pg_conn)
    manual_url, donor_url = _manual_url(), "https://www.topcv.vn/crawl"
    keeper = _job(pg_conn, c, "Kế toán trưởng", url=manual_url, status="CLOSED", deadline="2026-10-15")
    donor = _job(pg_conn, c, "Kế toán trưởng", url=donor_url, deadline="2026-12-01")
    plan = _plan_for(pg_conn, [keeper, donor], keeper)
    assert plan.protected_manual and not plan.revives and plan.listing_actions["close_listings"] == 1
    res = _merge(pg_conn, plan)

    assert res["listing_actions"] == {"closed_listings": 1, "deadline_stamped_listings": 2}
    row = _job_row(pg_conn, keeper)
    assert row[0] == "CLOSED" and row[1] == "unknown" and row[2] == "2026-10-15"     # không hồi sinh
    states = {u: (st, reason) for u, st, reason, _ in _listings(pg_conn, keeper)}
    assert states == {manual_url: ("CLOSED", "unknown"), donor_url: ("CLOSED", "unknown")}
    # bất biến C1: job CLOSED thì không còn listing OPEN hoặc UNKNOWN
    assert _one(pg_conn, "SELECT count(*) FROM job_sources_log WHERE job_id = %s "
                         "AND listing_status <> 'CLOSED'", (keeper,))[0] == 0
    assert "job_status" not in {f for f, _, _ in _mismatches(pg_conn, keeper)}


def test_manual_keeper_without_deadline_gets_the_derived_deadline_filled(pg_conn):
    c = _company(pg_conn)
    manual_url = _manual_url()
    keeper = _job(pg_conn, c, "Kế toán trưởng", url=manual_url, deadline=None)
    donor = _job(pg_conn, c, "Kế toán trưởng", url="https://www.topcv.vn/crawl", deadline="2026-12-01")
    plan = _plan_for(pg_conn, [keeper, donor], keeper)
    assert plan.protected_manual and plan.changes["deadline"]["old"] is None and plan.listing_actions == {}
    res = _merge(pg_conn, plan)
    assert res["listing_actions"] == {}
    row = _job_row(pg_conn, keeper)
    assert row[:4] == ("OPEN", None, "2026-12-01", manual_url)
    assert ("deadline", None, None) not in _mismatches(pg_conn, keeper)
    assert [f for f, _, _ in _mismatches(pg_conn, keeper)] == ["source_url"]


def test_manual_donor_is_not_protected_and_its_listing_joins_the_derivation(pg_conn):
    c = _company(pg_conn)
    keeper = _job(pg_conn, c, "Kế toán trưởng", url="https://www.topcv.vn/crawl", deadline="2026-10-15")
    donor = _job(pg_conn, c, "Kế toán trưởng", url=_manual_url(), deadline="2026-12-01")
    plan = _plan_for(pg_conn, [keeper, donor], keeper)
    assert not plan.protected_manual and plan.derived_changes["deadline"]["new"].isoformat() == "2026-12-01"
    _merge(pg_conn, plan)
    assert _job_row(pg_conn, keeper)[2] == "2026-12-01"
    assert _mismatches(pg_conn, keeper) == []


def test_wrong_close_count_for_manual_keeper_is_refused_and_leaves_nothing(pg_conn):
    c = _company(pg_conn)
    keeper = _job(pg_conn, c, "Kế toán trưởng", url=_manual_url(), status="CLOSED", deadline="2026-10-15")
    donor = _job(pg_conn, c, "Kế toán trưởng", url="https://www.topcv.vn/crawl", deadline="2026-12-01")
    plan = _plan_for(pg_conn, [keeper, donor], keeper)
    before = _state(pg_conn)
    with pytest.raises(db.MergeIntegrityError, match="đóng listing"):
        db.merge_job_group(pg_conn, keeper_id=keeper, donor_ids=[donor], expected=plan.expected,
                           changes=plan.changes, child=dataclasses.asdict(plan.child), derived_changes=None,
                           listing_actions={"close_listings": 5})
    pg_conn.rollback()
    assert _state(pg_conn) == before


def test_run_apply_end_to_end_with_manual_keeper_through_the_only_file(pg_conn, capsys):
    c = _company(pg_conn)
    manual_url = _manual_url()
    keeper = _job(pg_conn, c, "Kế toán trưởng", url=manual_url, deadline="2026-10-15")
    donor = _job(pg_conn, c, "Kế toán trưởng", url="https://www.topcv.vn/crawl", deadline="2026-12-01")
    only = md.OnlySpec(job_ids={keeper, donor}, keepers={keeper})
    assert _run(pg_conn, only=only) == md.EXIT_OK
    out = capsys.readouterr().out
    assert "job giữ nhập tay (luật suy ra không đè)" in out and "đã gộp" in out
    assert _job_row(pg_conn, keeper)[2] == "2026-10-15"
    assert [r[0] for r in _all(pg_conn, "SELECT job_id::text FROM job_postings")] == [keeper]
