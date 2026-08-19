"""
Script RIÊNG (không nằm trong pipeline crawl chính) — vá lại
industry/company_size/address/website cho công ty ĐÃ CÓ
companies.source_profile_url (xem sql/migration_add_source_profile_url.sql)
nhưng vẫn còn thiếu ít nhất 1 trong 4 field, bằng cách gọi LẠI
fetch_company_profile() trên đúng URL đã lưu.

KHÁC enrich_company_web_info.py (script chị em, dùng Tavily search +
Gemini): script NÀY KHÔNG gọi Tavily/Gemini, không tốn credit/token gì
cả — chỉ crawl lại 1 URL TopCV/VietnamWorks đã biết chắc chắn đúng công
ty (chính URL crawl gốc, không phải suy luận qua search), nên:
  - Chính xác hơn hẳn (đọc thẳng trang gốc có cấu trúc rõ, không qua
    search+LLM suy luận, không có rủi ro "nhầm pháp nhân chị em cùng
    thương hiệu" như enrich_company_web_info.py đã từng gặp).
  - Vá được CẢ 4 field (industry/company_size/address/website), không
    chỉ website/tax_id như bên kia. Từ 08/2026 vá thêm cả
    products_services (mô tả công ty, xem db.update_company_profile()
    mục "NỐI LẠI 08/2026") nếu adapter có trả profile["description"] —
    field này KHÔNG nằm trong điều kiện get_companies_needing_
    profile_backfill() (chỉ xét industry/company_size/address/website),
    nên company đã đủ 4 field kia nhưng thiếu products_services sẽ
    KHÔNG được chọn để backfill lại — chấp nhận được vì mục tiêu chính
    của field này là "nhặt kèm khi đằng nào cũng đang crawl lại", không
    đáng để quét lại toàn bộ DB chỉ vì riêng field này.
  - Miễn phí, không giới hạn quota — chỉ tốn thời gian chờ (throttle
    theo REQUEST_DELAY_SECONDS của adapter, giống crawl thường).

KHI NÀO DÙNG SCRIPT NÀO:
  - Công ty ĐÃ CÓ source_profile_url (crawl qua TopCV/VietnamWorks ít
    nhất 1 lần, kể cả khi lúc đó parser đọc thiếu field do bug) -> dùng
    script NÀY trước, ưu tiên tuyệt đối vì rẻ + chính xác hơn.
  - Công ty KHÔNG CÓ source_profile_url nào (tạo tay qua POST /companies,
    hoặc crawl từ nguồn không hỗ trợ fetch_company_profile) -> mới cần
    enrich_company_web_info.py (Tavily+Gemini, tốn credit, kém chính xác
    hơn vì phải suy luận qua search).

CHỌN ĐÚNG ADAPTER THEO DOMAIN: source_profile_url có thể là URL TopCV,
VietnamWorks, hoặc CareerViet (3 nguồn hiện có) — script tự nhận diện
qua domain trong URL, không cần người dùng chỉ định tay.

LƯU Ý RIÊNG CHO CAREERVIET (thêm 08/2026, xem adapters/careerviet.py):
CareerVietAdapter.fetch_company_profile() CHỦ ĐÍCH luôn trả industry=""
(trang công ty CareerViet không hiển thị field này) — company nguồn
CareerViet sẽ KHÔNG BAO GIỜ được vá industry qua script này, dù
company_size/address/website vẫn vá bình thường. Field industry của
các công ty này chỉ có thể vá qua enrich_company_web_info.py
(Tavily+Gemini). Hệ quả: company nguồn CareerViet có thể vẫn xuất hiện
lại trong get_companies_needing_profile_backfill() ở các lần chạy sau
dù đã vá hết những gì vá được (vì industry vẫn rỗng) — không phải lỗi,
chỉ là chi phí chạy lại 1 request vô ích mỗi lần, chấp nhận được.

Cách chạy:
    python backfill_company_profiles.py
    python backfill_company_profiles.py --limit 50   # test thử ít công ty trước
"""

import argparse
import logging
from typing import Optional
from urllib.parse import urlsplit

import db
from adapters.topcv import TopCVAdapter
from adapters.vietnamworks import VietnamWorksAdapter
from adapters.careerviet import CareerVietAdapter

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Map domain -> adapter chịu trách nhiệm crawl domain đó. Thêm nguồn mới
# (vd ITviec) chỉ cần thêm 1 dòng ở đây, không cần sửa gì khác trong file
# này — logic chọn adapter ở _adapter_for_url() bên dưới tự động dùng.
_DOMAIN_ADAPTERS = {
    "topcv.vn": TopCVAdapter,
    "vietnamworks.com": VietnamWorksAdapter,
    "careerviet.vn": CareerVietAdapter,
}


def _adapter_for_url(url: str):
    """Trả về INSTANCE adapter phù hợp với domain của url, hoặc None nếu
    domain không khớp adapter nào đã biết (không raise — để caller tự
    quyết định bỏ qua company đó, không dừng cả batch vì 1 URL lạ)."""
    netloc = urlsplit(url).netloc.lower().removeprefix("www.")
    for domain, adapter_cls in _DOMAIN_ADAPTERS.items():
        if netloc == domain or netloc.endswith("." + domain):
            return adapter_cls()
    return None


def run(limit: Optional[int] = None) -> dict:
    stats = {"checked": 0, "updated": 0, "unchanged": 0, "unknown_domain": 0, "errors": 0}

    conn = db.get_connection()
    # Cache adapter theo class -> tái dùng 1 Session/adapter cho cả batch
    # thay vì tạo mới mỗi công ty (session mới = mất lợi ích connection
    # pooling/cookie của curl_cffi, không cần thiết).
    adapter_cache: dict = {}

    def get_adapter(url: str):
        netloc = urlsplit(url).netloc.lower().removeprefix("www.")
        for domain, adapter_cls in _DOMAIN_ADAPTERS.items():
            if netloc == domain or netloc.endswith("." + domain):
                if adapter_cls not in adapter_cache:
                    adapter_cache[adapter_cls] = adapter_cls()
                return adapter_cache[adapter_cls]
        return None

    try:
        companies = db.get_companies_needing_profile_backfill(conn)
        if limit:
            companies = companies[:limit]

        logger.info("Tìm thấy %d công ty cần backfill lại profile", len(companies))

        for company_id, company_name, source_profile_url in companies:
            stats["checked"] += 1
            logger.info(
                "[%d/%d] %s -> %s",
                stats["checked"], len(companies), company_name, source_profile_url,
            )

            adapter = get_adapter(source_profile_url)
            if adapter is None:
                stats["unknown_domain"] += 1
                logger.warning(
                    "  Bỏ qua: domain của source_profile_url không khớp adapter nào đã biết (%s)",
                    source_profile_url,
                )
                continue

            try:
                profile = adapter.fetch_company_profile(source_profile_url) or {}
            except Exception as exc:  # noqa: BLE001 - không để 1 công ty lỗi dừng cả batch
                stats["errors"] += 1
                logger.error("  Lỗi fetch '%s': %s", company_name, exc)
                continue

            # profile rỗng hoàn toàn (mọi field "") -> trang có thể đã
            # đổi cấu trúc lần nữa, hoặc URL đã chết (công ty gỡ khỏi
            # TopCV/VietnamWorks) -> không có gì để cập nhật, KHÔNG coi
            # là lỗi (adapter tự phân biệt "fetch thất bại" bằng cách
            # trả dict rỗng-an-toàn, không phải None, ở fetch_company_profile()).
            has_any_value = any(profile.get(k) for k in
                                 ("tax_id", "real_website", "industry",
                                  "company_size", "address", "description"))
            if not has_any_value:
                stats["unchanged"] += 1
                logger.info("  Không lấy thêm được gì mới (trang có thể đã đổi/hết dữ liệu).")
                continue

            try:
                db.update_company_profile(
                    conn, company_id,
                    tax_id=profile.get("tax_id", ""),
                    website=profile.get("real_website", ""),
                    industry=profile.get("industry", ""),
                    company_size=profile.get("company_size", ""),
                    address=profile.get("address", ""),
                    products_services=profile.get("description", ""),
                )
                conn.commit()
            except Exception as exc:  # noqa: BLE001 - lỗi DB, không để dừng cả batch
                conn.rollback()
                stats["errors"] += 1
                logger.error("  Lỗi ghi DB cho '%s': %s", company_name, exc)
                continue

            stats["updated"] += 1
            logger.info(
                "  -> Đã vá: industry=%s | company_size=%s | address=%s | website=%s",
                profile.get("industry") or "(không có)",
                profile.get("company_size") or "(không có)",
                (profile.get("address") or "(không có)")[:40],
                profile.get("real_website") or "(không có)",
            )
    finally:
        conn.close()

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Backfill lại industry/company_size/address/website cho công ty đã "
                    "có source_profile_url, bằng cách gọi lại fetch_company_profile() — "
                    "KHÔNG dùng Tavily/Gemini, miễn phí."
    )
    parser.add_argument("--limit", type=int, default=None,
                         help="Giới hạn số công ty xử lý (dùng để test thử trước khi chạy full)")
    args = parser.parse_args()

    stats = run(limit=args.limit)

    print("\n===== KẾT QUẢ =====")
    print(f"Đã kiểm tra                          : {stats['checked']}")
    print(f"Đã vá thêm dữ liệu                    : {stats['updated']}")
    print(f"Không lấy thêm được gì mới            : {stats['unchanged']}")
    print(f"Domain không khớp adapter nào đã biết : {stats['unknown_domain']}")
    print(f"Lỗi                                   : {stats['errors']}")


if __name__ == "__main__":
    main()
