"""
Script RIÊNG (không nằm trong pipeline crawl chính) — vá lại job VietnamWorks
ĐÃ LƯU từ trước khi pipeline biết tải trang chi tiết (10/2026).

BỐI CẢNH: API search của VietnamWorks cắt jobDescription/jobRequirement
(kết thúc bằng "...") và luôn trả yearsOfExperience = 0. Hậu quả với dữ liệu cũ:
  - JD và yêu cầu lưu trong DB bị cắt (1239/1291 job lúc đo);
  - level gần như luôn là Junior (907/1413 job), rất ít Middle.
Job crawl SAU bản sửa đã đúng; script này chỉ vá job cũ.

Script làm gì, với mỗi job chọn được:
  1. Tải lại trang chi tiết (đúng hàm của adapter, cùng delay/throttle/ngắt mạch
     như crawl thường).
  2. Thay mô tả/yêu cầu/quyền lợi/kỹ năng bằng bản đầy đủ (field nào trang trả
     về rỗng thì GIỮ giá trị cũ, không ghi đè bằng rỗng); đồng bộ lại
     job_sources_log.raw_jd_content của đúng URL đó.
  3. Tính lại level bằng normalize.infer_level (cùng quy tắc với crawl mới).
  4. Ghi dấu job_sources_log.detail_checked_at.
Mỗi job là một transaction riêng, chạy lại an toàn.

CHẠY THỬ TRƯỚC (mặc định, KHÔNG ghi DB, vẫn tải trang nên tốn request):
    python -m scripts.backfill.backfill_vnw_detail --limit 20
Xem bảng "level cũ -> mới", rồi mới ghi thật:
    python -m scripts.backfill.backfill_vnw_detail --limit 20 --apply
    python -m scripts.backfill.backfill_vnw_detail --apply            # chạy hết, có thể ngắt giữa chừng

Chọn job nào (mặc định): job VietnamWorks chưa đóng, chưa ai sửa tay
(updated_by IS NULL), và (JD bị cắt HOẶC level đang là Junior).
  --all             bỏ điều kiện "JD cắt/Junior", xử lý mọi job VietnamWorks
                    (dùng khi muốn rà cả Manager/Lead cũ).
  --include-closed  gồm cả job đã CLOSED (thường 404, tốn request vô ích).
  --created-before YYYY-MM-DD
                    chỉ lấy job tạo TRƯỚC ngày đó. Đặt bằng ngày bản sửa VietnamWorks
                    bắt đầu crawl, để không tải lại job mới vốn đã đúng.
Thứ tự: job CŨ NHẤT trước, nên --limit 20 thử trên đúng nhóm cần vá.
KHÔNG chạy cùng lúc với một lượt crawl VietnamWorks (hai tiến trình cùng gửi
request tới một site, dễ bị chặn).

KHÔNG đụng job đã có updated_by: người trong team đã sửa tay, không ghi đè.
Job không tải được (404/lỗi mạng), trang chuyển hướng /410 (VietnamWorks trả HTTP
200; chính trang đó ghi "có thể đã bị xóa hoặc tạm thời không hỗ trợ", nên chỉ là
tín hiệu chưa chắc chắn) hoặc trang không giải mã được thì giữ nguyên và đếm riêng.
Script KHÔNG tự đóng job. Nếu tiêu đề trên trang khác hẳn tiêu đề đã lưu (nhà
tuyển dụng đổi tin sang vị trí khác nhưng giữ mã job) thì cũng giữ nguyên và đếm
riêng, để không ghi JD của vị trí này vào dòng mang tiêu đề vị trí kia. Nếu trang
chuyển hướng sang slug mới của CÙNG mã job
(nhà tuyển dụng sửa tiêu đề) thì script đi theo 1 bước rồi vá bình thường. Ngắt mạch (bị chặn liên tiếp) dừng cả script; chạy lại sau.

TIẾN ĐỘ: với --apply, job đã xử lý xong được ghi vào file trạng thái
(mặc định .backfill_vnw_detail.done) để lần chạy sau bỏ qua; dùng --reset-state
để xoá. Dry-run không ghi file này.

LƯU Ý TRÙNG LẶP: đổi level_id làm trigger set_job_hash tính lại content_hash (khoá cũ,
còn gồm level). Khoá chống trùng hiện hành (dedup_key = company + title + province) không
gồm level nên nhóm job nghi trùng KHÔNG đổi vì việc này. Script KHÔNG tự gộp, chỉ in số
nhóm trong view v_duplicate_job_candidates trước và sau (kỳ vọng bằng nhau).
"""

import argparse
import logging
import os
from collections import Counter
from typing import Callable, Optional

from scrapjd import db
from scrapjd import normalize
from scrapjd.adapters.base import CrawlBlockedError
from scrapjd.adapters.vietnamworks import VietnamWorksAdapter
from scrapjd.pipeline import _build_parsed_content_and_raw  # cùng cách dựng parsed_content như crawl
from scrapjd.db.pg_types import Conn, fetch_scalar

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                     datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

SOURCE_NAME = "VietnamWorks"
DEFAULT_STATE_FILE = ".backfill_vnw_detail.done"
_JD_KEYS = ("job_description", "requirements", "perks", "required_skills")
_PROGRESS_EVERY = 25
_SAMPLE_LIMIT = 10

_SELECT_SQL = """
SELECT * FROM (
    SELECT DISTINCT ON (jp.job_id)
           jp.job_id, jp.job_title, l.level_code, jp.parsed_content,
           jp.created_at, jsl.source_url
    FROM job_postings jp
    JOIN job_sources_log jsl ON jsl.job_id = jp.job_id AND jsl.source_name = %(source)s
    LEFT JOIN levels l ON l.level_id = jp.level_id
    WHERE jp.updated_by IS NULL
      AND (%(all_jobs)s
           OR l.level_code = 'Junior'
           OR right(rtrim(jp.parsed_content->>'requirements'), 3) = '...'
           OR right(rtrim(jp.parsed_content->>'job_description'), 3) = '...')
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
def select_jobs(conn: Conn, *, all_jobs: bool, include_closed: bool, limit: Optional[int],
                before: Optional[str] = None) -> list:
    """Danh sách job cần vá, CŨ NHẤT TRƯỚC (job cũ mới là thứ cần vá; job crawl sau
    bản sửa đã đúng, xử lý chúng chỉ tốn request). `before` (YYYY-MM-DD) chỉ lấy
    job tạo trước ngày đó. Mỗi job một dòng (URL VietnamWorks đầu tiên của job đó,
    tức nguồn chính). Đóng transaction đọc trước khi trả về."""
    with conn.cursor() as cur:
        cur.execute(_SELECT_SQL, {
            "source": SOURCE_NAME, "all_jobs": all_jobs,
            "include_closed": include_closed, "limit": limit, "before": before,
        })
        cols = ("job_id", "job_title", "level_code", "parsed_content", "created_at", "source_url")
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    conn.rollback()
    return rows


def count_duplicate_groups(conn: Conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM v_duplicate_job_candidates")
        n = fetch_scalar(cur)
    conn.rollback()
    return n


def _update_raw_jd(conn: Conn, job_id: str, source_url: str, raw_jd: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE job_sources_log SET raw_jd_content = %s WHERE job_id = %s AND source_url = %s",
            (raw_jd, job_id, source_url),
        )


# ----------------------------------------------------------------------
# Chốt chặn "trang này có còn là job đó không"
# ----------------------------------------------------------------------
# Nhà tuyển dụng VietnamWorks có thể sửa một tin đăng thành vị trí KHÁC HẲN mà
# vẫn giữ mã job, nên chỉ vá khi tiêu đề trên trang còn "gần giống" tiêu đề đã
# lưu. Hàm so khớp nằm ở normalize.titles_similar (pipeline crawl cũng dùng);
# lý do và dữ liệu thật xem chú thích ở đó.


# ----------------------------------------------------------------------
# Logic thuần (không DB, không mạng) — có test
# ----------------------------------------------------------------------
def plan_job(row: dict, refreshed: dict) -> dict:
    """Từ dòng DB + dữ liệu vừa tải về, tính thứ cần ghi.

    Trả dict: new_level, new_level_source, new_level_signals, level_changed, parsed_content (None nếu không có gì để
    ghi), raw_jd, jd_changed. Field JD trang trả về rỗng thì giữ giá trị cũ."""
    old = row.get("parsed_content") or {}
    empty = {k: ([] if k == "required_skills" else "") for k in _JD_KEYS}
    old_normalized = {k: old.get(k) or empty[k] for k in _JD_KEYS}
    merged = dict(old_normalized)
    for key, value in (refreshed.get("detail") or {}).items():
        if key in _JD_KEYS and value:
            merged[key] = value

    parsed_content, raw_jd = _build_parsed_content_and_raw(merged)
    decision = normalize.derive_level(
        refreshed.get("experience_text", ""), row["job_title"], refreshed.get("level_hint", ""),
    )
    new_level = decision.level
    return {
        "new_level": new_level,
        "new_level_source": decision.source,
        # Đúng hai chuỗi vừa đưa cho derive_level, để tính lại sau này không tải lại trang.
        "new_level_signals": normalize.build_level_signals(
            refreshed.get("experience_text", ""), refreshed.get("level_hint", ""),
        ),
        "level_changed": new_level != row.get("level_code"),
        "parsed_content": parsed_content,
        "raw_jd": raw_jd,
        "jd_changed": parsed_content is not None and parsed_content != old_normalized,
    }


class Summary:
    def __init__(self) -> None:
        self.selected = 0
        self.ok = 0
        self.unavailable = 0
        self.gone = 0
        self.title_mismatch = 0
        self.unparsable = 0
        self.errors = 0
        self.level_changed = 0
        self.jd_changed = 0
        self.unchanged = 0
        self.blocked = False
        self.interrupted = False
        self.transitions: Counter = Counter()
        self.samples: list = []


def process_job(conn: Conn, adapter: VietnamWorksAdapter, row: dict, *, apply: bool, level_ids: dict,
                summary: Summary) -> bool:
    """Xử lý một job. Trả True khi job đã xử lý xong và nên ghi vào file tiến độ
    (đã ghi DB thành công, hoặc không còn gì để vá)."""
    status, refreshed = adapter.fetch_refreshed_job(row["source_url"])
    if status == adapter.REFRESH_UNAVAILABLE:
        summary.unavailable += 1
        logger.info("Không tải được (giữ nguyên): %s", row["source_url"])
        return False
    if status == adapter.REFRESH_GONE:
        summary.gone += 1
        return False
    if status == adapter.REFRESH_UNPARSABLE:
        summary.unparsable += 1
        return False

    page_title = refreshed.get("page_title", "")
    if not normalize.titles_similar(row["job_title"], page_title):
        summary.title_mismatch += 1
        logger.warning("Tiêu đề khác hẳn, KHÔNG vá (tin có thể đã bị đổi thành vị trí khác): "
                       "đã lưu %r, trên trang %r | %s", row["job_title"], page_title,
                       row["source_url"])
        return False

    summary.ok += 1
    plan = plan_job(row, refreshed)
    if plan["level_changed"]:
        summary.level_changed += 1
        summary.transitions[(row.get("level_code") or "(trống)", plan["new_level"])] += 1
        if len(summary.samples) < _SAMPLE_LIMIT:
            summary.samples.append((row["job_title"], row.get("level_code"), plan["new_level"]))
    if plan["jd_changed"]:
        summary.jd_changed += 1
    if not plan["level_changed"] and not plan["jd_changed"]:
        summary.unchanged += 1
    logger.info("%s | level %s -> %s | JD %s | %s", row["job_title"][:50], row.get("level_code"),
                plan["new_level"], "đổi" if plan["jd_changed"] else "giữ", row["source_url"])

    if not apply:
        return False

    new_level_id = level_ids.get(plan["new_level"])
    if plan["level_changed"] and new_level_id is None:
        raise RuntimeError(f"Không tìm thấy level_id cho '{plan['new_level']}' trong bảng levels")
    try:
        if plan["level_changed"]:
            # Ghi level TỰ ĐỘNG: đóng dấu căn cứ + phiên bản quy tắc; dòng đã
            # 'manual' (người sửa level) được giữ nguyên (xem db.job_levels).
            db.update_job(conn, row["job_id"], level_id=new_level_id,
                          level_source=plan["new_level_source"],
                          level_rule_version=normalize.LEVEL_RULE_VERSION,
                          level_signals=plan["new_level_signals"])
        if plan["jd_changed"]:
            db.update_job_fields(conn, row["job_id"], parsed_content=plan["parsed_content"])
            _update_raw_jd(conn, row["job_id"], row["source_url"], plan["raw_jd"])
        db.mark_source_detail_checked(conn, row["source_url"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return True


def run(conn: Conn, adapter: VietnamWorksAdapter, rows: list, *, apply: bool, level_ids: dict,
        on_done: Optional[Callable[[str], None]] = None) -> Summary:
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


def print_report(summary: Summary, *, apply: bool, dup_before: int, dup_after: Optional[int]) -> None:
    mode = "ĐÃ GHI DB" if apply else "CHẠY THỬ (không ghi DB)"
    print(f"\n===== KẾT QUẢ — {mode} =====")
    print(f"Job được chọn:            {summary.selected}")
    print(f"Tải và giải mã được:      {summary.ok}")
    print(f"  - đổi level:            {summary.level_changed}")
    print(f"  - đổi JD:               {summary.jd_changed}")
    print(f"  - không có gì đổi:      {summary.unchanged}")
    print(f"Không tải được (giữ):     {summary.unavailable}")
    print(f"Chuyển hướng /410, có vẻ đã gỡ (giữ nguyên, chưa xác nhận): {summary.gone}")
    print(f"Tiêu đề trang khác hẳn tiêu đề đã lưu (giữ, cần xem tay): {summary.title_mismatch}")
    print(f"Không giải mã được (giữ): {summary.unparsable}")
    print(f"Lỗi khi xử lý:            {summary.errors}")
    if summary.blocked:
        print("!! Script dừng vì bị chặn. Chạy lại sau.")
    if summary.interrupted:
        print("!! Script dừng theo Ctrl+C.")
    if summary.transitions:
        print("\nLevel cũ -> mới:")
        for (old, new), n in sorted(summary.transitions.items(), key=lambda kv: -kv[1]):
            print(f"  {old:>9} -> {new:<9} {n}")
    if summary.samples:
        print("\nMột vài job đổi level:")
        for title, old, new in summary.samples:
            print(f"  {title[:60]}: {old} -> {new}")
    print(f"\nNhóm job nghi trùng (v_duplicate_job_candidates): trước = {dup_before}"
          + (f", sau = {dup_after}" if dup_after is not None else ""))
    if not apply:
        print("\nĐây là chạy thử. Thêm --apply để ghi thật.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Vá JD đầy đủ + level cho job VietnamWorks đã lưu")
    parser.add_argument("--apply", action="store_true", help="ghi vào DB (mặc định chỉ chạy thử)")
    parser.add_argument("--limit", type=int, default=None, help="chỉ xử lý N job đầu")
    parser.add_argument("--all", action="store_true", dest="all_jobs",
                        help="xử lý mọi job VietnamWorks, không chỉ JD cắt/Junior")
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
        rows = select_jobs(conn, all_jobs=args.all_jobs, include_closed=args.include_closed,
                           limit=args.limit, before=args.created_before)
        done = load_done(args.state_file)
        if done:
            before = len(rows)
            rows = [r for r in rows if str(r["job_id"]) not in done]
            logger.info("Bỏ qua %d job đã xong ở lần chạy trước (file %s)",
                        before - len(rows), args.state_file)
        logger.info("Có %d job cần xử lý (%s)", len(rows), "GHI DB" if args.apply else "chạy thử")

        level_ids = {code: db.get_level_id(conn, code) for code in normalize.LEVEL_ORDER}
        conn.rollback()
        dup_before = count_duplicate_groups(conn)

        adapter = VietnamWorksAdapter()
        state_fh = open(args.state_file, "a", encoding="utf-8") if args.apply else None
        try:
            def on_done(job_id: str) -> None:
                assert state_fh is not None  # on_done chỉ được truyền khi --apply, lúc đó file tiến độ đã mở
                state_fh.write(f"{job_id}\n")
                state_fh.flush()

            summary = run(conn, adapter, rows, apply=args.apply, level_ids=level_ids,
                          on_done=on_done if args.apply else None)
        finally:
            if state_fh is not None:
                state_fh.close()

        dup_after = count_duplicate_groups(conn) if args.apply else None
        print_report(summary, apply=args.apply, dup_before=dup_before, dup_after=dup_after)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
