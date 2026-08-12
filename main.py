"""
CLI chạy crawler.

Ví dụ:
    python main.py init-db
    python main.py crawl --category data-analyst --pages 3
    python main.py crawl --category data-engineer --pages 5
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

    conn = db.get_connection()
    try:
        adapter = source_cfg["adapter_cls"]()
        stats = run_pipeline(adapter, conn, args.category, args.pages)
        print("\n===== KẾT QUẢ =====")
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
    p_crawl.add_argument("--pages", type=int, default=DEFAULT_MAX_PAGES,
                          help=f"Số trang tối đa. Mặc định: {DEFAULT_MAX_PAGES}")

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
