"""
CLI chạy crawler.

Ví dụ:
    python main.py init-db
    python main.py crawl --category data-analyst --pages 3
    python main.py crawl --category data-engineer --pages 5
    python main.py crawl --category data-analyst --max-jobs 20
    python main.py stats
    python main.py create-admin --email admin@congty.vn --name "Nguyễn Văn A"
"""

import argparse
import getpass
import logging
import sys

import db
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

    conn = db.get_connection()
    try:
        adapter = source_cfg["adapter_cls"]()
        stats = run_pipeline(adapter, conn, args.category, effective_pages,
                              max_jobs=args.max_jobs)
        print("\n===== KẾT QUẢ =====")
        if args.max_jobs is not None:
            print(f"(Giới hạn theo --max-jobs={args.max_jobs})")
        print(f"Tổng job crawl được : {stats['fetched']}")
        print(f"Đã lưu vào DB        : {stats['inserted']}")
        print(f"Bỏ qua (đã tồn tại)  : {stats['skipped_duplicate']}")
        print(f"Đã vá job cũ (work_type/deadline): {stats.get('updated_existing', 0)}")
        print(f"Bỏ qua (fetch chi tiết thất bại)  : {stats.get('skipped_fetch_failed', 0)}")
        print(f"Bỏ qua (nhà tuyển dụng ẩn danh)   : {stats.get('skipped_anonymous_employer', 0)}")
        print(f"Lỗi                  : {stats['errors']}")
        print(f"Tổng job trong DB hiện tại: {db.count_jobs(conn)}")
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

    sub.add_parser("stats", help="Xem số lượng job hiện có trong DB")

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
    elif args.command == "create-admin":
        cmd_create_admin(args)


if __name__ == "__main__":
    main()
