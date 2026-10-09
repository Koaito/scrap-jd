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

from scrapjd.config import DETAIL_RECHECK_DAYS
from db.job_dedup_lock import lock_job_dedup_key
from db.job_levels import _check_level_signals, _check_level_stamp, _derived_level_assignments
from db.listing_state import (
    CONFLICT_NONE,
    insert_listing,
    job_is_closed_locked,
    mark_listing_detail_checked,
    sync_listings_after_job_update,
)
from scrapjd.normalize import LEVEL_SOURCE_MANUAL

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

# Giá trị hợp lệ của job_postings.closed_reason (CHECK chk_job_postings_closed_reason, xem
# sql/0037_add_job_closed_reason.sql). 'merged' là dự phòng, hiện chưa có nơi nào ghi.
JOB_CLOSED_REASONS = frozenset({"staff", "expired_auto", "merged", "unknown"})

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


def mark_source_detail_checked(conn, source_url: str, *, deadline=None) -> None:
    """Ghi nhận vừa fetch THÀNH CÔNG trang chi tiết của source_url này (không ghi khi fetch lỗi: lỗi có
    thể chỉ là tạm thời, lượt sau thử lại ngay). Ghi cả last_seen_at và, nếu có, hạn đọc được từ trang
    (`deadline`) vào listing, xem db.listing_state.mark_listing_detail_checked (C1).
    Không đụng job_postings nên không làm nhảy updated_at. Không tự commit."""
    mark_listing_detail_checked(conn, source_url, deadline=deadline)


def update_job_fields(conn, job_id: str, *, work_type: Optional[str] = None,
                       parsed_content: Optional[dict] = None) -> None:
    """Vá thêm work_type/parsed_content cho 1 job ĐÃ TỒN TẠI (chỉ
    ghi đè field nào có giá trị mới, không xóa dữ liệu cũ nếu lần crawl
    sau không lấy được field đó) — dùng cho job cũ crawl từ trước khi có
    các cột này. KHÔNG nhận deadline (C4 phần 2/3): hạn của job là giá trị suy ra từ các listing, hạn đọc được
    từ trang chi tiết đi qua mark_source_detail_checked (ghi vào listing rồi đồng bộ job)."""
    updates = []
    values = []
    if work_type:
        updates.append("work_type = %s")
        values.append(work_type)
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
                detail_fetched: bool = False,
                level_source: Optional[str] = None,
                level_rule_version: Optional[int] = None,
                level_signals: Optional[dict] = None) -> str:
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
    salary_period.sql). Mặc định "MONTH" khớp hành vi cũ.

    level_source / level_rule_version (10/2026): level này được suy ra thế nào.
    Pipeline crawl truyền căn cứ của normalize.derive_level() cùng
    normalize.LEVEL_RULE_VERSION; job nhập tay truyền 'manual' (không version).
    Để None = "chưa biết" (NULL), lệnh tính lại level sẽ xử lý sau — an toàn hơn
    đoán bừa. Xem _check_level_stamp() cho các tổ hợp hợp lệ.

    level_signals (10/2026): tín hiệu thô derive_level() đã đọc (normalize.
    build_level_signals), lưu vào job_postings.level_signals để tính lại level sau
    này không phải tải lại trang. Chỉ đi kèm level do máy suy; None = không lưu."""
    level_source, level_rule_version = _check_level_stamp(level_id, level_source, level_rule_version)
    level_signals = _check_level_signals(level_source, level_signals)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO job_postings (
                company_id, job_title, matching_industry, level_id, province_id,
                work_type, currency, salary_min, salary_max, salary_type,
                salary_period, job_status, source_url, deadline, parsed_content,
                created_by, level_source, level_rule_version, level_signals
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'OPEN', %s, %s, %s, %s, %s, %s, %s)
            RETURNING job_id
            """,
            (company_id, job_title, matching_industry, level_id, province_id,
             work_type, currency, salary_min, salary_max, salary_type, salary_period,
             source_url, deadline,
             json.dumps(parsed_content, ensure_ascii=False) if parsed_content else None,
             created_by, level_source, level_rule_version,
             json.dumps(level_signals, ensure_ascii=False) if level_signals is not None else None),
        )
        job_id = cur.fetchone()[0]

        # Listing đầu tiên của job (C1): OPEN vì job vừa tạo OPEN, hạn = hạn của job. INSERT thường, không
        # ON CONFLICT (C2: không còn dùng uq_job_source, ràng buộc đó gỡ ở migration riêng): job_id vừa sinh
        # nên chỉ có thể vướng UNIQUE (source_url) (D2) khi URL đã thuộc job khác, và khi đó raise
        # UniqueViolation là CỐ Ý (ồn ào): vòng lặp pipeline rollback cả job vừa insert thay vì để lại job
        # không có dòng log. Đổi đích ON CONFLICT sang (source_url) DO NOTHING sẽ nuốt lỗi đó.
        insert_listing(
            conn, job_id=job_id, source_name=source_name, source_url=source_url,
            salary_raw_text=salary_raw_text, raw_jd_content=raw_jd_content,
            detail_fetched=detail_fetched, deadline=deadline, on_conflict=CONFLICT_NONE,
        )
        return str(job_id)


def count_jobs(conn) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM job_postings")
        return cur.fetchone()[0]


def find_manual_job_duplicate(conn, *, company_id: str, job_title: str,
                               level_id: Optional[int],
                               province_id: Optional[int]) -> Optional[str]:
    """Tìm job CHƯA đóng mà POST /jobs nên coi là "đúng cái đó rồi": cùng khoá chống trùng
    (job_postings.dedup_key = công ty + tiêu đề chuẩn hoá + tỉnh, xem sql/0039_add_job_dedup_key.sql)
    VÀ cùng level. Dùng để chống tạo trùng khi POST /jobs bị gọi nhiều lần với data y hệt.

    TẠI SAO CẦN HÀM RIÊNG (không tái dùng job_exists_by_source_url()): job nhập tay không có link
    gốc, source_url tự sinh NGẪU NHIÊN mỗi lần ("manual://<uuid4-mới>"), nên chống trùng theo
    source_url không bao giờ bắt được job nhập tay bị gửi lặp (bấm Execute nhiều lần, double-click
    Submit...).

    Khác khoá của crawler ở ĐÚNG MỘT điểm, có chủ đích: level. Crawler không dùng level vì level do
    máy suy ra nên không đủ tin cậy làm danh tính. Nhập tay thì level do nhân viên chọn, nên cùng
    khoá nhưng KHÁC level được phép tạo job mới (create_job báo qua find_similar_open_jobs); cùng
    khoá và cùng level thì trả lại job cũ, để nhân viên vào sửa job đó thay vì tạo thêm.

    Tiêu đề chuẩn hoá (lower + gộp khoảng trắng) và tỉnh NULL do job_dedup_key() lo, nên không còn
    lệch với find_repost_candidate như trước (hàm này từng chỉ trim hai đầu). level_id so bằng
    IS NOT DISTINCT FROM để cả hai cùng thiếu level vẫn khớp. Job CLOSED không tính: cho phép tạo
    lại job y hệt khi job cũ đã bị đóng có chủ đích. Nhiều job khớp thì lấy job tạo sớm nhất (kết
    quả xác định).

    Trả về job_id đã có (str) nếu tìm thấy, None nếu chưa có."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT job_id FROM job_postings
            WHERE dedup_key = job_dedup_key(%s::uuid, %s::text, %s::int)
              AND level_id IS NOT DISTINCT FROM %s
              AND job_status != 'CLOSED'
            ORDER BY created_at, job_id
            LIMIT 1
            """,
            (company_id, job_title, province_id, level_id),
        )
        row = cur.fetchone()
        return str(row[0]) if row else None


def find_similar_open_jobs(conn, *, company_id: str, job_title: str,
                           province_id: Optional[int],
                           exclude_job_id: Optional[str] = None) -> list:
    """Các job CHƯA đóng cùng khoá chống trùng (dedup_key) với job vừa nhập, mọi level, để POST /jobs
    cảnh báo "đã có job giống" cho nhân viên. Chỉ đọc, không đóng transaction.

    exclude_job_id: bỏ job này ra (chính job vừa tạo hoặc vừa được trả lại). Mỗi phần tử là dict
    {job_id, job_title, level_code, job_status, deadline}, cũ nhất trước. Rỗng nếu không có job nào."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT j.job_id, j.job_title, l.level_code, j.job_status::text, j.deadline
              FROM job_postings j
              LEFT JOIN levels l ON l.level_id = j.level_id
             WHERE j.dedup_key = job_dedup_key(%s::uuid, %s::text, %s::int)
               AND j.job_status != 'CLOSED'
               AND (%s::uuid IS NULL OR j.job_id != %s::uuid)
             ORDER BY j.created_at, j.job_id
            """,
            (company_id, job_title, province_id, exclude_job_id, exclude_job_id),
        )
        return [
            {"job_id": str(r[0]), "job_title": r[1], "level_code": r[2],
             "job_status": r[3], "deadline": r[4]}
            for r in cur.fetchall()
        ]


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

    Hàm giành khoá advisory chống trùng (lock_job_dedup_key) trước khi tra nên PHẢI gọi trên kết
    nối không autocommit, và khoá được giữ tới khi nơi gọi commit/rollback. Chờ quá
    JOB_DEDUP_LOCK_TIMEOUT_MS thì raise JobDedupLockTimeout (nơi gọi phải rollback).

    IDEMPOTENT (08/2026, vá bug trùng job — xem find_manual_job_duplicate()):
    kiểm tra trùng TRƯỚC khi insert — nếu đã có job cùng khoá chống trùng
    (company_id + tiêu đề + tỉnh, job_postings.dedup_key) VÀ cùng level_id VÀ
    CHƯA bị đóng (job_status != 'CLOSED'), trả về job_id ĐÃ CÓ đó thay vì tạo
    mới. Cùng khoá nhưng KHÁC level thì vẫn tạo mới (route POST /jobs cảnh báo
    bằng find_similar_open_jobs). An toàn khi bấm
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
    # A4 (10/2026): giành khoá chống trùng TRƯỚC câu tra, giữ tới commit/rollback của nơi gọi, để
    # hai lần nhập tay (hoặc nhập tay lúc đang crawl) cùng khoá không cùng tra thấy "chưa có" rồi
    # cùng insert. Khoá theo khoá chống trùng (không gồm level) nên hai job KHÁC level vẫn tạo được,
    # chỉ là lần lượt. Giành lại khoá nhiều lần trong một transaction là an toàn (khoá tái nhập).
    lock_job_dedup_key(conn, company_id=company_id, job_title=job_title, province_id=province_id)
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
        # Level do người chọn: đóng dấu 'manual' để việc ghi tự động không đè.
        level_source=LEVEL_SOURCE_MANUAL if level_id is not None else None,
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
               closed_reason: Optional[str] = None,
               ss_team_notes: Optional[str] = None,
               parsed_content: Optional[dict] = None,
               updated_by: Optional[str] = None,
               clear_fields: Optional[Iterable[str]] = None,
               level_source=_UNSET,
               level_rule_version: Optional[int] = None,
               level_signals: Optional[dict] = None) -> bool:
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

    level_source / level_rule_version (10/2026): update_job() là đường SỬA TAY
    (PATCH /jobs/{id}, import), nên mặc định:
      - level_id ĐỔI GIÁ TRỊ (hoặc bị xoá qua clear_fields) -> đóng dấu
        level_source='manual', level_rule_version=NULL. So với giá trị CŨ ngay
        trong câu UPDATE: form gửi lại đúng level đang có (sửa field khác) thì
        KHÔNG bị coi là người sửa level.
      - level_id không đổi -> giữ nguyên dấu cũ.
    Script/lệnh tính lại level TỰ ĐỘNG phải truyền level_source (và
    level_rule_version khi nguồn do máy suy, xem _check_level_stamp; None = "chưa
    biết"). Khi đó dòng đã 'manual' được giữ nguyên, không bị đè
    (_derived_level_assignments). Quên truyền thì rơi về hướng AN TOÀN (manual,
    không bị tính lại đè), không phải hướng làm mất dữ liệu người sửa.

    level_signals (10/2026): tín hiệu thô đi kèm level do máy suy (chỉ ghi được khi
    truyền level_source). Đường sửa tay (không truyền level_source) KHÔNG đụng tới
    level_signals: đó là dữ kiện về nguồn crawl, không phụ thuộc ai đặt level.

    closed_reason (A2): vì sao job bị đóng, chỉ có nghĩa khi job_status='CLOSED'. Mặc định 'staff'
    (update_job là đường sửa tay: PATCH, import). check_expired_source_jobs truyền 'expired_auto'.
    Chỉ ghi khi job vừa chuyển từ OPEN sang CLOSED: job đã CLOSED mà form gửi lại job_status=CLOSED
    thì giữ lý do cũ (không biến 'expired_auto' thành 'staff'). Mở lại (job_status='OPEN') thì
    trigger set_job_closed_state tự xoá closed_reason/closed_at.

    Trả False nếu job_id không tồn tại (không có gì để update), True nếu
    đã update thành công — route dùng giá trị này để trả 404 đúng lúc."""
    if closed_reason is not None:
        if closed_reason not in JOB_CLOSED_REASONS:
            raise ValueError(f"closed_reason không hợp lệ: {closed_reason!r}")
        if job_status != "CLOSED":
            raise ValueError("closed_reason chỉ có nghĩa khi job_status='CLOSED'")
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
    if level_source is not _UNSET and level_id is None:
        raise ValueError("level_source chỉ có nghĩa khi truyền level_id")
    if level_signals is not None and level_source is _UNSET:
        raise ValueError("level_signals chỉ ghi được cùng level_source (đường ghi level tự động)")
    if level_id is not None:
        if level_source is _UNSET:
            # Đường sửa tay: chỉ coi là 'manual' khi level thật sự đổi (so với giá trị cũ).
            updates.extend([
                "level_id = %s",
                "level_source = CASE WHEN level_id IS DISTINCT FROM %s "
                "THEN 'manual' ELSE level_source END",
                "level_rule_version = CASE WHEN level_id IS DISTINCT FROM %s "
                "THEN NULL ELSE level_rule_version END",
            ])
            values.extend([level_id, level_id, level_id])
        else:
            level_sets, level_values = _derived_level_assignments(
                level_id, level_source, level_rule_version, level_signals,
            )
            updates.extend(level_sets)
            values.extend(level_values)
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
    was_closed = job_status == "OPEN" and job_is_closed_locked(conn, job_id)
    if job_status is not None:
        updates.append("job_status = %s")
        values.append(job_status)
        if job_status == "CLOSED":
            updates.append("closed_reason = CASE WHEN job_status <> 'CLOSED' THEN %s "
                           "ELSE closed_reason END")
            values.append(closed_reason or "staff")
    if ss_team_notes is not None:
        updates.append("ss_team_notes = %s")
        values.append(ss_team_notes)
    if parsed_content is not None:
        updates.append("parsed_content = %s")
        values.append(json.dumps(parsed_content, ensure_ascii=False))
    # Tên cột lấy từ whitelist đã kiểm tra ở đầu hàm, không phải từ client.
    for col in sorted(clear_cols):
        updates.append(f"{col} = NULL")
        if col == "level_id":
            # Người chủ động xoá level: ghi nhận là sửa tay (lệnh tính lại không điền lại).
            # Level vốn đã trống thì không có gì để ghi nhận.
            updates.extend([
                "level_source = CASE WHEN level_id IS NOT NULL THEN 'manual' ELSE level_source END",
                "level_rule_version = CASE WHEN level_id IS NOT NULL THEN NULL ELSE level_rule_version END",
            ])

    if not updates:
        return job_exists_by_id(conn, job_id)

    values.append(job_id)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE job_postings SET {', '.join(updates)} WHERE job_id = %s",
            values,
        )
        found = cur.rowcount > 0
    if found:
        sync_listings_after_job_update(
            conn, job_id, job_status=job_status, was_closed=was_closed,
            deadline_changed=deadline is not None or "deadline" in clear_cols, deadline=deadline,
        )
    return found


def job_exists_by_id(conn, job_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM job_postings WHERE job_id = %s LIMIT 1", (job_id,))
        return cur.fetchone() is not None
