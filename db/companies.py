"""
db.companies — tách từ db.py (God module) theo domain, xem README/kế hoạch refactor.
"""

import logging
from typing import Optional

import psycopg2.extras
import psycopg2
from normalize import normalize_company_size

logger = logging.getLogger(__name__)


def find_company_probe(conn, company_name: str):
    """Tra cứu nhanh theo TÊN (chỉ để quyết định có cần fetch_company_profile
    hay không, không phải nguồn match chính thức). Trả về
    (company_id, website, industry, company_size, address) hoặc None nếu
    chưa có công ty này.

    ĐỔI (08/2026, thêm VietnamWorks): bỏ tax_id ra khỏi probe này — xem
    lý do chi tiết trong probe_needs_enrichment() bên dưới.

    KHÔNG select source_profile_url ở đây dù bảng đã có cột này (xem
    migration_add_source_profile_url.sql) — probe này chỉ quyết định có
    cần enrich hay không dựa trên 4 field NỘI DUNG (website/industry/
    company_size/address), source_profile_url chỉ là "địa chỉ để tra lại",
    không phải nội dung cần đủ/thiếu, nên không đưa vào điều kiện của
    probe_needs_enrichment()."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT company_id, website, industry, company_size, address "
            "FROM companies WHERE lower(company_name) = lower(%s)",
            (company_name,),
        )
        row = cur.fetchone()
        return row if row else None


def get_or_create_company_by_profile(conn, company_name: str,
                                      province_id: Optional[int],
                                      tax_id: str = "",
                                      created_by: Optional[str] = None) -> str:
    """Match/tạo công ty — ƯU TIÊN theo tax_id (định danh thật, ổn định),
    fallback theo tên nếu không có tax_id.

    Thứ tự xử lý:
    1. Có tax_id -> tìm công ty đã có ĐÚNG tax_id này -> dùng luôn, bất kể
       tên có viết khác đi giữa các lần đăng tin.
    2. Không tìm thấy theo tax_id -> tìm theo tên (phòng trường hợp công ty
       này đã được tạo từ TRƯỚC khi có tính năng tax_id) -> nếu thấy, "vá"
       thêm tax_id vào record cũ đó.
    3. Không thấy gì cả -> tạo mới, kèm tax_id nếu có.
    """
    with conn.cursor() as cur:
        if tax_id:
            cur.execute("SELECT company_id FROM companies WHERE tax_id = %s", (tax_id,))
            row = cur.fetchone()
            if row:
                return str(row[0])

        cur.execute(
            "SELECT company_id, tax_id FROM companies WHERE lower(company_name) = lower(%s)",
            (company_name,),
        )
        row = cur.fetchone()
        if row:
            existing_id, existing_tax_id = row
            if tax_id and not existing_tax_id:
                cur.execute(
                    "UPDATE companies SET tax_id = %s WHERE company_id = %s",
                    (tax_id, existing_id),
                )
            return str(existing_id)

        cur.execute(
            """
            INSERT INTO companies (company_name, province_id, tax_id, created_by)
            VALUES (%s, %s, %s, %s)
            RETURNING company_id
            """,
            (company_name, province_id, tax_id or None, created_by),
        )
        return str(cur.fetchone()[0])


def get_or_create_company(conn, company_name: str, province_id: Optional[int]) -> str:
    """Giữ lại cho tương thích ngược — không có tax_id, chỉ match theo tên.
    Ưu tiên dùng get_or_create_company_by_profile() khi có tax_id."""
    return get_or_create_company_by_profile(conn, company_name, province_id, tax_id="")


def update_company_profile(conn, company_id: str, *, tax_id: str = "", website: str = "",
                            industry: str = "", company_size: str = "",
                            address: str = "", partnership_potential: str = "",
                            source_profile_url: str = "", products_services: str = "",
                            updated_by: Optional[str] = None) -> None:
    """Cập nhật thêm thông tin công ty (chỉ ghi đè field nào có giá trị mới,
    không xóa mất dữ liệu cũ nếu lần crawl sau không lấy được field đó).

    updated_by (thêm 08/2026): ss_user_id của người vừa sửa (JWT), CHỈ
    truyền khi gọi từ route ghi có bắt buộc đăng nhập (POST /companies) —
    lời gọi từ pipeline crawl (không có user thật) để None, cột
    updated_by trong DB giữ NULL cho các lần enrich tự động.

    partnership_potential (thêm 08/2026, xem
    sql/migration_add_partnership_potential.sql): staff tự chấm tay
    (HIGH/MEDIUM/LOW/UNVERIFIED), pipeline crawl KHÔNG gán field này —
    "" (mặc định) bị bỏ qua giống mọi field khác trong hàm này, cột DB
    tự giữ UNVERIFIED cho công ty mới/crawl tự động cho tới khi staff
    chủ động đánh giá qua PATCH /companies/{id}.

    source_profile_url (thêm 08/2026, xem
    sql/migration_add_source_profile_url.sql): URL trang hồ sơ công ty
    trên nguồn crawl gốc (TopCV/VietnamWorks) — LUÔN ghi lại mỗi khi
    pipeline.py thấy company_url mới cho công ty này, KỂ CẢ KHI
    probe_needs_enrichment() trả False (4 field nội dung đã đủ) — khác
    với các field kia (chỉ ghi khi "có giá trị mới VÀ field cũ rỗng/muốn
    đè"), source_profile_url nên LUÔN được cập nhật thành URL MỚI NHẤT
    thấy được, vì URL trang công ty có thể đổi qua thời gian (đổi slug,
    đổi từ /cong-ty/ sang /brand/ khi công ty mua gói Pro...) — giữ URL
    cũ có thể đã chết trong khi lẽ ra có URL mới hơn để backfill.

    products_services (NỐI LẠI 08/2026, xem lịch sử trao đổi): cột này
    thực ra CHƯA BAO GIỜ bị DROP thật ở DB — sql/migration_drop_
    products_services.sql tồn tại nhưng chưa từng được chạy trên DB thật.
    Trước đó code (pipeline.py) đã ngừng ghi field này dù mọi adapter vẫn
    fetch sẵn profile["description"] mỗi lần crawl — dữ liệu có trong tay
    nhưng bị vứt đi, khiến cột trống 100% dù còn tồn tại. Giờ nối lại việc
    GHI ở tầng crawl/enrich tự động (pipeline.py, backfill_company_
    profiles.py); CHỦ Ý KHÔNG thêm lại vào CompanyCreate/CompanyUpdate
    (api/schemas.py) hay UI — giữ nguyên quyết định cũ "bỏ khỏi CRM/form
    nhập tay", chỉ khác ở chỗ dữ liệu crawl được nên lưu lại thay vì vứt."""
    updates = []
    values = []
    if updated_by is not None:
        updates.append("updated_by = %s")
        values.append(updated_by)
    if tax_id:
        updates.append("tax_id = %s")
        values.append(tax_id)
    if website:
        updates.append("website = %s")
        values.append(website)
    if industry:
        updates.append("industry = %s")
        values.append(industry)
    if company_size:
        # Chuẩn hoá format (08/2026, xem normalize.normalize_company_size)
        # — VietnamWorks trả kèm "nhân viên", TopCV/CareerViet thì không,
        # bỏ hậu tố này để cột company_size đồng nhất 1 format duy nhất
        # dù nguồn crawl nào ghi vào.
        company_size = normalize_company_size(company_size)
    if company_size:
        updates.append("company_size = %s")
        values.append(company_size)
    if address:
        updates.append("address = %s")
        values.append(address)
    if partnership_potential:
        updates.append("partnership_potential = %s")
        values.append(partnership_potential)
    if source_profile_url:
        updates.append("source_profile_url = %s")
        values.append(source_profile_url)
    if products_services:
        updates.append("products_services = %s")
        values.append(products_services)

    if not updates:
        return

    values.append(company_id)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE companies SET {', '.join(updates)} WHERE company_id = %s",
            values,
        )


def patch_company_profile(conn, company_id: str, *,
                           company_name: Optional[str] = None,
                           tax_id: Optional[str] = None,
                           website: Optional[str] = None,
                           industry: Optional[str] = None,
                           company_size: Optional[str] = None,
                           address: Optional[str] = None,
                           province_id: Optional[int] = None,
                           fanpage_url: Optional[str] = None,
                           linkedin_url: Optional[str] = None,
                           partnership_potential: Optional[str] = None,
                           updated_by: Optional[str] = None) -> bool:
    """Sửa TỰ DO company đã tồn tại — dùng cho PATCH /companies/{id}
    (thêm 08/2026, xem lịch sử trao đổi). KHÁC update_company_profile()
    (pattern "vá thêm", chỉ field TRUTHY mới ghi đè, gửi "" bị bỏ qua —
    vẫn giữ nguyên cho POST /companies + pipeline crawl, KHÔNG đổi):
    hàm này dùng `is not None` giống update_job(), nên PATCH
    {"website": ""} XOÁ giá trị cũ thay vì bị bỏ qua — đúng ngữ nghĩa
    PATCH thật sự.

    partnership_potential: đây là kênh chính để staff chấm/sửa đánh giá
    tiềm năng hợp tác (HIGH/MEDIUM/LOW/UNVERIFIED) — validation giá trị
    hợp lệ do Pydantic enum ở CompanyUpdate lo, hàm này chỉ ghi thẳng.

    province_id: nơi gọi (router) tự resolve qua get_or_create_province()
    trước khi truyền vào đây (giống pattern level_id/province_id ở
    update_job()) — hàm này chỉ nhận ID, không tự tra tên tỉnh.

    tax_id có UNIQUE INDEX (uq_companies_tax_id) — nếu trùng company
    khác, cur.execute sẽ raise psycopg2.errors.UniqueViolation, nơi gọi
    (router) chịu trách nhiệm bắt lỗi này để trả 409 rõ ràng thay vì để
    lộ traceback 500 (KHÔNG tự merge như update_company_profile_with_merge()
    — sửa tay 1 company cụ thể khác với enrich tự động hàng loạt, gộp
    nhầm ở đây rủi ro hơn).

    Trả False nếu company_id không tồn tại, True nếu update thành công
    — route dùng để trả 404 đúng lúc."""
    updates = []
    values = []

    if updated_by is not None:
        updates.append("updated_by = %s")
        values.append(updated_by)
    if company_name is not None:
        updates.append("company_name = %s")
        values.append(company_name)
    if tax_id is not None:
        updates.append("tax_id = %s")
        values.append(tax_id or None)  # "" -> NULL, tránh vi phạm unique index bằng chuỗi rỗng
    if website is not None:
        updates.append("website = %s")
        values.append(website)
    if industry is not None:
        updates.append("industry = %s")
        values.append(industry)
    if company_size is not None:
        # Chuẩn hoá format — xem docstring trong update_company_profile()
        # ở trên. company_size="" (PATCH cố ý xoá field) vẫn qua
        # normalize_company_size("") -> "" bình thường, không đổi hành vi.
        company_size = normalize_company_size(company_size)
        updates.append("company_size = %s")
        values.append(company_size)
    if address is not None:
        updates.append("address = %s")
        values.append(address)
    if province_id is not None:
        updates.append("province_id = %s")
        values.append(province_id)
    if fanpage_url is not None:
        updates.append("fanpage_url = %s")
        values.append(fanpage_url)
    if linkedin_url is not None:
        updates.append("linkedin_url = %s")
        values.append(linkedin_url)
    if partnership_potential is not None:
        updates.append("partnership_potential = %s")
        values.append(partnership_potential)

    if not updates:
        return company_exists_by_id(conn, company_id)

    values.append(company_id)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE companies SET {', '.join(updates)} WHERE company_id = %s",
            values,
        )
        return cur.rowcount > 0


def company_exists_by_id(conn, company_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM companies WHERE company_id = %s", (company_id,))
        return cur.fetchone() is not None



    """Tra company_id ĐANG CÓ ĐÚNG tax_id này (nếu có). Dùng TRƯỚC khi
    update_company_profile() ghi tax_id mới vào 1 company khác, để phát
    hiện sớm case "2 company_id khác nhau hoá ra cùng 1 pháp nhân thật"
    (vd 1 công ty được crawl từ TopCV VÀ VietnamWorks với 2 tên hơi khác
    nhau, tạo 2 row riêng lúc get_or_create_company_by_profile(), rồi
    enrich_company_web_info.py tra ra CÙNG 1 tax_id cho cả 2 row) — nếu
    không bắt trước, UPDATE thẳng sẽ vi phạm uq_companies_tax_id (tax_id
    unique) và làm crash transaction.

    Trả None nếu tax_id rỗng hoặc chưa company nào có."""
    if not tax_id:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT company_id FROM companies WHERE tax_id = %s", (tax_id,))
        row = cur.fetchone()
        return str(row[0]) if row else None


def merge_companies(conn, source_company_id: str, target_company_id: str) -> None:
    """Gộp source_company_id VÀO target_company_id (source biến mất khỏi
    DB sau khi gọi hàm này) — dùng khi phát hiện 2 company_id khác nhau
    thực ra là CÙNG 1 pháp nhân (vd trùng tax_id phát hiện qua
    enrich_company_web_info.py).

    target LUÔN là company đã có sẵn tax_id đó từ trước (đáng tin hơn,
    vì tax_id là định danh pháp lý ổn định) — source là company vừa tra
    ra tax_id trùng, sẽ được gộp vào target rồi xoá.

    Chuyển toàn bộ job_postings + company_contacts đang trỏ vào source
    sang target. KHÔNG dedupe job trùng nội dung ở bước này (job giống
    hệt title/level/province giữa 2 company cũ rất có thể xảy ra sau khi
    gộp, nhưng dedupe tự động rủi ro xoá nhầm job còn hạn/job có
    ss_team_notes riêng) — để lại cho view v_duplicate_job_candidates
    (đã có sẵn trong schema.sql) cho người xem tay quyết định job nào
    giữ.

    Không tự commit() — nơi gọi chịu trách nhiệm commit/rollback (giữ
    nguyên tắc nhất quán với các hàm update_* khác trong module này),
    quan trọng hơn ở đây vì merge là thao tác nhiều bước, cần atomic:
    nếu bước nào lỗi giữa chừng, rollback() phải hoàn tác được TẤT CẢ,
    không để lại trạng thái nửa vời (vd đã chuyển job nhưng chưa xoá
    company nguồn)."""
    if source_company_id == target_company_id:
        return  # không có gì để gộp

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE job_postings SET company_id = %s WHERE company_id = %s",
            (target_company_id, source_company_id),
        )
        cur.execute(
            "UPDATE company_contacts SET company_id = %s WHERE company_id = %s",
            (target_company_id, source_company_id),
        )
        cur.execute(
            "DELETE FROM companies WHERE company_id = %s",
            (source_company_id,),
        )


def find_company_by_tax_id(conn, tax_id: str) -> Optional[str]:
    """Tìm company_id đã có SẴN đúng tax_id này, None nếu chưa có công ty
    nào — tax_id có UNIQUE INDEX (uq_companies_tax_id, xem sql/schema.sql)
    nên tối đa 1 kết quả khớp. Cùng logic tra cứu tax_id đã dùng inline
    trong get_or_create_company_by_profile() (bước 1), tách riêng ra đây
    để update_company_profile_with_merge() (enrich_company_web_info.py)
    dùng lại được mà không phải chép lại query."""
    if not tax_id:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT company_id FROM companies WHERE tax_id = %s", (tax_id,))
        row = cur.fetchone()
        return str(row[0]) if row else None


def update_company_profile_with_merge(conn, company_id: str, *, tax_id: str = "",
                                       website: str = "", industry: str = "",
                                       company_size: str = "",
                                       address: str = "") -> str:
    """Giống update_company_profile(), nhưng AN TOÀN với trường hợp
    tax_id mới tìm được trùng với 1 company_id KHÁC đã có sẵn — dùng cho
    enrich_company_web_info.py, nơi tax_id đến từ tra cứu web (không
    phải nguồn crawl gốc), nên khả năng khớp phải 1 company đã tồn tại
    từ trước (crawl bởi nguồn khác, tên ghi hơi khác) là có thật.

    update_company_profile() (bản gốc, dùng trong pipeline.py) KHÔNG có
    bước kiểm tra này vì tax_id ở đó luôn đến kèm 1 lần
    fetch_company_profile() DUY NHẤT ngay lúc company vừa được tạo/match
    trong CÙNG 1 lần crawl — rủi ro trùng thấp hơn nhiều, và
    get_or_create_company_by_profile() đã tự xử lý match theo tax_id
    trước khi tạo mới. Ở đây thì khác: company_id truyền vào đã tồn tại
    từ trước, enrich chỉ đang cố VÁ THÊM tax_id — nên phải tự kiểm tra
    lại.

    Trả về company_id THẬT SỰ chứa dữ liệu sau khi update — GIỐNG
    company_id truyền vào nếu không có trùng lặp, KHÁC (= company_id của
    company đã có sẵn tax_id đó) nếu vừa xảy ra merge. Nơi gọi PHẢI dùng
    giá trị trả về này cho các thao tác tiếp theo trên company đó (vd
    log), vì company_id cũ có thể đã bị xoá khỏi DB."""
    final_company_id = company_id

    if tax_id:
        existing_id = find_company_by_tax_id(conn, tax_id)
        if existing_id and existing_id != company_id:
            logger.warning(
                "Company %s và %s có cùng tax_id=%s -> gộp %s vào %s.",
                company_id, existing_id, tax_id, company_id, existing_id,
            )
            merge_companies(conn, source_company_id=company_id, target_company_id=existing_id)
            final_company_id = existing_id
            # Đã gộp xong -> KHÔNG cần set lại tax_id nữa (company đích
            # vốn đã có đúng tax_id này rồi), tránh 1 UPDATE thừa.
            tax_id = ""

    update_company_profile(
        conn, final_company_id,
        tax_id=tax_id, website=website, industry=industry,
        company_size=company_size, address=address,
    )
    return final_company_id


def get_companies_needing_profile_from_website(conn):
    """Lấy công ty ĐÃ CÓ website nhưng còn thiếu industry và/hoặc
    products_services — tập company mà enrich_company_profile_from_
    website.py (script mới, 08/2026) có thể vá được bằng cách đọc trang
    chủ/giới thiệu của chính website đó + Gemini phân loại, KHÔNG cần
    Tavily (rẻ hơn enrich_company_web_info.py).

    Chủ yếu nhắm tới công ty nguồn CareerViet (CareerVietAdapter cố ý
    không lấy industry, xem adapters/careerviet.py), nhưng KHÔNG giới hạn
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
    linkedin_url — đây là tập company mà get_company_fb_linkedin_link.py
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
    enrich_company_web_info.py (script RIÊNG, tra cứu qua Tavily search +
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
    company mà backfill_company_profiles.py (script RIÊNG, gọi thẳng lại
    fetch_company_profile() trên URL đã lưu, KHÔNG qua Tavily/Gemini) có
    thể vá lại.

    KHÁC get_companies_needing_web_lookup() ở trên (dùng cho
    enrich_company_web_info.py, nguồn Tavily/Gemini, chỉ vá website/
    tax_id): hàm này ưu tiên dùng TRƯỚC vì chính xác hơn hẳn (đọc thẳng
    trang gốc, không qua search+LLM suy luận) và vá được CẢ 4 field —
    chỉ những công ty KHÔNG có source_profile_url (vd tạo tay, hoặc
    crawl từ nguồn không hỗ trợ fetch_company_profile) mới cần tới
    enrich_company_web_info.py.

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


def list_companies(conn, *, keyword: Optional[str] = None,
                    has_social: Optional[bool] = None,
                    province_name: Optional[str] = None,
                    created_by: Optional[str] = None,
                    include_inactive: bool = False,
                    limit: int = 50, offset: int = 0):
    """Trả (list[dict] company, total_count) — dùng cho GET /companies.

    has_social=True  -> chỉ công ty đã có fanpage_url HOẶC linkedin_url.
    has_social=False -> chỉ công ty còn thiếu CẢ HAI (tập ứng viên cho
    get_company_fb_linkedin_link.py) — tiện cho dashboard theo dõi tiến
    độ enrich mà không cần chạy script tay để biết còn bao nhiêu.

    created_by: lọc công ty do 1 thành viên ss_team/admin cụ thể tự
    thêm tay (xem sql/migration_add_audit_columns.sql) — dùng cho trang
    "theo dõi hoạt động" nội bộ (08/2026). Công ty tạo qua crawl pipeline
    có created_by NULL, không khớp filter này với bất kỳ UUID nào.

    include_inactive (08/2026, xem sql/migration_add_company_soft_delete.sql):
    False (mặc định) -> chỉ trả company is_active=true, giống pattern
    company_contacts. True -> xem cả company đã xoá mềm (vd trang xem
    lại lịch sử/audit log cần hiển thị tên company dù đã bị xoá)."""
    conditions = []
    params: list = []

    if keyword:
        conditions.append("c.company_name ILIKE %s")
        params.append(f"%{keyword}%")
    if province_name:
        conditions.append("p.province_name = %s")
        params.append(province_name)
    if has_social is True:
        conditions.append("(c.fanpage_url IS NOT NULL OR c.linkedin_url IS NOT NULL)")
    elif has_social is False:
        conditions.append("(c.fanpage_url IS NULL AND c.linkedin_url IS NULL)")
    if created_by:
        conditions.append("c.created_by = %s")
        params.append(created_by)
    if not include_inactive:
        conditions.append("c.is_active = true")

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT count(*) AS total FROM companies c "
            f"LEFT JOIN provinces p ON p.province_id = c.province_id {where_clause}",
            params,
        )
        total = cur.fetchone()["total"]

        cur.execute(
            f"{_COMPANY_LIST_BASE_QUERY} {where_clause} "
            f"ORDER BY c.created_at DESC LIMIT %s OFFSET %s",
            params + [limit, offset],
        )
        rows = cur.fetchall()

    return rows, total


def get_company_by_id(conn, company_id: str):
    """Trả 1 dict company đầy đủ hoặc None — dùng cho GET
    /companies/{company_id}. KHÔNG còn products_services (08/2026, xem
    sql/migration_drop_products_services.sql)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_COMPANY_SELECT_COLUMNS} "
            f"{_COMPANY_FROM_JOINS} "
            f"WHERE c.company_id = %s",
            (company_id,),
        )
        return cur.fetchone()


def soft_delete_company(conn, company_id: str, updated_by: str) -> bool:
    """Xoá MỀM — is_active=false, KHÔNG DELETE thật (JD/HR contact cũ
    vẫn tham chiếu company_id này). GET /companies mặc định chỉ trả
    is_active=true (xem list_companies() — CẦN thêm filter is_active
    nếu muốn ẩn hẳn company đã xoá khỏi danh sách chính).

    Trả False nếu company_id không tồn tại HOẶC đã is_active=false từ
    trước (idempotent — gọi lại nhiều lần trên company đã xoá không lỗi,
    nhưng router dùng giá trị False này để BIẾT không cần ghi thêm 1
    dòng audit log nữa, tránh log trùng lặp mỗi lần gọi lại)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE companies SET is_active = false, updated_by = %s, "
            "updated_at = now() WHERE company_id = %s AND is_active = true",
            (updated_by, company_id),
        )
        return cur.rowcount > 0
