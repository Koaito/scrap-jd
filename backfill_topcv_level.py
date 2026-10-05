"""
Script RIÊNG (không nằm trong pipeline crawl chính) — vá level cho job TopCV
ĐÃ LƯU mà nhãn "Trên 5 năm" từng bị gán nhầm thành Senior.

BỐI CẢNH: normalize.infer_level() trước đây bắt "5 năm" trong cụm "Trên 5 năm"
nên job TopCV mang nhãn đó ra Senior thay vì Lead. Code đã sửa (job crawl mới
đúng), nhưng DB không lưu nhãn gốc nên job cũ vẫn sai. Senior cũ chỉ có thể đến
từ ba nguồn: tiêu đề có chữ "Senior" (đúng, giữ), nhãn "4 năm"/"5 năm" (đúng,
giữ) hoặc nhãn "Trên 5 năm" (sai, cần vá). Vì vậy script chỉ xét job đang là
Senior và chỉ có thể đổi theo hướng đã tính lại bằng cùng quy tắc với crawl mới.

Với mỗi job chọn được:
  1. Bỏ qua job mà level do TIÊU ĐỀ quyết định (normalize.level_from_title):
     số năm không đổi được kết quả nên không tốn request.
  2. Mức 2 (chính xác): tải lại trang TopCV, đọc nhãn "Kinh nghiệm" đang hiển
     thị, tính lại bằng normalize.infer_level (cùng quy tắc crawl mới).
  3. Mức 1 (gần đúng, dự phòng): khi không tải được trang hoặc trang không có
     nhãn, đọc chữ trong phần yêu cầu đã lưu bằng
     normalize.infer_level_from_requirements. Chỉ nâng Senior -> Lead khi yêu
     cầu đòi HƠN 5 năm; không có căn cứ rõ thì GIỮ NGUYÊN.
Báo cáo tách riêng số job đổi theo "nhãn trang" và theo "suy từ chữ" để bạn
biết phần nào chắc chắn. --no-fetch chỉ chạy mức 1 (không tốn request).

CHẠY THỬ TRƯỚC (mặc định, KHÔNG ghi DB, vẫn tải trang nên tốn request):
    python backfill_topcv_level.py --limit 20
Rồi ghi thật:
    python backfill_topcv_level.py --apply
Chỉ suy từ chữ, không tải trang:
    python backfill_topcv_level.py --no-fetch --apply

Chọn job nào: job TopCV đang Senior, chưa đóng, chưa ai sửa tay
(updated_by IS NULL). --include-closed gồm cả job CLOSED, --created-before
YYYY-MM-DD chỉ lấy job tạo trước ngày đó. Thứ tự: job CŨ NHẤT trước.
KHÔNG chạy cùng lúc với một lượt crawl TopCV (cùng gửi request tới một site,
dễ bị chặn). Bị chặn liên tiếp thì script tự dừng, chạy lại sau.

TIẾN ĐỘ: với --apply, job đã xong chắc chắn (đã ghi level mới, hoặc nhãn trang
xác nhận level hiện tại đúng) được ghi vào file trạng thái (mặc định
.backfill_topcv_level.done) để lần sau bỏ qua. Job chỉ suy từ chữ mà không đổi
được thì KHÔNG ghi, để lần chạy có tải trang sau thử lại. --reset-state xoá file.

LƯU Ý TRÙNG LẶP: đổi level_id làm trigger set_job_hash tính lại content_hash,
nên hai job cũ có thể thành "cùng khoá" (company + title + level + province).
Script KHÔNG tự gộp, chỉ in số nhóm trong v_duplicate_job_candidates trước/sau.
"""

import argparse
import logging
import os
from collections import Counter
from typing import Optional

import db
import normalize
from adapters.base import CrawlBlockedError
from adapters.topcv import TopCVAdapter

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

SOURCE_NAME = "TopCV"
DEFAULT_STATE_FILE = ".backfill_topcv_level.done"
_PROGRESS_EVERY = 25
_SAMPLE_LIMIT = 10

# Hai cách xác định level mới, ghi vào báo cáo.
BASIS_LABEL = "nhãn trang"
BASIS_TEXT = "suy từ chữ"

_SELECT_SQL = """
SELECT * FROM (
    SELECT DISTINCT ON (jp.job_id)
           jp.job_id, jp.job_title, l.level_code, jp.parsed_content,
           jp.created_at, jsl.source_url
    FROM job_postings jp
    JOIN job_sources_log jsl ON jsl.job_id = jp.job_id AND jsl.source_name = %(source)s
    JOIN levels l ON l.level_id = jp.level_id
    WHERE jp.updated_by IS NULL
      AND l.level_code = 'Senior'
      AND (%(include_closed)s OR jp.job_status <> 'CLOSED')
      AND (%(before)s::date IS NULL OR jp.created_at < %(before)s::date)
    ORDER BY jp.job_id, jsl.collected_date, jsl.log_id
) picked
ORDER BY picked.created_at ASC, picked.job_id
LIMIT %(limit)s
"""


# ----------------------------------------------------------------------
# DB
# ----------------------------------------------------------------------
def select_jobs(conn, *, include_closed: bool, limit: Optional[int],
                before: Optional[str] = None) -> list:
    """Job TopCV đang Senior cần xét, CŨ NHẤT TRƯỚC, mỗi job một dòng (URL TopCV
    đầu tiên của job đó). Đóng transaction đọc trước khi trả về."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_SQL, {
            "source": SOURCE_NAME, "include_closed": include_closed,
            "limit": limit, "before": before,
        })
        cols = ("job_id", "job_title", "level_code", "parsed_content", "created_at", "source_url")
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.rollback()
    return rows


def count_duplicate_groups(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v_duplicate_job_candidates")
        n = cur.fetchone()[0]
    conn.rollback()
    return n


# ----------------------------------------------------------------------
# Logic thuần (không DB, không mạng) — có test
# ----------------------------------------------------------------------
def plan_level(row: dict, label: Optional[str]) -> dict:
    """Tính level mới cho một job từ dòng DB và nhãn kinh nghiệm đọc được trên
    trang (None nếu không có).

    Trả dict: new_level, new_level_source, new_level_signals, basis (BASIS_LABEL | BASIS_TEXT), level_changed,
    confirmed (True khi nhãn trang đã xác nhận kết quả, tức không cần xét lại)."""
    current = row.get("level_code")
    if label:
        decision = normalize.derive_level(label, row["job_title"], "")
        return {"new_level": decision.level, "new_level_source": decision.source,
                # Tín hiệu thô đã đưa cho derive_level (TopCV không có level_hint).
                "new_level_signals": normalize.build_level_signals(label, ""),
                "basis": BASIS_LABEL,
                "level_changed": decision.level != current, "confirmed": True}

    requirements = (row.get("parsed_content") or {}).get("requirements") or ""
    guessed = normalize.infer_level_from_requirements(requirements)
    new_level = guessed or current
    # Suy từ chữ trong phần yêu cầu là cách gần đúng, không phải một nhánh của
    # derive_level: để căn cứ None ("chưa biết") để lệnh tính lại xử lý sau.
    return {"new_level": new_level, "new_level_source": None, "new_level_signals": None,
            "basis": BASIS_TEXT,
            "level_changed": new_level != current, "confirmed": False}


class Summary:
    def __init__(self):
        self.selected = 0
        self.title_decided = 0
        self.fetched_ok = 0
        self.no_label = 0
        self.unavailable = 0
        self.changed_by_label = 0
        self.changed_by_text = 0
        self.unchanged = 0
        self.errors = 0
        self.blocked = False
        self.interrupted = False
        self.transitions: Counter = Counter()
        self.samples: list = []


def process_job(conn, adapter, row: dict, *, apply: bool, level_ids: dict,
                summary: Summary) -> bool:
    """Xử lý một job. Trả True khi job đã xong chắc chắn và nên ghi vào file tiến
    độ (chỉ khi --apply)."""
    if normalize.level_from_title(row["job_title"]) is not None:
        # Tiêu đề đã chốt level: tính lại cũng ra đúng giá trị đang lưu.
        summary.title_decided += 1
        return apply

    label = None
    if adapter is not None:
        status, label = adapter.fetch_experience_label(row["source_url"])
        if status == adapter.REFRESH_OK:
            summary.fetched_ok += 1
        elif status == adapter.REFRESH_NO_LABEL:
            summary.no_label += 1
            logger.info("Trang không có nhãn kinh nghiệm, chuyển sang suy từ chữ: %s",
                        row["source_url"])
        else:
            summary.unavailable += 1
            logger.info("Không tải được, chuyển sang suy từ chữ: %s", row["source_url"])

    plan = plan_level(row, label)
    if plan["level_changed"]:
        if plan["basis"] == BASIS_LABEL:
            summary.changed_by_label += 1
        else:
            summary.changed_by_text += 1
        summary.transitions[(row.get("level_code") or "(trống)", plan["new_level"], plan["basis"])] += 1
        if len(summary.samples) < _SAMPLE_LIMIT:
            summary.samples.append((row["job_title"], row.get("level_code"),
                                    plan["new_level"], plan["basis"], label))
        logger.info("%s | level %s -> %s (%s%s) | %s", row["job_title"][:50], row.get("level_code"),
                    plan["new_level"], plan["basis"], f", nhãn {label!r}" if label else "",
                    row["source_url"])
    else:
        summary.unchanged += 1

    if not apply:
        return False
    if not plan["level_changed"]:
        return plan["confirmed"]

    new_level_id = level_ids.get(plan["new_level"])
    if new_level_id is None:
        raise RuntimeError(f"Không tìm thấy level_id cho '{plan['new_level']}' trong bảng levels")
    try:
        # Ghi level TỰ ĐỘNG: đóng dấu căn cứ (None = chưa biết, khi suy từ chữ);
        # dòng đã 'manual' (người sửa level) được giữ nguyên (xem db.job_levels).
        source = plan["new_level_source"]
        db.update_job(conn, row["job_id"], level_id=new_level_id, level_source=source,
                      level_rule_version=normalize.LEVEL_RULE_VERSION if source else None,
                      level_signals=plan["new_level_signals"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def run(conn, adapter, rows: list, *, apply: bool, level_ids: dict,
        on_done=None) -> Summary:
    """Duyệt danh sách job. Lỗi một job chỉ đếm rồi đi tiếp; bị chặn thì dừng;
    Ctrl+C dừng êm (job đã xong vẫn còn nguyên)."""
    summary = Summary()
    summary.selected = len(rows)
    try:
        for i, row in enumerate(rows, 1):
            try:
                finished = process_job(conn, adapter, row, apply=apply,
                                       level_ids=level_ids, summary=summary)
                if finished and on_done is not None:
                    on_done(row["job_id"])
            except CrawlBlockedError as exc:
                summary.blocked = True
                logger.error("Bị chặn, dừng script (chạy lại sau): %s", exc)
                break
            except Exception as exc:  # noqa: BLE001 - lỗi một job không dừng cả đợt
                summary.errors += 1
                logger.error("Lỗi xử lý job %s: %s", row["source_url"], exc)
            if i % _PROGRESS_EVERY == 0:
                logger.info("--- Tiến độ: %d/%d job", i, len(rows))
    except KeyboardInterrupt:
        summary.interrupted = True
        logger.warning("Đã dừng theo yêu cầu (Ctrl+C).")
    return summary


# ----------------------------------------------------------------------
# File tiến độ + báo cáo + CLI
# ----------------------------------------------------------------------
def load_done(path: str) -> set:
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {line.strip() for line in f if line.strip()}


def print_report(summary: Summary, *, apply: bool, fetch: bool, dup_before: int,
                 dup_after: Optional[int]) -> None:
    mode = "ĐÃ GHI DB" if apply else "CHẠY THỬ (không ghi DB)"
    print(f"\n===== KẾT QUẢ — {mode} =====")
    print(f"Job Senior được chọn:     {summary.selected}")
    print(f"  - level do tiêu đề quyết định (bỏ qua, giữ): {summary.title_decided}")
    if fetch:
        print(f"  - đọc được nhãn trang:  {summary.fetched_ok}")
        print(f"  - trang không có nhãn:  {summary.no_label} (suy từ chữ)")
        print(f"  - không tải được:       {summary.unavailable} (suy từ chữ)")
    print(f"Đổi level theo nhãn trang (chắc chắn): {summary.changed_by_label}")
    print(f"Đổi level theo suy từ chữ (gần đúng):  {summary.changed_by_text}")
    print(f"Giữ nguyên:               {summary.unchanged}")
    print(f"Lỗi khi xử lý:            {summary.errors}")
    if summary.blocked:
        print("!! Script dừng vì bị chặn. Chạy lại sau.")
    if summary.interrupted:
        print("!! Script dừng theo Ctrl+C.")
    if summary.transitions:
        print("\nLevel cũ -> mới:")
        for (old, new, basis), n in sorted(summary.transitions.items(), key=lambda kv: -kv[1]):
            print(f"  {old:>7} -> {new:<7} ({basis}) {n}")
    if summary.samples:
        print("\nMột vài job đổi level:")
        for title, old, new, basis, label in summary.samples:
            extra = f", nhãn {label!r}" if label else ""
            print(f"  {title[:60]}: {old} -> {new} ({basis}{extra})")
    print(f"\nNhóm job nghi trùng (v_duplicate_job_candidates): trước = {dup_before}"
          + (f", sau = {dup_after}" if dup_after is not None else ""))
    if not apply:
        print("\nĐây là chạy thử. Thêm --apply để ghi thật.")


def main():
    parser = argparse.ArgumentParser(
        description="Vá level Senior -> Lead cho job TopCV nhãn \"Trên 5 năm\" đã lưu")
    parser.add_argument("--apply", action="store_true", help="ghi vào DB (mặc định chỉ chạy thử)")
    parser.add_argument("--limit", type=int, default=None, help="chỉ xử lý N job đầu")
    parser.add_argument("--no-fetch", action="store_true", dest="no_fetch",
                        help="không tải trang, chỉ suy từ chữ trong phần yêu cầu (gần đúng)")
    parser.add_argument("--include-closed", action="store_true", help="gồm cả job đã CLOSED")
    parser.add_argument("--created-before", default=None, metavar="YYYY-MM-DD",
                        help="chỉ lấy job tạo trước ngày này")
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE, help="file ghi job đã xong")
    parser.add_argument("--reset-state", action="store_true", help="xoá file tiến độ rồi chạy")
    args = parser.parse_args()

    if args.reset_state and os.path.exists(args.state_file):
        os.remove(args.state_file)
        logger.info("Đã xoá file tiến độ %s", args.state_file)

    conn = db.get_connection()
    try:
        rows = select_jobs(conn, include_closed=args.include_closed, limit=args.limit,
                           before=args.created_before)
        done = load_done(args.state_file)
        if done:
            before = len(rows)
            rows = [r for r in rows if str(r["job_id"]) not in done]
            logger.info("Bỏ qua %d job đã xong ở lần chạy trước (file %s)",
                        before - len(rows), args.state_file)
        logger.info("Có %d job cần xử lý (%s, %s)", len(rows),
                    "GHI DB" if args.apply else "chạy thử",
                    "chỉ suy từ chữ" if args.no_fetch else "tải trang + suy từ chữ dự phòng")

        level_ids = {code: db.get_level_id(conn, code) for code in normalize.LEVEL_ORDER}
        conn.rollback()
        dup_before = count_duplicate_groups(conn)

        adapter = None if args.no_fetch else TopCVAdapter()
        state_fh = open(args.state_file, "a", encoding="utf-8") if args.apply else None
        try:
            def on_done(job_id):
                state_fh.write(f"{job_id}\n")
                state_fh.flush()

            summary = run(conn, adapter, rows, apply=args.apply, level_ids=level_ids,
                          on_done=on_done if args.apply else None)
        finally:
            if state_fh is not None:
                state_fh.close()

        dup_after = count_duplicate_groups(conn) if args.apply else None
        print_report(summary, apply=args.apply, fetch=not args.no_fetch,
                     dup_before=dup_before, dup_after=dup_after)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
