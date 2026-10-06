"""
Gộp job trùng — `python main.py merge-duplicates` (Phần 3b, phương án A).

TRẠNG THÁI: ĐỦ HAI NỬA. Không có --apply thì CHỈ ĐỌC (chọn nhóm, chọn job giữ, lập kế hoạch hợp
nhất trường + chuyển dữ liệu con, in báo cáo; KHÔNG ghi DB, KHÔNG xoá job). Có --apply thì gộp
thật, từng nhóm một transaction riêng (xem "GỘP THẬT" bên dưới).

PHƯƠNG ÁN A (đã chốt): job phụ sẽ bị XOÁ THẬT sau khi snapshot nguyên dòng vào audit_logs;
dữ liệu con được chuyển sang job giữ. Kế hoạch lập ở đây chính là thứ --apply thực thi.

CHỌN NHÓM
  - Mặc định: chỉ nhóm độ chắc "cao" (do báo cáo 3a chấm), không khác tỉnh, và không có >= 2
    job cùng chứa dữ liệu cần bảo vệ (người sửa/ghi chú/ứng tuyển/lưu/liên hệ).
  - Nhóm "cần xem", "thấp", khác tỉnh, và nhóm >= 2 job được bảo vệ KHÔNG tự gộp. Muốn gộp
    thì duyệt tay rồi đưa qua --only FILE:
      * FILE dạng danh sách job_id (mỗi dòng một id, '#' là ghi chú): gộp các nhóm mà TOÀN BỘ
        job của nhóm đều có trong file. Job giữ = đề xuất luật v0. Nhóm khác tỉnh hoặc nhóm
        >= 2 job được bảo vệ phải dùng dạng CSV bên dưới để chọn job giữ tay.
      * FILE là CSV xuất từ `report-duplicates --csv` (có cột job_id, de_xuat_giu): đã xoá các
        dòng của nhóm không duyệt; cột de_xuat_giu = 'x' là job giữ (sửa được). Nhóm khác tỉnh
        chỉ gộp khi bạn tự đánh dấu 'x'.
      * Nhóm chỉ có một phần job trong file thì bỏ qua (tránh gộp lệch ý), và báo ra.

CHỌN JOB GIỮ: luật v0 của 3a (có dữ liệu cần bảo vệ > OPEN > hạn muộn hơn > tạo sớm hơn).

HỢP NHẤT TRƯỜNG (lên job giữ; không ghi đè trường job giữ đã có)
  - Lương (currency, salary_min/max/type/period là MỘT khối): job giữ chưa có lương (min và
    max đều NULL) mà job khác có -> lấy khối của job khác (theo thứ tự xếp hạng). Hai bản
    lương khác nhau -> giữ của job giữ, ghi bản kia vào danh sách xung đột (sẽ vào audit).
  - Hạn nộp: job giữ chưa có hạn -> lấy hạn muộn nhất của job khác (ưu tiên job OPEN).
  - Trạng thái: OPEN nếu bất kỳ bản nào còn OPEN. Nếu job giữ đang CLOSED mà có bản OPEN
    ("hồi sinh"), lấy luôn hạn nộp và source_url của bản OPEN đó, vì check_expired_source_jobs
    xét theo job_postings.source_url/deadline: để nguyên hạn/URL cũ thì job vừa hồi sinh sẽ bị
    đóng lại ở lần kiểm tra sau.
  - Level: lấy của job có người đặt (level_source='manual' hoặc có updated_by); không có thì
    giữ của job giữ. Hai bản "có người đặt" khác level -> xung đột, chọn theo xếp hạng.
  - Ghi chú ss_team_notes: job giữ chưa có -> lấy của job khác; hai bản khác nhau -> giữ của
    job giữ và ghi bản kia vào xung đột.
  - Các cột khác (work_type, matching_industry, parsed_content...) giữ nguyên của job giữ.

CHUYỂN DỮ LIỆU CON (job_sources_log, saved_jobs, job_applications, job_contact_links)
  Chuyển sang job giữ. Vướng UNIQUE (cùng URL / cùng người dùng / cùng liên hệ đã có ở job
  giữ) thì giữ bản của job giữ và bỏ bản trùng; với liên hệ trùng, lịch sử trao đổi
  (job_contact_interactions) của liên kết bị bỏ được dồn sang liên kết của job giữ.
  Chuyển log URL nguồn là để lần crawl sau vẫn nhận ra URL cũ (tra qua job_sources_log) và
  không sinh lại bản trùng.

GỘP THẬT (--apply, nửa 2/2)
  Cần đã chạy `python main.py migrate` (migration_add_skip_updated_at_flag.sql và
  migration_add_merge_job_audit_action.sql); thiếu thì từ chối. Các tuỳ chọn:
    --apply        gộp thật (mặc định chỉ chạy thử).
    --limit N      chỉ gộp N nhóm đầu của kế hoạch (để thử từng ít một).
    --yes          bỏ qua bước hỏi xác nhận (chạy tự động); mặc định in tóm tắt rồi hỏi gõ 'yes'.
    --force        bỏ qua việc từ chối khi có crawl/bảo trì đang chạy. KHÔNG dừng crawl nào cả, chỉ in
                   cảnh báo: repo không có cơ chế huỷ crawl, và sửa dòng crawl_runs không dừng được
                   tiến trình thật.
  - Nhóm được chọn lại từ DB lúc chạy (không dùng kết quả của lần chạy thử trước).
  - Từ chối nếu có crawl_runs/maintenance_runs 'queued'/'running' (kiểm tra lại trước mỗi nhóm).
  - Mỗi nhóm: transaction riêng, khoá dòng job, đọc lại và so với kế hoạch (đổi giữa chừng thì
    bỏ qua nhóm đó, báo "stale"), snapshot vào audit_logs (MERGE_JOB) rồi mới xoá job phụ. updated_at
    của job giữ KHÔNG nhảy (cờ app.skip_updated_at). Xem db/job_merge.py::merge_job_group.
  - Nhóm stale hoặc lỗi: bỏ qua, chạy tiếp các nhóm sau, cuối báo cáo liệt kê. Exit code: 0 xong
    hết; 1 từ chối/chưa sẵn sàng/huỷ; 2 có nhóm stale/lỗi (các nhóm khác vẫn đã gộp); 130 bị ngắt.
  - Không xoá file CV trong storage của đơn ứng tuyển trùng bị bỏ; đường dẫn nằm trong audit.
  - Chưa có lệnh khôi phục (unmerge): cách khôi phục thủ công từ snapshot ghi trong README.

Logic quyết định là hàm THUẦN (không DB), có test ở tests/test_merge_duplicates.py. Phần SQL
nằm ở db/job_merge.py; hàm run() ở dưới điều phối (đọc -> lập kế hoạch -> hỏi xác nhận -> gộp
từng nhóm -> báo cáo).
"""

import csv
import dataclasses
import io
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Optional

import db
import duplicate_report as dr

logger = logging.getLogger(__name__)

_UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")

DEFAULT_SHOW = 5

# Các khối cột của job_postings được hợp nhất vào job giữ.
SALARY_COLUMNS = ("currency", "salary_min", "salary_max", "salary_type", "salary_period")
LEVEL_COLUMNS = ("level_id", "level_source", "level_rule_version", "level_signals")

KEEPER_FROM_RULE = "luật v0"
KEEPER_FROM_FILE = "đánh dấu x trong file"

# Lý do bỏ qua một nhóm.
SKIP_REVIEW = "độ chắc 'cần xem' (chỉ gộp khi duyệt tay qua --only)"
SKIP_LOW = "độ chắc 'thấp' (chỉ gộp khi duyệt tay qua --only)"
SKIP_PROVINCE = "khác tỉnh (không tự gộp; cần --only với CSV và đánh dấu job giữ bằng 'x')"
SKIP_MANUAL_PICK = ">= 2 job có dữ liệu cần bảo vệ (cần chọn job giữ tay: --only với CSV, đánh dấu 'x')"
SKIP_PARTIAL = "file --only chỉ liệt kê một phần job của nhóm"
SKIP_MULTI_KEEPER = "file --only đánh dấu 'x' cho nhiều hơn một job trong nhóm"
SKIP_VANISHED = "một job của nhóm không còn trong DB lúc đọc chi tiết (chạy lại)"


# ----------------------------------------------------------------------
# File --only
# ----------------------------------------------------------------------
@dataclass
class OnlySpec:
    """Danh sách đã duyệt tay từ file --only."""
    job_ids: set = field(default_factory=set)       # mọi job_id có trong file (chữ thường)
    keepers: set = field(default_factory=set)       # job_id được đánh dấu giữ (cột de_xuat_giu = 'x')
    invalid_lines: list = field(default_factory=list)  # dòng không có UUID hợp lệ (số dòng, nội dung)


def parse_only(text: str) -> OnlySpec:
    """Đọc nội dung file --only: CSV có cột job_id (kèm de_xuat_giu nếu có) hoặc danh sách
    job_id mỗi dòng một id. UUID viết hoa/thường đều được, tách theo chữ thường."""
    text = text.lstrip("\ufeff")
    spec = OnlySpec()
    lines = text.splitlines()
    first = next((ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")), "")
    header = [c.strip().lower() for c in next(csv.reader([first]), [])] if first else []

    if "job_id" in header:
        rows = csv.reader(io.StringIO(text))
        idx_job = header.index("job_id")
        idx_keep = header.index("de_xuat_giu") if "de_xuat_giu" in header else None
        started = False
        for lineno, row in enumerate(rows, 1):
            if not row or not "".join(row).strip() or row[0].lstrip().startswith("#"):
                continue
            if not started:        # dòng tiêu đề
                started = True
                continue
            raw = row[idx_job].strip() if idx_job < len(row) else ""
            if not _UUID_RE.fullmatch(raw):
                spec.invalid_lines.append((lineno, ",".join(row)[:80]))
                continue
            job_id = raw.lower()
            spec.job_ids.add(job_id)
            if idx_keep is not None and idx_keep < len(row) and row[idx_keep].strip().lower() == "x":
                spec.keepers.add(job_id)
        return spec

    for lineno, line in enumerate(lines, 1):
        code = line.split("#", 1)[0].strip()
        if not code:
            continue
        m = _UUID_RE.search(code)
        if m:
            spec.job_ids.add(m.group(0).lower())
        else:
            spec.invalid_lines.append((lineno, code[:80]))
    return spec


# ----------------------------------------------------------------------
# Chọn nhóm
# ----------------------------------------------------------------------
@dataclass
class Selection:
    group: dr.Group
    keeper_id: str
    keeper_source: str   # KEEPER_FROM_RULE | KEEPER_FROM_FILE


def auto_skip_reason(g: dr.Group) -> Optional[str]:
    """Lý do một nhóm KHÔNG tự gộp ở chế độ mặc định; None nếu được gộp."""
    if g.tier == dr.TIER_PROVINCE:
        return SKIP_PROVINCE
    if g.confidence == dr.CONF_REVIEW:
        return SKIP_REVIEW
    if g.confidence == dr.CONF_LOW:
        return SKIP_LOW
    if g.needs_manual_pick:
        return SKIP_MANUAL_PICK
    if g.keeper_id is None:     # phòng thủ: ngoài khác tỉnh thì luôn có đề xuất
        return SKIP_PROVINCE
    return None


def _select_from_file(g: dr.Group, only: OnlySpec) -> tuple:
    """(Selection | None, lý do bỏ qua | None) cho một nhóm có ít nhất một job trong file."""
    ids = [m["job_id"].lower() for m in g.members]
    if not all(i in only.job_ids for i in ids):
        return None, SKIP_PARTIAL
    marked = [m["job_id"] for m in g.members if m["job_id"].lower() in only.keepers]
    if len(marked) > 1:
        return None, SKIP_MULTI_KEEPER
    if marked:
        return Selection(g, marked[0], KEEPER_FROM_FILE), None
    if g.tier == dr.TIER_PROVINCE or g.keeper_id is None:
        return None, SKIP_PROVINCE
    if g.needs_manual_pick:
        return None, SKIP_MANUAL_PICK
    return Selection(g, g.keeper_id, KEEPER_FROM_RULE), None


def select_groups(groups: list, only: Optional[OnlySpec] = None) -> tuple:
    """(selections, skipped, unknown_ids, not_in_file).

    selections: [Selection] cần gộp; skipped: [(Group, lý do)]; unknown_ids: job_id trong
    file --only không thuộc nhóm nghi trùng nào (sai id, hoặc job đã hết trùng);
    not_in_file: số nhóm không được nhắc trong file (chỉ có nghĩa khi dùng --only)."""
    selections, skipped = [], []
    not_in_file = 0
    for g in groups:
        if only is None:
            reason = auto_skip_reason(g)
            if reason:
                skipped.append((g, reason))
            else:
                selections.append(Selection(g, g.keeper_id, KEEPER_FROM_RULE))
            continue
        if not any(m["job_id"].lower() in only.job_ids for m in g.members):
            not_in_file += 1
            continue
        sel, reason = _select_from_file(g, only)
        if sel:
            selections.append(sel)
        else:
            skipped.append((g, reason))
    known = {m["job_id"].lower() for g in groups for m in g.members}
    unknown = sorted(only.job_ids - known) if only else []
    return selections, skipped, unknown, not_in_file


# ----------------------------------------------------------------------
# Kế hoạch gộp một nhóm
# ----------------------------------------------------------------------
@dataclass
class ChildPlan:
    """Việc cần làm với dữ liệu con của các job bị gộp (id của dòng con)."""
    logs_move: list = field(default_factory=list)       # log_id đổi sang job giữ
    logs_drop: list = field(default_factory=list)       # log_id trùng URL với job giữ -> bỏ
    saved_move: list = field(default_factory=list)      # saved_job_id
    saved_drop: list = field(default_factory=list)
    apps_move: list = field(default_factory=list)       # application_id
    apps_drop: list = field(default_factory=list)       # đơn trùng người dùng -> bỏ (CV có thể mất)
    links_move: list = field(default_factory=list)      # link_id đổi sang job giữ
    links_merge: list = field(default_factory=list)     # {donor_link_id, keeper_link_id, n_interactions}


@dataclass
class MergePlan:
    group: dr.Group
    keeper_id: str
    keeper_source: str
    donor_ids: list                      # job bị gộp (xoá sau khi snapshot), theo thứ tự xếp hạng
    changes: dict                        # {cột: {"old": .., "new": ..}} áp lên job giữ
    notes: list                          # lý do từng thay đổi (tiếng Việt)
    conflicts: list                      # bản lệch bị bỏ lại, sẽ ghi vào audit
    warnings: list
    child: ChildPlan
    apps_with_cv_dropped: int = 0        # đơn bị bỏ có đính CV (cần nhắc: file CV trong storage)
    # Chi tiết các job của nhóm LÚC lập kế hoạch ({job_id: dict}, từ db.list_merge_job_details).
    # --apply so lại với dữ liệu đọc dưới khoá; khác thì nhóm bị bỏ qua (stale).
    expected: dict = field(default_factory=dict)

    @property
    def revives(self) -> bool:
        return self.changes.get("job_status", {}).get("new") == "OPEN"


def _short(job_id) -> str:
    return str(job_id)[:8]


def _has_salary(row: dict) -> bool:
    return row.get("salary_min") is not None or row.get("salary_max") is not None


def _salary_view(row: dict) -> dict:
    return {c: row.get(c) for c in SALARY_COLUMNS}


def _level_authoritative(row: dict) -> bool:
    """Level do người đặt/sửa: level_source='manual' hoặc job từng có người sửa (updated_by)."""
    return row.get("level_id") is not None and (row.get("level_source") == "manual" or bool(row.get("has_editor")))


class _Changes:
    """Gom thay đổi áp lên job giữ; bỏ qua cột mà giá trị mới bằng giá trị hiện tại."""

    def __init__(self, keeper: dict):
        self._keeper = keeper
        self.data: dict = {}

    def set(self, column: str, value) -> None:
        if self._keeper.get(column) != value:
            self.data[column] = {"old": self._keeper.get(column), "new": value}

    def set_block(self, columns: tuple, source: dict) -> None:
        for col in columns:
            self.set(col, source.get(col))


def _plan_salary(keeper: dict, donors: list, ch: _Changes, notes: list, conflicts: list) -> None:
    pool = [d for d in donors if _has_salary(d)]
    if not pool:
        return
    if _has_salary(keeper):
        chosen_view = (keeper["salary_min"], keeper["salary_max"])
        kept_id, others = keeper["job_id"], pool
    else:
        src = pool[0]
        ch.set_block(SALARY_COLUMNS, src)
        notes.append(f"lương lấy từ {_short(src['job_id'])} (job giữ chưa có lương)")
        chosen_view = (src["salary_min"], src["salary_max"])
        kept_id, others = src["job_id"], pool[1:]
    kept_row = keeper if kept_id == keeper["job_id"] else next(d for d in donors if d["job_id"] == kept_id)
    for d in others:
        if (d["salary_min"], d["salary_max"]) != chosen_view:
            conflicts.append({"field": "salary", "kept_job_id": kept_id, "kept": _salary_view(kept_row),
                              "other_job_id": d["job_id"], "other": _salary_view(d)})


def _plan_status_deadline_source(keeper: dict, donors: list, ch: _Changes, notes: list) -> None:
    open_donors = [d for d in donors if d.get("job_status") == "OPEN"]
    if keeper.get("job_status") != "OPEN" and open_donors:
        src = open_donors[0]
        ch.set("job_status", "OPEN")
        ch.set("deadline", src.get("deadline"))
        if src.get("source_url"):
            ch.set("source_url", src["source_url"])
        notes.append(f"hồi sinh: job giữ đang {keeper.get('job_status')}, lấy trạng thái OPEN + hạn nộp + "
                     f"source_url của {_short(src['job_id'])} (để lần kiểm tra hết hạn sau không đóng lại)")
        return
    if keeper.get("deadline") is None:
        pool = [d for d in open_donors if d.get("deadline") is not None] \
            or [d for d in donors if d.get("deadline") is not None]
        if pool:
            best = max(pool, key=lambda d: d["deadline"])
            ch.set("deadline", best["deadline"])
            notes.append(f"hạn nộp lấy từ {_short(best['job_id'])} (job giữ chưa có hạn)")


def _plan_level(keeper: dict, donors: list, ch: _Changes, notes: list, conflicts: list) -> None:
    auth = [d for d in donors if _level_authoritative(d)]
    if _level_authoritative(keeper):
        for d in auth:
            if d["level_id"] != keeper["level_id"]:
                conflicts.append({"field": "level", "kept_job_id": keeper["job_id"],
                                  "kept": keeper.get("level_code"), "other_job_id": d["job_id"],
                                  "other": d.get("level_code")})
        return
    if not auth:
        return
    src = auth[0]
    takes_stamp = src.get("level_source") == "manual" and keeper.get("level_source") != "manual"
    if src["level_id"] != keeper.get("level_id") or takes_stamp:
        ch.set_block(LEVEL_COLUMNS, src)
        notes.append(f"level lấy từ {_short(src['job_id'])} (có người đặt): "
                     f"{keeper.get('level_code') or '(trống)'} -> {src.get('level_code')}")
    for d in auth[1:]:
        if d["level_id"] != src["level_id"]:
            conflicts.append({"field": "level", "kept_job_id": src["job_id"], "kept": src.get("level_code"),
                              "other_job_id": d["job_id"], "other": d.get("level_code")})


def _plan_notes(keeper: dict, donors: list, ch: _Changes, notes: list, conflicts: list) -> None:
    with_notes = [d for d in donors if d.get("has_notes")]
    if not with_notes:
        return
    kept_text = (keeper.get("ss_team_notes") or "").strip()
    if kept_text:
        kept_id, others = keeper["job_id"], with_notes
    else:
        src = with_notes[0]
        ch.set("ss_team_notes", src["ss_team_notes"])
        notes.append(f"ghi chú lấy từ {_short(src['job_id'])} (job giữ chưa có ghi chú)")
        kept_text, kept_id, others = src["ss_team_notes"].strip(), src["job_id"], with_notes[1:]
    for d in others:
        if d["ss_team_notes"].strip() != kept_text:
            conflicts.append({"field": "ss_team_notes", "kept_job_id": kept_id, "kept": kept_text,
                              "other_job_id": d["job_id"], "other": d["ss_team_notes"]})


def _plan_children(keeper: dict, donors: list) -> tuple:
    """(ChildPlan, số đơn ứng tuyển có CV bị bỏ). Xử lý theo thứ tự xếp hạng, và dòng đã quyết
    chuyển sang job giữ cũng chiếm chỗ, để hai job bị gộp cùng có một khoá không cùng chuyển."""
    plan = ChildPlan()
    urls = {x["source_url"] for x in keeper["logs"] if x["source_url"] is not None}
    saved_users = {x["ss_user_id"] for x in keeper["saved"]}
    app_users = {x["ss_user_id"] for x in keeper["applications"]}
    link_by_contact = {x["contact_id"]: x["link_id"] for x in keeper["links"]}
    cv_dropped = 0
    for d in donors:
        for x in d["logs"]:
            if x["source_url"] is not None and x["source_url"] in urls:
                plan.logs_drop.append(x["log_id"])
            else:
                plan.logs_move.append(x["log_id"])
                if x["source_url"] is not None:
                    urls.add(x["source_url"])
        for x in d["saved"]:
            if x["ss_user_id"] in saved_users:
                plan.saved_drop.append(x["saved_job_id"])
            else:
                plan.saved_move.append(x["saved_job_id"])
                saved_users.add(x["ss_user_id"])
        for x in d["applications"]:
            if x["ss_user_id"] in app_users:
                plan.apps_drop.append(x["application_id"])
                cv_dropped += 1 if x["has_cv"] else 0
            else:
                plan.apps_move.append(x["application_id"])
                app_users.add(x["ss_user_id"])
        for x in d["links"]:
            if x["contact_id"] in link_by_contact:
                plan.links_merge.append({"donor_link_id": x["link_id"],
                                         "keeper_link_id": link_by_contact[x["contact_id"]],
                                         "n_interactions": x["n_interactions"]})
            else:
                plan.links_move.append(x["link_id"])
                link_by_contact[x["contact_id"]] = x["link_id"]
    return plan, cv_dropped


def plan_merge(selection: Selection, details: dict) -> MergePlan:
    """Kế hoạch gộp MỘT nhóm. `details` = {job_id: dict} từ db.list_merge_job_details, phải có đủ
    mọi job của nhóm (nơi gọi kiểm tra trước). Không DB, không ghi."""
    g = selection.group
    members = [details[m["job_id"]] for m in g.members]
    ranked = dr.rank_members(members)
    keeper = details[selection.keeper_id]
    donors = [r for r in ranked if r["job_id"] != selection.keeper_id]

    ch = _Changes(keeper)
    notes: list = []
    conflicts: list = []
    _plan_salary(keeper, donors, ch, notes, conflicts)
    _plan_status_deadline_source(keeper, donors, ch, notes)
    _plan_level(keeper, donors, ch, notes, conflicts)
    _plan_notes(keeper, donors, ch, notes, conflicts)
    child, cv_dropped = _plan_children(keeper, donors)

    warnings = []
    if selection.keeper_source == KEEPER_FROM_FILE and g.keeper_id and g.keeper_id != keeper["job_id"]:
        warnings.append(f"job giữ {_short(keeper['job_id'])} do bạn chọn, khác đề xuất {_short(g.keeper_id)} (luật v0)")
    if g.tier != dr.TIER_STRICT:
        warnings.append(f"nhóm thuộc tầng '{g.tier}' ({dr.TIER_LABELS[g.tier]}), đã được duyệt tay")
    if cv_dropped:
        warnings.append(f"{cv_dropped} đơn ứng tuyển trùng người dùng sẽ bị bỏ và có đính CV "
                        "(file CV trong storage cần xử lý riêng)")
    return MergePlan(
        group=g, keeper_id=keeper["job_id"], keeper_source=selection.keeper_source,
        donor_ids=[d["job_id"] for d in donors], changes=ch.data, notes=notes, conflicts=conflicts,
        warnings=warnings, child=child, apps_with_cv_dropped=cv_dropped,
        expected={m["job_id"]: details[m["job_id"]] for m in g.members},
    )


# ----------------------------------------------------------------------
# Báo cáo
# ----------------------------------------------------------------------
class Summary:
    """Số liệu tổng hợp từ các kế hoạch + nhóm bị bỏ qua."""

    def __init__(self, plans: list, skipped: list, *, total_groups: int, only: bool,
                 unknown_ids: list, not_in_file: int, invalid_lines: list):
        self.total_groups = total_groups
        self.only = only
        self.unknown_ids = unknown_ids
        self.not_in_file = not_in_file
        self.invalid_lines = invalid_lines
        self.planned = len(plans)
        self.jobs_deleted = sum(len(p.donor_ids) for p in plans)
        self.skipped = Counter(reason for _, reason in skipped)
        self.skipped_total = len(skipped)
        self.field_counts: Counter = Counter()
        for p in plans:
            if any(c in p.changes for c in SALARY_COLUMNS):
                self.field_counts["lương lấy từ job bị gộp"] += 1
            if p.revives:
                self.field_counts["hồi sinh (job giữ CLOSED -> OPEN)"] += 1
            elif "deadline" in p.changes:
                self.field_counts["hạn nộp lấy từ job bị gộp"] += 1
            if any(c in p.changes for c in LEVEL_COLUMNS):
                self.field_counts["level lấy từ job có người đặt"] += 1
            if "ss_team_notes" in p.changes:
                self.field_counts["ghi chú lấy từ job bị gộp"] += 1
        self.conflicts: Counter = Counter(c["field"] for p in plans for c in p.conflicts)
        self.by_confidence: Counter = Counter(p.group.confidence for p in plans)
        self.by_tier: Counter = Counter(p.group.tier for p in plans)
        c = [p.child for p in plans]
        self.children = {
            "job_sources_log": (sum(len(x.logs_move) for x in c), sum(len(x.logs_drop) for x in c)),
            "saved_jobs": (sum(len(x.saved_move) for x in c), sum(len(x.saved_drop) for x in c)),
            "job_applications": (sum(len(x.apps_move) for x in c), sum(len(x.apps_drop) for x in c)),
        }
        self.links_move = sum(len(x.links_move) for x in c)
        self.links_merge = sum(len(x.links_merge) for x in c)
        self.interactions_moved = sum(m["n_interactions"] for x in c for m in x.links_merge)
        self.cv_dropped = sum(p.apps_with_cv_dropped for p in plans)
        self.with_warnings = sum(1 for p in plans if p.warnings)


def _print_plan(no: int, p: MergePlan) -> None:
    g = p.group
    print(f"\n  [{no}] {g.company_name[:50]} | {g.title[:70]}")
    print(f"      {g.size} job · tầng {g.tier} · độ chắc {g.confidence} · "
          f"GIỮ {_short(p.keeper_id)} ({p.keeper_source}) · GỘP VÀO RỒI XOÁ: "
          + ", ".join(_short(j) for j in p.donor_ids))
    for note in p.notes:
        print(f"      - {note}")
    cp = p.child
    moved = (f"log {len(cp.logs_move)}, lưu {len(cp.saved_move)}, đơn {len(cp.apps_move)}, "
             f"liên hệ {len(cp.links_move)}")
    dropped = (f"log {len(cp.logs_drop)}, lưu {len(cp.saved_drop)}, đơn {len(cp.apps_drop)}, "
               f"liên hệ dồn {len(cp.links_merge)}")
    print(f"      - dữ liệu con: chuyển [{moved}]; bỏ vì trùng [{dropped}]")
    for c in p.conflicts:
        print(f"      ! xung đột {c['field']}: giữ {c['kept']!r} (của {_short(c['kept_job_id'])}), "
              f"bỏ {c['other']!r} (của {_short(c['other_job_id'])}) -> ghi vào audit")
    for w in p.warnings:
        print(f"      ! {w}")


def print_report(summary: Summary, plans: list, skipped: list, *, show: int = DEFAULT_SHOW,
                 apply: bool = False) -> None:
    s = summary
    scope = "theo file --only (đã duyệt tay)" if s.only else "mặc định (chỉ nhóm độ chắc 'cao')"
    if apply:
        print("\n===== GỘP JOB TRÙNG — KẾ HOẠCH (sắp gộp thật vì có --apply) =====")
    else:
        print("\n===== GỘP JOB TRÙNG — CHẠY THỬ (không có --apply: KHÔNG ghi DB) =====")
    print(f"Phạm vi: {scope}")
    print(f"Nhóm nghi trùng trong DB: {s.total_groups}")
    if s.only:
        print(f"  không được nhắc trong file --only:   {s.not_in_file}")
    print(f"Nhóm SẼ gộp:   {s.planned}  (xoá {s.jobs_deleted} job phụ sau khi snapshot vào audit_logs)")
    if s.planned:
        print("  theo độ chắc: " + ", ".join(f"{k}={v}" for k, v in sorted(s.by_confidence.items())))
        print("  theo tầng:    " + ", ".join(f"{k}={v}" for k, v in sorted(s.by_tier.items())))
    print(f"Nhóm BỎ QUA:   {s.skipped_total}")
    for reason, n in s.skipped.most_common():
        print(f"  {n:>4}  {reason}")
    if s.unknown_ids:
        print(f"Job_id trong file --only không thuộc nhóm nghi trùng nào: {len(s.unknown_ids)}")
        for job_id in s.unknown_ids[:10]:
            print(f"    {job_id}")
    if s.invalid_lines:
        print(f"Dòng không đọc được trong file --only: {len(s.invalid_lines)}")
        for lineno, content in s.invalid_lines[:10]:
            print(f"    dòng {lineno}: {content}")

    if s.planned:
        print("\nHợp nhất trường lên job giữ (số nhóm):")
        for label in ("lương lấy từ job bị gộp", "hạn nộp lấy từ job bị gộp",
                      "hồi sinh (job giữ CLOSED -> OPEN)", "level lấy từ job có người đặt",
                      "ghi chú lấy từ job bị gộp"):
            print(f"  {label:<44} {s.field_counts[label]}")
        print("Xung đột (bản lệch bị bỏ lại, ghi vào audit): "
              + (", ".join(f"{k}={v}" for k, v in sorted(s.conflicts.items())) or "không có"))
        print("Dữ liệu con (chuyển sang job giữ / bỏ vì trùng khoá):")
        for table, (moved, dropped) in s.children.items():
            print(f"  {table:<28} chuyển {moved:<5} bỏ {dropped}")
        print(f"  {'job_contact_links':<28} chuyển {s.links_move:<5} dồn vào liên kết sẵn có {s.links_merge} "
              f"(kéo theo {s.interactions_moved} lượt trao đổi)")
        if s.cv_dropped:
            print(f"  LƯU Ý: {s.cv_dropped} đơn ứng tuyển bị bỏ có đính CV (file CV trong storage cần xử lý riêng)")
        print(f"Nhóm có cảnh báo: {s.with_warnings}")

        flagged = [p for p in plans if p.conflicts or p.warnings or p.child.apps_drop or p.child.links_merge]
        flagged_ids = {id(p) for p in flagged}
        rest = [p for p in plans if id(p) not in flagged_ids]
        for title, subset in (("cần chú ý (xung đột / cảnh báo / dữ liệu con trùng)", flagged),
                              ("còn lại", rest)):
            if subset and show > 0:
                print(f"\n--- Nhóm {title}: {len(subset)} (xem {min(show, len(subset))} nhóm đầu) ---")
                for no, p in enumerate(subset[:show], 1):
                    _print_plan(no, p)

    if not apply:
        print("\nĐây là CHẠY THỬ: chưa ghi gì, chưa xoá job nào. Thêm --apply để gộp thật "
              "(nên backup DB trước, và thử --limit 1 trước).")
        print("Dùng --csv FILE để xuất kế hoạch từng nhóm ra file duyệt.")


_CSV_HEADER = (
    "nhom", "tang", "do_chac", "cong_ty", "tieu_de", "job_giu", "nguon_chon_giu", "job_bi_gop",
    "thay_doi_truong", "xung_dot", "log_chuyen", "log_bo", "luu_chuyen", "luu_bo", "don_chuyen",
    "don_bo", "lien_he_chuyen", "lien_he_don_vao", "canh_bao",
)


def write_csv(plans: list, fh) -> int:
    """Ghi mỗi nhóm sẽ gộp một dòng ra `fh` (file mở sẵn, newline=''); trả số dòng."""
    writer = csv.writer(fh)
    writer.writerow(_CSV_HEADER)
    for no, p in enumerate(plans, 1):
        cp, g = p.child, p.group
        writer.writerow([
            no, g.tier, g.confidence, g.company_name, g.title, p.keeper_id, p.keeper_source,
            "; ".join(p.donor_ids), "; ".join(p.notes),
            "; ".join(f"{c['field']}: giữ {c['kept']!r} bỏ {c['other']!r}" for c in p.conflicts),
            len(cp.logs_move), len(cp.logs_drop), len(cp.saved_move), len(cp.saved_drop),
            len(cp.apps_move), len(cp.apps_drop), len(cp.links_move), len(cp.links_merge),
            "; ".join(p.warnings),
        ])
    return len(plans)


def export_csv(plans: list, path: str) -> int:
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:   # utf-8-sig: Excel đọc đúng tiếng Việt
        return write_csv(plans, fh)


# ----------------------------------------------------------------------
# Chạy
# ----------------------------------------------------------------------
# Migration phải có trước khi chạy: (cả chạy thử lẫn --apply) / (riêng --apply).
_REQUIRED_MIGRATIONS = (
    "migration_add_job_level_source.sql",
    "migration_add_job_level_signals.sql",
)
_REQUIRED_MIGRATIONS_FOR_APPLY = _REQUIRED_MIGRATIONS + (
    "migration_add_skip_updated_at_flag.sql",
    "migration_add_merge_job_audit_action.sql",
)

EXIT_OK = 0
EXIT_REFUSED = 1        # chưa sẵn sàng / bị từ chối / người dùng huỷ
EXIT_PARTIAL = 2        # đã chạy nhưng có nhóm stale hoặc lỗi (các nhóm khác vẫn đã gộp)
EXIT_INTERRUPTED = 130  # Ctrl+C (nhóm đã gộp xong vẫn được giữ)


def build_plans(conn, selections: list) -> tuple:
    """(plans, skipped_thêm): đọc chi tiết các job của nhóm được chọn rồi lập kế hoạch. Nhóm có
    job biến mất giữa hai lần đọc thì bỏ qua với lý do SKIP_VANISHED."""
    ids = [m["job_id"] for sel in selections for m in sel.group.members]
    details = db.list_merge_job_details(conn, ids)
    plans, skipped = [], []
    for sel in selections:
        if any(m["job_id"] not in details for m in sel.group.members):
            skipped.append((sel.group, SKIP_VANISHED))
            continue
        plans.append(plan_merge(sel, details))
    return plans, skipped


def _check_ready(conn, *, apply: bool) -> Optional[str]:
    """Thông báo lỗi (tiếng Việt) nếu DB chưa sẵn sàng, None nếu ổn."""
    pending = set(db.list_pending_migrations(conn))
    conn.commit()
    required = _REQUIRED_MIGRATIONS_FOR_APPLY if apply else _REQUIRED_MIGRATIONS
    missing = [m for m in required if m in pending]
    if missing:
        return ("DB chưa áp dụng migration cần thiết: " + ", ".join(missing)
                + ". Chạy `python main.py migrate` trước.")
    if apply and not db.skip_updated_at_supported(conn):
        return ("Hàm trigger trg_set_updated_at() trong DB chưa biết cờ app.skip_updated_at, gộp bây giờ sẽ "
                "làm updated_at của các job nhảy. Áp dụng sql/migration_add_skip_updated_at_flag.sql "
                "(vd chạy `python main.py migrate`) rồi thử lại.")
    if apply and not db.merge_job_enum_supported(conn):
        return ("audit_action_enum chưa có giá trị MERGE_JOB nên không ghi được lịch sử gộp. Áp dụng "
                "sql/migration_add_merge_job_audit_action.sql (vd chạy `python main.py migrate`) rồi thử lại.")
    return None


def _describe_runs(runs: list) -> str:
    return "; ".join(f"{r['kind']} {r['label']} ({r['status']}, {r['age_minutes']} phút, {r['run_id'][:8]})"
                     for r in runs)


def _print_apply_intro(plans: list) -> None:
    jobs = sum(len(p.donor_ids) for p in plans)
    print(f"\n⚠️  SẮP GỘP THẬT {len(plans)} nhóm, XOÁ THẬT {jobs} job phụ khỏi job_postings.")
    print("   Mỗi job phụ được chụp nguyên dòng vào audit_logs (MERGE_JOB) trước khi xoá, nhưng "
          "CHƯA có lệnh khôi phục tự động (xem README).")
    print("   Hãy chắc đã backup DB (pg_dump hoặc snapshot) và KHÔNG crawl trong lúc gộp.")


def _ask_confirm(confirm: Callable[[str], str]) -> bool:
    try:
        return confirm("Gõ 'yes' để xác nhận gộp thật: ").strip().lower() == "yes"
    except EOFError:      # chạy không có người gõ (cron, pipe) mà quên --yes
        return False


@dataclass
class ApplyResult:
    merged: list = field(default_factory=list)      # [(MergePlan, kết quả của db.merge_job_group)]
    stale: list = field(default_factory=list)       # [(MergePlan, lý do)]
    failed: list = field(default_factory=list)      # [(MergePlan, lỗi)]
    aborted: Optional[str] = None                   # lý do dừng sớm (crawl bắt đầu, mất kết nối)
    not_run: int = 0                                # số nhóm chưa chạy do dừng sớm


def _apply_plans(conn, plans: list, *, force: bool, actor_id: Optional[str] = None) -> ApplyResult:
    """Gộp lần lượt từng nhóm, mỗi nhóm một transaction. Nhóm stale/lỗi bị bỏ qua, chạy tiếp."""
    result = ApplyResult()
    total = len(plans)
    for no, p in enumerate(plans, 1):
        if not force:
            runs = db.list_active_runs(conn)
            if runs:
                result.aborted = "có crawl/bảo trì bắt đầu chạy giữa lúc gộp: " + _describe_runs(runs)
                result.not_run = total - no + 1
                break
        label = f"[{no}/{total}] {p.group.company_name[:40]} | {p.group.title[:50]}"
        try:
            res = db.merge_job_group(
                conn, keeper_id=p.keeper_id, donor_ids=p.donor_ids, expected=p.expected,
                changes=p.changes, child=dataclasses.asdict(p.child), conflicts=p.conflicts,
                notes=p.notes, actor_id=actor_id)
            conn.commit()
        except db.MergeStaleError as exc:
            conn.rollback()
            result.stale.append((p, str(exc)))
            print(f"  {label}: BỎ QUA (stale) — {exc}")
        except Exception as exc:  # noqa: BLE001 - mọi lỗi của một nhóm không được chặn các nhóm sau
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 - kết nối đã hỏng
                pass
            result.failed.append((p, f"{type(exc).__name__}: {exc}"))
            logger.error("Lỗi khi gộp nhóm %s (đã rollback nhóm này): %s", _short(p.keeper_id), exc)
            print(f"  {label}: LỖI — {type(exc).__name__}: {exc}")
            if getattr(conn, "closed", 0):
                result.aborted = "mất kết nối tới DB"
                result.not_run = total - no
                break
        else:
            result.merged.append((p, res))
            print(f"  {label}: đã gộp, giữ {_short(p.keeper_id)}, xoá {len(p.donor_ids)} job phụ")
    return result


def print_apply_result(result: ApplyResult, *, planned: int, jobs_before: int, jobs_after: int,
                       groups_before: int, groups_after: int, show: int = DEFAULT_SHOW) -> None:
    r = result
    print("\n===== KẾT QUẢ GỘP THẬT =====")
    print(f"Nhóm trong kế hoạch: {planned}")
    print(f"  đã gộp:                  {len(r.merged)}  (xoá {sum(x['donors_deleted'] for _, x in r.merged)} job phụ)")
    print(f"  bỏ qua vì dữ liệu đã đổi (stale): {len(r.stale)}")
    print(f"  lỗi (đã rollback nhóm đó):        {len(r.failed)}")
    if r.aborted:
        print(f"  CHƯA CHẠY do dừng sớm:    {r.not_run}  ({r.aborted})")
    for title, rows in (("stale (chạy lại lệnh để lập kế hoạch mới)", r.stale), ("lỗi", r.failed)):
        if rows:
            print(f"\nNhóm {title}:")
            for p, reason in rows[:show if show > 0 else None]:
                print(f"  giữ {_short(p.keeper_id)} | {p.group.company_name[:40]} | {p.group.title[:50]}: {reason}")
            if show > 0 and len(rows) > show:
                print(f"  ... và {len(rows) - show} nhóm nữa")
    if r.merged:
        totals: Counter = Counter()
        for _, x in r.merged:
            for table, (moved, dropped) in x["children"].items():
                totals[f"{table}.chuyển"] += moved
                totals[f"{table}.bỏ/dồn"] += dropped
        print("\nDữ liệu con (đã chuyển sang job giữ / đã bỏ vì trùng khoá, với liên hệ: số liên kết dồn):")
        for table in ("job_sources_log", "saved_jobs", "job_applications", "job_contact_links"):
            print(f"  {table:<20} chuyển {totals[table + '.chuyển']:<5} bỏ/dồn {totals[table + '.bỏ/dồn']}")
        print(f"  lượt trao đổi liên hệ đã dồn: {sum(x['interactions_moved'] for _, x in r.merged)}")
        n_mismatch = sum(x["link_status_conflicts"] for _, x in r.merged)
        if n_mismatch:
            print(f"  liên kết liên hệ có interaction_status lệch (giữ của job giữ, bản lệch ghi trong audit): {n_mismatch}")
        cv = sum(x["cv_dropped"] for _, x in r.merged)
        if cv:
            print(f"  LƯU Ý: {cv} đơn ứng tuyển trùng bị bỏ có đính CV. File CV trong storage KHÔNG bị xoá; "
                  "đường dẫn nằm trong audit_logs (changes.snapshot.job_applications.dropped[].cv_url).")
        print(f"Dòng audit_logs MERGE_JOB đã ghi: {sum(len(x['log_ids']) for _, x in r.merged)}")
    print(f"\nSố job trong DB:                       trước = {jobs_before}, sau = {jobs_after}")
    print(f"Nhóm nghi trùng (báo cáo 3a):          trước = {groups_before}, sau = {groups_after}")
    print("updated_at của các job giữ không đổi (cờ app.skip_updated_at).")
    if r.stale or r.failed or r.aborted:
        print("\nCó nhóm chưa gộp. Chạy lại cùng lệnh để xử lý tiếp (nhóm đã gộp sẽ không còn trong kế hoạch).")


def run(conn, *, only: Optional[OnlySpec] = None, show: int = DEFAULT_SHOW,
        csv_path: Optional[str] = None, apply: bool = False, limit: Optional[int] = None,
        yes: bool = False, force: bool = False, confirm: Callable[[str], str] = input,
        actor_id: Optional[str] = None) -> int:
    """Chạy lệnh trên một kết nối. Không `apply` thì chỉ SELECT. Trả exit code (xem EXIT_*)."""
    error = _check_ready(conn, apply=apply)
    if error:
        print(f"❌ {error}")
        return EXIT_REFUSED
    if apply:
        runs = db.list_active_runs(conn)
        if runs and not force:
            print("❌ Đang có crawl/bảo trì chạy, từ chối gộp để tránh sinh thêm job trùng giữa chừng:")
            for r in runs:
                print(f"   - {r['kind']} {r['label']} ({r['status']}, {r['age_minutes']} phút, run {r['run_id'][:8]})")
            print("   Đợi chúng xong rồi chạy lại. Dòng 'running' đã treo từ lâu có thể là lượt bị chết dở "
                  "(API tự dọn sau ~30 phút). Dùng --force để bỏ qua kiểm tra này (KHÔNG dừng crawl nào).")
            return EXIT_REFUSED
        if runs:
            print(f"⚠️  --force: bỏ qua kiểm tra crawl/bảo trì đang chạy ({_describe_runs(runs)}). "
                  "Lệnh này KHÔNG dừng chúng.")

    rows = db.list_duplicate_job_rows(conn)
    groups = dr.build_groups(rows)
    selections, skipped, unknown, not_in_file = select_groups(groups, only)
    plans, skipped_more = build_plans(conn, selections)
    skipped = skipped + skipped_more
    summary = Summary(plans, skipped, total_groups=len(groups), only=only is not None,
                      unknown_ids=unknown, not_in_file=not_in_file,
                      invalid_lines=only.invalid_lines if only else [])
    print_report(summary, plans, skipped, show=show, apply=apply)
    if csv_path:
        n = export_csv(plans, csv_path)
        print(f"\nĐã xuất kế hoạch {n} nhóm ra {csv_path}")
    if not apply:
        return EXIT_OK

    if limit is not None:
        if len(plans) > limit:
            print(f"\n--limit {limit}: chỉ gộp {limit} nhóm đầu của kế hoạch ({len(plans) - limit} nhóm để lần sau).")
        plans = plans[:limit]
    if not plans:
        print("\nKhông có nhóm nào để gộp.")
        return EXIT_OK
    _print_apply_intro(plans)
    if yes:
        print("--yes: bỏ qua bước hỏi xác nhận.")
    elif not _ask_confirm(confirm):
        print("Đã huỷ, không thay đổi gì.")
        return EXIT_REFUSED

    jobs_before = db.count_jobs(conn)
    conn.rollback()
    print("\nĐang gộp:")
    try:
        result = _apply_plans(conn, plans, force=force, actor_id=actor_id)
    except KeyboardInterrupt:
        try:
            conn.rollback()
        except Exception:  # noqa: BLE001
            pass
        print("\n⛔ Bị ngắt. Nhóm đang gộp dở đã rollback; các nhóm đã gộp xong trước đó vẫn được giữ. "
              "Chạy lại lệnh để tiếp tục.")
        return EXIT_INTERRUPTED
    jobs_after = db.count_jobs(conn)
    conn.rollback()
    groups_after = len(dr.build_groups(db.list_duplicate_job_rows(conn)))
    print_apply_result(result, planned=len(plans), jobs_before=jobs_before, jobs_after=jobs_after,
                       groups_before=len(groups), groups_after=groups_after, show=show)
    return EXIT_PARTIAL if (result.stale or result.failed or result.aborted) else EXIT_OK


def run_cli(args) -> int:
    """Điểm vào cho `python main.py merge-duplicates` (args từ argparse trong main.py)."""
    if args.show < 0:
        print("❌ --show phải >= 0.")
        return EXIT_REFUSED
    apply = bool(getattr(args, "apply", False))
    limit = getattr(args, "limit", None)
    if limit is not None and limit < 1:
        print("❌ --limit phải >= 1.")
        return EXIT_REFUSED
    for flag in ("limit", "yes", "force"):
        if getattr(args, flag, None) not in (None, False) and not apply:
            print(f"❌ --{flag} chỉ dùng được cùng --apply (không có --apply lệnh chỉ chạy thử).")
            return EXIT_REFUSED
    only = None
    if args.only:
        try:
            with open(args.only, "r", encoding="utf-8") as fh:
                only = parse_only(fh.read())
        except OSError as exc:
            print(f"❌ Không đọc được file --only {args.only}: {exc}")
            return EXIT_REFUSED
        if not only.job_ids:
            print(f"❌ File --only {args.only} không có job_id hợp lệ nào, dừng để tránh hiểu nhầm "
                  "thành 'gộp tất cả'.")
            return EXIT_REFUSED
    conn = db.get_connection()
    try:
        return run(conn, only=only, show=args.show, csv_path=args.csv, apply=apply, limit=limit,
                   yes=bool(getattr(args, "yes", False)), force=bool(getattr(args, "force", False)))
    finally:
        conn.close()
