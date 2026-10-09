"""
db.company_enrichment — truy vấn chọn công ty cần bổ sung thông tin
(website/ngành/social link) cho các script enrich_*/backfill_* (tách từ
scrapjd/db/companies.py, 10/2026).
"""



def get_companies_needing_profile_from_website(conn):
    """Lấy công ty ĐÃ CÓ website nhưng còn thiếu industry và/hoặc
    products_services — tập company mà enrich_company_profile_from_
    website.py (script mới, 08/2026) có thể vá được bằng cách đọc trang
    chủ/giới thiệu của chính website đó + Gemini phân loại, KHÔNG cần
    Tavily (rẻ hơn scrapjd/maintenance/enrich_company_web_info.py).

    Chủ yếu nhắm tới công ty nguồn CareerViet (CareerVietAdapter cố ý
    không lấy industry, xem scrapjd/adapters/careerviet.py), nhưng KHÔNG giới hạn
    riêng nguồn nào — bất kỳ công ty nào đã có website mà vẫn thiếu
    industry hoặc products_services đều thuộc tập này (kể cả công ty tạo
    tay qua POST /companies có điền website nhưng bỏ trống 1 trong 2).

    Điều kiện là OR (không phải AND): company chỉ cần thiếu 1 trong 2
    field là được chọn lại — company đã có industry nhưng thiếu
    products_services (hoặc ngược lại) vẫn được chạy lại để vá nốt field
    còn thiếu, KHÔNG cần cờ/tham số riêng như bản trước 08/2026. Company
    mà Gemini từng trả confidence thấp cho 1 field (không lưu) vẫn còn
    rỗng ở field đó nên vẫn được chọn lại ở lần chạy sau — chấp nhận
    được, không có cơ chế đánh dấu "đã thử nhưng thất bại".

    Trả về list[(company_id, company_name, website)]."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT company_id, company_name, website
            FROM companies
            WHERE website IS NOT NULL AND website != ''
              AND ((industry IS NULL OR industry = '')
                   OR (products_services IS NULL OR products_services = ''))
            ORDER BY company_name
            """
        )
        return cur.fetchall()


def get_companies_needing_social_links(conn):
    """Lấy các công ty ĐÃ CÓ website nhưng còn thiếu fanpage_url hoặc
    linkedin_url — đây là tập company mà scrapjd/maintenance/get_company_fb_linkedin_link.py
    có thể enrich được (script đó cần website làm điểm bắt đầu để tìm
    link social trong footer/header trang công ty, không tự đoán mò).

    Trả về list[(company_id, company_name, website)]."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT company_id, company_name, website
            FROM companies
            WHERE website IS NOT NULL AND website != ''
              AND (fanpage_url IS NULL OR linkedin_url IS NULL)
            ORDER BY company_name
            """
        )
        return cur.fetchall()


def update_company_social_links(conn, company_id: str, *,
                                 fanpage_url: str = "", linkedin_url: str = "") -> None:
    """Vá thêm fanpage_url/linkedin_url cho 1 công ty (chỉ ghi đè field nào
    tìm thấy giá trị mới, không xóa dữ liệu cũ nếu lần chạy sau không tìm
    thấy — cùng nguyên tắc với update_company_profile())."""
    updates = []
    values = []
    if fanpage_url:
        updates.append("fanpage_url = %s")
        values.append(fanpage_url)
    if linkedin_url:
        updates.append("linkedin_url = %s")
        values.append(linkedin_url)

    if not updates:
        return

    values.append(company_id)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE companies SET {', '.join(updates)} WHERE company_id = %s",
            values,
        )


def get_companies_needing_web_lookup(conn):
    """Lấy các công ty còn thiếu website HOẶC tax_id — tập company mà
    scrapjd/maintenance/enrich_company_web_info.py (script RIÊNG, tra cứu qua Tavily search +
    Gemini trích xuất) có thể thử vá thêm.

    Chỉ cần thiếu 1 trong 2 field là đủ điều kiện — vì 1 lần gọi search
    có thể trả về cả 2 field cùng lúc (đỡ tốn thêm request nếu công ty
    đã có website nhưng thiếu tax_id, hoặc ngược lại), không cần tách
    2 hàm riêng cho từng field.

    Trả về list[(company_id, company_name)]."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT company_id, company_name
            FROM companies
            WHERE (website IS NULL OR website = '')
               OR (tax_id IS NULL OR tax_id = '')
            ORDER BY company_name
            """
        )
        return cur.fetchall()


def get_companies_needing_profile_backfill(conn):
    """Lấy công ty ĐÃ CÓ source_profile_url (xem
    sql/migration_add_source_profile_url.sql) nhưng vẫn còn thiếu ít
    nhất 1 trong 4 field industry/company_size/address/website — tập
    company mà scrapjd/maintenance/backfill_company_profiles.py (script RIÊNG, gọi thẳng lại
    fetch_company_profile() trên URL đã lưu, KHÔNG qua Tavily/Gemini) có
    thể vá lại.

    KHÁC get_companies_needing_web_lookup() ở trên (dùng cho
    scrapjd/maintenance/enrich_company_web_info.py, nguồn Tavily/Gemini, chỉ vá website/
    tax_id): hàm này ưu tiên dùng TRƯỚC vì chính xác hơn hẳn (đọc thẳng
    trang gốc, không qua search+LLM suy luận) và vá được CẢ 4 field —
    chỉ những công ty KHÔNG có source_profile_url (vd tạo tay, hoặc
    crawl từ nguồn không hỗ trợ fetch_company_profile) mới cần tới
    scrapjd/maintenance/enrich_company_web_info.py.

    Trả về list[(company_id, company_name, source_profile_url)]."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT company_id, company_name, source_profile_url
            FROM companies
            WHERE source_profile_url IS NOT NULL AND source_profile_url != ''
              AND (
                    (industry IS NULL OR industry = '')
                 OR (company_size IS NULL OR company_size = '')
                 OR (address IS NULL OR address = '')
                 OR (website IS NULL OR website = '')
              )
            ORDER BY company_name
            """
        )
        return cur.fetchall()
