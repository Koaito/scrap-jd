"""
db.job_merge — phần chạy SQL của lệnh gộp job trùng (`python main.py merge-duplicates`, xem
merge_duplicates.py, Phần 3b). Logic chọn job giữ / hợp nhất trường / kế hoạch chuyển dữ liệu
con là hàm THUẦN ở merge_duplicates.py; ở đây chỉ chạy SQL.

  - list_merge_job_details: ĐỌC chi tiết job + dữ liệu con (dùng để lập kế hoạch).
  - merge_job_group:        GHI — gộp MỘT nhóm trong transaction của nơi gọi (xem docstring hàm). Từ C3c,
                            trạng thái/hạn/URL của job giữ là giá trị SUY RA từ các listing sau khi gộp
                            (db.job_sync), trừ job giữ nhập tay (xem docstring hàm).
  - merge_job_enum_supported / list_active_runs: kiểm tra DB sẵn sàng + crawl/bảo trì đang chạy.
"""

from typing import Optional

from psycopg2.extras import Json

from db.audit_logs import log_action
from db.job_level_recompute import SKIP_UPDATED_AT_SETTING
from db.job_sync import sync_job_from_listings
from db.listing_state import close_job_listings, set_job_listings_deadline

# Cột của job_postings cần để lập kế hoạch gộp. Khác db.list_duplicate_job_rows (3a): lấy đủ các
# khối cần hợp nhất (lương, hạn, trạng thái, level + dấu, ghi chú), không lấy tên công ty/tỉnh.
_JOB_COLUMNS = (
    "job_id", "company_id", "job_title", "level_id", "level_code", "level_source",
    "level_rule_version", "level_signals", "province_id", "currency", "salary_min", "salary_max",
    "salary_type", "salary_period", "deadline", "job_status", "closed_reason", "ss_team_notes",
    "source_url", "created_at", "has_editor", "has_notes",
)

# Cột của job_sources_log (một listing) cần để suy ra job sau gộp (C3c): cùng bộ cột với
# db.job_derivation._LISTING_COLUMNS, trừ job_id (đã nằm ở khoá ngoài), cộng log_id.
_LOG_COLUMNS = ("log_id", "source_url", "listing_status", "deadline", "first_seen_at", "last_seen_at",
                "closed_reason", "closed_at")


def _fetch_merge_job_details(conn, job_ids: list) -> dict:
    """Thân của list_merge_job_details, KHÔNG đóng transaction (merge_job_group dùng trong khi
    đang giữ khoá dòng). Xem list_merge_job_details cho định dạng kết quả."""
    ids = [str(j) for j in dict.fromkeys(job_ids)]
    if not ids:
        return {}
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT jp.job_id, jp.company_id, jp.job_title, jp.level_id, l.level_code,
                   jp.level_source, jp.level_rule_version, jp.level_signals, jp.province_id,
                   jp.currency, jp.salary_min, jp.salary_max, jp.salary_type::text,
                   jp.salary_period::text, jp.deadline, jp.job_status::text, jp.closed_reason,
                   jp.ss_team_notes, jp.source_url, jp.created_at,
                   (jp.updated_by IS NOT NULL) AS has_editor,
                   (COALESCE(btrim(jp.ss_team_notes), '') <> '') AS has_notes
            FROM job_postings jp
            LEFT JOIN levels l ON l.level_id = jp.level_id
            WHERE jp.job_id = ANY(%s::uuid[])
            """,
            (ids,),
        )
        for r in cur.fetchall():
            row = dict(zip(_JOB_COLUMNS, r))
            row["job_id"] = str(row["job_id"])
            row["company_id"] = str(row["company_id"])
            row.update(logs=[], saved=[], applications=[], links=[])
            out[row["job_id"]] = row

        cur.execute(
            "SELECT job_id, log_id, source_url, listing_status, deadline, first_seen_at, last_seen_at, "
            "closed_reason, closed_at FROM job_sources_log "
            "WHERE job_id = ANY(%s::uuid[]) ORDER BY collected_date, log_id",
            (ids,),
        )
        for job_id, log_id, *rest in cur.fetchall():
            if str(job_id) in out:
                out[str(job_id)]["logs"].append(dict(zip(_LOG_COLUMNS, [str(log_id), *rest])))

        cur.execute(
            "SELECT job_id, saved_job_id, ss_user_id FROM saved_jobs "
            "WHERE job_id = ANY(%s::uuid[]) ORDER BY created_at, saved_job_id",
            (ids,),
        )
        for job_id, saved_id, user_id in cur.fetchall():
            if str(job_id) in out:
                out[str(job_id)]["saved"].append({"saved_job_id": str(saved_id), "ss_user_id": str(user_id)})

        cur.execute(
            "SELECT job_id, application_id, ss_user_id, (cv_url IS NOT NULL) FROM job_applications "
            "WHERE job_id = ANY(%s::uuid[]) ORDER BY applied_at, application_id",
            (ids,),
        )
        for job_id, app_id, user_id, has_cv in cur.fetchall():
            if str(job_id) in out:
                out[str(job_id)]["applications"].append(
                    {"application_id": str(app_id), "ss_user_id": str(user_id), "has_cv": bool(has_cv)})

        cur.execute(
            """
            SELECT l.job_id, l.link_id, l.contact_id,
                   (SELECT count(*) FROM job_contact_interactions i WHERE i.link_id = l.link_id)
            FROM job_contact_links l
            WHERE l.job_id = ANY(%s::uuid[]) ORDER BY l.created_at, l.link_id
            """,
            (ids,),
        )
        for job_id, link_id, contact_id, n_inter in cur.fetchall():
            if str(job_id) in out:
                out[str(job_id)]["links"].append(
                    {"link_id": str(link_id), "contact_id": str(contact_id), "n_interactions": n_inter})
    for row in out.values():
        row["n_applications"] = len(row["applications"])
        row["n_saved"] = len(row["saved"])
        row["n_contact_links"] = len(row["links"])
    return out


def list_merge_job_details(conn, job_ids: list) -> dict:
    """{job_id (str): dict} cho các job trong `job_ids`, mỗi dict gồm:

      - _JOB_COLUMNS (has_editor = updated_by IS NOT NULL; has_notes = ss_team_notes không rỗng);
      - logs:         [{log_id, source_url, listing_status, deadline, first_seen_at, last_seen_at,
                        closed_reason, closed_at}]        từ job_sources_log (trạng thái từng listing, C3c);
      - saved:        [{saved_job_id, ss_user_id}]      từ saved_jobs;
      - applications: [{application_id, ss_user_id, has_cv}] từ job_applications;
      - links:        [{link_id, contact_id, n_interactions}] từ job_contact_links;
      - n_applications / n_saved / n_contact_links: số phần tử của ba danh sách trên (cùng tên
        với db.list_duplicate_job_rows để dùng chung luật "dữ liệu cần bảo vệ").

    Mọi id trả về dạng str. job_id không tồn tại thì vắng mặt trong kết quả. CHỈ ĐỌC; đóng
    transaction đọc trước khi trả."""
    out = _fetch_merge_job_details(conn, job_ids)
    conn.rollback()
    return out


# ----------------------------------------------------------------------
# Kiểm tra sẵn sàng
# ----------------------------------------------------------------------
def merge_job_enum_supported(conn) -> bool:
    """True nếu audit_action_enum đã có giá trị 'MERGE_JOB' (sql/migration_add_merge_job_audit_action.sql).
    Thiếu thì log_action(MERGE_JOB) sẽ lỗi ở giữa lúc gộp nên --apply từ chối ngay từ đầu."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
            "WHERE t.typname = 'audit_action_enum' AND e.enumlabel = 'MERGE_JOB'"
        )
        found = cur.fetchone() is not None
    conn.rollback()
    return found


def list_active_runs(conn) -> list:
    """Các lượt crawl (crawl_runs) và bảo trì (maintenance_runs) đang 'queued'/'running', mỗi
    phần tử {kind, run_id, label, status, age_minutes}. Bảng chưa tồn tại (DB chưa migrate) thì
    bỏ qua bảng đó. CHỈ ĐỌC; đóng transaction đọc trước khi trả.

    Lưu ý: dòng 'running' mà process đã chết (vd CLI bị tắt máy) vẫn nằm đó tới khi watchdog của
    API dọn (30 phút); age_minutes giúp nhận ra trường hợp này."""
    out: list = []
    with conn.cursor() as cur:
        for kind, table, label_sql in (
            ("crawl", "crawl_runs", "source || ' / ' || category"),
            ("bảo trì", "maintenance_runs", "job_type::text"),
        ):
            cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
            if cur.fetchone()[0] is None:
                continue
            cur.execute(
                f"SELECT run_id, {label_sql}, status::text, "
                f"EXTRACT(EPOCH FROM (now() - started_at)) / 60 FROM {table} "
                "WHERE status IN ('queued', 'running') ORDER BY started_at"
            )
            for run_id, label, status, age in cur.fetchall():
                out.append({"kind": kind, "run_id": str(run_id), "label": label, "status": status,
                            "age_minutes": int(age or 0)})
    conn.rollback()
    return out


# ----------------------------------------------------------------------
# Gộp MỘT nhóm
# ----------------------------------------------------------------------
class MergeStaleError(Exception):
    """Dữ liệu của nhóm đã đổi (hoặc job biến mất) giữa lúc lập kế hoạch và lúc gộp: kế hoạch
    không còn đúng nên KHÔNG gộp nhóm này (chạy lại lệnh để lập kế hoạch mới)."""


class MergeIntegrityError(Exception):
    """Số liệu lúc gộp không khớp kế hoạch (số dòng ghi khác dự kiến, còn dữ liệu trỏ vào job
    phụ...). Nơi gọi PHẢI rollback; không có gì được giữ lại."""


# Cột job_postings mà kế hoạch gộp được phép GHI TRỰC TIẾP lên job giữ (merge_duplicates.SALARY_COLUMNS +
# LEVEL_COLUMNS + ghi chú + hạn nộp). Danh sách trắng: tên cột đi thẳng vào câu SQL nên không bao giờ nhận
# tên cột từ bên ngoài. `deadline` ở đây chỉ dành cho job giữ nhập tay có hạn trống (điền hạn suy ra).
# Trạng thái, closed_reason, hạn và source_url do luật suy ra (C3c) KHÔNG nằm ở đây: chúng chỉ được ghi qua
# db.job_sync.sync_job_from_listings (xem _DERIVED_JOB_COLUMNS), để một nơi duy nhất quyết định.
_WRITABLE_JOB_COLUMNS = frozenset({
    "currency", "salary_min", "salary_max", "salary_type", "salary_period",
    "level_id", "level_source", "level_rule_version", "level_signals",
    "deadline", "ss_team_notes",
})

# Cột mà db.job_sync có thể ghi cho job giữ; `derived_changes` của kế hoạch chỉ được chứa các cột này.
_DERIVED_JOB_COLUMNS = frozenset({"job_status", "closed_reason", "deadline", "source_url"})

_CHILD_ID_COLUMNS = {
    "job_sources_log": "log_id",
    "saved_jobs": "saved_job_id",
    "job_applications": "application_id",
    "job_contact_links": "link_id",
}


def _norm_status(value) -> Optional[str]:
    return (value or "").strip() or None


def _expect_rows(cur, expected: int, what: str) -> None:
    if cur.rowcount != expected:
        raise MergeIntegrityError(f"{what}: ghi {cur.rowcount} dòng, kế hoạch dự kiến {expected}")


def _rows_by_job(cur, sql: str, donors: list) -> list:
    """[(job_id str, id str, row dict)] — to_jsonb của từng dòng con của các job phụ."""
    cur.execute(sql, (donors,))
    return [(str(r[0]), str(r[1]), r[2]) for r in cur.fetchall()]


def _check_children_covered(table: str, db_ids: set, plan_ids: list) -> None:
    """Mọi dòng con của job phụ phải nằm đúng một lần trong kế hoạch (chuyển hoặc bỏ)."""
    if len(plan_ids) != len(set(plan_ids)) or set(plan_ids) != db_ids:
        raise MergeIntegrityError(
            f"{table}: dữ liệu con của job phụ không khớp kế hoạch "
            f"(DB {len(db_ids)} dòng, kế hoạch {len(plan_ids)} dòng)")


def _split(rows: list, donor: str, move_ids: list, drop_ids: list) -> tuple:
    """([id chuyển], [dòng bị bỏ]) của riêng một job phụ, từ rows = [(job_id, id, dòng)]."""
    move, drop = set(move_ids), set(drop_ids)
    return ([i for j, i, _ in rows if j == donor and i in move],
            [r for j, i, r in rows if j == donor and i in drop])


def merge_job_group(conn, *, keeper_id: str, donor_ids: list, expected: dict, changes: dict,
                    child: dict, derived_changes: Optional[dict], conflicts: Optional[list] = None,
                    notes: Optional[list] = None, listing_actions: Optional[dict] = None,
                    actor_id: Optional[str] = None, lock_timeout_ms: int = 10_000) -> dict:
    """Gộp MỘT nhóm job trùng vào job giữ, trong transaction HIỆN TẠI của `conn`. KHÔNG commit
    và KHÔNG rollback ở đây: nơi gọi commit khi hàm trả về, rollback nếu hàm raise (mọi bước bên
    dưới nằm chung một transaction nên lỗi ở bất cứ bước nào đều không để lại gì).

    Tham số: `expected` = {job_id: dict} từ list_merge_job_details lúc lập kế hoạch (cả job giữ
    lẫn job phụ); `changes` = {cột: {"old", "new"}} GHI TRỰC TIẾP lên job giữ (lương, level, ghi chú,
    và hạn trống của job giữ nhập tay); `child` = MergePlan.child dưới dạng dict
    (dataclasses.asdict); `conflicts`/`notes` = bản lệch và lý do từ kế hoạch (chỉ để ghi audit).

    C3c, trạng thái/hạn/URL của job giữ theo luật SUY RA từ các listing sau khi gộp:
      - `derived_changes` (BẮT BUỘC, không mặc định để người gọi không quên quyết định) = {cột: {"old", "new"}}
        mà kế hoạch DỰ ĐOÁN sync_job_from_listings sẽ ghi
        (chỉ các cột trong _DERIVED_JOB_COLUMNS). Không None (kể cả {}) thì sau khi dữ liệu con đã
        chuyển, hàm gọi sync_job_from_listings cho job giữ và đối chiếu kết quả THẬT với dự đoán; lệch
        => MergeIntegrityError (rollback cả nhóm). None = KHÔNG đồng bộ: dành cho job giữ NHẬP TAY, nơi
        luật suy ra không được đè trạng thái/hạn/source_url của nó (bạn chốt 09/10, phương án a).
      - `listing_actions` (chỉ có với job giữ nhập tay, để listing theo kịp giá trị được giữ nguyên):
        "close_listings": N => job giữ đang CLOSED, đóng mọi listing chưa CLOSED của nó (luật 2 của
        db.listing_state; N là số kế hoạch dự kiến, lệch => MergeIntegrityError);
        "stamp_deadline": ngày => ghi hạn đó vào mọi listing OPEN của job giữ (cùng cách update_job ghi
        hạn nhân viên sửa), để lần đồng bộ sau không suy ra hạn khác.

    Các bước:
      1. Bật cờ app.skip_updated_at (set_config local) => updated_at KHÔNG nhảy, kể cả job giữ,
         và lock_timeout để không treo vô hạn nếu có giao dịch khác đang giữ job.
      2. Khoá các dòng job_postings của nhóm (FOR UPDATE, theo thứ tự job_id), đọc lại chi tiết và
         so với `expected`; khác => raise MergeStaleError (không gộp). Khoá này cũng chờ các lượt
         lưu/ứng tuyển đang dở trên job phụ (chúng giữ khoá khoá ngoại).
      3. Chụp snapshot (job phụ nguyên dòng, dòng con bị bỏ, liên kết liên hệ bị dồn).
      4. Cập nhật job giữ; chuyển/bỏ dữ liệu con (kiểm số dòng); dồn liên kết liên hệ trùng
         (lịch sử trao đổi sang liên kết của job giữ, interaction_status theo luật: giữ của job
         giữ, trống thì lấy của bên kia, lệch thì ghi vào audit); rồi (C3c) đưa listing và job giữ
         theo luật suy ra: hành động listing của job giữ nhập tay, hoặc sync_job_from_listings.
      5. Kiểm tra KHÔNG còn dòng nào trỏ vào job phụ rồi mới xoá job phụ.
      6. Ghi audit_logs MERGE_JOB: mỗi job phụ một dòng (changes = {merged_into, snapshot}), job
         giữ một dòng nếu có trường đổi hoặc có xung đột (changes = {cột: {old, new}, merged_from,
         conflicts, link_status_conflicts}).

    Trả dict tóm tắt (status='merged', số dòng chuyển/bỏ, log_ids)."""
    donors = [str(d) for d in donor_ids]
    keeper_id = str(keeper_id)
    ids = [keeper_id, *donors]
    if not donors or keeper_id in donors or len(set(donors)) != len(donors):
        raise MergeIntegrityError("nhóm gộp không hợp lệ (không có job phụ, hoặc trùng id)")
    bad_cols = set(changes) - _WRITABLE_JOB_COLUMNS
    if bad_cols:
        raise MergeIntegrityError(f"kế hoạch ghi cột không được phép: {sorted(bad_cols)}")
    bad_derived = set(derived_changes or {}) - _DERIVED_JOB_COLUMNS
    if bad_derived:
        raise MergeIntegrityError(f"kế hoạch suy ra cột không được phép: {sorted(bad_derived)}")
    listing_actions = dict(listing_actions or {})
    if set(listing_actions) - {"close_listings", "stamp_deadline"}:
        raise MergeIntegrityError(f"hành động listing không hợp lệ: {sorted(listing_actions)}")
    conflicts = list(conflicts or [])
    notes = list(notes or [])

    with conn.cursor() as cur:
        # --- 1. cờ giữ updated_at + giới hạn chờ khoá (chỉ trong transaction này)
        cur.execute("SELECT set_config(%s, 'on', true)", (SKIP_UPDATED_AT_SETTING,))
        cur.execute("SELECT set_config('lock_timeout', %s, true)", (f"{int(lock_timeout_ms)}ms",))

        # --- 2. khoá + so lại với kế hoạch
        cur.execute("SELECT job_id FROM job_postings WHERE job_id = ANY(%s::uuid[]) "
                    "ORDER BY job_id FOR UPDATE", (ids,))
        locked = {str(r[0]) for r in cur.fetchall()}
        if locked != set(ids):
            raise MergeStaleError(f"{len(set(ids) - locked)} job của nhóm không còn trong DB")
        fresh = _fetch_merge_job_details(conn, ids)
        changed = [j for j in ids if fresh.get(j) != expected.get(j)]
        if changed:
            raise MergeStaleError("dữ liệu đã đổi từ lúc lập kế hoạch: "
                                  + ", ".join(j[:8] for j in changed))

        # --- 3. snapshot (đọc dưới khoá, trước mọi thay đổi)
        job_rows = {r[0]: r[2] for r in _rows_by_job(
            cur, "SELECT job_id, job_id, to_jsonb(jp.*) FROM job_postings jp WHERE job_id = ANY(%s::uuid[])",
            donors)}
        log_rows = _rows_by_job(
            cur, "SELECT job_id, log_id, (to_jsonb(l.*) - 'raw_jd_content') "
                 "|| jsonb_build_object('raw_jd_content_chars', length(l.raw_jd_content)) "
                 "FROM job_sources_log l WHERE job_id = ANY(%s::uuid[])", donors)
        saved_rows = _rows_by_job(
            cur, "SELECT job_id, saved_job_id, to_jsonb(s.*) FROM saved_jobs s "
                 "WHERE job_id = ANY(%s::uuid[])", donors)
        app_rows = _rows_by_job(
            cur, "SELECT job_id, application_id, to_jsonb(a.*) FROM job_applications a "
                 "WHERE job_id = ANY(%s::uuid[])", donors)
        link_rows = _rows_by_job(
            cur, "SELECT job_id, link_id, to_jsonb(k.*) FROM job_contact_links k "
                 "WHERE job_id = ANY(%s::uuid[])", donors)
        _check_children_covered("job_sources_log", {r[1] for r in log_rows},
                                child["logs_move"] + child["logs_drop"])
        _check_children_covered("saved_jobs", {r[1] for r in saved_rows},
                                child["saved_move"] + child["saved_drop"])
        _check_children_covered("job_applications", {r[1] for r in app_rows},
                                child["apps_move"] + child["apps_drop"])
        _check_children_covered(
            "job_contact_links", {r[1] for r in link_rows},
            child["links_move"] + [m["donor_link_id"] for m in child["links_merge"]])

        # --- 4a. job giữ
        if changes:
            assignments, params = [], []
            for col, change in changes.items():
                assignments.append(f"{col} = %s")
                value = change["new"]
                params.append(Json(value) if col == "level_signals" and value is not None else value)
            cur.execute(f"UPDATE job_postings SET {', '.join(assignments)} WHERE job_id = %s",
                        params + [keeper_id])
            _expect_rows(cur, 1, "cập nhật job giữ")

        # --- 4b. dữ liệu con: chuyển sang job giữ / bỏ bản trùng khoá
        for table, move_key, drop_key in (
            ("job_sources_log", "logs_move", "logs_drop"),
            ("saved_jobs", "saved_move", "saved_drop"),
            ("job_applications", "apps_move", "apps_drop"),
        ):
            pk = _CHILD_ID_COLUMNS[table]
            if child[move_key]:
                cur.execute(f"UPDATE {table} SET job_id = %s WHERE {pk} = ANY(%s::uuid[]) "
                            "AND job_id = ANY(%s::uuid[])", (keeper_id, child[move_key], donors))
                _expect_rows(cur, len(child[move_key]), f"chuyển {table}")
            if child[drop_key]:
                cur.execute(f"DELETE FROM {table} WHERE {pk} = ANY(%s::uuid[]) "
                            "AND job_id = ANY(%s::uuid[])", (child[drop_key], donors))
                _expect_rows(cur, len(child[drop_key]), f"bỏ {table} trùng")

        # --- 4c. liên kết liên hệ: chuyển trước, dồn sau (liên kết đích của một lần dồn có thể
        # chính là liên kết vừa được chuyển từ job phụ khác)
        if child["links_move"]:
            cur.execute("UPDATE job_contact_links SET job_id = %s WHERE link_id = ANY(%s::uuid[]) "
                        "AND job_id = ANY(%s::uuid[])", (keeper_id, child["links_move"], donors))
            _expect_rows(cur, len(child["links_move"]), "chuyển job_contact_links")
        merged_links, status_conflicts, interactions_moved = [], [], 0
        for m in child["links_merge"]:
            donor_link, keeper_link = m["donor_link_id"], m["keeper_link_id"]
            cur.execute("SELECT link_id, contact_id, interaction_status FROM job_contact_links "
                        "WHERE link_id = ANY(%s::uuid[])", ([donor_link, keeper_link],))
            got = {str(r[0]): (str(r[1]), _norm_status(r[2])) for r in cur.fetchall()}
            if set(got) != {donor_link, keeper_link} or got[donor_link][0] != got[keeper_link][0]:
                raise MergeIntegrityError("liên kết liên hệ cần dồn không còn khớp kế hoạch")
            contact_id, donor_status = got[donor_link]
            keeper_status = got[keeper_link][1]
            if donor_status and not keeper_status:
                cur.execute("UPDATE job_contact_links SET interaction_status = %s WHERE link_id = %s",
                            (donor_status, keeper_link))
                _expect_rows(cur, 1, "lấy interaction_status của liên kết bị dồn")
            elif donor_status and keeper_status and donor_status != keeper_status:
                status_conflicts.append({"contact_id": contact_id, "kept": keeper_status,
                                         "other": donor_status, "kept_link_id": keeper_link,
                                         "other_link_id": donor_link})
            cur.execute("SELECT interaction_id FROM job_contact_interactions WHERE link_id = %s",
                        (donor_link,))
            moved_ids = [str(r[0]) for r in cur.fetchall()]
            if moved_ids:
                cur.execute("UPDATE job_contact_interactions SET link_id = %s WHERE link_id = %s",
                            (keeper_link, donor_link))
                _expect_rows(cur, len(moved_ids), "chuyển lịch sử trao đổi")
            interactions_moved += len(moved_ids)
            merged_links.append({"donor_link_id": donor_link, "keeper_link_id": keeper_link,
                                 "interactions_moved": moved_ids})
        if merged_links:
            gone = [m["donor_link_id"] for m in merged_links]
            cur.execute("DELETE FROM job_contact_links WHERE link_id = ANY(%s::uuid[]) "
                        "AND job_id = ANY(%s::uuid[])", (gone, donors))
            _expect_rows(cur, len(gone), "bỏ job_contact_links trùng")

        # --- 4d. listing rồi job giữ theo luật suy ra (C3c). Listing của job phụ đã nằm ở job giữ.
        listing_log: dict = {}
        if "close_listings" in listing_actions:
            closed = close_job_listings(conn, keeper_id)
            if closed != listing_actions["close_listings"]:
                raise MergeIntegrityError(
                    f"đóng listing của job giữ: đóng {closed}, kế hoạch dự kiến {listing_actions['close_listings']}")
            listing_log["closed_listings"] = closed
        if "stamp_deadline" in listing_actions:
            listing_log["deadline_stamped_listings"] = set_job_listings_deadline(
                conn, keeper_id, listing_actions["stamp_deadline"])
        if derived_changes is not None:
            actual = sync_job_from_listings(conn, keeper_id)
            planned = {col: (c["old"], c["new"]) for col, c in derived_changes.items()}
            if actual != planned:
                raise MergeIntegrityError(
                    f"đồng bộ job giữ theo listing ghi {actual}, kế hoạch dự đoán {planned}")

        # --- 5. không còn gì trỏ vào job phụ thì mới xoá
        for table in _CHILD_ID_COLUMNS:
            cur.execute(f"SELECT count(*) FROM {table} WHERE job_id = ANY(%s::uuid[])", (donors,))
            left = cur.fetchone()[0]
            if left:
                raise MergeIntegrityError(f"{table} còn {left} dòng trỏ vào job phụ, không xoá job")
        cur.execute("DELETE FROM job_postings WHERE job_id = ANY(%s::uuid[])", (donors,))
        _expect_rows(cur, len(donors), "xoá job phụ")

    # --- 6. audit
    keeper_short = keeper_id[:8]
    link_info = {m["donor_link_id"]: m for m in merged_links}
    log_ids = []
    for donor in donors:
        row = job_rows[donor]
        logs_moved, logs_dropped = _split(log_rows, donor, child["logs_move"], child["logs_drop"])
        saved_moved, saved_dropped = _split(saved_rows, donor, child["saved_move"], child["saved_drop"])
        apps_moved, apps_dropped = _split(app_rows, donor, child["apps_move"], child["apps_drop"])
        links_moved, _ = _split(link_rows, donor, child["links_move"], [])
        snapshot = {
            "job": row,
            "job_sources_log": {"moved": logs_moved, "dropped": logs_dropped},
            "saved_jobs": {"moved": saved_moved, "dropped": saved_dropped},
            "job_applications": {"moved": apps_moved, "dropped": apps_dropped},
            "job_contact_links": {
                "moved": links_moved,
                "merged": [{"link": r, "merged_into_link_id": link_info[i]["keeper_link_id"],
                            "interactions_moved": link_info[i]["interactions_moved"]}
                           for j, i, r in link_rows if j == donor and i in link_info],
            },
        }
        log_ids.append(log_action(
            conn, actor_id=actor_id, action_type="MERGE_JOB", entity_type="JOB", entity_id=donor,
            entity_label=(row.get("job_title") or "")[:255] or None,
            company_id=str(row["company_id"]),
            changes={"merged_into": keeper_id, "snapshot": snapshot},
            note=f"Gộp job trùng vào {keeper_short} bằng lệnh merge-duplicates; job này đã bị xoá, "
                 "dữ liệu gốc nằm trong changes.snapshot.",
        ))
    derived = dict(derived_changes or {})
    if changes or derived or listing_log or conflicts or status_conflicts:
        keeper_changes = {col: {"old": c["old"], "new": c["new"]} for col, c in {**changes, **derived}.items()}
        keeper_changes["merged_from"] = donors
        if listing_log:
            keeper_changes["listing_actions"] = listing_log
        if conflicts:
            keeper_changes["conflicts"] = conflicts
        if status_conflicts:
            keeper_changes["link_status_conflicts"] = status_conflicts
        if notes:
            keeper_changes["notes"] = notes
        log_ids.append(log_action(
            conn, actor_id=actor_id, action_type="MERGE_JOB", entity_type="JOB", entity_id=keeper_id,
            entity_label=(fresh[keeper_id].get("job_title") or "")[:255] or None,
            company_id=fresh[keeper_id]["company_id"], changes=keeper_changes,
            note=f"Job giữ của nhóm gộp: nhận {len(donors)} job trùng "
                 f"({', '.join(d[:8] for d in donors)}) bằng lệnh merge-duplicates.",
        ))

    return {
        "status": "merged", "keeper_id": keeper_id, "donors_deleted": len(donors), "log_ids": log_ids,
        "children": {
            "job_sources_log": (len(child["logs_move"]), len(child["logs_drop"])),
            "saved_jobs": (len(child["saved_move"]), len(child["saved_drop"])),
            "job_applications": (len(child["apps_move"]), len(child["apps_drop"])),
            "job_contact_links": (len(child["links_move"]), len(merged_links)),
        },
        "interactions_moved": interactions_moved, "link_status_conflicts": len(status_conflicts),
        "listing_actions": listing_log, "derived_changes": sorted(derived),
        "cv_dropped": sum(1 for _, i, r in app_rows if i in set(child["apps_drop"]) and r.get("cv_url")),
    }
