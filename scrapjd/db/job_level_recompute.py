"""
db.job_level_recompute — phần chạy SQL của lệnh tính lại level hàng loạt
(`python main.py recompute-levels`, xem scrapjd/cli/recompute_levels.py). Logic quyết định
level mới là hàm THUẦN ở scrapjd/cli/recompute_levels.py; ở đây chỉ chọn job, tra nhóm trùng
và ghi.

Luật đóng dấu/không đè 'manual' vẫn nằm ở MỘT chỗ là scrapjd/db/job_levels.py: hàm ghi ở
đây dùng lại _derived_level_assignments, không tự viết lại luật.
"""

import json
from typing import Optional

from scrapjd.db.job_levels import _derived_level_assignments
from scrapjd.db.pg_types import Conn, fetch_scalar

# Tên cờ phiên mà trg_set_updated_at() đọc (sql/migration_add_skip_updated_at_flag.sql).
SKIP_UPDATED_AT_SETTING = "app.skip_updated_at"

_CANDIDATE_COLUMNS = (
    "job_id", "job_title", "level_id", "level_code", "level_source",
    "level_rule_version", "level_signals", "content_hash", "has_editor",
)


def skip_updated_at_supported(conn: Conn) -> bool:
    """True nếu hàm trigger trg_set_updated_at() trong DB đã biết cờ phiên
    app.skip_updated_at. Dùng để từ chối --apply khi chưa chạy migration: thiếu
    bước này, cờ bị trigger bỏ qua và updated_at của mọi job bị đổi âm thầm."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_functiondef(p.oid) FROM pg_proc p WHERE p.proname = 'trg_set_updated_at'"
        )
        row = cur.fetchone()
    conn.rollback()
    return row is not None and SKIP_UPDATED_AT_SETTING in row[0]


def list_level_recompute_candidates(conn: Conn, rule_version: int, limit: Optional[int] = None) -> list:
    """Job cần xem lại level: chưa đóng dấu (level_source IS NULL) hoặc đóng dấu theo
    bộ quy tắc cũ hơn `rule_version`. Dòng 'manual' không bao giờ được chọn (version
    NULL nên không thoả điều kiện, và level_source <> NULL). Chọn theo dấu, không
    đoán theo giá trị level.

    Mỗi job là dict gồm _CANDIDATE_COLUMNS; has_editor = updated_by IS NOT NULL (để
    nơi gọi bỏ qua job từng có người sửa mà chưa rõ có sửa level không). Cũ nhất
    trước. Đóng transaction đọc trước khi trả về."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT jp.job_id, jp.job_title, jp.level_id, l.level_code, jp.level_source,
                   jp.level_rule_version, jp.level_signals, jp.content_hash,
                   (jp.updated_by IS NOT NULL) AS has_editor
            FROM job_postings jp
            LEFT JOIN levels l ON l.level_id = jp.level_id
            WHERE jp.level_source IS NULL OR jp.level_rule_version < %s
            ORDER BY jp.created_at, jp.job_id
            LIMIT %s
            """,
            (rule_version, limit),
        )
        rows = [dict(zip(_CANDIDATE_COLUMNS, r)) for r in cur.fetchall()]
    conn.rollback()
    return rows


def compute_content_hashes_for_levels(conn: Conn, new_levels: dict) -> dict:
    """{job_id: content_hash sẽ có nếu job đổi sang level_id mới}, tính bằng chính
    hàm generate_job_hash của DB (cùng công thức với trigger set_job_hash), không
    ghi gì. `new_levels` là {job_id: level_id}."""
    if not new_levels:
        return {}
    ids = list(new_levels)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT jp.job_id, generate_job_hash(jp.company_id, jp.job_title, v.level_id, jp.province_id)
            FROM job_postings jp
            JOIN unnest(%s::uuid[], %s::int[]) AS v(job_id, level_id) ON v.job_id = jp.job_id
            """,
            (ids, [new_levels[i] for i in ids]),
        )
        out = {str(r[0]): r[1] for r in cur.fetchall()}
    conn.rollback()
    return out


def get_jobs_by_content_hashes(conn: Conn, hashes: list) -> dict:
    """{content_hash: {job_id: job_title}} cho các job HIỆN CÓ mang một trong các hash
    đó (để biết nhóm trùng trước/sau khi đổi level)."""
    hashes = [h for h in set(hashes) if h]
    if not hashes:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT content_hash, job_id, job_title FROM job_postings WHERE content_hash = ANY(%s)",
            (hashes,),
        )
        out: dict = {}
        for content_hash, job_id, title in cur.fetchall():
            out.setdefault(content_hash, {})[str(job_id)] = title
    conn.rollback()
    return out


def count_duplicate_job_groups(conn: Conn) -> int:
    """Số nhóm trong v_duplicate_job_candidates (job nghi trùng)."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v_duplicate_job_candidates")
        n = fetch_scalar(cur)
    conn.rollback()
    return n


def write_recomputed_levels(conn: Conn, changes: list) -> tuple:
    """Ghi một lô level tính lại trong transaction HIỆN TẠI (nơi gọi commit/rollback).
    Trả (số dòng đã ghi, danh sách job_id bị bỏ qua vì đã đổi giữa lúc chọn và lúc ghi).

    Mỗi phần tử của `changes` là dict: job_id, new_level_id, new_level_source,
    new_level_signals (dict hoặc None) và các giá trị CŨ lúc chọn: old_job_title,
    old_level_id, old_level_source, old_level_rule_version, old_level_signals,
    rule_version.

    - Bật cờ app.skip_updated_at bằng set_config(..., true): chỉ có hiệu lực trong
      transaction này, hết transaction tự mất; updated_at không nhảy. content_hash
      vẫn được trigger set_job_hash tính lại như mọi lần đổi level.
    - Ghi kiểu so-sánh-rồi-ghi (WHERE khớp đúng giá trị cũ đã đọc): nếu giữa lúc chọn
      và lúc ghi crawl hoặc người dùng đã đổi dòng đó (đổi tiêu đề, level, dấu, tín
      hiệu, hoặc chuyển sang 'manual') thì 0 dòng khớp và job được bỏ qua, không đè
      lên dữ liệu mới hơn.
    - Dòng 'manual' vẫn được bảo vệ thêm một lớp bởi _derived_level_assignments."""
    if not changes:
        return 0, []
    with conn.cursor() as cur:
        cur.execute("SELECT set_config(%s, 'on', true)", (SKIP_UPDATED_AT_SETTING,))
        written, stale = 0, []
        for ch in changes:
            sets, set_params = _derived_level_assignments(
                ch["new_level_id"], ch["new_level_source"], ch["rule_version"], ch["new_level_signals"],
            )
            old_signals = ch["old_level_signals"]
            cur.execute(
                f"UPDATE job_postings SET {', '.join(sets)} "
                "WHERE job_id = %s AND job_title = %s "
                "AND level_id IS NOT DISTINCT FROM %s "
                "AND level_source IS NOT DISTINCT FROM %s "
                "AND level_rule_version IS NOT DISTINCT FROM %s "
                "AND level_signals IS NOT DISTINCT FROM %s::jsonb",
                set_params + [
                    ch["job_id"], ch["old_job_title"], ch["old_level_id"], ch["old_level_source"],
                    ch["old_level_rule_version"],
                    json.dumps(old_signals, ensure_ascii=False) if old_signals is not None else None,
                ],
            )
            if cur.rowcount == 1:
                written += 1
            else:
                stale.append(ch["job_id"])
    return written, stale
