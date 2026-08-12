"""
CLI chạy crawler.

Ví dụ:
    python main.py init-db
    python main.py crawl --category data-analyst --pages 3
    python main.py crawl --category data-engineer --pages 5
    python main.py crawl --category data-analyst --max-jobs 20
    python main.py stats
"""

import argparse
import logging
import sys

import db
from adapters.topcv import TopCVAdapter
from adapters.vietnamworks import VietnamWorksAdapter
from pipeline import run_pipeline
from config import (
    TOPCV_CATEGORIES, VIETNAMWORKS_CATEGORIES, DEFAULT_CATEGORY, DEFAULT_MAX_PAGES,
)

# Đăng ký nguồn crawl ở đây — thêm nguồn mới sau này (ITviec...) chỉ cần
# thêm 1 dòng vào dict này, không sửa gì logic cmd_crawl() bên dưới.
SOURCES = {
    "topcv": {"adapter_cls": TopCVAdapter, "categories": TOPCV_CATEGORIES},
    "vietnamworks": {"adapter_cls": VietnamWorksAdapter, "categories": VIETNAMWORKS_CATEGORIES},
}
DEFAULT_SOURCE = "topcv"

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

    args = parser.parse_args()

    if args.command == "init-db":
        cmd_init_db(args)
    elif args.command == "crawl":
        cmd_crawl(args)
    elif args.command == "stats":
        cmd_stats(args)


if __name__ == "__main__":
    main()
