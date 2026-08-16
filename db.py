"""
Module DB — nói chuyện với PostgreSQL thật theo đúng schema.sql.
Đây là phần "dùng chung", không quan tâm dữ liệu đến từ TopCV hay nguồn nào.
"""

import json
import logging
import uuid as uuid_module
from datetime import datetime, timezone
from typing import Optional

import psycopg2
import psycopg2.extras
import psycopg2.pool

from config import DB_CONFIG, DB_POOL_MAX, DB_POOL_MIN

logger = logging.getLogger(__name__)


def is_valid_uuid(value: Optional[str]) -> bool:
    """Kiểm tra `value` có đúng định dạng UUID không, TRƯỚC khi đưa vào
    query Postgres. BUG ĐÃ VÁ (08/2026, phát hiện qua test thật): nếu
    truyền thẳng 1 chuỗi sai định dạng UUID (vd người dùng quên thay thế
    placeholder mẫu như "<company_id_vừa_tạo_ở_bước_1>" bằng ID thật) vào
    cột kiểu UUID, psycopg2 raise lỗi KHÔNG được bắt (InvalidTextRepresentation)
    -> vọt thành 500 Internal Server Error mù mờ, không rõ nguyên nhân cho
    người gọi API. Validate trước bằng hàm này để trả 400 rõ ràng thay
    vì để Postgres tự raise lỗi giữa chừng request."""
    if not value:
        return False
    try:
        uuid_module.UUID(str(value))
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def get_connection():
    """Mở 1 connection Postgres MỚI, ĐỘC LẬP với pool bên dưới — dùng cho
    CLI/script chạy 1 lần rồi thoát (main.py, enrich_company_web_info.py,
    get_company_fb_linkedin_link.py, api/crawl_runner.py chạy nền). Các
    nơi này mở/đóng đúng 1 lần mỗi lần chạy, tần suất thấp -> không cần
    pool, và code gọi conn.close() trực tiếp (không phải
    release_connection()) nên KHÔNG được đổi hàm này sang lấy từ pool
    (nếu đổi, conn.close() ở những nơi đó sẽ đóng vật lý connection mà
    không trả "chỗ" lại cho pool, làm pool rò rỉ dần tới khi hết
    maxconn).

    Muốn dùng pool (traffic lặp lại nhiều lần/giây, như API layer) ->
    dùng get_pooled_connection() + release_connection() bên dưới."""
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn


# ============================================================
# CONNECTION POOL — dùng riêng cho API layer (thêm 08/2026)
#
# api/deps.py:get_db() chạy lại trên MỖI request HTTP -> mở/đóng
# connection Postgres thật mỗi lần (get_connection() ở trên) tốn round-
# trip TCP + TLS handshake không cần thiết khi traffic tăng (nhiều
# người dùng dashboard cùng lúc). psycopg2.pool.ThreadedConnectionPool
# giữ sẵn 1 nhóm connection mở sẵn, "mượn"/"trả" thay vì mở/đóng thật.
#
# ThreadedConnectionPool (không phải SimpleConnectionPool) vì FastAPI
# chạy route `def` thường trong 1 threadpool riêng (xem docstring
# api/deps.py) -> nhiều thread có thể gọi getconn()/putconn() đồng
# thời, cần bản pool an toàn với thread (SimpleConnectionPool không
# đảm bảo điều này).
#
# Khởi tạo LAZY (init_pool() gọi 1 lần lúc app khởi động qua FastAPI
# lifespan/startup event trong api/app.py) thay vì khởi tạo ngay lúc
# import module — để main.py/các script CLI import db.py mà KHÔNG cần
# kết nối DB ngay (vd `python main.py --help`), và để lỗi kết nối DB
# lúc khởi động API log rõ ràng thay vì crash mù mờ ngay lúc import.
# ============================================================

_pool: Optional[psycopg2.pool.ThreadedConnectionPool] = None


def init_pool(minconn: int = DB_POOL_MIN, maxconn: int = DB_POOL_MAX) -> None:
    """Khởi tạo pool 1 LẦN — gọi trong FastAPI startup event
    (api/app.py). Gọi lại khi pool đã tồn tại là no-op (an toàn nếu lỡ
    gọi 2 lần, vd test hoặc reload).

    minconn/maxconn: đọc từ config.py (DB_POOL_MIN/DB_POOL_MAX, đọc từ
    env DB_POOL_MIN/DB_POOL_MAX) — CÂN NHẮC set maxconn thấp hơn giới
    hạn connection Postgres phía Render/Supabase cho phép (managed
    Postgres tier free thường giới hạn thấp, vd 20-60 connection), để
    tránh pool "xin" nhiều hơn DB cho phép -> lỗi connect khi pool cố
    mở connection thứ maxconn."""
    global _pool
    if _pool is not None:
        logger.warning("init_pool() gọi lại khi pool đã tồn tại — bỏ qua.")
        return
    _pool = psycopg2.pool.ThreadedConnectionPool(minconn, maxconn, **DB_CONFIG)
    logger.info("Đã khởi tạo connection pool (minconn=%s, maxconn=%s).", minconn, maxconn)


def get_pooled_connection():
    """Mượn 1 connection từ pool — dùng trong api/deps.py:get_db().
    PHẢI trả lại bằng release_connection() (KHÔNG gọi conn.close()
    trực tiếp, xem lý do trong docstring get_connection() ở trên).

    Raise lỗi rõ ràng nếu gọi trước khi init_pool() chạy (lỗi cấu hình
    ở api/app.py, không nên xảy ra khi chạy qua uvicorn bình thường)
    thay vì để AttributeError mù mờ (None.getconn())."""
    if _pool is None:
        raise RuntimeError(
            "Connection pool chưa được khởi tạo — init_pool() phải chạy "
            "trong FastAPI startup event trước khi có request nào chạm "
            "get_db(). Kiểm tra lại api/app.py."
        )
    conn = _pool.getconn()
    conn.autocommit = False
    return conn


def release_connection(conn) -> None:
    """Trả connection về pool — dùng thay cho conn.close() trong
    api/deps.py:get_db(). An toàn gọi cả khi pool chưa init (no-op),
    tránh lỗi kép nếu request lỗi ngay từ get_pooled_connection()."""
    if _pool is None:
        return
    _pool.putconn(conn)


def close_pool() -> None:
    """Đóng TOÀN BỘ connection trong pool — gọi trong FastAPI shutdown
    event (api/app.py), tránh connection bị bỏ "treo" (leak) phía
    Postgres khi Render restart/deploy lại server."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None
        logger.info("Đã đóng connection pool.")


def apply_schema(conn, schema_path: str = "sql/schema.sql"):
    """Chạy file schema.sql (idempotent — có thể chạy lại nhiều lần an toàn)."""
    with open(schema_path, "r", encoding="utf-8") as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    logger.info("Đã áp dụng schema từ %s", schema_path)


def get_or_create_province(conn, province_name: str) -> Optional[int]:
    if not province_name:
        province_name = "Khác"
    with conn.cursor() as cur:
        cur.execute(
            "SELECT province_id FROM provinces WHERE province_name = %s", (province_name,)
        )
        row = cur.fetchone()
        if row:
            return row[0]
        cur.execute(
            "INSERT INTO provinces (province_name) VALUES (%s) RETURNING province_id",
            (province_name,),
        )
        return cur.fetchone()[0]


def get_level_id(conn, level_code: str) -> Optional[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT level_id FROM levels WHERE level_code = %s", (level_code,))
        row = cur.fetchone()
        return row[0] if row else None


def find_company_probe(conn, company_name: str):
    """Tra cứu nhanh theo TÊN (chỉ để quyết định có cần fetch_company_profile
    hay không, không phải nguồn match chính thức). Trả về
    (company_id, website, industry, company_size, address) hoặc None nếu
    chưa có công ty này.

    ĐỔI (08/2026, thêm VietnamWorks): bỏ tax_id ra khỏi probe này — xem
    lý do chi tiết trong probe_needs_enrichment() bên dưới."""
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

    products_services ĐÃ BỊ BỎ (08/2026, xem
    sql/migration_drop_products_services.sql) — không còn field CRM mô
    tả sản phẩm/dịch vụ nào ở company nữa, công ty chỉ còn hồ sơ thuần."""
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
        updates.append("company_size = %s")
        values.append(company_size)
    if address:
        updates.append("address = %s")
        values.append(address)
    if partnership_potential:
        updates.append("partnership_potential = %s")
        values.append(partnership_potential)

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


def get_open_jobs_with_source_url(conn):
    """Lấy job đang OPEN và có source_url (job crawl — job nhập tay
    KHÔNG có source_url nên tự động bị loại, không có gì để re-check).
    Dùng cho check_expired_source_jobs.py — script re-check job còn OPEN
    trong DB có còn tồn tại thật ở nguồn (TopCV/VietnamWorks) hay không.

    KHÔNG lấy job đã EXPIRED/CLOSED — không cần re-check job vốn đã
    không còn hiệu lực từ trước.

    Trả về list[(job_id, job_title, source_url, deadline)]."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT job_id, job_title, source_url, deadline
            FROM job_postings
            WHERE job_status = 'OPEN'
              AND source_url IS NOT NULL AND source_url != ''
            ORDER BY created_at
            """
        )
        return cur.fetchall()


def probe_needs_enrichment(probe) -> bool:
    """probe = kết quả find_company_probe() (company_id, website, industry,
    company_size, address) hoặc None. Trả True nếu nên gọi
    fetch_company_profile() — tức là công ty chưa từng thấy, hoặc đã thấy
    nhưng còn thiếu field nào đó trong bộ mô tả công ty.

    ĐỔI (08/2026, thêm VietnamWorks): TRƯỚC ĐÂY dùng (tax_id, website) để
    quyết định — với TopCV thì work vì công ty luôn CÓ CƠ HỘI lấy được
    tax_id (trang company profile TopCV hiển thị "Mã số thuế"). Nhưng
    VietnamWorks KHÔNG BAO GIỜ hiển thị mã số thuế công ty (đã xác nhận
    08/2026) -> nếu vẫn dùng tax_id, mọi công ty crawl từ VietnamWorks sẽ
    có tax_id RỖNG VĨNH VIỄN -> hàm này LUÔN trả True -> pipeline.py gọi
    lại fetch_company_profile() ở MỌI LẦN CRAWL cho MỌI công ty VNW, dù
    đã có đủ dữ liệu từ trước -> tốn request thừa vô hạn, ngược hẳn mục
    đích thiết kế ban đầu ("chỉ crawl công ty 1 lần").

    Giờ đổi sang: coi công ty là "đã đủ" khi đã có TẤT CẢ 4 field lấy được
    qua fetch_company_profile() (website, industry, company_size, address)
    — không quan tâm tax_id nữa (nguồn nào có thì companies.tax_id vẫn
    được lưu qua get_or_create_company_by_profile()/update_company_profile()
    như cũ, chỉ là KHÔNG dùng nó để quyết định có cần crawl lại hay không).

    Tác dụng phụ có lợi: đồng thời sửa luôn phần "công ty enrich dở dang"
    — trước đây nếu 1 lần crawl chỉ lấy được website nhưng thiếu industry
    (vd TopCV đổi label tạm thời), công ty coi như "đủ" mãi mãi vì đã có
    website; giờ sẽ tiếp tục được thử vá lại industry/company_size/address
    ở các lần crawl sau, cho tới khi đủ cả 4 field."""
    if probe is None:
        return True
    _, website, industry, company_size, address = probe
    return not (website and industry and company_size and address)


def job_exists_by_source_url(conn, source_url: str) -> bool:
    """Chống trùng theo link JD gốc — nếu link này đã crawl rồi thì bỏ qua,
    tránh insert lại job giống hệt mỗi lần chạy crawler."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM job_sources_log WHERE source_url = %s LIMIT 1",
            (source_url,),
        )
        return cur.fetchone() is not None


def get_job_probe_by_source_url(conn, source_url: str):
    """Tra cứu nhanh 1 job đã có theo source_url — trả về
    (job_id, work_type, deadline, parsed_content) hoặc None nếu job này
    chưa từng crawl.

    Dùng để quyết định có cần fetch_job_full_detail() + update lại job CŨ
    hay không (job cũ có thể được crawl từ TRƯỚC khi tính năng work_type/
    deadline/parsed_content tồn tại, nên còn thiếu các field này)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT jp.job_id, jp.work_type, jp.deadline, jp.parsed_content
            FROM job_postings jp
            JOIN job_sources_log jsl ON jsl.job_id = jp.job_id
            WHERE jsl.source_url = %s
            LIMIT 1
            """,
            (source_url,),
        )
        return cur.fetchone()


def job_needs_detail_enrichment(probe) -> bool:
    """probe = kết quả get_job_probe_by_source_url() (job_id, work_type,
    deadline, parsed_content) hoặc None. Trả True nếu nên gọi
    fetch_job_full_detail() — tức là job chưa từng thấy, hoặc đã thấy
    nhưng còn thiếu work_type/deadline/parsed_content (job cũ crawl từ
    trước khi có các tính năng này)."""
    if probe is None:
        return True
    _, work_type, deadline, parsed_content = probe
    return not work_type or not deadline or not parsed_content


def update_job_fields(conn, job_id: str, *, work_type: Optional[str] = None,
                       deadline=None, parsed_content: Optional[dict] = None) -> None:
    """Vá thêm work_type/deadline/parsed_content cho 1 job ĐÃ TỒN TẠI (chỉ
    ghi đè field nào có giá trị mới, không xóa dữ liệu cũ nếu lần crawl
    sau không lấy được field đó) — dùng cho job cũ crawl từ trước khi có
    các cột này."""
    updates = []
    values = []
    if work_type:
        updates.append("work_type = %s")
        values.append(work_type)
    if deadline:
        updates.append("deadline = %s")
        values.append(deadline)
    if parsed_content:
        updates.append("parsed_content = %s")
        values.append(json.dumps(parsed_content, ensure_ascii=False))

    if not updates:
        return

    values.append(job_id)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE job_postings SET {', '.join(updates)} WHERE job_id = %s",
            values,
        )


def insert_job(conn, *, company_id: str, job_title: str, matching_industry: str,
                level_id: Optional[int], province_id: Optional[int],
                work_type: Optional[str], currency: str,
                salary_min: Optional[int], salary_max: Optional[int],
                salary_type: str, source_url: str, source_name: str,
                salary_raw_text: str = "", deadline=None,
                parsed_content: Optional[dict] = None,
                raw_jd_content: str = "",
                created_by: Optional[str] = None) -> str:
    """Insert 1 job_postings + 1 job_sources_log tương ứng. content_hash được
    trigger Postgres tự tính (xem sql/schema.sql mục 5).

    parsed_content: dict {job_description, requirements, perks,
    required_skills} -> lưu vào job_postings.parsed_content (JSONB), dùng
    để tra cứu/lọc nhanh theo từng phần đã tách sẵn.
    raw_jd_content: text đã tách theo heading (KHÔNG phải HTML thô — HTML
    thô có nhiều rác kỹ thuật như SVG/class không có giá trị tra cứu lại)
    -> lưu vào job_sources_log.raw_jd_content, làm bằng chứng gốc để đối
    chiếu khi parsed_content bị lệch."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO job_postings (
                company_id, job_title, matching_industry, level_id, province_id,
                work_type, currency, salary_min, salary_max, salary_type,
                job_status, source_url, deadline, parsed_content, created_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN', %s, %s, %s, %s)
            RETURNING job_id
            """,
            (company_id, job_title, matching_industry, level_id, province_id,
             work_type, currency, salary_min, salary_max, salary_type, source_url,
             deadline,
             json.dumps(parsed_content, ensure_ascii=False) if parsed_content else None,
             created_by),
        )
        job_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO job_sources_log (job_id, source_name, source_url,
                                          salary_raw_content, raw_jd_content)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (job_id, source_url) DO NOTHING
            """,
            (job_id, source_name, source_url, salary_raw_text, raw_jd_content or None),
        )
        return str(job_id)


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


def count_jobs(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM job_postings")
        return cur.fetchone()[0]


# ============================================================
# GHI TỪ FRONTEND (thêm 08/2026) — team tự thêm/sửa/đóng job qua giao
# diện web, KHÔNG qua crawl. Tách riêng khỏi nhóm hàm pipeline crawl ở
# trên vì mục đích khác (ghi tay 1 job đơn lẻ theo yêu cầu người dùng,
# không phải insert hàng loạt từ adapter).
# ============================================================

def find_manual_job_duplicate(conn, *, company_id: str, job_title: str,
                               level_id: Optional[int],
                               province_id: Optional[int]) -> Optional[str]:
    """Tìm job đã tồn tại TRÙNG (company_id, job_title, level_id,
    province_id) — CÙNG bộ khoá mà trigger Postgres generate_job_hash()
    dùng để tính content_hash (xem sql/schema.sql mục 5) — dùng để chống
    trùng khi POST /jobs bị gọi nhiều lần với data y hệt.

    TẠI SAO CẦN HÀM RIÊNG (không tái dùng job_exists_by_source_url() có
    sẵn): job crawl chống trùng theo source_url (link JD gốc, ổn định,
    duy nhất) — nhưng job NHẬP TAY qua create_manual_job() không có link
    gốc thật, source_url tự sinh NGẪU NHIÊN mỗi lần gọi
    ("manual://<uuid4-mới>") NÊN LUÔN LUÔN KHÁC NHAU -> cơ chế chống
    trùng theo source_url KHÔNG BAO GIỜ bắt được job nhập tay bị gửi lặp
    (vd người dùng bấm "Execute" trên Swagger nhiều lần, hoặc double-
    click nút Submit ở frontend sau này) -> mỗi lần bấm tạo 1 job_id mới
    dù nội dung y hệt (phát hiện qua test thật 08/2026).

    So khớp job_title không phân biệt hoa/thường + bỏ khoảng trắng thừa
    (giống cách content_hash chuẩn hoá) — level_id/province_id dùng
    IS NOT DISTINCT FROM để so khớp đúng cả trường hợp NULL (khác NULL
    != NULL thông thường của SQL, nếu dùng = thường sẽ luôn False khi 1
    trong 2 bên NULL, bỏ sót trường hợp cả 2 cùng thiếu level/province).

    Trả về job_id đã có (str) nếu tìm thấy trùng, None nếu chưa có."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT job_id FROM job_postings
            WHERE company_id = %s
              AND lower(trim(job_title)) = lower(trim(%s))
              AND level_id IS NOT DISTINCT FROM %s
              AND province_id IS NOT DISTINCT FROM %s
              AND job_status != 'CLOSED'
            LIMIT 1
            """,
            (company_id, job_title, level_id, province_id),
        )
        row = cur.fetchone()
        return str(row[0]) if row else None


def create_manual_job(conn, *, job_title: str, company_id: str,
                       matching_industry: str = "",
                       level_id: Optional[int] = None,
                       province_id: Optional[int] = None,
                       work_type: Optional[str] = None,
                       currency: str = "VNĐ",
                       salary_min: Optional[int] = None,
                       salary_max: Optional[int] = None,
                       salary_type: str = "NEGOTIABLE",
                       deadline=None,
                       parsed_content: Optional[dict] = None,
                       created_by: Optional[str] = None) -> str:
    """Tạo 1 job NHẬP TAY từ frontend (không qua crawl/adapter). Tái dùng
    thẳng insert_job() đã có sẵn cho pipeline crawl — cùng 1 hàm ghi, chỉ
    khác nguồn gọi tới, tránh viết trùng logic INSERT job_postings +
    job_sources_log.

    IDEMPOTENT (08/2026, vá bug trùng job — xem find_manual_job_duplicate()):
    kiểm tra trùng TRƯỚC khi insert — nếu đã có job cùng (company_id,
    job_title, level_id, province_id) VÀ CHƯA bị đóng (job_status !=
    'CLOSED'), trả về job_id ĐÃ CÓ đó thay vì tạo mới. An toàn khi bấm
    "Execute"/Submit nhiều lần với data y hệt (double-click, F5, gọi lại
    do timeout tưởng lỗi...). Chỉ bỏ qua job đã CLOSED khi so khớp — cho
    phép tạo lại 1 job y hệt title/company nếu job cũ đã bị đóng có chủ
    đích (không coi đó là "trùng ngoài ý muốn").

    KHÁC job crawl ở 2 điểm, để phân biệt rõ trong dữ liệu:
    - source_name = 'MANUAL' (thay vì 'TopCV'/'VietnamWorks').
    - source_url tự sinh dạng 'manual://<uuid>' — job nhập tay không có
      link JD gốc thật, nhưng job_sources_log.source_url là NOT NULL về
      mặt logic nghiệp vụ (dùng làm khoá chống trùng cho job crawl) nên
      cần 1 giá trị duy nhất thay vì để trống, tránh nhầm với chuỗi rỗng
      ở nơi khác trong code đang coi "" là chưa có giá trị.

    parsed_content (thêm 08/2026, xem lịch sử trao đổi): dict
    {job_description, requirements, perks, required_skills} — trước đây
    job nhập tay KHÔNG có chỗ lưu mô tả JD chi tiết (chỉ job crawl mới
    có), giờ mở field này cho cả 2 nguồn, dùng chung 1 cột JSONB
    job_postings.parsed_content, cùng cấu trúc pipeline crawl đang
    dùng (xem pipeline._build_parsed_content_and_raw())."""
    existing_job_id = find_manual_job_duplicate(
        conn, company_id=company_id, job_title=job_title,
        level_id=level_id, province_id=province_id,
    )
    if existing_job_id:
        logger.info(
            "POST /jobs trùng (company_id=%s, job_title=%r, level_id=%s, "
            "province_id=%s) -> trả về job đã có %s, KHÔNG tạo mới.",
            company_id, job_title, level_id, province_id, existing_job_id,
        )
        return existing_job_id

    import uuid
    source_url = f"manual://{uuid.uuid4()}"
    return insert_job(
        conn,
        company_id=company_id,
        job_title=job_title,
        matching_industry=matching_industry,
        level_id=level_id,
        province_id=province_id,
        work_type=work_type,
        currency=currency,
        salary_min=salary_min,
        salary_max=salary_max,
        salary_type=salary_type,
        source_url=source_url,
        source_name="MANUAL",
        deadline=deadline,
        parsed_content=parsed_content,
        created_by=created_by,
    )


def update_job(conn, job_id: str, *, job_title: Optional[str] = None,
               matching_industry: Optional[str] = None,
               level_id: Optional[int] = None,
               province_id: Optional[int] = None,
               work_type: Optional[str] = None,
               currency: Optional[str] = None,
               salary_min: Optional[int] = None,
               salary_max: Optional[int] = None,
               salary_type: Optional[str] = None,
               deadline=None,
               job_status: Optional[str] = None,
               ss_team_notes: Optional[str] = None,
               parsed_content: Optional[dict] = None,
               updated_by: Optional[str] = None) -> bool:
    """Sửa TỰ DO các field của 1 job đã tồn tại — dùng cho PATCH /jobs/{id}
    phía frontend. KHÔNG phân biệt job crawl hay job nhập tay (team không
    cần phân quyền, mọi người dùng nội bộ ngang quyền — xem quyết định
    thiết kế trong API_README.md).

    Cũng là cách "xoá mềm" 1 job: gọi update_job(job_id, job_status='CLOSED')
    thay vì DELETE thật — job vẫn còn trong DB nên KHÔNG bị crawl lại tạo
    trùng ở lượt crawl sau (get_job_probe_by_source_url() vẫn thấy job
    này qua job_sources_log, không insert lại).

    salary_min/salary_max: CHO PHÉP truyền 0 (khác None) — vd người dùng
    muốn xoá lương cũ, sửa lại "Thoả thuận" (NEGOTIABLE, cả 2 đều None).
    Vì vậy dùng cờ has_* để phân biệt "không truyền field này" (giữ
    nguyên) với "truyền None có chủ đích" (xoá giá trị cũ) — khác các
    hàm update_* khác trong file này vốn coi giá trị rỗng/None là "bỏ
    qua", ở đây cần phân biệt rõ hơn vì lương là field có thể cố ý set
    về rỗng.

    parsed_content (thêm 08/2026): gửi dict {job_description, requirements,
    perks, required_skills} sẽ GHI ĐÈ TOÀN BỘ giá trị cũ (không merge
    từng key con — client tự gộp với giá trị cũ nếu chỉ muốn sửa 1 phần,
    lấy giá trị cũ qua GET /jobs/{id} trước khi PATCH).

    Trả False nếu job_id không tồn tại (không có gì để update), True nếu
    đã update thành công — route dùng giá trị này để trả 404 đúng lúc."""
    updates = []
    values = []

    if updated_by is not None:
        updates.append("updated_by = %s")
        values.append(updated_by)
    if job_title is not None:
        updates.append("job_title = %s")
        values.append(job_title)
    if matching_industry is not None:
        updates.append("matching_industry = %s")
        values.append(matching_industry)
    if level_id is not None:
        updates.append("level_id = %s")
        values.append(level_id)
    if province_id is not None:
        updates.append("province_id = %s")
        values.append(province_id)
    if work_type is not None:
        updates.append("work_type = %s")
        values.append(work_type)
    if currency is not None:
        updates.append("currency = %s")
        values.append(currency)
    if salary_min is not None:
        updates.append("salary_min = %s")
        values.append(salary_min)
    if salary_max is not None:
        updates.append("salary_max = %s")
        values.append(salary_max)
    if salary_type is not None:
        updates.append("salary_type = %s")
        values.append(salary_type)
    if deadline is not None:
        updates.append("deadline = %s")
        values.append(deadline)
    if job_status is not None:
        updates.append("job_status = %s")
        values.append(job_status)
    if ss_team_notes is not None:
        updates.append("ss_team_notes = %s")
        values.append(ss_team_notes)
    if parsed_content is not None:
        updates.append("parsed_content = %s")
        values.append(json.dumps(parsed_content, ensure_ascii=False))

    if not updates:
        return job_exists_by_id(conn, job_id)

    values.append(job_id)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE job_postings SET {', '.join(updates)} WHERE job_id = %s",
            values,
        )
        return cur.rowcount > 0


def job_exists_by_id(conn, job_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM job_postings WHERE job_id = %s LIMIT 1", (job_id,))
        return cur.fetchone() is not None


# ============================================================
# QUERY LAYER CHO API (thêm 08/2026, dùng cho FastAPI layer)
#
# Khác các hàm phía trên (dùng cho pipeline crawl — mỗi hàm 1 việc hẹp,
# match/update theo khoá cụ thể), nhóm hàm dưới đây phục vụ đọc dữ liệu
# LIST + FILTER + PHÂN TRANG cho client bên ngoài (frontend, dashboard).
#
# Dùng RealDictCursor để trả về list[dict] luôn (key = tên cột) — tiện
# FastAPI/Pydantic convert thẳng sang JSON, không cần map thủ công theo
# vị trí cột như các hàm cũ (tránh lỗi khi ai đó thêm/đổi thứ tự SELECT).
#
# CHỈ ĐỌC (không insert/update) -> không cần lo transaction/commit ở đây,
# nơi gọi (route FastAPI) không cần commit sau khi gọi các hàm này.
# ============================================================

_JOB_SELECT_COLUMNS = """
        jp.job_id, jp.job_title, jp.matching_industry, jp.work_type,
        jp.currency, jp.salary_min, jp.salary_max, jp.salary_type,
        jp.deadline, jp.job_status, jp.source_url, jp.created_at, jp.updated_at,
        jp.created_by, jp.updated_by,
        c.company_id, c.company_name,
        l.level_code,
        p.province_name
"""

_JOB_FROM_JOINS = """
    FROM job_postings jp
    JOIN companies c ON c.company_id = jp.company_id
    LEFT JOIN levels l ON l.level_id = jp.level_id
    LEFT JOIN provinces p ON p.province_id = jp.province_id
"""

# Giữ tên _JOB_LIST_BASE_QUERY để KHÔNG phải sửa list_jobs()/get_jobs_by_company_id()
# bên dưới — chúng chỉ cần "SELECT ... FROM ... JOIN..." nguyên khối, không cần
# thêm cột, nên ghép lại y hệt bản cũ.
_JOB_LIST_BASE_QUERY = f"SELECT {_JOB_SELECT_COLUMNS} {_JOB_FROM_JOINS}"


def list_jobs(conn, *, industry: Optional[str] = None, province_name: Optional[str] = None,
              level_code: Optional[str] = None, work_type: Optional[str] = None,
              keyword: Optional[str] = None, job_status: Optional[str] = None,
              limit: int = 50, offset: int = 0):
    """Trả (list[dict] job, total_count) — dùng cho GET /jobs.

    Mọi filter đều optional, bỏ qua field nào = None. `keyword` so khớp
    kiểu ILIKE trên job_title (không phân biệt hoa/thường, không cần
    khớp chính xác) — đủ dùng cho ô tìm kiếm đơn giản, KHÔNG phải full-
    text search (nếu sau này cần search nhanh trên dữ liệu lớn, nên
    thêm GIN index + to_tsvector riêng, không sửa hàm này vội).

    limit/offset: phân trang chuẩn — FastAPI route validate limit tối
    đa (tránh client xin limit=999999 kéo sập DB), hàm này KHÔNG tự
    giới hạn, cứ tin tưởng giá trị truyền vào."""
    conditions = []
    params: list = []

    if industry:
        conditions.append("jp.matching_industry = %s")
        params.append(industry)
    if province_name:
        conditions.append("p.province_name = %s")
        params.append(province_name)
    if level_code:
        conditions.append("l.level_code = %s")
        params.append(level_code)
    if work_type:
        conditions.append("jp.work_type = %s")
        params.append(work_type)
    if job_status:
        conditions.append("jp.job_status = %s")
        params.append(job_status)
    if keyword:
        conditions.append("jp.job_title ILIKE %s")
        params.append(f"%{keyword}%")

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SELECT count(*) AS total FROM job_postings jp "
                    f"JOIN companies c ON c.company_id = jp.company_id "
                    f"LEFT JOIN levels l ON l.level_id = jp.level_id "
                    f"LEFT JOIN provinces p ON p.province_id = jp.province_id "
                    f"{where_clause}", params)
        total = cur.fetchone()["total"]

        cur.execute(
            f"{_JOB_LIST_BASE_QUERY} {where_clause} "
            f"ORDER BY jp.created_at DESC LIMIT %s OFFSET %s",
            params + [limit, offset],
        )
        rows = cur.fetchall()

    return rows, total


def get_job_by_id(conn, job_id: str):
    """Trả 1 dict job đầy đủ (kèm parsed_content JSONB) hoặc None nếu
    không tìm thấy — dùng cho GET /jobs/{job_id}."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_JOB_SELECT_COLUMNS}, jp.parsed_content, jp.ss_team_notes "
            f"{_JOB_FROM_JOINS} "
            f"WHERE jp.job_id = %s",
            (job_id,),
        )
        return cur.fetchone()


_COMPANY_SELECT_COLUMNS = """
        c.company_id, c.company_name, c.tax_id, c.website, c.industry,
        c.company_size, c.address, c.fanpage_url, c.linkedin_url,
        c.partnership_potential,
        c.created_at, c.updated_at, c.created_by, c.updated_by,
        p.province_name
"""

_COMPANY_FROM_JOINS = """
    FROM companies c
    LEFT JOIN provinces p ON p.province_id = c.province_id
"""

_COMPANY_LIST_BASE_QUERY = f"SELECT {_COMPANY_SELECT_COLUMNS} {_COMPANY_FROM_JOINS}"


def list_companies(conn, *, keyword: Optional[str] = None,
                    has_social: Optional[bool] = None,
                    province_name: Optional[str] = None,
                    limit: int = 50, offset: int = 0):
    """Trả (list[dict] company, total_count) — dùng cho GET /companies.

    has_social=True  -> chỉ công ty đã có fanpage_url HOẶC linkedin_url.
    has_social=False -> chỉ công ty còn thiếu CẢ HAI (tập ứng viên cho
    get_company_fb_linkedin_link.py) — tiện cho dashboard theo dõi tiến
    độ enrich mà không cần chạy script tay để biết còn bao nhiêu."""
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


def get_jobs_by_company_id(conn, company_id: str):
    """Trả list[dict] toàn bộ job đang mở của 1 công ty — dùng cho
    GET /companies/{company_id}/jobs (chi tiết công ty kèm job liên
    quan, tiện cho trang detail phía frontend)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"{_JOB_LIST_BASE_QUERY} WHERE jp.company_id = %s "
            f"ORDER BY jp.created_at DESC",
            (company_id,),
        )
        return cur.fetchall()


def get_stats_summary(conn) -> dict:
    """Số liệu tổng quan cho dashboard — GET /stats."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT count(*) AS total_jobs FROM job_postings")
        total_jobs = cur.fetchone()["total_jobs"]

        cur.execute("SELECT count(*) AS total_companies FROM companies")
        total_companies = cur.fetchone()["total_companies"]

        cur.execute(
            "SELECT count(*) AS n FROM companies "
            "WHERE fanpage_url IS NOT NULL OR linkedin_url IS NOT NULL"
        )
        companies_with_social = cur.fetchone()["n"]

        cur.execute(
            "SELECT matching_industry, count(*) AS n FROM job_postings "
            "WHERE matching_industry IS NOT NULL "
            "GROUP BY matching_industry ORDER BY n DESC"
        )
        by_industry = cur.fetchall()

        cur.execute(
            "SELECT source_name, count(*) AS n FROM job_sources_log "
            "GROUP BY source_name ORDER BY n DESC"
        )
        by_source = cur.fetchall()

        # Thêm 08/2026 — dashboard frontend cần tổng số đơn ứng tuyển
        # toàn hệ thống, trước đây không có cách nào lấy được ngoại trừ
        # cộng dồn list_applications_for_job() cho từng job (tốn N lượt
        # gọi API). Đếm thẳng 1 lần ở đây rẻ hơn nhiều so với thêm 1
        # endpoint /stats/applications riêng.
        cur.execute("SELECT count(*) AS n FROM job_applications")
        total_applications = cur.fetchone()["n"]

    return {
        "total_jobs": total_jobs,
        "total_companies": total_companies,
        "companies_with_social": companies_with_social,
        "by_industry": by_industry,
        "by_source": by_source,
        "total_applications": total_applications,
    }


# ============================================================
# AUTH LAYER — đăng nhập người dùng (08/2026)
#
# KHÁC với API_KEY tĩnh (api/auth.py, dùng chung cho MỌI request kiểu
# "máy gọi máy") — nhóm hàm dưới đây phục vụ đăng nhập TỪNG NGƯỜI thật
# qua frontend (JWT access token + refresh token xoay vòng, xem
# api/security.py). Dùng chung bảng app_users đã có (mở rộng thêm
# cột qua sql/migration_add_auth.sql) thay vì tạo bảng users riêng — bảng
# này vốn đã đại diện đúng "người trong team".
# ============================================================

def get_user_by_email(conn, email: str):
    """Trả dict đầy đủ field (kể cả password_hash, failed_login_count,
    locked_until — CHỈ dùng nội bộ cho luồng login, KHÔNG lộ ra response
    API, xem api/schemas.py UserOut không có các field này) hoặc None
    nếu không tìm thấy. So khớp email KHÔNG phân biệt hoa/thường."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM app_users WHERE lower(email) = lower(%s)",
            (email,),
        )
        return cur.fetchone()


def get_user_by_id(conn, ss_user_id: str):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM app_users WHERE ss_user_id = %s", (ss_user_id,))
        return cur.fetchone()


def create_user(conn, *, full_name: str, email: str, password_hash: str,
                 role: str = "user", must_change_password: bool = True) -> str:
    """Tạo 1 tài khoản MỚI — qua POST /auth/users (admin tạo hộ, mọi
    role) hoặc CLI `python main.py create-admin` (tạo admin đầu tiên).
    Từ Phần 2 (đăng ký công khai) sẽ có thêm luồng tự đăng ký, luôn cố
    định role='user' ở tầng route, không cho tự chọn.

    role: 1 trong 3 giá trị 'user' < 'ss_team' < 'admin' (xem
    api.deps.ROLE_HIERARCHY, sql/migration_add_role_hierarchy.sql) —
    mặc định 'user' (thấp nhất, chỉ xem), KHÔNG tự cấp quyền CRUD như
    hành vi cũ (trước đây mặc định 'member' = toàn quyền CRUD)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app_users
                (full_name, email, role, password_hash, must_change_password, is_active)
            VALUES (%s, %s, %s, %s, %s, true)
            RETURNING ss_user_id
            """,
            (full_name, email, role, password_hash, must_change_password),
        )
        return str(cur.fetchone()[0])


def update_user_password(conn, ss_user_id: str, password_hash: str,
                          must_change_password: bool = False) -> None:
    """Ghi mật khẩu MỚI — dùng khi user tự đổi mật khẩu (must_change_password
    thường = False sau đó) hoặc admin reset hộ (thường = True, ép đổi lại
    ngay lần đăng nhập kế tiếp — xem docstring cột trong migration)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_users SET password_hash = %s, must_change_password = %s "
            "WHERE ss_user_id = %s",
            (password_hash, must_change_password, ss_user_id),
        )


def record_failed_login(conn, ss_user_id: str, *, lock_threshold: int, lock_minutes: int) -> bool:
    """Tăng failed_login_count lên 1; nếu vừa CHẠM ngưỡng lock_threshold,
    khoá tài khoản lock_minutes phút (set locked_until) và reset
    failed_login_count về 0 (để lần khoá SAU tính lại từ đầu, không cộng
    dồn vô hạn). Trả True nếu tài khoản VỪA bị khoá ở lần gọi này (route
    dùng để trả thông báo phù hợp), False nếu chỉ tăng đếm bình thường."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT failed_login_count FROM app_users WHERE ss_user_id = %s",
            (ss_user_id,),
        )
        row = cur.fetchone()
        if row is None:
            return False
        new_count = row[0] + 1

        if new_count >= lock_threshold:
            cur.execute(
                "UPDATE app_users SET failed_login_count = 0, "
                "locked_until = now() + (%s || ' minutes')::interval "
                "WHERE ss_user_id = %s",
                (lock_minutes, ss_user_id),
            )
            return True

        cur.execute(
            "UPDATE app_users SET failed_login_count = %s WHERE ss_user_id = %s",
            (new_count, ss_user_id),
        )
        return False


def is_account_locked(user_row) -> bool:
    """Kiểm tra thuần Python (không query thêm) — user_row lấy từ
    get_user_by_email()/get_user_by_id(), đọc field locked_until có sẵn."""
    locked_until = user_row.get("locked_until") if user_row else None
    if locked_until is None:
        return False
    now = datetime.now(timezone.utc)
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    return locked_until > now


def reset_failed_login(conn, ss_user_id: str) -> None:
    """Gọi sau khi đăng nhập ĐÚNG mật khẩu — xoá đếm sai, mở khoá (nếu
    có), cập nhật last_login_at."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_users SET failed_login_count = 0, locked_until = NULL, "
            "last_login_at = now() WHERE ss_user_id = %s",
            (ss_user_id,),
        )


def create_refresh_token(conn, *, ss_user_id: str, token_hash: str, expires_at,
                          user_agent: Optional[str] = None,
                          ip_address: Optional[str] = None) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO auth_refresh_tokens
                (ss_user_id, token_hash, expires_at, user_agent, ip_address)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING refresh_token_id
            """,
            (ss_user_id, token_hash, expires_at, user_agent, ip_address),
        )
        return str(cur.fetchone()[0])


def get_refresh_token_by_hash(conn, token_hash: str):
    """Trả dict (refresh_token_id, ss_user_id, expires_at, revoked_at,
    replaced_by_token_id...) hoặc None. Route tự kiểm tra hết hạn/đã
    revoke — hàm này chỉ tra cứu thuần, không tự raise/chặn gì."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM auth_refresh_tokens WHERE token_hash = %s", (token_hash,)
        )
        return cur.fetchone()


def revoke_refresh_token(conn, refresh_token_id: str,
                          replaced_by_token_id: Optional[str] = None) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE auth_refresh_tokens SET revoked_at = now(), replaced_by_token_id = %s "
            "WHERE refresh_token_id = %s AND revoked_at IS NULL",
            (replaced_by_token_id, refresh_token_id),
        )


def revoke_all_refresh_tokens_for_user(conn, ss_user_id: str) -> int:
    """Thu hồi TOÀN BỘ refresh token còn sống của 1 user — dùng khi: phát
    hiện refresh token bị TÁI SỬ DỤNG sau khi đã revoke (dấu hiệu bị đánh
    cắp, xem docstring cột replaced_by_token_id trong migration), hoặc
    khi đổi mật khẩu (đăng xuất mọi thiết bị khác cho an toàn), hoặc admin
    reset mật khẩu hộ người khác. Trả số token vừa bị thu hồi."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE auth_refresh_tokens SET revoked_at = now() "
            "WHERE ss_user_id = %s AND revoked_at IS NULL",
            (ss_user_id,),
        )
        return cur.rowcount


def list_users(conn):
    """Danh sách thành viên team (không lộ password_hash) — ss_team trở
    lên xem được (GET /auth/users, thêm 08/2026), dùng cho trang quản lý
    user phía frontend. phone/track thêm vào SELECT 08/2026 (xem
    migration_add_phone_track.sql) để khớp UserOut mới, không bắt buộc
    frontend phải dùng ngay."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT ss_user_id, full_name, email, role, is_active, "
            "must_change_password, last_login_at, created_at, phone, track "
            "FROM app_users ORDER BY created_at"
        )
        return cur.fetchall()


def update_user_role(conn, ss_user_id: str, new_role: str) -> bool:
    """Đổi role của 1 user — CHỈ gọi từ route admin-only (PATCH
    /auth/users/{id}/role). Route tự chặn admin đổi role CHÍNH MÌNH
    TRƯỚC KHI gọi hàm này (xem api/routers/auth.py) — hàm ở đây không tự
    biết "ai đang gọi", chỉ thực thi UPDATE thuần, tránh trộn logic
    nghiệp vụ vào tầng DB. Trả False nếu ss_user_id không tồn tại."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_users SET role = %s WHERE ss_user_id = %s",
            (new_role, ss_user_id),
        )
        return cur.rowcount > 0


def update_user_active_status(conn, ss_user_id: str, is_active: bool) -> bool:
    """Khoá/mở khoá VĨNH VIỄN 1 tài khoản — CHỈ gọi từ route admin-only
    (PATCH /auth/users/{id}/active-status). Route tự chặn admin tự khoá
    CHÍNH MÌNH TRƯỚC KHI gọi hàm này, cùng nguyên tắc với
    update_user_role() ở trên. Trả False nếu ss_user_id không tồn tại.

    Vô hiệu hoá KHÔNG revoke refresh token đang có — access token cũ
    (JWT, tối đa 30 phút) vẫn dùng được tới khi hết hạn tự nhiên, nhưng
    request refresh token tiếp theo sẽ bị chặn vì login()/refresh() đều
    kiểm tra is_active (xem api/routers/auth.py). Chấp nhận độ trễ tối
    đa 30 phút này — revoke JWT đang active cần thêm cơ chế blacklist,
    không cần thiết ở quy mô team nhỏ."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_users SET is_active = %s WHERE ss_user_id = %s",
            (is_active, ss_user_id),
        )
        return cur.rowcount > 0


# ------------------------------------------------------------------
# Đăng ký công khai + xác thực email (thêm 08/2026, xem
# sql/migration_add_email_verification.sql) — KHÁC create_user() ở
# trên (admin tạo hộ) vì ai cũng gọi được (không cần JWT), luôn cố định
# role='user', email_verified=false cho tới khi verify.
# ------------------------------------------------------------------

def create_user_pending_verification(conn, *, full_name: str, email: str,
                                      password_hash: str, verify_token: str,
                                      verify_expires,
                                      phone: Optional[str] = None,
                                      track: Optional[str] = None) -> str:
    """Tạo tài khoản role='user' CHƯA xác thực — KHÁC create_user() ở
    chỗ must_change_password=False (mật khẩu do CHÍNH người dùng tự đặt
    lúc đăng ký, không phải mật khẩu tạm admin sinh hộ, không cần ép đổi
    lại) và có thêm email_verify_token/expires. is_active vẫn true ngay
    từ đầu (is_active là cờ RIÊNG cho admin khoá tài khoản, KHÁC
    email_verified — 2 khái niệm độc lập, xem docstring migration).

    phone/track: thêm 08/2026 (xem sql/migration_add_phone_track.sql) —
    trước đó frontend đã gửi 2 field này lên nhưng không có chỗ lưu."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app_users
                (full_name, email, role, password_hash, must_change_password,
                 is_active, email_verified, email_verify_token, email_verify_expires,
                 phone, track)
            VALUES (%s, %s, 'user', %s, false, true, false, %s, %s, %s, %s)
            RETURNING ss_user_id
            """,
            (full_name, email, password_hash, verify_token, verify_expires, phone, track),
        )
        return str(cur.fetchone()[0])


def get_user_by_verify_token(conn, verify_token: str):
    """Trả dict user (đủ field, kể cả email_verify_expires) hoặc None
    nếu token không tồn tại — KHÔNG tự kiểm tra hết hạn ở đây, route tự
    so sánh email_verify_expires với thời gian hiện tại (tách trách
    nhiệm: hàm này chỉ tra cứu, route quyết định logic nghiệp vụ)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM app_users WHERE email_verify_token = %s",
            (verify_token,),
        )
        return cur.fetchone()


def mark_email_verified(conn, ss_user_id: str) -> None:
    """Đánh dấu đã xác thực + XOÁ token (đặt NULL) — token chỉ dùng
    được ĐÚNG 1 LẦN, xoá ngay sau khi verify thành công để không ai
    verify lại lần 2 bằng link cũ (link cũ giờ vô nghĩa, không trỏ tới
    token nào còn tồn tại trong DB nữa)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_users SET email_verified = true, "
            "email_verify_token = NULL, email_verify_expires = NULL "
            "WHERE ss_user_id = %s",
            (ss_user_id,),
        )


def set_new_verify_token(conn, ss_user_id: str, verify_token: str, verify_expires) -> None:
    """Ghi ĐÈ token xác thực mới — dùng cho POST /auth/resend-verification
    (token cũ hết hạn hoặc email thất lạc, user xin gửi lại). Token cũ
    (nếu còn) bị thay thế hoàn toàn, không dùng lại được nữa."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_users SET email_verify_token = %s, "
            "email_verify_expires = %s WHERE ss_user_id = %s",
            (verify_token, verify_expires, ss_user_id),
        )


# ------------------------------------------------------------------
# Company contacts (HR contact) — CRUD thêm 08/2026 (Phần 1 phân
# quyền). Bảng đã có sẵn từ schema.sql gốc (dùng nội bộ qua
# merge_companies() khi gộp company trùng), nhưng CHƯA từng có route
# public nào — đây là lần đầu lộ ra API, ss_team trở lên mới thấy được
# (xem require_role("ss_team") ở router).
# ------------------------------------------------------------------

def list_company_contacts(conn, company_id: str, *, include_inactive: bool = False):
    """Danh sách contact của 1 company. include_inactive=True để xem lại
    contact đã soft-delete (xem lịch sử liên hệ cũ) — mặc định False,
    chỉ trả contact đang active."""
    query = (
        "SELECT * FROM company_contacts WHERE company_id = %s"
        + ("" if include_inactive else " AND is_active = true")
        + " ORDER BY created_at DESC"
    )
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, (company_id,))
        return cur.fetchall()


def get_company_contact_by_id(conn, contact_id: str):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM company_contacts WHERE contact_id = %s", (contact_id,))
        return cur.fetchone()


def create_company_contact(conn, *, company_id: str, contact_name: str,
                            job_title: Optional[str] = None, work_email: Optional[str] = None,
                            social_link: Optional[str] = None, phone_number: Optional[str] = None,
                            found_source: Optional[str] = None, created_by: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO company_contacts
                (company_id, contact_name, job_title, work_email, social_link,
                 phone_number, found_source, collected_date, created_by, updated_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_DATE, %s, %s)
            RETURNING contact_id
            """,
            (company_id, contact_name, job_title, work_email, social_link,
             phone_number, found_source, created_by, created_by),
        )
        return str(cur.fetchone()[0])


def update_company_contact(conn, contact_id: str, *, contact_name: Optional[str] = None,
                            job_title: Optional[str] = None, work_email: Optional[str] = None,
                            social_link: Optional[str] = None, phone_number: Optional[str] = None,
                            contact_status: Optional[str] = None,
                            last_contacted_date=None, updated_by: str) -> bool:
    """Chỉ field truyền vào (khác None) mới bị ghi đè — giống pattern
    update_job()/update_company_profile() đã có, tránh phải gửi lại
    toàn bộ object mỗi lần PATCH."""
    fields, values = [], []
    for col, val in [
        ("contact_name", contact_name), ("job_title", job_title),
        ("work_email", work_email), ("social_link", social_link),
        ("phone_number", phone_number), ("contact_status", contact_status),
        ("last_contacted_date", last_contacted_date),
    ]:
        if val is not None:
            fields.append(f"{col} = %s")
            values.append(val)

    if not fields:
        return True  # không có gì để cập nhật, coi như thành công

    fields.append("updated_by = %s")
    values.append(updated_by)
    fields.append("updated_at = now()")
    values.append(contact_id)

    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE company_contacts SET {', '.join(fields)} WHERE contact_id = %s",
            values,
        )
        return cur.rowcount > 0


def soft_delete_company_contact(conn, contact_id: str, updated_by: str) -> bool:
    """Xoá MỀM — is_active=false, KHÔNG DELETE thật (xem
    sql/migration_add_role_hierarchy.sql mục 2 để hiểu lý do giữ lịch
    sử). GET mặc định sẽ không còn thấy contact này nữa."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE company_contacts SET is_active = false, updated_by = %s, "
            "updated_at = now() WHERE contact_id = %s",
            (updated_by, contact_id),
        )
        return cur.rowcount > 0


class ContactHasLinksError(Exception):
    """Contact đang có job_contact_links (đã từng gắn với job cụ thể,
    vd qua job_contact_interactions ghi log liên hệ) — không cho hard
    delete để tránh vỡ FK (job_contact_links.contact_id KHÔNG có ON
    DELETE CASCADE) hoặc mất lịch sử liên hệ theo từng job nếu cố
    cascade xuống. Nơi gọi (router) bắt exception này để trả lỗi rõ
    ràng cho staff, thay vì để lộ ra IntegrityError thô từ Postgres."""


def hard_delete_company_contact(conn, contact_id: str) -> bool:
    """Xoá THẬT — chỉ dùng làm bước 2 sau khi contact đã soft-delete
    (is_active=false), theo đúng thiết kế 2 bước đã quyết định (xem
    lịch sử trao đổi 08/2026): staff xoá mềm trước để xác nhận, xoá
    cứng chỉ để dọn hẳn contact rác/trùng/nhập nhầm không còn cần giữ
    lịch sử nữa — KHÔNG dùng để xoá nhanh 1 bước như soft-delete.

    Chặn (raise ContactHasLinksError) nếu contact còn job_contact_links
    — nghĩa là đã từng được gắn với ít nhất 1 job cụ thể (có thể kèm
    log tương tác ở job_contact_interactions) — xoá thật sẽ mất lịch sử
    liên hệ theo job đó, hoặc vỡ FK nếu Postgres chặn. Trường hợp này
    contact vẫn ở trạng thái xoá mềm (không đổi gì), staff cần tự xử lý
    job_contact_links liên quan trước nếu thực sự muốn xoá hẳn.

    KHÔNG tự kiểm tra is_active — nơi gọi (router) chịu trách nhiệm xác
    nhận contact đã soft-delete trước khi gọi hàm này (theo đúng luồng
    2 bước qua UI), hàm này chỉ lo phần DELETE + kiểm tra FK.

    Trả True nếu có xoá (rowcount > 0), False nếu contact_id không tồn
    tại (đã bị xoá trước đó / gõ sai id)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM job_contact_links WHERE contact_id = %s LIMIT 1",
            (contact_id,),
        )
        if cur.fetchone() is not None:
            raise ContactHasLinksError(
                f"Contact {contact_id} đang có liên kết với job (job_contact_links) — "
                "không thể xoá cứng, sẽ mất lịch sử liên hệ theo job đó."
            )
        cur.execute(
            "DELETE FROM company_contacts WHERE contact_id = %s",
            (contact_id,),
        )
        return cur.rowcount > 0


# ------------------------------------------------------------------
# Job applications — học viên (role='user') ứng tuyển job, staff xem ai
# đã nộp (08/2026, xem sql/migration_add_applications_saved_jobs.sql).
# KHÔNG có update/soft-delete — 1 lượt ứng tuyển là sự kiện xảy ra 1
# lần, không sửa lại; huỷ nhầm thì tạo lại record mới nếu cần (không
# nằm trong phạm vi hiện tại).
# ------------------------------------------------------------------

def create_job_application(conn, *, ss_user_id: str, job_id: str, note: Optional[str] = None) -> str:
    """Raise psycopg2.errors.UniqueViolation nếu user đã ứng tuyển job
    này rồi (uq_job_applications_user_job) — router bắt lỗi này để trả
    409 thay vì để lộ traceback 500."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO job_applications (ss_user_id, job_id, note)
            VALUES (%s, %s, %s)
            RETURNING application_id
            """,
            (ss_user_id, job_id, note),
        )
        return str(cur.fetchone()[0])


def list_applications_for_user(conn, ss_user_id: str):
    """Đơn ứng tuyển của 1 học viên — join thêm job_title/company_name
    để hiển thị trực tiếp, không cần frontend gọi thêm GET /jobs/{id}
    cho từng dòng."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT a.application_id, a.ss_user_id, a.job_id, a.note, a.applied_at,
                   j.job_title, j.job_status, c.company_name
            FROM job_applications a
            JOIN job_postings j ON j.job_id = a.job_id
            JOIN companies c ON c.company_id = j.company_id
            WHERE a.ss_user_id = %s
            ORDER BY a.applied_at DESC
            """,
            (ss_user_id,),
        )
        return cur.fetchall()


def list_applications_for_job(conn, job_id: str):
    """Ai đã ứng tuyển 1 job — staff (ss_team+) dùng để chủ động gửi hồ
    sơ cho HR. Join thêm full_name/email/phone từ app_users (bảng dùng
    chung cho mọi role, xem migration_add_role_hierarchy.sql) để staff
    khỏi phải tra riêng. phone thêm 08/2026 (xem
    migration_add_phone_track.sql) — có thể NULL nếu học viên đăng ký
    trước khi cột này tồn tại, hoặc bỏ trống lúc đăng ký (không bắt buộc)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT a.application_id, a.ss_user_id, a.job_id, a.note, a.applied_at,
                   u.full_name, u.email, u.phone
            FROM job_applications a
            JOIN app_users u ON u.ss_user_id = a.ss_user_id
            WHERE a.job_id = %s
            ORDER BY a.applied_at DESC
            """,
            (job_id,),
        )
        return cur.fetchall()


def delete_job_application(conn, *, ss_user_id: str, job_id: str) -> bool:
    """Huỷ ứng tuyển — DELETE thật (08/2026, đổi ý so với thiết kế ban
    đầu coi ứng tuyển là "sự kiện lịch sử không sửa/xoá" — xem lịch sử
    trao đổi: học viên cần rút lại được nếu bấm nhầm/đổi ý). Không cần
    is_active/soft-delete kiểu company_contacts — application không có
    giá trị tra cứu lịch sử như HR contact, xoá thật đơn giản hơn và
    học viên có thể ứng tuyển lại (uq_job_applications_user_job không
    còn chặn vì record cũ đã mất).

    Trả False nếu chưa từng ứng tuyển job này (không có gì để xoá) — route
    dùng để trả 404 đúng lúc."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM job_applications WHERE ss_user_id = %s AND job_id = %s",
            (ss_user_id, job_id),
        )
        return cur.rowcount > 0


# ------------------------------------------------------------------
# Saved jobs — bookmark riêng tư của học viên, KHÁC ứng tuyển. Không có
# route nào cho staff xem saved_jobs của người khác (xem migration).
# ------------------------------------------------------------------

def create_saved_job(conn, *, ss_user_id: str, job_id: str) -> str:
    """Raise psycopg2.errors.UniqueViolation nếu job đã được lưu rồi
    (uq_saved_jobs_user_job) — router bắt lỗi này để trả 409."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO saved_jobs (ss_user_id, job_id)
            VALUES (%s, %s)
            RETURNING saved_job_id
            """,
            (ss_user_id, job_id),
        )
        return str(cur.fetchone()[0])


def list_saved_jobs_for_user(conn, ss_user_id: str):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT s.saved_job_id, s.ss_user_id, s.job_id, s.created_at,
                   j.job_title, j.job_status, c.company_name
            FROM saved_jobs s
            JOIN job_postings j ON j.job_id = s.job_id
            JOIN companies c ON c.company_id = j.company_id
            WHERE s.ss_user_id = %s
            ORDER BY s.created_at DESC
            """,
            (ss_user_id,),
        )
        return cur.fetchall()


def delete_saved_job(conn, *, ss_user_id: str, job_id: str) -> bool:
    """Bỏ lưu — DELETE thật (không soft-delete, đây chỉ là bookmark,
    không cần giữ lịch sử như company_contacts)."""
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM saved_jobs WHERE ss_user_id = %s AND job_id = %s",
            (ss_user_id, job_id),
        )
        return cur.rowcount > 0


# ------------------------------------------------------------------
# Quên mật khẩu — thêm 08/2026, mirror ĐÚNG cơ chế
# get_user_by_verify_token/mark_email_verified/set_new_verify_token ở
# trên (email xác thực đăng ký), chỉ khác tên cột. Xem
# sql/migration_add_password_reset.sql.
# ------------------------------------------------------------------

def set_password_reset_token(conn, ss_user_id: str, reset_token: str, reset_expires) -> None:
    """Ghi token reset mật khẩu — gọi bởi POST /auth/forgot-password.
    Ghi ĐÈ token cũ nếu có (user xin gửi lại nhiều lần), token cũ (nếu
    còn) hết hiệu lực ngay vì không còn tồn tại trong DB để đối chiếu."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_users SET password_reset_token = %s, "
            "password_reset_expires = %s WHERE ss_user_id = %s",
            (reset_token, reset_expires, ss_user_id),
        )


def get_user_by_reset_token(conn, reset_token: str):
    """Trả dict user (đủ field, kể cả password_reset_expires) hoặc None
    nếu token không tồn tại — KHÔNG tự kiểm tra hết hạn ở đây, route tự
    so sánh password_reset_expires với thời gian hiện tại (tách trách
    nhiệm, giống get_user_by_verify_token())."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM app_users WHERE password_reset_token = %s",
            (reset_token,),
        )
        return cur.fetchone()


def reset_password_with_token(conn, ss_user_id: str, password_hash: str) -> None:
    """Ghi mật khẩu MỚI + XOÁ token reset (đặt NULL) trong CÙNG 1 câu
    UPDATE — token chỉ dùng được ĐÚNG 1 LẦN, xoá ngay sau khi dùng để
    không ai reset lại lần 2 bằng link cũ. must_change_password=false
    (khác update_user_password() mặc định — ở đây user VỪA TỰ CHỌN mật
    khẩu mới thật sự qua link email, không phải mật khẩu tạm admin sinh
    hộ, nên không cần ép đổi lại lần nữa)."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app_users SET password_hash = %s, must_change_password = false, "
            "password_reset_token = NULL, password_reset_expires = NULL "
            "WHERE ss_user_id = %s",
            (password_hash, ss_user_id),
        )
