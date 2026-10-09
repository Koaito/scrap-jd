"""
Báo cáo job nghi trùng — `python main.py report-duplicates` (Phần 3a). CHỈ ĐỌC: không ghi
DB, không đổi schema, không cần migration mới.

MỤC ĐÍCH: có số liệu thật để quyết luật chọn job giữ khi gộp job trùng (Phần 3b) trước khi
viết bất kỳ lệnh ghi nào. Báo cáo KHÔNG gộp, KHÔNG xoá, KHÔNG đổi gì.

NHÓM NGHI TRÙNG: các job cùng company_id và cùng tiêu đề chuẩn hoá (lower + gộp khoảng
trắng, đúng công thức của job_dedup_key). Khoá chống trùng của job (dedup_key, A3) là công ty
+ tiêu đề + tỉnh, không level; v_duplicate_job_candidates gom đúng theo khoá đó. Báo cáo này
gom rộng hơn một bậc (bỏ cả tỉnh) để thấy thêm các cặp khác tỉnh, xếp riêng ở tầng "province".
Mọi trạng thái job (OPEN/CLOSED) đều được xét, giống view.

PHÂN LOẠI (mỗi nhóm):
  - tầng (tier): strict = cùng tỉnh và cùng level; level = cùng tỉnh, khác level (strict và
    level đều là cùng dedup_key, chính là khoá mà pipeline coi là "đăng lại"); province = khác
    tỉnh (khác dedup_key, dễ là tin riêng của từng chi nhánh, không đề xuất gộp).
  - độ chắc: cao / cần xem / thấp, theo tầng, có chung URL nguồn không, có >= 2 tin cùng
    đang OPEN không (xem confidence()).
  - dữ liệu cần bảo vệ khi gộp: job từng có người sửa, ghi chú, đơn ứng tuyển, lượt lưu,
    liên hệ.
  - ĐỀ XUẤT job giữ (luật v0, chỉ để xem, CHƯA là luật đã chốt): job có dữ liệu cần bảo vệ >
    job OPEN > hạn nộp muộn hơn > tạo sớm hơn. Nhóm khác tỉnh không có đề xuất.

Logic quyết định là hàm THUẦN (không DB), có test ở tests/test_duplicate_report.py. Phần SQL
nằm ở db/job_duplicates.py.
"""

import csv
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import db

logger = logging.getLogger(__name__)

TIER_STRICT = "strict"      # cùng tỉnh, cùng level
TIER_LEVEL = "level"        # cùng tỉnh, khác level (vẫn cùng dedup_key)
TIER_PROVINCE = "province"  # khác tỉnh (khác dedup_key)

CONF_HIGH = "cao"
CONF_REVIEW = "cần xem"
CONF_LOW = "thấp"

TIER_LABELS = {
    TIER_STRICT: "cùng tỉnh + cùng level",
    TIER_LEVEL: "cùng tỉnh, KHÁC level",
    TIER_PROVINCE: "KHÁC tỉnh",
}
_CONF_ORDER = {CONF_HIGH: 0, CONF_REVIEW: 1, CONF_LOW: 2}
_TIER_ORDER = {TIER_STRICT: 0, TIER_LEVEL: 1, TIER_PROVINCE: 2}

DEFAULT_SHOW = 5  # số nhóm in chi tiết cho MỖI mức độ chắc


# ----------------------------------------------------------------------
# Logic thuần (không DB, không mạng) — có test
# ----------------------------------------------------------------------
def site_of_url(url: Optional[str]) -> str:
    """Tên miền (bỏ 'www.') của một URL nguồn, '?' nếu không có. Dùng để biết hai job đến từ
    cùng một trang hay khác trang. Không khai báo danh sách nguồn riêng để khỏi lệch với
    sources_registry khi thêm nguồn mới."""
    host = (urlparse(url or "").hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or "?"


def member_urls(row: dict) -> set:
    """Mọi URL nguồn đã biết của một job: job_postings.source_url và job_sources_log."""
    urls = set(row.get("log_urls") or [])
    if row.get("source_url"):
        urls.add(row["source_url"])
    return urls


def primary_site(row: dict) -> str:
    """Trang của job_postings.source_url (URL job đang trỏ tới, có thể đã đổi sang tin đăng
    lại, xem db.link_repost_source; không phải nguồn gốc bất biến); thiếu thì lấy URL log
    đầu tiên theo thứ tự chữ cái."""
    url = row.get("source_url") or next(iter(sorted(row.get("log_urls") or [])), None)
    return site_of_url(url)


def protection_labels(row: dict) -> list:
    """Những dữ liệu do người dùng/nhân viên tạo đang gắn vào job (việc gộp phải giữ lại)."""
    out = []
    if row.get("has_editor"):
        out.append("người sửa")
    if row.get("has_notes"):
        out.append("ghi chú")
    if row.get("n_applications"):
        out.append(f"ứng tuyển {row['n_applications']}")
    if row.get("n_saved"):
        out.append(f"đã lưu {row['n_saved']}")
    if row.get("n_contact_links"):
        out.append(f"liên hệ {row['n_contact_links']}")
    return out


def is_protected(row: dict) -> bool:
    return bool(protection_labels(row))


def classify_tier(members: list) -> str:
    """strict: một tỉnh + một level; level: một tỉnh, nhiều level; province: nhiều tỉnh.
    (Tỉnh trống được coi là một giá trị.)"""
    provinces = {r.get("province_id") for r in members}
    if len(provinces) > 1:
        return TIER_PROVINCE
    levels = {r.get("level_code") for r in members}
    return TIER_STRICT if len(levels) == 1 else TIER_LEVEL


def has_shared_url(members: list) -> bool:
    """Có URL nguồn nào xuất hiện ở >= 2 job trong nhóm không (cùng một tin gắn vào hai job:
    bằng chứng trùng mạnh nhất)."""
    seen: Counter = Counter()
    for r in members:
        for url in member_urls(r):
            seen[url] += 1
    return any(n >= 2 for n in seen.values())


def confidence(tier: str, open_count: int, shared_url: bool) -> str:
    """Độ chắc là trùng thật.
      - khác tỉnh: thấp (dễ là tin riêng từng chi nhánh); nếu còn chung URL nguồn thì cần xem;
      - chung URL nguồn: cao;
      - khác level: cần xem (level gán sai/đổi theo thời gian, hoặc hai vị trí khác cấp);
      - cùng tỉnh và cùng level: cao nếu <= 1 tin đang mở (đăng lại / chéo nguồn, đúng loại pipeline
        đã coi là một job); cần xem nếu >= 2 tin cùng đang mở (có thể là hai vị trí khác nhau)."""
    if tier == TIER_PROVINCE:
        return CONF_REVIEW if shared_url else CONF_LOW
    if shared_url:
        return CONF_HIGH
    if tier == TIER_LEVEL:
        return CONF_REVIEW
    return CONF_REVIEW if open_count >= 2 else CONF_HIGH


# Thứ tự tiêu chí chọn job giữ (luật v0, đề xuất). Mỗi phần tử khoá sắp xếp ứng với một nhãn.
_KEEPER_CRITERIA = (
    "có dữ liệu cần bảo vệ (người sửa/ghi chú/ứng tuyển/lưu/liên hệ)",
    "đang OPEN",
    "hạn nộp muộn hơn",
    "tạo sớm hơn",
)


def _keeper_key(row: dict) -> tuple:
    deadline = row.get("deadline")
    return (
        0 if is_protected(row) else 1,
        0 if row.get("job_status") == "OPEN" else 1,
        -deadline.toordinal() if deadline else 1,  # hạn muộn trước, không có hạn ở cuối
        row["created_at"],
        row["job_id"],
    )


def propose_keeper(members: list) -> tuple:
    """(job_id đề xuất giữ, lý do) theo luật v0. Lý do là tiêu chí đầu tiên làm job này hơn
    job đứng thứ hai. Nhóm chỉ có hai job giống hệt mọi tiêu chí: lấy job tạo sớm hơn."""
    ranked = sorted(members, key=_keeper_key)
    first, second = ranked[0], ranked[1]
    k1, k2 = _keeper_key(first), _keeper_key(second)
    for i, label in enumerate(_KEEPER_CRITERIA):
        if k1[i] != k2[i]:
            return first["job_id"], label
    return first["job_id"], "giống nhau mọi tiêu chí (kể cả thời điểm tạo), lấy theo job_id cho ổn định"


def rank_members(members: list) -> list:
    """Các job của một nhóm theo luật chọn giữ v0, tốt nhất (nên giữ) trước. Dùng chung với
    merge_duplicates.py (Phần 3b) để thứ tự job giữ / job bị gộp luôn khớp báo cáo."""
    return sorted(members, key=_keeper_key)


@dataclass
class Group:
    company_id: str
    company_name: str
    title: str
    members: list
    tier: str
    confidence: str
    open_count: int
    shared_url: bool
    cross_source: bool
    protected_count: int
    keeper_id: Optional[str]
    keeper_why: str
    reasons: list = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def needs_manual_pick(self) -> bool:
        """>= 2 job đều có dữ liệu cần bảo vệ: gộp sẽ phải chuyển/đối chiếu dữ liệu của cả hai."""
        return self.protected_count >= 2


def analyze_group(members: list) -> Group:
    """Phân loại một nhóm (>= 2 job cùng company_id + norm_title) từ các dict của
    db.list_duplicate_job_rows."""
    members = sorted(members, key=lambda r: (r["created_at"], r["job_id"]))
    tier = classify_tier(members)
    open_count = sum(1 for r in members if r.get("job_status") == "OPEN")
    shared = has_shared_url(members)
    sites = sorted({primary_site(r) for r in members})
    cross = len(sites) >= 2
    protected_count = sum(1 for r in members if is_protected(r))
    keeper_id, keeper_why = (None, "") if tier == TIER_PROVINCE else propose_keeper(members)

    reasons = [TIER_LABELS[tier]]
    if shared:
        reasons.append("chung URL nguồn")
    if cross:
        reasons.append("khác nguồn: " + ", ".join(sites))
    else:
        reasons.append(f"cùng nguồn: {sites[0]}")
    if open_count >= 2:
        reasons.append(f"{open_count} tin cùng đang OPEN")
    elif open_count == 1:
        reasons.append("1 tin OPEN, còn lại đã đóng (dạng đăng lại)")
    else:
        reasons.append("tất cả đã CLOSED")
    return Group(
        company_id=members[0]["company_id"], company_name=members[0].get("company_name") or "",
        title=members[0]["job_title"], members=members, tier=tier,
        confidence=confidence(tier, open_count, shared), open_count=open_count, shared_url=shared,
        cross_source=cross, protected_count=protected_count, keeper_id=keeper_id,
        keeper_why=keeper_why, reasons=reasons,
    )


def build_groups(rows: list) -> list:
    """Gom các dòng thành nhóm nghi trùng (>= 2 job cùng company_id + norm_title). Sắp theo độ
    chắc (cao trước), rồi tầng, rồi nhóm lớn trước, rồi theo tên công ty/tiêu đề cho ổn định."""
    by_key: dict = {}
    for r in rows:
        by_key.setdefault((r["company_id"], r["norm_title"]), []).append(r)
    groups = [analyze_group(m) for m in by_key.values() if len(m) >= 2]
    groups.sort(key=lambda g: (_CONF_ORDER[g.confidence], _TIER_ORDER[g.tier], -g.size,
                               g.company_name, g.title))
    return groups


def count_key_groups(rows: list) -> int:
    """Số dedup_key có >= 2 job trong `rows`: phải bằng số dòng của
    v_duplicate_job_candidates (dùng để đối chiếu hai cách đếm)."""
    return sum(1 for n in Counter(r["dedup_key"] for r in rows).values() if n >= 2)


class Summary:
    """Số liệu tổng hợp từ danh sách nhóm để in báo cáo."""

    def __init__(self, groups: list, *, total_jobs: int, jobs_in_groups: int,
                 key_groups: int, view_groups: Optional[int]):
        self.total_jobs = total_jobs
        self.jobs_in_groups = jobs_in_groups
        self.groups = len(groups)
        self.key_groups = key_groups
        self.view_groups = view_groups
        self.by_tier: Counter = Counter(g.tier for g in groups)
        self.by_conf: Counter = Counter(g.confidence for g in groups)
        self.by_size: Counter = Counter(g.size for g in groups)
        self.shared_url = sum(1 for g in groups if g.shared_url)
        self.cross_source = sum(1 for g in groups if g.cross_source)
        self.multi_open = sum(1 for g in groups if g.open_count >= 2)
        self.single_open = sum(1 for g in groups if g.open_count == 1)
        self.all_closed = sum(1 for g in groups if g.open_count == 0)
        self.with_protected = sum(1 for g in groups if g.protected_count >= 1)
        self.need_manual = sum(1 for g in groups if g.needs_manual_pick)
        self.no_proposal = sum(1 for g in groups if g.keeper_id is None)
        self.extra_jobs = sum(g.size - 1 for g in groups)  # job "dư" nếu mỗi nhóm giữ một


# ----------------------------------------------------------------------
# Báo cáo
# ----------------------------------------------------------------------
def _short(job_id) -> str:
    return str(job_id)[:8]


def _day(value) -> str:
    return str(value)[:10] if value else "—"


def _salary(row: dict) -> str:
    lo, hi = row.get("salary_min"), row.get("salary_max")
    if lo is None and hi is None:
        return "—"
    return f"{lo if lo is not None else '?'}–{hi if hi is not None else '?'}"


def _print_group(no: int, g: Group) -> None:
    print(f"\n  [{no}] {g.company_name[:50]} | {g.title[:70]}")
    print(f"      {g.size} job · tầng: {g.tier} · độ chắc: {g.confidence} · " + "; ".join(g.reasons[1:]))
    if g.needs_manual_pick:
        print(f"      LƯU Ý: {g.protected_count} job đều có dữ liệu cần bảo vệ, gộp phải chuyển dữ liệu của cả hai.")
    for r in g.members:
        mark = "GIỮ?" if r["job_id"] == g.keeper_id else "    "
        prot = ", ".join(protection_labels(r)) or "không"
        print(f"      {mark} {_short(r['job_id'])} {(r.get('job_status') or '?'):<6} {(r.get('level_code') or '-'):<8} "
              f"{(r.get('province_name') or '-')[:14]:<14} tạo {_day(r.get('created_at'))} "
              f"hạn {_day(r.get('deadline'))} lương {_salary(r)} · {primary_site(r)} · bảo vệ: {prot}")
    if g.keeper_id:
        print(f"      Đề xuất giữ {_short(g.keeper_id)} (luật v0, chưa chốt): {g.keeper_why}")
    else:
        print("      Không đề xuất gộp (khác tỉnh: nhiều khả năng là tin riêng của từng chi nhánh).")


def print_report(summary: Summary, groups: list, *, show: int = DEFAULT_SHOW) -> None:
    s = summary
    print("\n===== BÁO CÁO JOB NGHI TRÙNG (chỉ đọc, không ghi DB) =====")
    print(f"Tổng job trong DB: {s.total_jobs}")
    print(f"Nhóm nghi trùng (cùng công ty + tiêu đề chuẩn hoá): {s.groups}, gồm {s.jobs_in_groups} job "
          f"(nếu mỗi nhóm giữ một thì dư {s.extra_jobs} job)")
    if s.view_groups is None:
        print(f"  Đối chiếu view v_duplicate_job_candidates: không đọc được (tính lại: {s.key_groups} nhóm cùng khoá)")
    else:
        status = "khớp" if s.view_groups == s.key_groups else "LỆCH (có thể do job đổi giữa hai lần đọc, chạy lại)"
        print(f"  Đối chiếu view v_duplicate_job_candidates: view = {s.view_groups}, "
              f"tính lại ở đây = {s.key_groups} nhóm cùng khoá ({status})")

    print("\nTheo tầng:")
    for tier in (TIER_STRICT, TIER_LEVEL, TIER_PROVINCE):
        print(f"  {TIER_LABELS[tier]:<44} {s.by_tier[tier]}")
    print("Theo độ chắc là trùng thật:")
    for conf in (CONF_HIGH, CONF_REVIEW, CONF_LOW):
        print(f"  {conf:<44} {s.by_conf[conf]}")
    print("Theo vòng đời:")
    print(f"  {'đúng 1 tin OPEN, còn lại đã đóng (đăng lại)':<44} {s.single_open}")
    print(f"  {'>= 2 tin cùng đang OPEN':<44} {s.multi_open}")
    print(f"  {'tất cả đã CLOSED':<44} {s.all_closed}")
    print("Bằng chứng khác:")
    print(f"  {'khác nguồn (nhiều trang)':<44} {s.cross_source}")
    print(f"  {'chung URL nguồn':<44} {s.shared_url}")
    print("Dữ liệu cần bảo vệ khi gộp:")
    print(f"  {'nhóm có >= 1 job có dữ liệu cần bảo vệ':<44} {s.with_protected}")
    print(f"  {'nhóm có >= 2 job như vậy (phải chọn tay)':<44} {s.need_manual}")
    print("Kích thước nhóm: " + ", ".join(f"{size} job x {n}" for size, n in sorted(s.by_size.items())))

    for conf in (CONF_HIGH, CONF_REVIEW, CONF_LOW):
        subset = [g for g in groups if g.confidence == conf]
        if not subset or show <= 0:
            continue
        print(f"\n--- Độ chắc \"{conf}\": {len(subset)} nhóm (xem {min(show, len(subset))} nhóm đầu) ---")
        for no, g in enumerate(subset[:show], 1):
            _print_group(no, g)

    print("\nBáo cáo này CHỈ ĐỌC. 'GIỮ?' là đề xuất theo luật v0 để bạn xem, chưa phải luật đã chốt và "
          "chưa có lệnh nào gộp hay xoá job.")
    print("Dùng --csv FILE để xuất toàn bộ nhóm ra file duyệt tay.")


_CSV_HEADER = (
    "nhom", "tang", "do_chac", "de_xuat_giu", "ly_do_nhom", "cong_ty", "tieu_de", "job_id", "level",
    "tinh", "trang_thai", "ngay_tao", "han_nop", "luong_min", "luong_max", "nguon", "so_url_nguon",
    "nguoi_sua", "ghi_chu", "ung_tuyen", "da_luu", "lien_he",
)


def write_csv(groups: list, fh) -> int:
    """Ghi mọi nhóm ra `fh` (file mở sẵn, newline=''), mỗi job một dòng; trả số dòng job."""
    writer = csv.writer(fh)
    writer.writerow(_CSV_HEADER)
    n = 0
    for no, g in enumerate(groups, 1):
        for r in g.members:
            writer.writerow([
                no, g.tier, g.confidence, "x" if r["job_id"] == g.keeper_id else "", "; ".join(g.reasons),
                g.company_name, r["job_title"], r["job_id"], r.get("level_code") or "",
                r.get("province_name") or "", r.get("job_status") or "", _day(r.get("created_at")),
                _day(r.get("deadline")) if r.get("deadline") else "",
                "" if r.get("salary_min") is None else r["salary_min"],
                "" if r.get("salary_max") is None else r["salary_max"],
                primary_site(r), len(member_urls(r)),
                "x" if r.get("has_editor") else "", "x" if r.get("has_notes") else "",
                r.get("n_applications") or 0, r.get("n_saved") or 0, r.get("n_contact_links") or 0,
            ])
            n += 1
    return n


def export_csv(groups: list, path: str) -> int:
    # utf-8-sig: Excel mở tiếng Việt đúng dấu.
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        return write_csv(groups, fh)


# ----------------------------------------------------------------------
# Chạy
# ----------------------------------------------------------------------
def run(conn, *, show: int = DEFAULT_SHOW, csv_path: Optional[str] = None) -> int:
    """Chạy báo cáo trên một kết nối. Chỉ SELECT. Trả exit code (0 = xong)."""
    rows = db.list_duplicate_job_rows(conn)
    total_jobs = db.count_jobs(conn)
    conn.rollback()
    view_groups = db.count_duplicate_job_groups(conn)

    groups = build_groups(rows)
    summary = Summary(groups, total_jobs=total_jobs, jobs_in_groups=len(rows),
                      key_groups=count_key_groups(rows), view_groups=view_groups)
    print_report(summary, groups, show=show)
    if csv_path:
        n = export_csv(groups, csv_path)
        print(f"\nĐã xuất {n} dòng job ({len(groups)} nhóm) ra {csv_path}")
    return 0


def run_cli(args) -> int:
    """Điểm vào cho `python main.py report-duplicates` (args từ argparse trong main.py)."""
    if args.show < 0:
        print("❌ --show phải >= 0.")
        return 1
    conn = db.get_connection()
    try:
        return run(conn, show=args.show, csv_path=args.csv)
    finally:
        conn.close()
