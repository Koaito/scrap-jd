"""
Lệnh tính lại level hàng loạt — `python main.py recompute-levels` (Phần 2b của
kế hoạch level_source/level_rule_version/level_signals).

KHI NÀO DÙNG: sau khi đổi quy tắc suy level (normalize.LEVEL_RULE_VERSION tăng), hoặc
để xử lý các job chưa được đóng dấu (level_source IS NULL). Chạy hoàn toàn trong DB,
không tải trang nào: mọi thứ cần để tính lại đã lưu ở job_postings (job_title +
level_signals).

CHỌN JOB: level_source IS NULL, hoặc level_rule_version < LEVEL_RULE_VERSION hiện
tại. Chọn theo DẤU, không đoán theo giá trị level. Dòng 'manual' (người đã đặt/sửa
level) KHÔNG BAO GIỜ được chọn, và việc ghi còn bị chặn thêm một lớp ở
db.job_levels.

QUY TẮC TÍNH (mỗi job):
  1. Job từng có người sửa (updated_by) mà level chưa đóng dấu: BỎ QUA hoàn toàn. Người
     đó có thể đã sửa level; số này ít nên xem tay an toàn hơn là đè nhầm.
  2. Job CÓ level_signals (crawl từ sau 2a): tính lại đầy đủ bằng
     normalize.derive_level_from_signals (tiêu đề + tín hiệu đã lưu), giống lúc crawl.
  3. Job KHÔNG có level_signals (job cũ, tín hiệu đã mất): chỉ tin kết quả khi TIÊU ĐỀ
     nêu rõ một cấp (căn cứ 'title'). Còn lại tính lại sẽ rơi về 'default' Junior, có
     thể tệ hơn level đang có, nên GIỮ NGUYÊN level và để dấu NULL.
  Khi level không đổi nhưng dấu mới khác dấu cũ, ghi dấu (nhóm "đóng dấu") để lần sau
  job không bị chọn lại.

AN TOÀN:
  - Mặc định CHẠY THỬ (không ghi). Thêm --apply mới ghi.
  - updated_at KHÔNG nhảy: ghi trong transaction bật cờ app.skip_updated_at (cần đã chạy
    `python main.py migrate`; --apply tự kiểm tra và từ chối nếu thiếu). Không khoá bảng.
  - content_hash vẫn được tính lại (level còn nằm trong hash này). Nhóm job nghi trùng theo
    khoá chống trùng (dedup_key, view v_duplicate_job_candidates) KHÔNG đổi khi đổi level vì
    khoá không gồm level. Lệnh vẫn in job rời/nhập nhóm cùng content_hash (khoá cũ) để tham
    khảo; KHÔNG tự gộp.
  - Ghi theo lô (mỗi lô một transaction). Bị ngắt giữa chừng thì lô đã commit vẫn giữ,
    chạy lại sẽ tiếp tục vì job đã xong không còn được chọn.
  - Ghi kiểu so-sánh-rồi-ghi: job bị crawl/người sửa giữa lúc chọn và lúc ghi sẽ được
    bỏ qua và báo số lượng, không đè dữ liệu mới.
"""

import logging
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from scrapjd import db
from scrapjd import normalize
from scrapjd.db.job_level_recompute import SKIP_UPDATED_AT_SETTING

logger = logging.getLogger(__name__)

ACTION_CHANGE = "change"            # level đổi (kèm dấu mới)
ACTION_STAMP = "stamp"              # level giữ nguyên, chỉ ghi dấu mới
ACTION_KEEP = "keep"                # job cũ không có căn cứ title: giữ level, dấu vẫn NULL
ACTION_SKIP_EDITED = "skip_edited"  # có người sửa + chưa biết level: không đụng
ACTION_UNCHANGED = "unchanged"      # dấu và level đã đúng (không xảy ra với job do hàm chọn ra)

DEFAULT_BATCH_SIZE = 500
DEFAULT_SHOW = 10

# Migration phải có trước khi chạy: (cả chạy thử lẫn --apply) / (riêng --apply).
_REQUIRED_MIGRATIONS = (
    "migration_add_job_level_source.sql",
    "migration_add_job_level_signals.sql",
)
_REQUIRED_MIGRATIONS_FOR_APPLY = _REQUIRED_MIGRATIONS + ("migration_add_skip_updated_at_flag.sql",)


@dataclass(frozen=True)
class Plan:
    action: str
    new_level: Optional[str] = None
    new_source: Optional[str] = None
    new_signals: Optional[dict] = None


# ----------------------------------------------------------------------
# Logic thuần (không DB, không mạng) — có test
# ----------------------------------------------------------------------
def plan_job(row: dict, rule_version: int = normalize.LEVEL_RULE_VERSION) -> Plan:
    """Quyết định việc cần làm cho MỘT job (dict từ db.list_level_recompute_candidates)."""
    has_stamp = row.get("level_source") is not None
    if not has_stamp and row.get("has_editor"):
        return Plan(ACTION_SKIP_EDITED)

    signals = row.get("level_signals")
    decision = normalize.derive_level_from_signals(row["job_title"], signals)
    if signals is None and decision.source != normalize.LEVEL_SOURCE_TITLE:
        # Job cũ, tín hiệu đã mất: chỉ tiêu đề nêu rõ cấp mới đáng tin.
        return Plan(ACTION_KEEP)

    same_level = decision.level == row.get("level_code")
    same_stamp = (row.get("level_source") == decision.source
                  and row.get("level_rule_version") == rule_version)
    if same_level and same_stamp:
        return Plan(ACTION_UNCHANGED)
    # Tín hiệu giữ nguyên (không có thì vẫn NULL): ghi lại đúng cái đã lưu.
    return Plan(ACTION_CHANGE if not same_level else ACTION_STAMP,
                decision.level, decision.source, signals)


def build_plans(rows: list, rule_version: int = normalize.LEVEL_RULE_VERSION) -> list:
    """[(row, Plan)] cho mọi job được chọn, giữ thứ tự."""
    return [(row, plan_job(row, rule_version)) for row in rows]


def simulate_group_effects(members: dict, moves: list) -> dict:
    """Ảnh hưởng của việc đổi level lên nhóm cùng content_hash (khoá cũ, còn gồm level; không còn là
    tiêu chí job trùng từ A3, chỉ để tham khảo).

    members: {content_hash: {job_id: job_title}} — job HIỆN CÓ mang các hash liên quan.
    moves:   [{job_id, job_title, old_hash, new_hash}] — job đổi hash do đổi level.

    Trả {"left": [...], "joined": [...], "groups_before": n, "groups_after": n}:
      left   = job rời một nhóm trùng (nhóm cũ có >= 2 job), kèm job còn lại trong nhóm;
      joined = job vào một nhóm trùng (sau khi đổi, hash mới có >= 2 job, kể cả khi bạn
               cùng nhóm cũng là job đang đổi), kèm các job còn lại trong nhóm.
      groups_before/after = số nhóm trùng (>= 2 job) tính trên các hash bị đụng tới."""
    before = {h: dict(js) for h, js in members.items()}
    after = {h: dict(js) for h, js in members.items()}
    moved = [m for m in moves if m["old_hash"] != m["new_hash"]]
    for m in moved:
        after.setdefault(m["old_hash"], {}).pop(m["job_id"], None)
        after.setdefault(m["new_hash"], {})[m["job_id"]] = m["job_title"]

    left, joined = [], []
    for m in moved:
        if len(before.get(m["old_hash"], {})) > 1:
            left.append({"job_id": m["job_id"], "job_title": m["job_title"],
                         "others": sorted(after[m["old_hash"]])})
        if len(after[m["new_hash"]]) > 1:
            joined.append({"job_id": m["job_id"], "job_title": m["job_title"],
                           "others": sorted(set(after[m["new_hash"]]) - {m["job_id"]})})
    touched = {m["old_hash"] for m in moved} | {m["new_hash"] for m in moved}
    return {
        "left": left, "joined": joined,
        "groups_before": sum(1 for h in touched if len(before.get(h, {})) > 1),
        "groups_after": sum(1 for h in touched if len(after.get(h, {})) > 1),
    }


class Summary:
    """Số liệu tổng hợp từ danh sách (row, Plan) để in báo cáo."""

    def __init__(self, plans: list):
        self.selected = len(plans)
        self.by_action: Counter = Counter(plan.action for _, plan in plans)
        self.with_signals = sum(1 for row, _ in plans if row.get("level_signals") is not None)
        self.transitions: Counter = Counter()   # (cũ, mới, căn cứ) -> số job
        self.stamps: Counter = Counter()        # căn cứ -> số job chỉ đóng dấu
        self.samples: dict = {a: [] for a in (ACTION_CHANGE, ACTION_SKIP_EDITED)}
        for row, plan in plans:
            if plan.action == ACTION_CHANGE:
                self.transitions[(row.get("level_code") or "(trống)", plan.new_level, plan.new_source)] += 1
            elif plan.action == ACTION_STAMP:
                self.stamps[plan.new_source] += 1
            if plan.action in self.samples:
                self.samples[plan.action].append((row, plan))


def to_change(row: dict, plan: Plan, level_ids: dict, rule_version: int) -> dict:
    """Dựng dict cho db.write_recomputed_levels từ một job + kế hoạch đã chọn."""
    new_level_id = level_ids.get(plan.new_level)
    if new_level_id is None:
        raise RuntimeError(f"Không tìm thấy level_id cho '{plan.new_level}' trong bảng levels")
    return {
        "job_id": str(row["job_id"]), "new_level_id": new_level_id,
        "new_level_source": plan.new_source, "new_level_signals": plan.new_signals,
        "rule_version": rule_version, "old_job_title": row["job_title"],
        "old_level_id": row["level_id"], "old_level_source": row["level_source"],
        "old_level_rule_version": row["level_rule_version"], "old_level_signals": row["level_signals"],
    }


# ----------------------------------------------------------------------
# Báo cáo
# ----------------------------------------------------------------------
def _short(job_id) -> str:
    return str(job_id)[:8]


def print_report(summary: Summary, effects: Optional[dict], *, apply: bool, rule_version: int,
                 show: int = DEFAULT_SHOW, written: Optional[int] = None, stale: Optional[list] = None,
                 dup_before: Optional[int] = None, dup_after: Optional[int] = None) -> None:
    mode = "ĐÃ GHI DB" if apply else "CHẠY THỬ (không ghi DB)"
    n = summary.by_action
    print(f"\n===== TÍNH LẠI LEVEL (quy tắc phiên bản {rule_version}) — {mode} =====")
    print(f"Job được chọn (chưa đóng dấu hoặc dấu cũ hơn v{rule_version}): {summary.selected}")
    print(f"  - có tín hiệu (tính lại đầy đủ):            {summary.with_signals}")
    print(f"  - không có tín hiệu (job cũ, chỉ căn cứ title): {summary.selected - summary.with_signals}")
    print(f"Đổi level:                                     {n[ACTION_CHANGE]}")
    print(f"Chỉ đóng dấu (level giữ nguyên):               {n[ACTION_STAMP]}")
    print(f"Giữ nguyên, dấu vẫn NULL (cũ, không căn cứ title): {n[ACTION_KEEP]}")
    print(f"Bỏ qua vì từng có người sửa (updated_by):      {n[ACTION_SKIP_EDITED]}")
    if n[ACTION_UNCHANGED]:
        print(f"Đã đúng sẵn:                                   {n[ACTION_UNCHANGED]}")

    if summary.transitions:
        print("\nLevel cũ -> mới:")
        for (old, new, source), cnt in sorted(summary.transitions.items(), key=lambda kv: -kv[1]):
            print(f"  {old:>8} -> {new:<8} (căn cứ {source}) {cnt}")
        print(f"\nMột vài job đổi level (tối đa {show}):")
        for row, plan in summary.samples[ACTION_CHANGE][:show]:
            print(f"  {_short(row['job_id'])} {row['job_title'][:60]}: "
                  f"{row.get('level_code') or '(trống)'} -> {plan.new_level} ({plan.new_source})")
    if summary.stamps:
        print("\nChỉ đóng dấu theo căn cứ: "
              + ", ".join(f"{s}={c}" for s, c in sorted(summary.stamps.items())))
    if summary.samples[ACTION_SKIP_EDITED]:
        print(f"\nJob bị bỏ qua vì từng có người sửa (xem tay, tối đa {show}):")
        for row, _ in summary.samples[ACTION_SKIP_EDITED][:show]:
            print(f"  {_short(row['job_id'])} {row['job_title'][:60]} (level hiện tại {row.get('level_code')})")

    if effects is not None:
        print("\nẢnh hưởng tới nhóm cùng content_hash (khoá cũ, còn gồm level; nhóm trùng theo dedup_key không đổi khi đổi level):")
        print(f"  Nhóm trùng bị đụng tới: trước = {effects['groups_before']}, sau = {effects['groups_after']}")
        print(f"  Job RỜI nhóm trùng cũ: {len(effects['left'])}")
        for e in effects["left"][:show]:
            print(f"    {_short(e['job_id'])} {e['job_title'][:50]} (nhóm cũ còn "
                  f"{', '.join(_short(o) for o in e['others'][:5]) or 'không ai'})")
        print(f"  Job NHẬP vào nhóm trùng mới: {len(effects['joined'])}")
        for e in effects["joined"][:show]:
            print(f"    {_short(e['job_id'])} {e['job_title'][:50]} (trùng với "
                  f"{', '.join(_short(o) for o in e['others'][:5])})")
        if effects["left"] or effects["joined"]:
            print("  Lệnh này KHÔNG tự gộp job. Các nhóm trên là nhóm cùng content_hash (khoá cũ), không phải nhóm trùng theo dedup_key.")

    if apply:
        print(f"\nĐã ghi: {written} job. updated_at giữ nguyên (cờ {SKIP_UPDATED_AT_SETTING}).")
        if stale:
            print(f"Bỏ qua {len(stale)} job vì đã bị đổi giữa lúc chọn và lúc ghi (chạy lại để xử lý):")
            for job_id in stale[:show]:
                print(f"    {job_id}")
        print(f"Nhóm job nghi trùng (v_duplicate_job_candidates): trước = {dup_before}, sau = {dup_after}")
    else:
        print("\nĐây là chạy thử, chưa ghi gì. Thêm --apply để ghi thật.")


# ----------------------------------------------------------------------
# Chạy
# ----------------------------------------------------------------------
def _check_ready(conn, *, apply: bool) -> Optional[str]:
    """Trả thông báo lỗi (tiếng Việt) nếu DB chưa sẵn sàng, None nếu ổn."""
    pending = set(db.list_pending_migrations(conn))
    conn.commit()
    required = _REQUIRED_MIGRATIONS_FOR_APPLY if apply else _REQUIRED_MIGRATIONS
    missing = [m for m in required if m in pending]
    if missing:
        return ("DB chưa áp dụng migration cần thiết: " + ", ".join(missing)
                + ". Chạy `python main.py migrate` trước.")
    if apply and not db.skip_updated_at_supported(conn):
        return ("Hàm trigger trg_set_updated_at() trong DB chưa biết cờ app.skip_updated_at, ghi bây giờ sẽ "
                "làm updated_at của mọi job nhảy. Áp dụng sql/migration_add_skip_updated_at_flag.sql "
                "(vd chạy `python main.py migrate`, hoặc init-db nếu DB dựng từ schema mới) rồi thử lại.")
    return None


def analyze_duplicate_effects(conn, plans: list, level_ids: dict) -> dict:
    """Ảnh hưởng nhóm trùng của các job SẼ ĐỔI level (không tính job chỉ đóng dấu)."""
    changing = [(row, plan) for row, plan in plans if plan.action == ACTION_CHANGE]
    new_levels = {str(row["job_id"]): level_ids[plan.new_level] for row, plan in changing}
    new_hashes = db.compute_content_hashes_for_levels(conn, new_levels)
    moves = [
        {"job_id": str(row["job_id"]), "job_title": row["job_title"],
         "old_hash": row.get("content_hash"), "new_hash": new_hashes[str(row["job_id"])]}
        for row, _ in changing if str(row["job_id"]) in new_hashes
    ]
    members = db.get_jobs_by_content_hashes(
        conn, [m["old_hash"] for m in moves] + [m["new_hash"] for m in moves])
    return simulate_group_effects(members, moves)


def run(conn, *, apply: bool, limit: Optional[int] = None, batch_size: int = DEFAULT_BATCH_SIZE,
        show: int = DEFAULT_SHOW) -> int:
    """Chạy lệnh trên một kết nối. Trả exit code (0 = xong, 1 = DB chưa sẵn sàng)."""
    error = _check_ready(conn, apply=apply)
    if error:
        print(f"❌ {error}")
        return 1
    rule_version = normalize.LEVEL_RULE_VERSION
    level_ids = {code: db.get_level_id(conn, code) for code in normalize.LEVEL_ORDER}
    conn.rollback()

    rows = db.list_level_recompute_candidates(conn, rule_version, limit)
    plans = build_plans(rows, rule_version)
    summary = Summary(plans)
    effects = analyze_duplicate_effects(conn, plans, level_ids)

    if not apply:
        print_report(summary, effects, apply=False, rule_version=rule_version, show=show)
        return 0

    dup_before = db.count_duplicate_job_groups(conn)
    changes = [to_change(row, plan, level_ids, rule_version)
               for row, plan in plans if plan.action in (ACTION_CHANGE, ACTION_STAMP)]
    written, stale = 0, []
    for start in range(0, len(changes), batch_size):
        batch = changes[start:start + batch_size]
        try:
            w, s = db.write_recomputed_levels(conn, batch)
            conn.commit()
        except Exception:
            conn.rollback()
            logger.error("Lỗi khi ghi lô bắt đầu ở job #%d; các lô trước đó đã commit, chạy lại lệnh "
                         "để tiếp tục.", start + 1)
            raise
        written += w
        stale.extend(s)
        logger.info("Đã ghi %d/%d job", min(start + batch_size, len(changes)), len(changes))
    dup_after = db.count_duplicate_job_groups(conn)
    print_report(summary, effects, apply=True, rule_version=rule_version, show=show,
                 written=written, stale=stale, dup_before=dup_before, dup_after=dup_after)
    return 0


def run_cli(args) -> int:
    """Điểm vào cho `python main.py recompute-levels` (args từ argparse trong main.py)."""
    if args.batch_size < 1:
        print("❌ --batch-size phải >= 1.")
        return 1
    conn = db.get_connection()
    try:
        return run(conn, apply=args.apply, limit=args.limit, batch_size=args.batch_size, show=args.show)
    finally:
        conn.close()
