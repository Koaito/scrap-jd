"""
Module DB — nói chuyện với PostgreSQL thật theo đúng schema.sql.
Đây là phần "dùng chung", không quan tâm dữ liệu đến từ TopCV hay nguồn nào.
"""

import json
import logging
from typing import Optional

import psycopg2
import psycopg2.extras

from config import DB_CONFIG

logger = logging.getLogger(__name__)


def get_connection():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    return conn


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
                                      tax_id: str = "") -> str:
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
            INSERT INTO companies (company_name, province_id, tax_id)
            VALUES (%s, %s, %s)
            RETURNING company_id
            """,
            (company_name, province_id, tax_id or None),
        )
        return str(cur.fetchone()[0])


def get_or_create_company(conn, company_name: str, province_id: Optional[int]) -> str:
    """Giữ lại cho tương thích ngược — không có tax_id, chỉ match theo tên.
    Ưu tiên dùng get_or_create_company_by_profile() khi có tax_id."""
    return get_or_create_company_by_profile(conn, company_name, province_id, tax_id="")


def update_company_profile(conn, company_id: str, *, tax_id: str = "", website: str = "",
                            industry: str = "", company_size: str = "",
                            address: str = "", products_services: str = "") -> None:
    """Cập nhật thêm thông tin công ty (chỉ ghi đè field nào có giá trị mới,
    không xóa mất dữ liệu cũ nếu lần crawl sau không lấy được field đó)."""
    updates = []
    values = []
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


def find_company_by_tax_id(conn, tax_id: str) -> Optional[str]:
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
                                       address: str = "",
                                       products_services: str = "") -> str:
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
        products_services=products_services,
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
                raw_jd_content: str = "") -> str:
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
                job_status, source_url, deadline, parsed_content
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN', %s, %s, %s)
            RETURNING job_id
            """,
            (company_id, job_title, matching_industry, level_id, province_id,
             work_type, currency, salary_min, salary_max, salary_type, source_url,
             deadline,
             json.dumps(parsed_content, ensure_ascii=False) if parsed_content else None),
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

def create_manual_job(conn, *, job_title: str, company_id: str,
                       matching_industry: str = "",
                       level_id: Optional[int] = None,
                       province_id: Optional[int] = None,
                       work_type: Optional[str] = None,
                       currency: str = "VNĐ",
                       salary_min: Optional[int] = None,
                       salary_max: Optional[int] = None,
                       salary_type: str = "NEGOTIABLE",
                       deadline=None) -> str:
    """Tạo 1 job NHẬP TAY từ frontend (không qua crawl/adapter). Tái dùng
    thẳng insert_job() đã có sẵn cho pipeline crawl — cùng 1 hàm ghi, chỉ
    khác nguồn gọi tới, tránh viết trùng logic INSERT job_postings +
    job_sources_log.

    KHÁC job crawl ở 2 điểm, để phân biệt rõ trong dữ liệu:
    - source_name = 'MANUAL' (thay vì 'TopCV'/'VietnamWorks').
    - source_url tự sinh dạng 'manual://<uuid>' — job nhập tay không có
      link JD gốc thật, nhưng job_sources_log.source_url là NOT NULL về
      mặt logic nghiệp vụ (dùng làm khoá chống trùng cho job crawl) nên
      cần 1 giá trị duy nhất thay vì để trống, tránh nhầm với chuỗi rỗng
      ở nơi khác trong code đang coi "" là chưa có giá trị."""
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
               ss_team_notes: Optional[str] = None) -> bool:
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

    Trả False nếu job_id không tồn tại (không có gì để update), True nếu
    đã update thành công — route dùng giá trị này để trả 404 đúng lúc."""
    updates = []
    values = []

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
        c.created_at, c.updated_at,
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
    """Trả 1 dict company đầy đủ (kèm products_services) hoặc None —
    dùng cho GET /companies/{company_id}."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"SELECT {_COMPANY_SELECT_COLUMNS}, c.products_services "
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

    return {
        "total_jobs": total_jobs,
        "total_companies": total_companies,
        "companies_with_social": companies_with_social,
        "by_industry": by_industry,
        "by_source": by_source,
    }
