"""
db.jobs — GHI job (insert/update/tạo tay) và các hàm "probe" cho pipeline crawl.

Tách từ db.py (God module) theo domain; 10/2026 tách tiếp phần đọc sang
db/job_queries.py (list_jobs, get_job_by_id, get_jobs_by_company_id) và phần
thống kê sang db/job_health.py (get_job_data_health).
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from config import DETAIL_RECHECK_DAYS

logger = logging.getLogger(__name__)


# BUG FIX (migrate Next.js, Phần 1 mục 3.3 của plan): sentinel riêng cho
# 2 tham số salary_min/salary_max của update_job() bên dưới — dùng thay
# cho default `None`, vì `None` đã bị dùng để biểu diễn "xoá lương cũ
# có chủ đích" (khác "không gửi field, giữ nguyên giá trị cũ"). Trước
# đây cả 2 tình huống này (không gửi field / gửi giá trị 0 hoặc null có
# chủ đích) đều đi qua cùng default `Optional[int] = None`, khiến
# update_job() KHÔNG CÓ CÁCH nào phân biệt "client không đụng tới field
# này" với "client cố ý gửi null" — dù docstring cũ đã khẳng định có
# phân biệt bằng "cờ has_*", code thực tế lại check `is not None` như
# field bình thường.
#
# Sentinel (thay vì thêm 1 tham số `bool` song song từng field) cho
# hàm nhận được CẢ `0` lẫn `None` như 2 giá trị hợp lệ khác nhau để
# ghi — cờ `bool` chỉ trả lời được "có gửi hay không", vẫn phải tự suy
# ra giá trị ghi là gì. Field khác gặp vấn đề tương tự trong tương lai
# chỉ cần đổi default sang _UNSET, không cần sửa lại chữ ký hàm.
_UNSET = object()

# Thêm 09/2026 (migrate Next.js, JobForm — trang sửa job): 4 field job mà
# update_job() bên dưới trước đây KHÔNG CÓ CÁCH NÀO đưa về NULL, vì mọi
# tham số của chúng dùng `is not None` để nghĩa là "có gửi" — gửi
# `null`/để trống chỉ bị bỏ qua, không báo lỗi (staff không thể xoá
# deadline, level, tỉnh, hình thức làm việc đã lỡ nhập).
#
# KHÔNG đổi 4 tham số đó sang sentinel `_UNSET` như cách làm với
# salary_min/salary_max ở trên, dù comment phía trên từng gợi ý vậy: nơi
# gọi thứ 2 là api/services/import_executor.py::_update_row() truyền
# level_id/province_id/work_type/deadline = None với nghĩa "ô này trống
# trong file import -> GIỮ NGUYÊN", nếu đổi default sang sentinel mà quên
# đổi None -> JOB_UNSET ở đó thì mỗi dòng import thiếu cột sẽ XOÁ dữ liệu
# cũ — sai theo hướng mất dữ liệu, không lộ ngay. Thay vào đó thêm 1 tham
# số RIÊNG `clear_fields` (mặc định rỗng = hành vi cũ giữ nguyên tuyệt
# đối cho mọi nơi gọi hiện có): chỉ nơi nào TƯỜNG MINH yêu cầu xoá (hiện
# chỉ patch_job(), khi client gửi field = null có chủ đích) mới đụng tới.
#
# Key = tên field ở tầng API (JobUpdate), value = tên CỘT trong
# job_postings. Cũng là whitelist duy nhất được nối vào câu SQL — không
# bao giờ nhận tên cột từ client.
JOB_CLEARABLE_FIELD_TO_COLUMN = {
    "deadline": "deadline",
    "level_code": "level_id",
    "province_name": "province_id",
    "work_type": "work_type",
}


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
    (job_id, work_type, deadline, parsed_content, detail_checked_at) hoặc None
    nếu job này chưa từng crawl. detail_checked_at là lần gần nhất fetch thành
    công trang chi tiết của CHÍNH URL này (NULL = chưa ghi nhận).

    Dùng để quyết định có cần fetch_job_full_detail() + update lại job CŨ
    hay không (job cũ có thể được crawl từ TRƯỚC khi tính năng work_type/
    deadline/parsed_content tồn tại, nên còn thiếu các field này)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT jp.job_id, jp.work_type, jp.deadline, jp.parsed_content,
                   jsl.detail_checked_at
            FROM job_postings jp
            JOIN job_sources_log jsl ON jsl.job_id = jp.job_id
            WHERE jsl.source_url = %s
            LIMIT 1
            """,
            (source_url,),
        )
        return cur.fetchone()


def job_needs_detail_enrichment(probe, *, now=None, recheck_days=None) -> bool:
    """probe = kết quả get_job_probe_by_source_url() (job_id, work_type,
    deadline, parsed_content, detail_checked_at) hoặc None. Trả True nếu nên gọi
    fetch_job_full_detail().

    - Job chưa từng thấy -> True.
    - Job đã đủ work_type + deadline + parsed_content -> False.
    - Job còn thiếu field: True nếu CHƯA từng ghi nhận fetch chi tiết (job cũ,
      crawl từ trước khi có cột detail_checked_at) HOẶC lần fetch gần nhất đã
      cách đây >= DETAIL_RECHECK_DAYS ngày. Trước đây điều kiện này luôn True,
      nên tin nào nguồn không ghi hạn nộp thì bị fetch lại ở mọi lượt crawl,
      mãi mãi. Vẫn tự chữa được: sửa xong selector bị hỏng thì tối đa chừng ấy
      ngày sau job được vá. recheck_days=0 -> luôn True (hành vi cũ)."""
    if probe is None:
        return True
    _, work_type, deadline, parsed_content, checked_at = probe
    if work_type and deadline and parsed_content:
        return False
    if checked_at is None:
        return True
    if recheck_days is None:
        recheck_days = DETAIL_RECHECK_DAYS
    if recheck_days <= 0:
        return True
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return now - checked_at >= timedelta(days=recheck_days)


def mark_source_detail_checked(conn, source_url: str) -> None:
    """Ghi nhận vừa fetch THÀNH CÔNG trang chi tiết của source_url này (không
    ghi khi fetch lỗi: lỗi có thể chỉ là tạm thời, lượt sau thử lại ngay).
    Không đụng job_postings nên không làm nhảy updated_at. Không tự commit."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE job_sources_log SET detail_checked_at = now() WHERE source_url = %s",
            (source_url,),
        )


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
                salary_period: str = "MONTH",
                created_by: Optional[str] = None,
                detail_fetched: bool = False) -> str:
    """Insert 1 job_postings + 1 job_sources_log tương ứng. content_hash được
    trigger Postgres tự tính (xem sql/schema.sql mục 5).

    parsed_content: dict {job_description, requirements, perks,
    required_skills} -> lưu vào job_postings.parsed_content (JSONB), dùng
    để tra cứu/lọc nhanh theo từng phần đã tách sẵn.
    raw_jd_content: text đã tách theo heading (KHÔNG phải HTML thô — HTML
    thô có nhiều rác kỹ thuật như SVG/class không có giá trị tra cứu lại)
    -> lưu vào job_sources_log.raw_jd_content, làm bằng chứng gốc để đối
    chiếu khi parsed_content bị lệch.
    salary_period: "MONTH" | "YEAR" — chu kỳ trả lương của salary_min/max
    (xem normalize.NormalizedSalary.salary_period + sql/migration_add_
    salary_period.sql). Mặc định "MONTH" khớp hành vi cũ."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO job_postings (
                company_id, job_title, matching_industry, level_id, province_id,
                work_type, currency, salary_min, salary_max, salary_type,
                salary_period, job_status, source_url, deadline, parsed_content,
                created_by
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN', %s, %s, %s, %s)
            RETURNING job_id
            """,
            (company_id, job_title, matching_industry, level_id, province_id,
             work_type, currency, salary_min, salary_max, salary_type, salary_period,
             source_url, deadline,
             json.dumps(parsed_content, ensure_ascii=False) if parsed_content else None,
             created_by),
        )
        job_id = cur.fetchone()[0]

        cur.execute(
            """
            INSERT INTO job_sources_log (job_id, source_name, source_url,
                                          salary_raw_content, raw_jd_content,
                                          detail_checked_at)
            VALUES (%s, %s, %s, %s, %s, CASE WHEN %s THEN now() END)
            ON CONFLICT (job_id, source_url) DO NOTHING
            """,
            (job_id, source_name, source_url, salary_raw_text, raw_jd_content or None,
             detail_fetched),
        )
        return str(job_id)


def link_repost_source(conn, job_id: str, *, source_name: str, source_url: str,
                       raw_jd_content: str = "", salary_raw_text: str = "") -> bool:
    """Ghi 1 source_url mới vào job ĐÃ CÓ như một nguồn phụ (job_sources_log),
    không tạo job mới. Dùng khi pipeline nhận ra tin vừa crawl là đăng lại của
    job đã có (cùng company/title/level/province nhưng khác source_url).

    Trước đây tin đăng lại bị bỏ mà KHÔNG ghi gì, nên lượt crawl sau URL đó vẫn
    "chưa từng thấy": fetch chi tiết, xử lý công ty rồi lại bỏ, lặp mãi. Có dòng
    log này thì get_job_probe_by_source_url() nhận ra URL, đi nhánh "job đã có"
    và không fetch lại nếu job đã đủ field.

    raw_jd_content được giữ làm bằng chứng gốc của tin đăng lại: nếu sau này
    khoá trùng được siết chặt hơn thì còn dữ liệu để xem lại tin nào từng bị
    gộp nhầm. job_postings.source_url (nguồn gốc của job) KHÔNG đổi.

    Không tự commit (đúng quy ước của lớp db: nơi gọi chịu trách nhiệm).
    Trả True nếu vừa thêm dòng mới, False nếu (job_id, source_url) đã có."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO job_sources_log (job_id, source_name, source_url,
                                          salary_raw_content, raw_jd_content,
                                          detail_checked_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (job_id, source_url) DO NOTHING
            """,
            (job_id, source_name, source_url, salary_raw_text, raw_jd_content or None),
        )
        return cur.rowcount > 0


def extend_job_deadline(conn, job_id: str, new_deadline) -> bool:
    """Dời deadline của job OPEN ra SAU (hoặc điền khi đang NULL), không bao
    giờ rút ngắn. Dùng khi pipeline nhận ra tin đăng lại có hạn nộp mới hơn:
    job cũ đã quá hạn (deadline < hôm nay nhưng vẫn OPEN, đang nằm trong danh
    sách "job hết hạn" của tab tình trạng dữ liệu) sẽ sống lại đúng với thực
    tế là nhà tuyển dụng vừa đăng lại.

    Một câu UPDATE có điều kiện nên không cần đọc deadline cũ trước và không
    có race giữa đọc-rồi-ghi. Job CLOSED không bị đụng (người dùng đã chủ động
    đóng). Không tự commit. Trả True nếu có dòng được cập nhật."""
    if new_deadline is None:
        return False
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE job_postings
               SET deadline = %s
             WHERE job_id = %s
               AND job_status = 'OPEN'
               AND (deadline IS NULL OR deadline < %s)
            """,
            (new_deadline, job_id, new_deadline),
        )
        return cur.rowcount > 0


def find_jobs_by_source_url_regex(conn, *, source_name: str, url_regex: str) -> list:
    """Các job đã có ÍT NHẤT MỘT nguồn (job_sources_log) của source_name với
    source_url khớp url_regex (regex POSIX của Postgres). Dùng để tìm job cùng
    mã số ở nguồn mà URL đổi theo tiêu đề (VietnamWorks: ...-<mã>-jv, nhà tuyển
    dụng sửa tiêu đề thì phần chữ của URL đổi còn mã giữ nguyên).

    Chỉ ĐỌC, không tự commit. Trả list[(job_id, job_title, job_status,
    updated_by, created_at)], cũ nhất trước (created_at, rồi job_id) để hai dòng
    cùng điểm thì pipeline chọn được dòng tạo sớm nhất. updated_by khác NULL nghĩa
    là đã có người trong team sửa tay. url_regex do adapter dựng từ mã số (chỉ
    chữ số), không bao giờ lấy từ người dùng."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT jp.job_id, jp.job_title, jp.job_status, jp.updated_by, jp.created_at
            FROM job_postings jp
            WHERE EXISTS (
                SELECT 1 FROM job_sources_log jsl
                WHERE jsl.job_id = jp.job_id
                  AND jsl.source_name = %s
                  AND jsl.source_url ~ %s
            )
            ORDER BY jp.created_at, jp.job_id
            """,
            (source_name, url_regex),
        )
        return cur.fetchall()


def update_job_from_recrawl(conn, job_id: str, *, job_title: str,
                             level_id: Optional[int] = None,
                             work_type: Optional[str] = None,
                             parsed_content: Optional[dict] = None,
                             salary: Optional[dict] = None) -> bool:
    """Cập nhật 1 job ĐÃ CÓ bằng dữ liệu vừa crawl lại (tin đã đổi tiêu đề nên URL
    mới, xem pipeline._update_job_by_job_code). Ghi:
      - job_title: luôn ghi;
      - level_id / work_type / parsed_content: chỉ ghi khi có giá trị (None/rỗng
        = giữ nguyên, để lần crawl không lấy được field đó không xoá dữ liệu cũ);
      - salary: dict {currency, salary_min, salary_max, salary_type,
        salary_period}, ghi NGUYÊN BỘ khi truyền (salary_min/max None là NULL
        thật); None = giữ nguyên lương cũ.
    KHÔNG đụng công ty, tỉnh, ngành, deadline (deadline có extend_job_deadline
    riêng, không bao giờ rút ngắn).

    Chốt chặn nằm ngay trong câu UPDATE: chỉ ghi khi job còn OPEN và chưa có ai
    sửa tay (updated_by IS NULL), nên nếu trong lúc pipeline xử lý có người vừa
    sửa/đóng job thì không bị ghi đè (không có race giữa đọc rồi ghi). Không tự
    commit. Trả True nếu có dòng được cập nhật."""
    updates = ["job_title = %s"]
    values = [job_title]
    if level_id is not None:
        updates.append("level_id = %s")
        values.append(level_id)
    if work_type:
        updates.append("work_type = %s")
        values.append(work_type)
    if parsed_content:
        updates.append("parsed_content = %s")
        values.append(json.dumps(parsed_content, ensure_ascii=False))
    if salary is not None:
        updates.extend([
            "currency = %s", "salary_min = %s", "salary_max = %s",
            "salary_type = %s", "salary_period = %s",
        ])
        values.extend([
            salary["currency"], salary["salary_min"], salary["salary_max"],
            salary["salary_type"], salary["salary_period"],
        ])

    values.append(job_id)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE job_postings SET {', '.join(updates)} "
            "WHERE job_id = %s AND job_status = 'OPEN' AND updated_by IS NULL",
            values,
        )
        return cur.rowcount > 0


def count_jobs(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM job_postings")
        return cur.fetchone()[0]


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
                       salary_period: str = "MONTH",
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
    dùng (xem pipeline._build_parsed_content_and_raw()).

    salary_period (thêm 08/2026, xem sql/migration_add_salary_period.sql):
    "MONTH" | "YEAR" — mặc định "MONTH". Job nhập tay KHÔNG qua
    normalize_salary() (staff tự gõ salary_min/max sẵn số VNĐ/USD), nên
    KHÔNG tự suy luận được period từ text như job crawl — staff phải tự
    chọn đúng "YEAR" qua API nếu nhập lương năm, nếu không sẽ mặc định
    hiểu là lương/tháng (giữ nguyên hành vi trước khi có cột này)."""
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
        salary_period=salary_period,
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
               salary_min=_UNSET,
               salary_max=_UNSET,
               salary_type: Optional[str] = None,
               salary_period: Optional[str] = None,
               deadline=None,
               job_status: Optional[str] = None,
               ss_team_notes: Optional[str] = None,
               parsed_content: Optional[dict] = None,
               updated_by: Optional[str] = None,
               clear_fields: Optional[Iterable[str]] = None) -> bool:
    """Sửa TỰ DO các field của 1 job đã tồn tại — dùng cho PATCH /jobs/{id}
    phía frontend. KHÔNG phân biệt job crawl hay job nhập tay (team không
    cần phân quyền, mọi người dùng nội bộ ngang quyền — xem quyết định
    thiết kế trong API_README.md).

    Cũng là cách "xoá mềm" 1 job: gọi update_job(job_id, job_status='CLOSED')
    thay vì DELETE thật — job vẫn còn trong DB nên KHÔNG bị crawl lại tạo
    trùng ở lượt crawl sau (get_job_probe_by_source_url() vẫn thấy job
    này qua job_sources_log, không insert lại).

    salary_min/salary_max: CHO PHÉP truyền 0 HOẶC None (khác việc
    KHÔNG truyền field này) — vd người dùng muốn xoá lương cũ, sửa lại
    "Thoả thuận" (NEGOTIABLE). Dùng sentinel `_UNSET` (định nghĩa đầu
    module) làm default, KHÔNG phải `None`, để phân biệt đúng 3 tình
    huống: "không gửi field" (giữ nguyên giá trị cũ, default =
    `_UNSET`), "gửi 0" (xoá lương, coi là "Thoả thuận"), "gửi None có
    chủ đích" (cũng xoá lương) — khác các hàm update_* khác trong file
    này vốn coi `None` là "bỏ qua", ở đây `None` là 1 giá trị HỢP LỆ
    cần ghi, không phải tín hiệu "bỏ qua".

    **Cần truyền đúng `_UNSET` ở CẢ 2 nơi gọi hàm này** (không chỉ 1):
    `api/routers/jobs.py::patch_job()` (dựa vào
    `payload.model_fields_set` của Pydantic để biết field có mặt trong
    body PATCH hay không) và
    `api/services/import_executor.py::_update_row()` (dựa vào
    `"salary_min" in data`/`"salary_max" in data` — dict thuần dựng lúc
    build preview, không qua Pydantic nên không có `model_fields_set`).
    Quên 1 trong 2 nơi sẽ khiến field lương "sống 2 luật khác nhau" tuỳ
    đường vào (PATCH thủ công vs luồng import).

    parsed_content (thêm 08/2026): gửi dict {job_description, requirements,
    perks, required_skills} sẽ GHI ĐÈ TOÀN BỘ giá trị cũ (không merge
    từng key con — client tự gộp với giá trị cũ nếu chỉ muốn sửa 1 phần,
    lấy giá trị cũ qua GET /jobs/{id} trước khi PATCH).

    salary_period (thêm 08/2026): "MONTH" | "YEAR" — dùng pattern optional
    thường (bỏ qua nếu None) giống salary_type, KHÔNG dùng cờ has_* như
    salary_min/max, vì đây là enum chữ chứ không phải số — không có
    trường hợp "0 khác None" cần phân biệt ở đây.

    clear_fields (thêm 09/2026): tập TÊN CỘT cần đưa về NULL, chỉ nhận
    giá trị trong JOB_CLEARABLE_FIELD_TO_COLUMN.values() ("deadline",
    "level_id", "province_id", "work_type") — xem comment ở
    JOB_CLEARABLE_FIELD_TO_COLUMN lý do dùng tham số riêng thay vì sentinel.
    Mặc định None/rỗng = KHÔNG xoá gì (hành vi cũ, mọi nơi gọi hiện có —
    import_executor, check_expired_source_jobs — không bị ảnh hưởng).
    Raise ValueError nếu chứa tên cột ngoài whitelist (lỗi lập trình, đồng
    thời chặn nối chuỗi lạ vào SQL) hoặc nếu 1 cột vừa được gán giá trị
    mới vừa yêu cầu xoá (mâu thuẫn — không tự đoán bên nào thắng).

    Trả False nếu job_id không tồn tại (không có gì để update), True nếu
    đã update thành công — route dùng giá trị này để trả 404 đúng lúc."""
    updates = []
    values = []

    clear_cols = set(clear_fields or ())
    unknown = clear_cols - set(JOB_CLEARABLE_FIELD_TO_COLUMN.values())
    if unknown:
        raise ValueError(f"clear_fields chứa cột không được phép xoá: {sorted(unknown)}")
    _given = {
        "level_id": level_id, "province_id": province_id,
        "work_type": work_type, "deadline": deadline,
    }
    conflict = sorted(c for c in clear_cols if _given[c] is not None)
    if conflict:
        raise ValueError(f"cột vừa được gán giá trị vừa yêu cầu xoá: {conflict}")

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
    if salary_min is not _UNSET:
        updates.append("salary_min = %s")
        values.append(salary_min)  # ghi được cả 0 lẫn None nếu client cố ý gửi null
    if salary_max is not _UNSET:
        updates.append("salary_max = %s")
        values.append(salary_max)  # ghi được cả 0 lẫn None nếu client cố ý gửi null
    if salary_type is not None:
        updates.append("salary_type = %s")
        values.append(salary_type)
    if salary_period is not None:
        updates.append("salary_period = %s")
        values.append(salary_period)
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
    # Tên cột lấy từ whitelist đã kiểm tra ở đầu hàm, không phải từ client.
    for col in sorted(clear_cols):
        updates.append(f"{col} = NULL")

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
