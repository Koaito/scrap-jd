"""
Đo tỷ lệ gộp nhầm — `python main.py report-reposts` (A5). CHỈ ĐỌC: không ghi DB, không đổi schema,
không cần migration mới.

MỤC ĐÍCH: khoá trùng của crawler là công ty + tiêu đề chuẩn hoá + tỉnh, KHÔNG gồm level (level do
máy suy ra nên không đủ tin cậy làm danh tính). Cái giá đã biết: hai vị trí cùng tên, cùng công
ty, cùng tỉnh nhưng khác cấp sẽ bị coi là một job. Lệnh này đo cái giá đó bằng dữ liệu thật thay
vì đoán: với mỗi job đã nhận thêm tin (>= 2 dòng job_sources_log), so nội dung JD của các tin với
nhau. Các tin cùng một vị trí thì JD gần như giống nhau; tin có JD khác hẳn là nghi gộp nhầm.

CÁCH ĐO (hàm thuần, có test ở tests/test_repost_report.py):
  - Văn bản so = phần "Mô tả công việc" + "Yêu cầu ứng viên" của raw_jd_content (các tiêu đề này do
    pipeline._build_parsed_content_and_raw ghi). Bỏ phần "Quyền lợi" vì giới thiệu công ty và
    quyền lợi hay dùng chung giữa các JD của cùng một công ty, làm điểm giống cao giả; còn yêu cầu
    (số năm kinh nghiệm...) chính là chỗ hai cấp bậc khác nhau. Văn bản không có tiêu đề phần nào
    (dữ liệu cũ) thì so toàn bộ.
  - Độ giống = Jaccard trên tập cụm 3 từ liên tiếp (chữ thường, bỏ dấu câu, giữ dấu tiếng Việt).
    Tin có ít hơn MIN_WORDS từ thì "không so được" (thiếu nội dung), không tính vào tỷ lệ.
  - Mỗi job: so mọi cặp tin, lấy cặp KHÁC NHAU NHẤT làm điểm của job. Phân loại theo điểm đó:
    giống (>= 0.80) / gần giống (>= 0.50) / khác nhiều / nghi gộp nhầm (dưới --threshold, mặc
    định 0.20).
  - Tách số liệu theo (a) cặp thấp nhất cùng trang hay khác trang (hai trang trình bày JD khác
    nhau nên điểm khác trang vốn thấp hơn), (b) có tin do merge-duplicates chuyển sang hay không
    (đọc từ audit_logs MERGE_JOB; cho biết lần gộp thật đã làm có gộp nhầm không).

GIỚI HẠN (nói rõ để không đọc số quá mức):
  - Chỉ bắt được gộp nhầm khi NỘI DUNG KHÁC. Hai vị trí khác cấp nhưng dùng chung một JD thì điểm
    cao và không bị bắt.
  - job_sources_log không lưu tin được nối bằng cách nào (khoá trùng hay mã job), nên mọi cặp tin
    của một job đều được so, kể cả tin cùng mã job (chắc chắn cùng job) — chúng chỉ làm tỷ lệ thấp
    đi, không làm cao lên.
  - Job phụ đã bị merge-duplicates xoá không còn trong DB; log bị bỏ vì trùng URL đã mất nội dung.
    Chỉ đếm số bị bỏ, không so được.
  - Các ngưỡng là ước lượng ban đầu, CHƯA hiệu chuẩn bằng mắt. Xem phân bố và --csv để chỉnh.

Phần SQL nằm ở scrapjd/db/job_reposts.py.
"""

import csv
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from itertools import combinations
from typing import Optional
from urllib.parse import urlparse

from scrapjd import db

SHINGLE_SIZE = 3       # số từ liên tiếp trong một "cụm"
MIN_WORDS = 20         # ít hơn số từ này thì không đủ nội dung để so
MAX_LOGS_PER_JOB = 12  # job có nhiều tin hơn: chỉ so tin cũ nhất (tránh bùng nổ số cặp)

SAME_AT = 0.80         # >= : giống
SIMILAR_AT = 0.50      # >= : gần giống
DEFAULT_SUSPECT_BELOW = 0.20   # <  : nghi gộp nhầm (chỉnh bằng --threshold)

DEFAULT_SHOW = 10      # số job nghi gộp nhầm in chi tiết
SNIPPET_CHARS = 200

CLASS_SAME = "giống"
CLASS_SIMILAR = "gần giống"
CLASS_DIFFERENT = "khác nhiều"
CLASS_SUSPECT = "nghi gộp nhầm"
CLASS_NOT_COMPARABLE = "không so được"
_CLASS_ORDER = (CLASS_SAME, CLASS_SIMILAR, CLASS_DIFFERENT, CLASS_SUSPECT)

ORIGIN_PIPELINE = "pipeline"
ORIGIN_MERGE = "merge-duplicates"

_SECTION_RE = re.compile(r"^=== (.+?) ===[ \t]*$", re.MULTILINE)
_WORD_RE = re.compile(r"\w+")
# Tên phần (đã chuẩn hoá) dùng để so: xem docstring đầu file.
_ROLE_HEADINGS = frozenset({"mô tả công việc", "yêu cầu ứng viên"})


# ----------------------------------------------------------------------
# Logic thuần (không DB, không mạng) — có test
# ----------------------------------------------------------------------
def _norm(text: str) -> str:
    return unicodedata.normalize("NFC", text).casefold()


def role_text(raw: Optional[str]) -> str:
    """Phần văn bản dùng để so của một raw_jd_content: nối các phần "Mô tả công việc" và "Yêu cầu
    ứng viên". Có tiêu đề phần nhưng thiếu cả hai phần này (vd chỉ có "Quyền lợi") thì trả rỗng
    — so phần quyền lợi với phần yêu cầu của tin khác sẽ ra điểm thấp giả. Không có tiêu đề nào
    (dữ liệu cũ / nhập tay) thì trả toàn bộ văn bản."""
    if not raw or not raw.strip():
        return ""
    matches = list(_SECTION_RE.finditer(raw))
    if not matches:
        return raw.strip()
    parts = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(raw)
        if _norm(m.group(1)).strip() in _ROLE_HEADINGS:
            parts.append(raw[m.end():end].strip())
    return "\n".join(p for p in parts if p)


def tokenize(text: str) -> list:
    """Từ của `text`: NFC + chữ thường, bỏ dấu câu, GIỮ dấu tiếng Việt (bỏ dấu sẽ làm "quản lý"
    và "quan ly" trùng nhau, không có lợi gì khi so JD cùng nguồn gốc)."""
    return _WORD_RE.findall(_norm(text))


def shingles(tokens: list, size: int = SHINGLE_SIZE) -> frozenset:
    """Tập cụm `size` từ liên tiếp. Rỗng nếu ít từ hơn `size`."""
    if len(tokens) < size:
        return frozenset()
    return frozenset(" ".join(tokens[i:i + size]) for i in range(len(tokens) - size + 1))


def jaccard(a: frozenset, b: frozenset) -> float:
    """|A giao B| / |A hợp B|. Hai tập rỗng: 0.0 (không có gì để nói là giống)."""
    union = len(a | b)
    return len(a & b) / union if union else 0.0


def classify(similarity: float, suspect_below: float = DEFAULT_SUSPECT_BELOW) -> str:
    """Nhãn của một điểm giống (xem docstring đầu file)."""
    if similarity >= SAME_AT:
        return CLASS_SAME
    if similarity >= SIMILAR_AT:
        return CLASS_SIMILAR
    if similarity >= suspect_below:
        return CLASS_DIFFERENT
    return CLASS_SUSPECT


def site_of_url(url: Optional[str]) -> str:
    """Tên miền (bỏ 'www.') của URL nguồn, '?' nếu không có."""
    host = (urlparse(url or "").hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or "?"


def _snippet(text: str) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= SNIPPET_CHARS else flat[:SNIPPET_CHARS].rstrip() + "…"


@dataclass
class LogInfo:
    log_id: str
    source_url: Optional[str]
    site: str
    collected_date: object
    origin: str                     # ORIGIN_PIPELINE | ORIGIN_MERGE
    donor_job_id: Optional[str]     # chỉ có khi origin = ORIGIN_MERGE
    n_words: int                    # số từ của văn bản so
    snippet: str
    shingle_set: frozenset = field(default_factory=frozenset, repr=False)

    @property
    def comparable(self) -> bool:
        return self.n_words >= MIN_WORDS and bool(self.shingle_set)


@dataclass
class Pair:
    a: LogInfo
    b: LogInfo
    similarity: float

    @property
    def same_site(self) -> bool:
        return self.a.site == self.b.site

    @property
    def involves_merge(self) -> bool:
        return ORIGIN_MERGE in (self.a.origin, self.b.origin)


@dataclass
class JobResult:
    job_id: str
    company_name: str
    job_title: str
    level_code: Optional[str]
    province_name: Optional[str]
    job_status: str
    n_logs: int                     # số log của job trong DB
    logs: list                      # LogInfo của các log được xét (<= MAX_LOGS_PER_JOB)
    pairs: list = field(default_factory=list)   # Pair của mọi cặp so được, đã sắp thấp trước
    klass: str = CLASS_NOT_COMPARABLE

    @property
    def truncated(self) -> bool:
        return self.n_logs > len(self.logs)

    @property
    def n_comparable_logs(self) -> int:
        return sum(1 for log in self.logs if log.comparable)

    @property
    def worst(self) -> Optional[Pair]:
        return self.pairs[0] if self.pairs else None

    @property
    def score(self) -> Optional[float]:
        return self.pairs[0].similarity if self.pairs else None


def build_log_info(row: dict, origins_moved: dict) -> LogInfo:
    text = role_text(row.get("raw_jd_content"))
    tokens = tokenize(text)
    moved = origins_moved.get(row["log_id"])
    return LogInfo(
        log_id=row["log_id"], source_url=row.get("source_url"), site=site_of_url(row.get("source_url")),
        collected_date=row.get("collected_date"),
        origin=ORIGIN_MERGE if moved else ORIGIN_PIPELINE,
        donor_job_id=moved["donor_job_id"] if moved else None,
        n_words=len(tokens), snippet=_snippet(text), shingle_set=shingles(tokens),
    )


def build_results(rows: list, origins_moved: Optional[dict] = None,
                  suspect_below: float = DEFAULT_SUSPECT_BELOW) -> list:
    """Từ các dòng db.list_multi_source_job_logs (đã sắp theo job rồi tin cũ nhất trước) dựng
    JobResult cho từng job, theo thứ tự xuất hiện. Job có nhiều hơn MAX_LOGS_PER_JOB tin chỉ xét
    các tin cũ nhất."""
    origins_moved = origins_moved or {}
    grouped: dict = {}
    for row in rows:
        grouped.setdefault(row["job_id"], []).append(row)

    results = []
    for job_id, job_rows in grouped.items():
        first = job_rows[0]
        logs = [build_log_info(r, origins_moved) for r in job_rows[:MAX_LOGS_PER_JOB]]
        result = JobResult(
            job_id=job_id, company_name=first.get("company_name") or "", job_title=first.get("job_title") or "",
            level_code=first.get("level_code"), province_name=first.get("province_name"),
            job_status=first.get("job_status") or "", n_logs=len(job_rows), logs=logs,
        )
        comparable = [log for log in logs if log.comparable]
        pairs = [Pair(a, b, jaccard(a.shingle_set, b.shingle_set)) for a, b in combinations(comparable, 2)]
        # Thấp trước; bằng điểm thì thứ tự tin cũ nhất trước (xác định, không ngẫu nhiên).
        pairs.sort(key=lambda p: (p.similarity, p.a.log_id, p.b.log_id))
        result.pairs = pairs
        if pairs:
            result.klass = classify(pairs[0].similarity, suspect_below)
        results.append(result)
    return results


def reclassify(results: list, suspect_below: float) -> None:
    """Gán lại nhãn theo ngưỡng mới (không so lại độ giống)."""
    for r in results:
        r.klass = classify(r.score, suspect_below) if r.pairs else CLASS_NOT_COMPARABLE


def histogram(results: list, bins: int = 10) -> list:
    """Số job theo điểm giống thấp nhất, chia `bins` khoảng đều trên [0, 1]; điểm 1.0 vào khoảng
    cuối. Chỉ tính job so được."""
    counts = [0] * bins
    for r in results:
        if r.score is None:
            continue
        counts[min(int(r.score * bins), bins - 1)] += 1
    return counts


@dataclass
class Summary:
    total_jobs: int
    n_multi: int                    # job có >= 2 tin
    n_comparable: int
    n_not_comparable: int
    n_truncated: int
    n_logs_unusable: int            # log (trong job so được hoặc không) thiếu nội dung
    merge_logs_moved: int
    merge_logs_dropped: int
    by_class: Counter
    # (tổng so được, số nghi gộp nhầm) theo từng cách tách
    split_same_site: tuple
    split_cross_site: tuple
    split_with_merge: tuple
    split_without_merge: tuple
    hist: list


def _suspect_split(results: list, predicate) -> tuple:
    subset = [r for r in results if r.pairs and predicate(r.worst)]
    return len(subset), sum(1 for r in subset if r.klass == CLASS_SUSPECT)


def summarize(results: list, *, total_jobs: int, merge_moved: int = 0, merge_dropped: int = 0) -> Summary:
    comparable = [r for r in results if r.pairs]
    return Summary(
        total_jobs=total_jobs, n_multi=len(results), n_comparable=len(comparable),
        n_not_comparable=len(results) - len(comparable),
        n_truncated=sum(1 for r in results if r.truncated),
        n_logs_unusable=sum(1 for r in results for log in r.logs if not log.comparable),
        merge_logs_moved=merge_moved, merge_logs_dropped=merge_dropped,
        by_class=Counter(r.klass for r in comparable),
        split_same_site=_suspect_split(results, lambda p: p.same_site),
        split_cross_site=_suspect_split(results, lambda p: not p.same_site),
        split_with_merge=_suspect_split(results, lambda p: p.involves_merge),
        split_without_merge=_suspect_split(results, lambda p: not p.involves_merge),
        hist=histogram(results),
    )


def suspects(results: list) -> list:
    """Job nghi gộp nhầm, điểm thấp nhất trước (bằng điểm thì theo công ty rồi job_id)."""
    return sorted((r for r in results if r.klass == CLASS_SUSPECT),
                  key=lambda r: (r.score, r.company_name, r.job_id))


# ----------------------------------------------------------------------
# In báo cáo / xuất CSV
# ----------------------------------------------------------------------
def _pct(part: int, whole: int) -> str:
    return f"{100 * part / whole:.1f}%" if whole else "—"


def _day(value) -> str:
    return str(value)[:10] if value else "—"


def _short(url: Optional[str], width: int = 90) -> str:
    url = url or "—"
    return url if len(url) <= width else url[:width - 1] + "…"


def _origin_label(log: LogInfo) -> str:
    if log.origin == ORIGIN_MERGE:
        return f"{ORIGIN_MERGE} (từ job {(log.donor_job_id or '?')[:8]})"
    return ORIGIN_PIPELINE


def _print_job(no: int, r: JobResult) -> None:
    worst = r.worst
    print(f"\n#{no}  job {r.job_id[:8]} [{r.job_status}]  {r.company_name} — {r.job_title}")
    print(f"    level {r.level_code or '-'}, tỉnh {r.province_name or '-'}, "
          f"{r.n_logs} tin ({r.n_comparable_logs} đủ nội dung), "
          f"độ giống thấp nhất {worst.similarity:.2f} "
          f"({'cùng trang' if worst.same_site else 'KHÁC trang'})")
    for tag, log in (("A", worst.a), ("B", worst.b)):
        print(f"    tin {tag}: {log.site}  {_day(log.collected_date)}  {_origin_label(log)}  "
              f"{log.n_words} từ")
        print(f"          {_short(log.source_url)}")
        print(f"          \"{log.snippet}\"")


def print_report(summary: Summary, results: list, *, show: int, suspect_below: float) -> None:
    s = summary
    print("=" * 78)
    print("BÁO CÁO ĐO TỶ LỆ GỘP NHẦM (chỉ đọc)")
    print("=" * 78)
    print(f"Tổng job trong DB: {s.total_jobs}")
    print(f"Job có >= 2 tin nguồn (job_sources_log): {s.n_multi}")
    print(f"  {'so được (>= 2 tin đủ nội dung)':<44} {s.n_comparable}")
    print(f"  {'không so được (thiếu nội dung hoặc quá ngắn)':<44} {s.n_not_comparable}")
    if s.n_truncated:
        print(f"  {f'job có > {MAX_LOGS_PER_JOB} tin, chỉ so {MAX_LOGS_PER_JOB} tin cũ nhất':<44} {s.n_truncated}")
    print(f"Tin do merge-duplicates chuyển sang job giữ: {s.merge_logs_moved}; "
          f"tin bị bỏ vì trùng khi gộp (không còn nội dung): {s.merge_logs_dropped}")
    print(f"\nCách đo: Jaccard trên cụm {SHINGLE_SIZE} từ của phần \"Mô tả công việc\" + \"Yêu cầu ứng viên\"; "
          "mỗi job lấy cặp tin KHÁC NHAU NHẤT.")

    print(f"\nPhân loại job theo cặp khác nhau nhất ({s.n_comparable} job so được):")
    bounds = {
        CLASS_SAME: f">= {SAME_AT:.2f}",
        CLASS_SIMILAR: f"{SIMILAR_AT:.2f} - {SAME_AT:.2f}",
        CLASS_DIFFERENT: f"{suspect_below:.2f} - {SIMILAR_AT:.2f}",
        CLASS_SUSPECT: f"< {suspect_below:.2f}",
    }
    for klass in _CLASS_ORDER:
        n = s.by_class.get(klass, 0)
        print(f"  {f'{klass} ({bounds[klass]})':<44} {n:>5}  {_pct(n, s.n_comparable):>6}")

    n_sus = s.by_class.get(CLASS_SUSPECT, 0)
    print(f"\nTỷ lệ nghi gộp nhầm: {n_sus} / {s.n_comparable} = {_pct(n_sus, s.n_comparable)}")
    for label, (total, sus) in (
        ("cặp thấp nhất cùng trang", s.split_same_site),
        ("cặp thấp nhất KHÁC trang (điểm vốn thấp hơn, đọc riêng)", s.split_cross_site),
        ("cặp thấp nhất có tin do merge-duplicates", s.split_with_merge),
        ("cặp thấp nhất không dính merge-duplicates", s.split_without_merge),
    ):
        print(f"  {label:<58} {sus:>4} / {total:<5} {_pct(sus, total):>6}")

    print("\nPhân bố điểm giống thấp nhất của job (mỗi # ~ 1 job, tỷ lệ theo cột lớn nhất):")
    peak = max(s.hist) if s.hist else 0
    for i, n in enumerate(s.hist):
        bar = "#" * (round(40 * n / peak) if peak else 0)
        print(f"  {i / len(s.hist):.1f} - {(i + 1) / len(s.hist):.1f}  {n:>5}  {bar}")

    flagged = suspects(results)
    if flagged and show > 0:
        print(f"\n--- Job nghi gộp nhầm: {len(flagged)} (xem {min(show, len(flagged))} job có điểm thấp nhất) ---")
        for no, r in enumerate(flagged[:show], 1):
            _print_job(no, r)

    print("\nBáo cáo này CHỈ ĐỌC, không gộp hay tách gì. Ngưỡng là ước lượng ban đầu, chưa hiệu chuẩn:")
    print("dùng --csv FILE để xem từng cặp, đối chiếu bằng mắt vài chục cặp ở mỗi mức rồi chỉnh --threshold.")
    print("Chỉ bắt được gộp nhầm khi NỘI DUNG KHÁC; hai vị trí khác cấp dùng chung một JD thì không bị bắt.")


_CSV_HEADER = (
    "job_id", "cong_ty", "tieu_de", "level", "tinh", "trang_thai", "phan_loai_job", "do_giong",
    "cung_trang",
    "tin_a_log_id", "tin_a_trang", "tin_a_ngay", "tin_a_nguon", "tin_a_so_tu", "tin_a_url",
    "tin_b_log_id", "tin_b_trang", "tin_b_ngay", "tin_b_nguon", "tin_b_so_tu", "tin_b_url",
)


def write_csv(results: list, fh) -> int:
    """Ghi mọi cặp tin so được ra `fh` (file mở sẵn, newline=''), mỗi cặp một dòng; trả số dòng.
    Job điểm thấp nhất trước, trong job cặp thấp nhất trước."""
    writer = csv.writer(fh)
    writer.writerow(_CSV_HEADER)
    n = 0
    for r in sorted((r for r in results if r.pairs), key=lambda r: (r.score, r.company_name, r.job_id)):
        for p in r.pairs:
            writer.writerow([
                r.job_id, r.company_name, r.job_title, r.level_code or "", r.province_name or "",
                r.job_status, r.klass, f"{p.similarity:.3f}", "x" if p.same_site else "",
                p.a.log_id, p.a.site, _day(p.a.collected_date), p.a.origin, p.a.n_words, p.a.source_url or "",
                p.b.log_id, p.b.site, _day(p.b.collected_date), p.b.origin, p.b.n_words, p.b.source_url or "",
            ])
            n += 1
    return n


def export_csv(results: list, path: str) -> int:
    # utf-8-sig: Excel mở tiếng Việt đúng dấu.
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        return write_csv(results, fh)


# ----------------------------------------------------------------------
# Chạy
# ----------------------------------------------------------------------
def run(conn, *, show: int = DEFAULT_SHOW, csv_path: Optional[str] = None,
        suspect_below: float = DEFAULT_SUSPECT_BELOW) -> int:
    """Chạy báo cáo trên một kết nối. Chỉ SELECT. Trả exit code (0 = xong)."""
    rows = db.list_multi_source_job_logs(conn)
    origins = db.list_merge_log_origins(conn)
    total_jobs = db.count_jobs(conn)
    conn.rollback()

    results = build_results(rows, origins["moved"], suspect_below)
    summary = summarize(results, total_jobs=total_jobs, merge_moved=len(origins["moved"]),
                        merge_dropped=origins["dropped"])
    print_report(summary, results, show=show, suspect_below=suspect_below)
    if csv_path:
        n = export_csv(results, csv_path)
        print(f"\nĐã xuất {n} cặp tin ({summary.n_comparable} job) ra {csv_path}")
    return 0


def run_cli(args) -> int:
    """Điểm vào cho `python main.py report-reposts` (args từ argparse trong main.py)."""
    if args.show < 0:
        print("❌ --show phải >= 0.")
        return 1
    if not 0 < args.threshold <= SIMILAR_AT:
        print(f"❌ --threshold phải trong khoảng (0, {SIMILAR_AT}].")
        return 1
    conn = db.get_connection()
    try:
        return run(conn, show=args.show, csv_path=args.csv, suspect_below=args.threshold)
    finally:
        conn.close()
