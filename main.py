"""
CLI chạy crawler.

Ví dụ:
    python main.py init-db
    python main.py crawl --category data-analyst --pages 3
    python main.py crawl --category data-engineer --pages 5
    python main.py crawl --category data-analyst --max-jobs 20
    python main.py crawl --category data-analyst --max-jobs 5 --no-track   # chạy thử, không ghi lịch sử
    python main.py stats
    python main.py snapshots --source careerviet
    python main.py snapshot-export 12 --out tests/fixture_careerviet_listing.html
    python main.py create-admin --email admin@congty.vn --name "Nguyễn Văn A"
"""

import argparse
import getpass
import logging
import sys

import db
from adapters.base import CrawlBlockedError
from pipeline import run_pipeline
from config import (
    TOPCV_CATEGORIES, VIETNAMWORKS_CATEGORIES, DEFAULT_CATEGORY, DEFAULT_MAX_PAGES,
)
# SOURCES/DEFAULT_SOURCE giờ sống ở 1 nguồn sự thật duy nhất
# (sources_registry.py) — xem docstring file đó để biết lý do (trước
# đây bị khai báo lặp lại thủ công ở đây + 3 nơi khác trong api/, dễ
# lệch, đã từng gây bug CareerViet "crawl được nhưng không hiện trên
# web"). Thêm nguồn crawl mới -> sửa sources_registry.py, KHÔNG sửa
# file này.
from sources_registry import SOURCES, DEFAULT_SOURCE

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def cmd_init_db(args):
    conn = db.get_connection()
    try:
        db.apply_schema(conn)
        print("✅ Đã tạo/cập nhật schema trong database.")
    finally:
        conn.close()


def cmd_migrate(args):
    """Chạy MỌI migration_*.sql (sql/) chưa được áp dụng cho DB đang
    kết nối — xem docstring db.apply_migrations()/db.connection để biết
    cơ chế tracking (bảng schema_migrations) và lý do an toàn chạy lại
    trên DB đã tồn tại từ trước (mọi migration đều idempotent).

    --check: CHỈ liệt kê migration còn thiếu, KHÔNG chạy gì — dùng để
    kiểm tra trước khi deploy (vd script CI/CD có thể gọi lệnh này,
    exit code khác 0 nếu còn migration chưa chạy, để chặn deploy sớm
    thay vì phát hiện lỗi sau khi code mới đã lên production mà DB
    chưa kịp cập nhật)."""
    conn = db.get_connection()
    try:
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


def cmd_create_admin(args):
    """Tạo tài khoản ADMIN đầu tiên — chỉ dùng qua CLI (chạy trực tiếp
    trên máy/server có quyền truy cập DB), vì POST /auth/users trên API
    yêu cầu ĐÃ CÓ admin để gọi (require_admin) — "con gà quả trứng" lúc
    khởi tạo hệ thống lần đầu. Sau khi có 1 admin, tạo user tiếp theo
    (admin hoặc member) nên làm qua POST /auth/users từ frontend."""
    # Import ở đây (không import ở đầu file) vì api/security.py raise
    # lỗi ngay lúc import nếu thiếu JWT_SECRET_KEY — không muốn việc đó
    # chặn luôn các lệnh CLI khác (crawl/stats) vốn không cần tới auth.
    from api import security

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
        # toàn vì vòng lặp trong pipeline.py sẽ dừng ngay khi đủ
        # --max-jobs, không thật sự crawl tới 999 trang.
        effective_pages = 999
    else:
        effective_pages = DEFAULT_MAX_PAGES

    if getattr(args, "no_track", False):
        _crawl_untracked(args, source_cfg, effective_pages)
        return

    # Mặc định (đợt 3.5, 10/2026): chạy qua CÙNG đường với nút "Crawl" trên web
    # (api/crawl_runner.execute) — có dòng trong crawl_runs, cờ blocked/degraded,
    # snapshot HTML gốc, log live. Trước đây CLI gọi thẳng run_pipeline() nên
    # lượt chạy trên máy không để lại dấu vết nào trong DB.
    from api import crawl_runner

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


def _print_crawl_result(conn, args, stats):
    print("\n===== KẾT QUẢ =====")
    if args.max_jobs is not None:
        print(f"(Giới hạn theo --max-jobs={args.max_jobs})")
    print(f"Tổng job crawl được : {stats.get('fetched', 0)}")
    print(f"Đã lưu vào DB        : {stats.get('inserted', 0)}")
    print(f"Bỏ qua (đã tồn tại)  : {stats.get('skipped_duplicate', 0)}")
    print(f"Đã vá job cũ (work_type/deadline): {stats.get('updated_existing', 0)}")
    print(f"Bỏ qua (fetch chi tiết thất bại)  : {stats.get('skipped_fetch_failed', 0)}")
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
        help="Áp dụng các migration_*.sql (sql/) CHƯA chạy cho DB này (xem sql/README_MIGRATIONS.md)",
    )
    p_migrate.add_argument(
        "--check", action="store_true",
        help="Chỉ liệt kê migration còn thiếu, không chạy gì (exit code 1 nếu còn thiếu)",
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
