"""
Script RIÊNG, chạy 1 LẦN (không nằm trong pipeline crawl chính) — sửa lại
dữ liệu company_size ĐÃ CÓ SẴN trong DB cho đồng nhất format: bỏ hậu tố
"nhân viên" (VietnamWorks trả kèm hậu tố này, vd "100-499 nhân viên",
"25-99 nhân viên" — trong khi TopCV/CareerViet chỉ trả khoảng số thuần,
vd "100-499", "5.000-9.999" — khiến cột company_size bị trộn lẫn 2
format khác nhau tuỳ công ty đến từ nguồn nào).

Dùng CHUNG logic chuẩn hoá với normalize.normalize_company_size() — hàm
này giờ cũng được gọi ở db.update_company_profile()/patch_company_profile()
để MỌI lần ghi company_size MỚI (crawl tự động lẫn PATCH tay qua API) từ
nay về sau đều tự động đúng format, không lặp lại vấn đề này. Script ở
đây CHỈ xử lý dữ liệu CŨ đã lỡ ghi sai từ trước khi có normalize.

AN TOÀN: mặc định DRY-RUN — chỉ in ra sẽ đổi gì, KHÔNG ghi DB. Phải thêm
--apply mới thực sự UPDATE + commit.

Cách chạy:
    python fix_company_size_format.py            # xem trước sẽ đổi gì
    python fix_company_size_format.py --apply     # ghi thật vào DB
"""

import argparse
import logging

import db
from normalize import normalize_company_size

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def run(apply: bool = False) -> dict:
    stats = {"checked": 0, "changed": 0, "unchanged": 0}

    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            # Chỉ lấy company có company_size KHÔNG rỗng — company_size
            # NULL/'' không cần đụng tới, normalize_company_size("") vẫn
            # trả "" nên xử lý cũng vô hại nhưng khỏi tốn vòng lặp thừa.
            cur.execute(
                "SELECT company_id, company_name, company_size "
                "FROM companies WHERE company_size IS NOT NULL AND company_size != ''"
            )
            rows = cur.fetchall()

        logger.info("Tìm thấy %d công ty có company_size cần kiểm tra", len(rows))

        for company_id, company_name, old_size in rows:
            stats["checked"] += 1
            new_size = normalize_company_size(old_size)

            if new_size == old_size:
                stats["unchanged"] += 1
                continue

            stats["changed"] += 1
            logger.info("  %s: %r -> %r", company_name, old_size, new_size)

            if apply:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE companies SET company_size = %s WHERE company_id = %s",
                        (new_size, company_id),
                    )

        if apply:
            conn.commit()
        else:
            conn.rollback()  # không có gì để rollback (chưa UPDATE) — an toàn, chỉ để rõ ý
    finally:
        conn.close()

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Chuẩn hoá lại dữ liệu company_size cũ trong DB — bỏ hậu tố "
                    "'nhân viên' để đồng nhất format giữa các nguồn crawl."
    )
    parser.add_argument("--apply", action="store_true",
                         help="Ghi thật vào DB (mặc định chỉ DRY-RUN, không đổi gì).")
    args = parser.parse_args()

    stats = run(apply=args.apply)

    print("\n===== KẾT QUẢ =====")
    print(f"Đã kiểm tra                 : {stats['checked']}")
    print(f"Cần đổi format ({'ĐÃ GHI' if args.apply else 'chưa ghi, xem trước'}) : {stats['changed']}")
    print(f"Không cần đổi                : {stats['unchanged']}")
    if not args.apply and stats["changed"] > 0:
        print("\n-> Đây là DRY-RUN, DB CHƯA bị đổi gì. Chạy lại kèm --apply để ghi thật:")
        print("   python fix_company_size_format.py --apply")


if __name__ == "__main__":
    main()
