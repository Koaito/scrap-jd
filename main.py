"""
CLI chạy crawler.

Ví dụ:
    python main.py init-db
    python main.py migrate --check
    python main.py migrate
    python main.py migrate --baseline --except migration_add_crawl_snapshots.sql
    python main.py crawl --category data-analyst --pages 3
    python main.py crawl --category data-engineer --pages 5
    python main.py crawl --category data-analyst --max-jobs 20
    python main.py crawl --category data-analyst --max-jobs 5 --no-track   # chạy thử, không ghi lịch sử
    python main.py recompute-levels            # chạy thử: chỉ in, không ghi
    python main.py recompute-levels --apply    # ghi thật (updated_at giữ nguyên)
    python main.py report-duplicates           # báo cáo job nghi trùng (chỉ đọc)
    python main.py report-duplicates --csv trung.csv   # xuất toàn bộ nhóm ra file duyệt tay
    python main.py report-reposts              # đo tỷ lệ gộp nhầm: so JD các tin đã gộp (chỉ đọc)
    python main.py report-reposts --csv gop.csv --threshold 0.3   # xuất từng cặp tin, chỉnh ngưỡng nghi gộp nhầm
    python main.py check-listing-derivation    # so giá trị job suy ra từ listing với giá trị đang lưu (chỉ đọc)
    python main.py check-listing-derivation --csv lech.csv --strict   # xuất từng trường lệch; mã thoát 2 nếu còn lệch
    python main.py merge-duplicates            # gộp job trùng: CHẠY THỬ, chỉ in kế hoạch (không ghi)
    python main.py merge-duplicates --only duyet.csv --csv ke_hoach.csv   # chỉ nhóm đã duyệt tay
    python main.py merge-duplicates --apply --limit 1   # gộp thật 1 nhóm đầu (hỏi xác nhận; backup DB trước)
    python main.py merge-duplicates --apply --yes       # gộp thật, bỏ qua hỏi xác nhận (chạy tự động)
    python main.py stats
    python main.py snapshots --source careerviet
    python main.py snapshot-export 12 --out tests/fixture_careerviet_listing.html
    python main.py create-admin --email admin@congty.vn --name "Nguyễn Văn A"
"""

import argparse
import getpass
import logging
import sys

from scrapjd import db
from scrapjd.adapters.base import CrawlBlockedError
from scrapjd.pipeline import run_pipeline
from scrapjd.cli import check_listing_derivation
from scrapjd.cli import duplicate_report
from scrapjd.cli import merge_duplicates
from scrapjd.cli import recompute_levels
from scrapjd.cli import repost_report
from scrapjd.config import (
    TOPCV_CATEGORIES, VIETNAMWORKS_CATEGORIES, DEFAULT_CATEGORY, DEFAULT_MAX_PAGES,
)
# SOURCES/DEFAULT_SOURCE giờ sống ở 1 nguồn sự thật duy nhất
# (scrapjd/sources_registry.py) — xem docstring file đó để biết lý do (trước
# đây bị khai báo lặp lại thủ công ở đây + 3 nơi khác trong scrapjd/api/, dễ
# lệch, đã từng gây bug CareerViet "crawl được nhưng không hiện trên
# web"). Thêm nguồn crawl mới -> sửa scrapjd/sources_registry.py, KHÔNG sửa
# file này.
from scrapjd.sources_registry import SOURCES, DEFAULT_SOURCE
from scrapjd.db.pg_types import Conn, fetch_scalar

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def cmd_init_db(args):
    """Dựng/cập nhật schema từ sql/schema.sql, rồi ghi nhận mọi migration (cũ migration_*.sql
    và mới NNNN_*.sql) hiện có là đã áp dụng (schema.sql đã chứa kết quả của chúng), để `migrate`
    về sau chỉ chạy các migration mới."""
    conn = db.get_connection()
    try:
        db.apply_schema(conn)
        db.baseline_migrations(conn)
        print("✅ Đã tạo/cập nhật schema trong database.")
    finally:
        conn.close()


# Bảng mà `migrate --baseline` yêu cầu phải có sẵn trước khi ghi nhận DB là
# "đã ở trạng thái mới nhất". Giá trị là file migration tạo bảng đó (None =
# luôn bắt buộc). Bảng thiếu mà file tạo nó nằm trong --except thì được bỏ qua.
_BASELINE_REQUIRED_TABLES = {
    "app_users": None,
    "job_postings": None,
    "companies": None,
    "crawl_runs": "migration_add_crawl_runs.sql",
    "crawl_batches": "migration_add_crawl_batches.sql",
    "crawl_run_logs": "migration_add_crawl_progress_logs.sql",
    "maintenance_runs": "migration_add_maintenance_runs.sql",
    "import_previews": "migration_add_import_export.sql",
}


def _missing_baseline_tables(conn: Conn, except_files):
    """Các bảng bắt buộc còn thiếu trong DB (sau khi trừ bảng sẽ được tạo bởi
    file nằm trong except_files)."""
    missing = []
    with conn.cursor() as cur:
        for table, creator in _BASELINE_REQUIRED_TABLES.items():
            cur.execute("SELECT to_regclass(%s)", (f"public.{table}",))
            if fetch_scalar(cur) is None and creator not in except_files:
                missing.append(table)
    return missing


def _cmd_migrate_baseline(conn: Conn, args):
    """`migrate --baseline`: ghi nhận migration chưa có log là đã áp dụng mà
    KHÔNG chạy SQL. Chỉ dùng khi DB đã ở trạng thái mới nhất."""
    except_files = set(args.except_files or [])
    pending = db.list_pending_migrations(conn)
    unknown = sorted(except_files - set(pending))
    if unknown:
        print("❌ --except chứa file không nằm trong danh sách migration chưa áp dụng:")
        for filename in unknown:
            print(f"   - {filename}")
        sys.exit(1)

    missing = _missing_baseline_tables(conn, except_files)
    if missing:
        print("❌ DB này chưa có đủ bảng để coi là \"đã ở trạng thái mới nhất\":")
        for table in missing:
            print(f"   - thiếu bảng {table}")
        print("   Không baseline. Với DB mới hãy dùng `python main.py init-db`; "
              "với DB cũ, thêm file tạo bảng đó vào --except để chạy thật.")
        sys.exit(1)

    to_mark = [f for f in pending if f not in except_files]
    if not to_mark:
        print("✅ Không có migration nào cần ghi nhận.")
        return
    print(f"Sẽ GHI NHẬN {len(to_mark)} migration là đã áp dụng (KHÔNG chạy SQL):")
    for filename in to_mark:
        print(f"   - {filename}")
    if except_files:
        print("Giữ nguyên chưa áp dụng (sẽ chạy ở lần `migrate` sau):")
        for filename in sorted(except_files):
            print(f"   - {filename}")
    if not args.yes:
        answer = input("Gõ 'yes' để xác nhận: ").strip().lower()
        if answer != "yes":
            print("Đã huỷ, không thay đổi gì.")
            sys.exit(1)

    marked = db.baseline_migrations(conn, except_files=except_files)
    print(f"✅ Đã ghi nhận {len(marked)} migration.")
    if except_files:
        print("   Chạy tiếp `python main.py migrate` để áp dụng các file còn lại.")


def cmd_migrate(args):
    """Chạy MỌI migration (sql/: cũ migration_*.sql rồi mới NNNN_*.sql theo số) chưa được áp
    dụng cho DB đang kết nối — xem docstring db.apply_migrations()/db.connection để biết
    cơ chế tracking (bảng schema_migrations) và lý do an toàn chạy lại
    trên DB đã tồn tại từ trước (mọi migration đều idempotent).

    --check: CHỈ liệt kê migration còn thiếu, KHÔNG chạy gì — dùng để
    kiểm tra trước khi deploy (vd script CI/CD có thể gọi lệnh này,
    exit code khác 0 nếu còn migration chưa chạy, để chặn deploy sớm
    thay vì phát hiện lỗi sau khi code mới đã lên production mà DB
    chưa kịp cập nhật).

    --baseline: chỉ ghi nhận migration chưa có log là đã áp dụng, KHÔNG chạy
    SQL — dùng một lần cho DB đã ở trạng thái mới nhất nhưng bảng
    schema_migrations còn thiếu. --except <file> (lặp lại được) giữ file đó
    ở trạng thái chưa áp dụng để lần `migrate` sau chạy thật."""
    if (args.except_files or args.yes) and not args.baseline:
        print("❌ --except và --yes chỉ dùng kèm --baseline.")
        sys.exit(1)
    if args.baseline and args.check:
        print("❌ Không dùng --baseline cùng --check.")
        sys.exit(1)
    conn = db.get_connection()
    try:
        if args.baseline:
            _cmd_migrate_baseline(conn, args)
            return
        if args.check:
            pending = db.list_pending_migrations(conn)
            if not pending:
                print("✅ DB đã theo kịp mọi migration (sql/) — không có gì cần chạy.")
                return
            print(f"⚠️  Còn {len(pending)} migration CHƯA áp dụng cho DB này:")
            for filename in pending:
                print(f"   - {filename}")
            sys.exit(1)

        applied = db.apply_migrations(conn)
        if not applied:
            print("✅ DB đã theo kịp mọi migration (sql/) — không có gì cần chạy.")
        else:
            print(f"✅ Đã áp dụng {len(applied)} migration mới:")
            for filename in applied:
                print(f"   - {filename}")
    finally:
        conn.close()


def cmd_recompute_levels(args):
    """Tính lại level cho job chưa đóng dấu / dấu cũ hơn LEVEL_RULE_VERSION, hoàn toàn
    trong DB. Mặc định chạy thử; --apply mới ghi. Xem docstring scrapjd/cli/recompute_levels.py."""
    sys.exit(recompute_levels.run_cli(args))


def cmd_report_duplicates(args):
    """Báo cáo job nghi trùng (Phần 3a), CHỈ ĐỌC. Xem docstring scrapjd/cli/duplicate_report.py."""
    sys.exit(duplicate_report.run_cli(args))


def cmd_report_reposts(args):
    """Đo tỷ lệ gộp nhầm (A5), CHỈ ĐỌC. Xem docstring scrapjd/cli/repost_report.py."""
    sys.exit(repost_report.run_cli(args))


def cmd_check_listing_derivation(args):
    """So job suy ra từ listing với job đang lưu (C2), CHỈ ĐỌC. Xem docstring scrapjd/cli/check_listing_derivation.py."""
    sys.exit(check_listing_derivation.run_cli(args))


def cmd_merge_duplicates(args):
    """Gộp job trùng (Phần 3b). Mặc định chạy thử; --apply mới gộp thật. Xem docstring scrapjd/cli/merge_duplicates.py."""
    sys.exit(merge_duplicates.run_cli(args))


def cmd_create_admin(args):
    """Tạo tài khoản ADMIN đầu tiên — chỉ dùng qua CLI (chạy trực tiếp
    trên máy/server có quyền truy cập DB), vì POST /auth/users trên API
    yêu cầu ĐÃ CÓ admin để gọi (require_admin) — "con gà quả trứng" lúc
    khởi tạo hệ thống lần đầu. Sau khi có 1 admin, tạo user tiếp theo
    (admin hoặc member) nên làm qua POST /auth/users từ frontend."""
    # Import ở đây (không import ở đầu file) vì scrapjd/api/security.py raise
    # lỗi ngay lúc import nếu thiếu JWT_SECRET_KEY — không muốn việc đó
    # chặn luôn các lệnh CLI khác (crawl/stats) vốn không cần tới auth.
    from scrapjd.api import security

    email = args.email
    full_name = args.name

    conn = db.get_connection()
    try:
        if db.get_user_by_email(conn, email) is not None:
            print(f"❌ Email '{email}' đã có tài khoản.")
            sys.exit(1)

        password = getpass.getpass("Nhập mật khẩu cho tài khoản admin (không hiện ký tự): ")
        password_confirm = getpass.getpass("Nhập lại mật khẩu: ")
        if password != password_confirm:
            print("❌ 2 lần nhập mật khẩu không khớp.")
            sys.exit(1)
        if len(password) < 8:
            print("❌ Mật khẩu cần tối thiểu 8 ký tự.")
            sys.exit(1)

        ss_user_id = db.create_user(
            conn,
            full_name=full_name,
            email=email,
            password_hash=security.hash_password(password),
            role="admin",
            must_change_password=False,  # tự gõ mật khẩu thật ngay từ đầu, không cần ép đổi lại
        )
        conn.commit()
        print(f"✅ Đã tạo tài khoản admin: {full_name} <{email}> (ss_user_id={ss_user_id})")
    finally:
        conn.close()


def cmd_crawl(args):
    if args.source not in SOURCES:
        print(f"❌ Source '{args.source}' không tồn tại. "
              f"Các source có sẵn: {list(SOURCES.keys())}")
        sys.exit(1)

    source_cfg = SOURCES[args.source]
    categories = source_cfg["categories"]
    if args.category not in categories:
        print(f"❌ Category '{args.category}' không tồn tại cho source '{args.source}'. "
              f"Các category có sẵn: {list(categories.keys())}")
        sys.exit(1)

    # --pages mặc định để None (chưa gán DEFAULT_MAX_PAGES ngay ở argparse)
    # để phân biệt được "người dùng không truyền --pages" với "truyền
    # đúng bằng giá trị mặc định" — cần biết điều này để quyết định có
    # tự động nới --pages lên khi chỉ dùng --max-jobs hay không (xem bên
    # dưới).
    if args.pages is not None:
        effective_pages = args.pages
    elif args.max_jobs is not None:
        # Chỉ giới hạn theo --max-jobs, không quan tâm số trang -> nới
        # --pages lên rất cao để KHÔNG PHẢI --pages là thứ chặn crawl lại
        # (--max-jobs mới là giới hạn thực sự người dùng muốn). Vẫn an
        # toàn vì vòng lặp trong scrapjd/pipeline.py sẽ dừng ngay khi đủ
        # --max-jobs, không thật sự crawl tới 999 trang.
        effective_pages = 999
    else:
        effective_pages = DEFAULT_MAX_PAGES

    if getattr(args, "no_track", False):
        _crawl_untracked(args, source_cfg, effective_pages)
        return

    # Mặc định (đợt 3.5, 10/2026): chạy qua CÙNG đường với nút "Crawl" trên web
    # (scrapjd/api/crawl_runner.execute) — có dòng trong crawl_runs, cờ blocked/degraded,
    # snapshot HTML gốc, log live. Trước đây CLI gọi thẳng run_pipeline() nên
    # lượt chạy trên máy không để lại dấu vết nào trong DB.
    from scrapjd.api import crawl_runner

    conn = db.get_connection()
    try:
        try:
            run_id = db.create_crawl_run(
                conn, source=args.source, category=args.category,
                pages=effective_pages, max_jobs=args.max_jobs, triggered_by=None,
            )
        except db.ActiveCrawlExistsError as exc:
            print(f"❌ {exc}")
            print("   (Mỗi nguồn chỉ chạy 1 lượt tại 1 thời điểm — kể cả lượt bấm trên web.)")
            sys.exit(1)
        except Exception as exc:  # noqa: BLE001 - vd bảng crawl_runs chưa có (chưa migrate)
            conn.rollback()
            print(f"⚠️  Không tạo được dòng theo dõi lượt crawl ({exc}).")
            print("   Chạy `python main.py migrate` để bật theo dõi. Lượt này chạy KHÔNG ghi lịch sử.")
            _crawl_untracked(args, source_cfg, effective_pages)
            return

        try:
            crawl_runner.execute(run_id)
        except KeyboardInterrupt:
            # Ctrl+C: execute() không bắt KeyboardInterrupt nên dòng sẽ kẹt
            # 'running' tới khi watchdog dọn (30 phút). Đánh dấu lỗi ngay.
            conn.rollback()
            db.mark_crawl_run_error(conn, run_id, "Bị dừng thủ công (Ctrl+C) trên máy chạy CLI.")
            print("\n⛔ Đã dừng thủ công. Lượt crawl được ghi nhận là lỗi trong lịch sử.")
            sys.exit(130)

        row = db.get_crawl_run(conn, run_id)
        stats = (row or {}).get("stats") or {}
        if row and row["status"] == "error":
            print("\n===== LỖI =====")
            print(f"❌ {row.get('error')}")
            if stats.get("blocked"):
                print(f"Đã crawl {stats.get('fetched', 0)} job, lưu {stats.get('inserted', 0)} "
                      f"trước khi dừng. (bị chặn)")
                sys.exit(2)
            sys.exit(1)
        _print_crawl_result(conn, args, stats)
        print(f"(run_id={run_id} — xem trong lịch sử crawl; snapshot: "
              f"`python main.py snapshots --run-id {run_id}`)")
    finally:
        conn.close()


def _crawl_untracked(args, source_cfg, effective_pages):
    """Đường cũ: gọi thẳng run_pipeline(), KHÔNG ghi crawl_runs/snapshot. Dùng
    khi `--no-track` hoặc bảng crawl_runs chưa có."""
    conn = db.get_connection()
    try:
        adapter = source_cfg["adapter_cls"]()
        try:
            stats = run_pipeline(adapter, conn, args.category, effective_pages,
                                  max_jobs=args.max_jobs)
        except CrawlBlockedError as exc:
            # Bị chặn (trang đầu thất bại hoặc ngắt mạch giữa chừng): in gọn
            # + exit code 2 thay vì traceback, để script/cron phân biệt được
            # "bị chặn" (2) với lỗi chương trình (1).
            partial = exc.stats or {}
            print("\n===== BỊ CHẶN =====")
            print(f"❌ {exc}")
            print(f"Đã crawl {partial.get('fetched', 0)} job, lưu {partial.get('inserted', 0)} "
                  f"trước khi dừng.")
            sys.exit(2)
        _print_crawl_result(conn, args, stats)
    finally:
        conn.close()


def _print_crawl_result(conn: Conn, args, stats):
    print("\n===== KẾT QUẢ =====")
    if args.max_jobs is not None:
        print(f"(Giới hạn theo --max-jobs={args.max_jobs})")
    print(f"Tổng job crawl được : {stats.get('fetched', 0)}")
    print(f"Đã lưu vào DB        : {stats.get('inserted', 0)}")
    print(f"Bỏ qua (đã tồn tại)  : {stats.get('skipped_duplicate', 0)}")
    print(f"Đã vá job cũ (work_type/deadline): {stats.get('updated_existing', 0)}")
    print(f"Bỏ qua (fetch chi tiết thất bại)  : {stats.get('skipped_fetch_failed', 0)}")
    print(f"Bỏ qua (adapter không lấy được chi tiết): {stats.get('skipped_detail_unavailable', 0)}")
    print(f"Bỏ qua (nhà tuyển dụng ẩn danh)   : {stats.get('skipped_anonymous_employer', 0)}")
    print(f"Bỏ qua (URL đã có, không fetch lại): {stats.get('skipped_known_url', 0)}")
    print(f"Lỗi                  : {stats.get('errors', 0)}")
    for group, label in (("listing", "danh sách"), ("detail", "chi tiết JD")):
        info = (stats.get("field_empty") or {}).get(group)
        if not info:
            continue
        empties = [
            f"{name}={f['empty']}/{info['total']} ({f['rate']:.0%})"
            for name, f in info["fields"].items() if f["empty"]
        ]
        print(f"Trường rỗng ({label}, {info['total']} record): "
              f"{', '.join(empties) if empties else 'không có'}")
    degraded = (stats.get("degraded") or {}).get("reasons")
    if degraded:
        print("⚠️  DEGRADED — dữ liệu lượt này nhiều khả năng sai (selector/cấu trúc "
              "trang đổi?):")
        for reason in degraded:
            if reason.get("type") == "field_empty":
                print(f"   - '{reason['field']}' ({reason['group']}) rỗng "
                      f"{reason['empty']}/{reason['total']} ({reason['rate']:.0%})")
            else:
                print(f"   - {reason.get('type')}")
    print(f"Tổng job trong DB hiện tại: {db.count_jobs(conn)}")


def cmd_snapshots(args):
    """Liệt kê snapshot HTML/JSON gốc đã lưu (không kèm nội dung)."""
    conn = db.get_connection()
    try:
        rows = db.list_crawl_snapshots(
            conn, run_id=args.run_id, source=args.source, limit=args.limit,
        )
        if not rows:
            print("Không có snapshot nào (chưa chạy migration? chưa có lượt crawl qua API?).")
            return
        for r in rows:
            print(f"#{r['id']:<6} {r['created_at']:%Y-%m-%d %H:%M}  {r['source']:<13} "
                  f"{r['kind']:<8} {r['reason']:<18} {r['raw_bytes']:>8}B  run={r['run_id']}  {r['url']}")
    finally:
        conn.close()


def cmd_snapshot_export(args):
    """Ghi nội dung 1 snapshot ra file — dùng để thay fixture tổng hợp trong
    tests/ bằng HTML/JSON thật (vd: snapshot-export 12 --out
    tests/fixture_careerviet_listing.html)."""
    conn = db.get_connection()
    try:
        snap = db.get_crawl_snapshot(conn, args.id)
        if snap is None:
            print(f"❌ Không có snapshot id={args.id}.")
            sys.exit(1)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(snap["body"])
        note = " (BỊ CẮT theo SNAPSHOT_MAX_CHARS)" if snap["truncated"] else ""
        print(f"✅ Đã ghi {snap['raw_bytes']} byte -> {args.out}{note}")
        print(f"   {snap['source']} / {snap['kind']} / {snap['reason']} / {snap['url']}")
        print("   ⚠️  Kiểm tra và ẩn dữ liệu cá nhân (email, số điện thoại) trước khi commit "
              "làm fixture.")
    finally:
        conn.close()


def cmd_stats(args):
    conn = db.get_connection()
    try:
        print(f"Tổng job trong DB: {db.count_jobs(conn)}")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="TopCV job crawler cho team Student Success")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Tạo bảng/schema trong PostgreSQL")

    p_migrate = sub.add_parser(
        "migrate",
        help="Áp dụng các migration (sql/: migration_*.sql cũ và NNNN_*.sql mới) CHƯA chạy cho DB này "
             "(xem sql/README_MIGRATIONS.md)",
    )
    p_migrate.add_argument(
        "--check", action="store_true",
        help="Chỉ liệt kê migration còn thiếu, không chạy gì (exit code 1 nếu còn thiếu)",
    )
    p_migrate.add_argument(
        "--baseline", action="store_true",
        help="Ghi nhận migration chưa có log là ĐÃ áp dụng mà không chạy SQL "
             "(dùng một lần cho DB đã ở trạng thái mới nhất)",
    )
    p_migrate.add_argument(
        "--except", dest="except_files", action="append", default=[],
        metavar="FILE",
        help="Kèm --baseline: giữ file migration này ở trạng thái chưa áp dụng "
             "để lần migrate sau chạy thật (lặp lại được)",
    )
    p_migrate.add_argument(
        "--yes", action="store_true",
        help="Kèm --baseline: bỏ qua bước hỏi xác nhận",
    )

    p_recompute = sub.add_parser(
        "recompute-levels",
        help="Tính lại level job theo quy tắc hiện tại, từ tiêu đề + tín hiệu đã lưu (mặc định chạy thử)",
    )
    p_recompute.add_argument(
        "--apply", action="store_true",
        help="Ghi vào DB (mặc định chỉ chạy thử, in báo cáo). Cần đã chạy `migrate`",
    )
    p_recompute.add_argument("--limit", type=int, default=None, help="Chỉ xét N job đầu (cũ nhất trước)")
    p_recompute.add_argument(
        "--batch-size", type=int, default=recompute_levels.DEFAULT_BATCH_SIZE, dest="batch_size",
        help="Số job mỗi lần commit khi --apply (mặc định %(default)s)",
    )
    p_recompute.add_argument(
        "--show", type=int, default=recompute_levels.DEFAULT_SHOW,
        help="Số dòng ví dụ in cho mỗi nhóm trong báo cáo (mặc định %(default)s)",
    )

    p_dups = sub.add_parser(
        "report-duplicates",
        help="Báo cáo job nghi trùng (cùng công ty + tiêu đề), phân loại và đề xuất job giữ. CHỈ ĐỌC, không ghi DB",
    )
    p_dups.add_argument(
        "--show", type=int, default=duplicate_report.DEFAULT_SHOW,
        help="Số nhóm in chi tiết cho MỖI mức độ chắc (cao/cần xem/thấp) (mặc định %(default)s)",
    )
    p_dups.add_argument(
        "--csv", default=None, metavar="FILE",
        help="Xuất toàn bộ nhóm (mỗi job một dòng) ra file CSV để duyệt tay",
    )

    p_reposts = sub.add_parser(
        "report-reposts",
        help="Đo tỷ lệ gộp nhầm: so nội dung JD giữa các tin của cùng một job (tin đăng lại, tin do "
             "merge-duplicates chuyển sang). CHỈ ĐỌC, không ghi DB",
    )
    p_reposts.add_argument(
        "--show", type=int, default=repost_report.DEFAULT_SHOW,
        help="Số job nghi gộp nhầm in chi tiết (mặc định %(default)s)",
    )
    p_reposts.add_argument(
        "--csv", default=None, metavar="FILE",
        help="Xuất mọi cặp tin so được (mỗi cặp một dòng) ra file CSV để duyệt tay",
    )
    p_reposts.add_argument(
        "--threshold", type=float, default=repost_report.DEFAULT_SUSPECT_BELOW,
        help="Độ giống dưới ngưỡng này thì coi là nghi gộp nhầm, trong (0, 0.5] (mặc định %(default)s; "
             "ước lượng ban đầu, chỉnh sau khi xem phân bố)",
    )

    p_derive = sub.add_parser(
        "check-listing-derivation",
        help="So trạng thái, hạn, URL của job suy ra từ listing với giá trị đang lưu trong job_postings, "
             "phân loại chỗ lệch (C2). CHỈ ĐỌC, không ghi DB",
    )
    p_derive.add_argument(
        "--show", type=int, default=check_listing_derivation.DEFAULT_SHOW,
        help="Số dòng ví dụ in cho MỖI loại lệch (mặc định %(default)s; 0 = chỉ in số liệu)",
    )
    p_derive.add_argument(
        "--csv", default=None, metavar="FILE",
        help="Xuất mọi trường lệch (mỗi trường một dòng) ra file CSV",
    )
    p_derive.add_argument(
        "--strict", action="store_true",
        help="Thoát với mã 2 nếu còn bất kỳ trường nào lệch (dùng làm cổng trước khi chuyển bước)",
    )

    p_merge = sub.add_parser(
        "merge-duplicates",
        help="Gộp job trùng (Phần 3b): chọn job giữ, hợp nhất trường, chuyển dữ liệu con, xoá job phụ "
             "(snapshot vào audit_logs). Mặc định chạy thử (in kế hoạch); --apply mới gộp thật",
    )
    p_merge.add_argument(
        "--only", default=None, metavar="FILE",
        help="Chỉ gộp các nhóm đã duyệt tay: file danh sách job_id, hoặc CSV từ report-duplicates "
             "(cột de_xuat_giu = 'x' là job giữ). Không có thì chỉ xét nhóm độ chắc 'cao'",
    )
    p_merge.add_argument(
        "--show", type=int, default=merge_duplicates.DEFAULT_SHOW,
        help="Số nhóm in chi tiết cho mỗi loại (cần chú ý / còn lại) (mặc định %(default)s)",
    )
    p_merge.add_argument(
        "--csv", default=None, metavar="FILE",
        help="Xuất kế hoạch từng nhóm sẽ gộp ra file CSV để duyệt",
    )
    p_merge.add_argument(
        "--apply", action="store_true",
        help="Gộp thật (XOÁ job phụ). Cần đã chạy `migrate`; từ chối nếu có crawl/bảo trì đang chạy; "
             "hỏi xác nhận trước khi ghi. Nên backup DB trước",
    )
    p_merge.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Kèm --apply: chỉ gộp N nhóm đầu của kế hoạch",
    )
    p_merge.add_argument(
        "--yes", action="store_true",
        help="Kèm --apply: bỏ qua bước hỏi xác nhận (dùng khi chạy tự động)",
    )
    p_merge.add_argument(
        "--force", action="store_true",
        help="Kèm --apply: vẫn gộp dù có crawl/bảo trì đang chạy. KHÔNG dừng crawl nào, chỉ bỏ qua kiểm tra",
    )

    p_crawl = sub.add_parser("crawl", help="Crawl job từ TopCV/VietnamWorks và lưu vào DB")
    p_crawl.add_argument("--source", default=DEFAULT_SOURCE,
                          help=f"Nguồn cần crawl. Mặc định: {DEFAULT_SOURCE}. "
                               f"Có sẵn: {list(SOURCES.keys())}")
    p_crawl.add_argument("--category", default=DEFAULT_CATEGORY,
                          help=f"Ngành cần crawl. Mặc định: {DEFAULT_CATEGORY}. "
                               f"Có sẵn (TopCV): {list(TOPCV_CATEGORIES.keys())}; "
                               f"(VietnamWorks): {list(VIETNAMWORKS_CATEGORIES.keys())}")
    p_crawl.add_argument("--pages", type=int, default=None,
                          help=f"Số trang tối đa. Mặc định: {DEFAULT_MAX_PAGES} "
                               f"(trừ khi chỉ dùng --max-jobs, xem bên dưới). "
                               f"1 trang TopCV ~20-25 job, 1 trang VietnamWorks ~50 job.")
    p_crawl.add_argument("--max-jobs", type=int, default=None,
                          help="Giới hạn TỔNG SỐ JD sẽ crawl, dừng ngay khi đủ "
                               "(không cần đợi hết --pages) — tiện khi chỉ muốn "
                               "lấy 1 lượng nhỏ để test/lấy mẫu thay vì tính theo "
                               "trang. Có thể dùng CÙNG --pages (dừng ở điều kiện "
                               "nào tới trước); nếu chỉ truyền --max-jobs mà không "
                               "truyền --pages, tự động crawl đủ số trang cần thiết.")

    p_crawl.add_argument("--no-track", action="store_true",
                          help="Không ghi lượt crawl vào lịch sử (crawl_runs) và không lưu "
                               "snapshot — hành vi cũ, tiện khi chạy thử nhanh.")

    sub.add_parser("stats", help="Xem số lượng job hiện có trong DB")

    p_snaps = sub.add_parser(
        "snapshots",
        help="Liệt kê snapshot HTML/JSON gốc của các lượt crawl (qua API) để debug parser",
    )
    p_snaps.add_argument("--run-id", default=None, help="Chỉ lấy snapshot của 1 lượt crawl")
    p_snaps.add_argument("--source", default=None, help="Chỉ lấy snapshot của 1 nguồn")
    p_snaps.add_argument("--limit", type=int, default=30, help="Số dòng tối đa (mặc định 30)")

    p_snap_export = sub.add_parser(
        "snapshot-export",
        help="Ghi nội dung 1 snapshot ra file (dùng làm fixture thật cho tests/)",
    )
    p_snap_export.add_argument("id", type=int, help="id snapshot (xem lệnh `snapshots`)")
    p_snap_export.add_argument("--out", required=True, help="Đường dẫn file đầu ra")

    p_create_admin = sub.add_parser(
        "create-admin",
        help="Tạo tài khoản admin đầu tiên cho hệ thống login (chỉ dùng lúc khởi tạo)",
    )
    p_create_admin.add_argument("--email", required=True, help="Email đăng nhập")
    p_create_admin.add_argument("--name", required=True, help="Họ tên đầy đủ")

    args = parser.parse_args()

    if args.command == "init-db":
        cmd_init_db(args)
    elif args.command == "migrate":
        cmd_migrate(args)
    elif args.command == "recompute-levels":
        cmd_recompute_levels(args)
    elif args.command == "report-duplicates":
        cmd_report_duplicates(args)
    elif args.command == "report-reposts":
        cmd_report_reposts(args)
    elif args.command == "check-listing-derivation":
        cmd_check_listing_derivation(args)
    elif args.command == "merge-duplicates":
        cmd_merge_duplicates(args)
    elif args.command == "crawl":
        cmd_crawl(args)
    elif args.command == "stats":
        cmd_stats(args)
    elif args.command == "snapshots":
        cmd_snapshots(args)
    elif args.command == "snapshot-export":
        cmd_snapshot_export(args)
    elif args.command == "create-admin":
        cmd_create_admin(args)


if __name__ == "__main__":
    main()
