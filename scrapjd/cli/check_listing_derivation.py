"""
So giá trị job SUY RA từ listing với giá trị job ĐANG LƯU — `python main.py check-listing-derivation`
(C2, nửa 1/2). CHỈ ĐỌC: không ghi DB, không đổi schema, không cần migration.

MỤC ĐÍCH: nhóm C đổi job thành giá trị tổng hợp từ listing (db/job_derivation.py định nghĩa phép tổng
hợp). Trước khi cho code GHI giá trị suy ra vào job (C2 nửa 2/2) và trước khi gỡ các ca đặc biệt (C4, đã xong), cần
bằng chứng bằng dữ liệu thật rằng hai bên khớp, và nếu lệch thì lệch ở đâu, vì sao. Lệnh này in số liệu đó.

So bốn trường: job_status, closed_reason (chỉ khi cả hai bên CLOSED), deadline, source_url. Mỗi trường lệch
được tách tiếp theo loại để đọc ra nguyên nhân:
  - job_status:    \"job OPEN nhưng mọi listing CLOSED\" / \"job CLOSED nhưng có listing OPEN hoặc UNKNOWN\";
  - deadline:      job không hạn mà listing có / job có hạn mà listing không / cả hai có hạn, job muộn hơn /
                   sớm hơn (job muộn hơn từng là dấu của extend_job_deadline, đã gỡ ở C4 phần 2/3);
  - source_url:    URL của job là một listing khác của job / URL của job không có listing nào.
Job chưa có listing nào được đếm riêng (\"không suy ra được\"), không tính là lệch.

--strict: thoát với mã 2 nếu còn bất kỳ lệch nào (dùng làm cổng trước khi chuyển bước). Mặc định luôn mã 0
khi chạy xong, vì lúc này lệch là thông tin chứ chưa phải lỗi.

Hàm so (compare_job) và phân loại là hàm THUẦN, có test ở tests/test_check_listing_derivation.py.
"""

import csv
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from scrapjd import db
from scrapjd.db.job_derivation import DerivedJob, derive_job_from_listings

DEFAULT_SHOW = 5
FIELDS = ("job_status", "closed_reason", "deadline", "source_url")


@dataclass
class Mismatch:
    job_id: str
    job_title: str
    company_name: str
    field: str
    kind: str
    stored: object
    derived: object
    n_listings: int


@dataclass
class Report:
    total_jobs: int = 0
    jobs_without_listings: int = 0
    jobs_compared: int = 0
    jobs_matching: int = 0
    mismatches: list = field(default_factory=list)

    def by_field(self) -> Counter:
        return Counter(m.field for m in self.mismatches)

    def by_kind(self) -> Counter:
        return Counter((m.field, m.kind) for m in self.mismatches)

    def jobs_with_mismatch(self) -> int:
        return len({m.job_id for m in self.mismatches})


def _kind_status(stored: str, derived: str) -> str:
    return ("job OPEN nhưng mọi listing CLOSED" if stored == "OPEN"
            else "job CLOSED nhưng có listing OPEN hoặc UNKNOWN")


def _kind_deadline(stored, derived) -> str:
    if stored is None:
        return "job không có hạn, listing có"
    if derived is None:
        return "job có hạn, listing không có"
    return "cả hai có hạn, job muộn hơn" if stored > derived else "cả hai có hạn, job sớm hơn"


def _kind_url(stored: Optional[str], listing_urls: set) -> str:
    return ("URL của job là một listing khác của job" if stored in listing_urls
            else "URL của job không có listing nào")


def compare_job(job: dict, derived: DerivedJob) -> list:
    """Danh sách (trường, loại, giá trị đang lưu, giá trị suy ra) cho các trường lệch của một job."""
    out = []
    if job["job_status"] != derived.job_status:
        out.append(("job_status", _kind_status(job["job_status"], derived.job_status),
                    job["job_status"], derived.job_status))
    elif derived.job_status == "CLOSED" and job["closed_reason"] != derived.closed_reason:
        out.append(("closed_reason", "lý do đóng khác nhau", job["closed_reason"], derived.closed_reason))
    if job["deadline"] != derived.deadline:
        out.append(("deadline", _kind_deadline(job["deadline"], derived.deadline),
                    job["deadline"], derived.deadline))
    if job["source_url"] != derived.source_url:
        urls = {l["source_url"] for l in job["listings"]}
        out.append(("source_url", _kind_url(job["source_url"], urls), job["source_url"], derived.source_url))
    return out


def build_report(jobs: list) -> Report:
    """Báo cáo từ kết quả db.list_jobs_with_listings(). Hàm thuần."""
    rep = Report(total_jobs=len(jobs))
    for job in jobs:
        derived = derive_job_from_listings(job["listings"])
        if derived is None:
            rep.jobs_without_listings += 1
            continue
        rep.jobs_compared += 1
        diffs = compare_job(job, derived)
        if not diffs:
            rep.jobs_matching += 1
        for fld, kind, stored, der in diffs:
            rep.mismatches.append(Mismatch(
                job_id=job["job_id"], job_title=job["job_title"] or "", company_name=job["company_name"] or "",
                field=fld, kind=kind, stored=stored, derived=der, n_listings=len(job["listings"])))
    return rep


def _pct(part: int, whole: int) -> str:
    return f"{part * 100 / whole:.1f}%" if whole else "-"


def _short(value, width: int = 70) -> str:
    text = "(trống)" if value is None else str(value)
    return text if len(text) <= width else text[: width - 1] + "…"


def print_report(rep: Report, *, show: int) -> None:
    print("=" * 78)
    print("SO GIÁ TRỊ JOB SUY RA TỪ LISTING VỚI GIÁ TRỊ ĐANG LƯU (chỉ đọc)")
    print("=" * 78)
    print(f"Tổng số job: {rep.total_jobs}")
    print(f"  chưa có listing nào (không suy ra được): {rep.jobs_without_listings}")
    print(f"  so được: {rep.jobs_compared}")
    print(f"    khớp cả 4 trường: {rep.jobs_matching} ({_pct(rep.jobs_matching, rep.jobs_compared)})")
    print(f"    có ít nhất một trường lệch: {rep.jobs_with_mismatch()} "
          f"({_pct(rep.jobs_with_mismatch(), rep.jobs_compared)})")
    if not rep.mismatches:
        print("\nKhông có trường nào lệch.")
        return
    print("\nLệch theo trường:")
    by_kind = rep.by_kind()
    for fld, n in sorted(rep.by_field().items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {fld}: {n}")
        for (f2, kind), k in sorted(by_kind.items(), key=lambda kv: -kv[1]):
            if f2 == fld:
                print(f"      {k:>6}  {kind}")
    if show <= 0:
        return
    for (fld, kind), _ in sorted(by_kind.items(), key=lambda kv: (kv[0][0], -kv[1])):
        rows = [m for m in rep.mismatches if m.field == fld and m.kind == kind][:show]
        print(f"\n--- {fld}: {kind} (tối đa {show} dòng) ---")
        for m in rows:
            print(f"  {m.job_id[:8]}  {_short(m.company_name, 28)} | {_short(m.job_title, 34)} "
                  f"| {m.n_listings} listing")
            print(f"      đang lưu: {_short(m.stored)}   suy ra: {_short(m.derived)}")


def write_csv(rep: Report, fh) -> int:
    w = csv.writer(fh)
    w.writerow(["job_id", "cong_ty", "tieu_de", "truong", "loai", "dang_luu", "suy_ra", "so_listing"])
    for m in rep.mismatches:
        w.writerow([m.job_id, m.company_name, m.job_title, m.field, m.kind,
                    "" if m.stored is None else m.stored, "" if m.derived is None else m.derived,
                    m.n_listings])
    return len(rep.mismatches)


def run(conn, *, show: int = DEFAULT_SHOW, csv_path: Optional[str] = None, strict: bool = False) -> int:
    """Chạy trên một kết nối. Chỉ SELECT. Trả exit code: 0 xong; 2 nếu strict và còn lệch."""
    jobs = db.list_jobs_with_listings(conn)
    rep = build_report(jobs)
    print_report(rep, show=show)
    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as fh:
            n = write_csv(rep, fh)
        print(f"\nĐã xuất {n} dòng lệch ra {csv_path}")
    return 2 if strict and rep.mismatches else 0


def run_cli(args) -> int:
    """Điểm vào cho `python main.py check-listing-derivation` (args từ argparse trong main.py)."""
    if args.show < 0:
        print("❌ --show phải >= 0.")
        return 1
    conn = db.get_connection()
    try:
        return run(conn, show=args.show, csv_path=args.csv, strict=args.strict)
    finally:
        conn.close()
