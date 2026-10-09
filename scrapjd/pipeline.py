"""
Pipeline LÕI — phần dùng chung 100% cho mọi nguồn crawl.
Chỉ nói chuyện với BaseAdapter / RawJobRecord, không biết TopCV hay
ITviec là gì (đúng kiến trúc "1 khung chung + N adapter riêng" đã bàn).
"""

import logging
from dataclasses import asdict, dataclass
from typing import Callable, Optional

from scrapjd.adapters.base import DEFAULT_DEDUP_RESOLVERS, BaseAdapter, CrawlBlockedError
from scrapjd import db
from scrapjd.db.job_recrawl import AUTO_REOPEN_REASONS
from scrapjd import normalize
from scrapjd.config import DEGRADED_EMPTY_RATE
from scrapjd.field_stats import WARN_MIN_SAMPLES, EmptyFieldCounter, degraded_reasons
from scrapjd.pipeline_stats import PipelineStats

logger = logging.getLogger(__name__)


def _build_parsed_content_and_raw(job_detail: dict):
    """Từ dict trả về bởi fetch_job_full_detail(), build:
    - parsed_content: dict gọn để lưu JSONB (job_postings.parsed_content)
    - raw_jd_content: text đã tách theo heading, nối lại làm bằng chứng
      gốc (job_sources_log.raw_jd_content) — KHÔNG phải HTML thô, vì HTML
      thô có nhiều rác kỹ thuật (SVG/class) không có giá trị tra cứu lại.

    Trả (None, "") nếu job_detail rỗng/không có nội dung gì đáng lưu, để
    tránh ghi đè dữ liệu cũ bằng giá trị rỗng khi fetch chi tiết thất bại.
    """
    if not job_detail:
        return None, ""

    # Dedupe required_skills — nghi do artifact khi parse DOM (TopCV có
    # thể render lặp khối "Kỹ năng cần có", hoặc API VietnamWorks trả kèm
    # trùng phần tử). Dedupe ở đây (dùng chung cho mọi adapter) thay vì
    # sửa riêng từng adapter — an toàn hơn, không phụ thuộc nguồn dữ liệu
    # cụ thể nào. Giữ đúng thứ tự xuất hiện đầu tiên (không dùng set() vì
    # không đảm bảo thứ tự, và so khớp không phân biệt hoa/thường + khoảng
    # trắng thừa để bắt cả case trùng do khác biệt viết hoa nhỏ).
    raw_skills = job_detail.get("required_skills", []) or []
    seen = set()
    deduped_skills = []
    for skill in raw_skills:
        if not isinstance(skill, str):
            continue
        key = skill.strip().lower()
        if key and key not in seen:
            seen.add(key)
            deduped_skills.append(skill.strip())

    parsed_content = {
        "job_description": job_detail.get("job_description", ""),
        "requirements": job_detail.get("requirements", ""),
        "perks": job_detail.get("perks", ""),
        "required_skills": deduped_skills,
    }
    # Không có gì đáng lưu -> coi như rỗng, tránh insert 1 JSONB toàn chuỗi rỗng
    if not any(parsed_content.values()):
        return None, ""

    raw_parts = []
    if parsed_content["job_description"]:
        raw_parts.append("=== Mô tả công việc ===\n" + parsed_content["job_description"])
    if parsed_content["requirements"]:
        raw_parts.append("=== Yêu cầu ứng viên ===\n" + parsed_content["requirements"])
    if parsed_content["perks"]:
        raw_parts.append("=== Quyền lợi ứng viên ===\n" + parsed_content["perks"])
    raw_jd_content = "\n\n".join(raw_parts)

    return parsed_content, raw_jd_content


def _handle_existing_job(adapter: BaseAdapter, conn, raw, job_probe, stats: PipelineStats,
                          field_counter: "EmptyFieldCounter | None" = None) -> None:
    """Xử lý job ĐÃ TỪNG crawl trước đó (source_url trùng) — tách ra từ
    run_pipeline() (08/2026, xem lịch sử trao đổi refactor pipeline.py)
    thuần vì lý do đọc/test dễ hơn, KHÔNG đổi hành vi: bản gốc đây là
    bước (1) trong 1 hàm ~250 dòng làm hết mọi việc.

    Job đã crawl trước đó thì KHÔNG insert lại, nhưng job cũ có thể còn
    thiếu work_type/deadline/parsed_content (nếu được crawl từ trước
    khi các field này tồn tại) -> vá thêm rồi bỏ qua phần insert, không
    dừng cả job này như 1 lỗi.

    Luôn cộng stats.skipped_duplicate (job này chắc chắn KHÔNG được
    insert mới, có vá được hay không không ảnh hưởng điều đó) — người
    gọi (run_pipeline) luôn `continue` ngay sau khi gọi hàm này."""
    existing_job_id = job_probe[0]
    if db.job_needs_detail_enrichment(job_probe):
        detail = adapter.fetch_job_full_detail(raw.source_url)
        if field_counter is not None:
            field_counter.record_detail(detail)
        if detail is None:
            # Fetch thất bại thật sự — KHÔNG update gì cả (khác
            # với "update bằng rỗng"), để job này vẫn được
            # job_needs_detail_enrichment() nhận diện là còn
            # thiếu và tự thử lại ở lần crawl kế tiếp.
            stats.skipped_fetch_failed += 1
            logger.warning(
                "Bỏ qua vá job cũ (fetch chi tiết thất bại): %s @ %s",
                raw.job_title, raw.source_url,
            )
        else:
            new_work_type = normalize.normalize_work_type(detail.get("work_type", ""))
            new_deadline = normalize.normalize_deadline(detail.get("deadline_text", ""))
            new_parsed_content, _ = _build_parsed_content_and_raw(detail)

            # Hạn đọc được KHÔNG ghi thẳng vào job (C4 phần 2/3): mark_source_detail_checked ghi nó vào listing
            # rồi đồng bộ job, nên hạn job luôn là giá trị suy ra từ các listing.
            db.update_job_fields(
                conn, existing_job_id,
                work_type=new_work_type,
                parsed_content=new_parsed_content,
            )
            # Ghi dấu "đã fetch chi tiết": nếu nguồn thật sự không có field
            # còn thiếu thì job không bị fetch lại ở mọi lượt crawl nữa, chỉ
            # sau DETAIL_RECHECK_DAYS ngày (xem db.job_needs_detail_enrichment).
            db.mark_source_detail_checked(conn, raw.source_url, deadline=new_deadline)
            conn.commit()
            if new_work_type or new_deadline or new_parsed_content:
                stats.updated_existing += 1
                logger.info(
                    "Đã vá work_type/deadline/parsed_content cho job cũ: %s",
                    raw.job_title,
                )
    stats.skipped_duplicate += 1


def _resolve_company(adapter: BaseAdapter, conn, raw, company_name: str, province_id) -> str:
    """Tìm hoặc tạo company ứng với job đang xử lý, kèm enrich profile
    nếu cần — tách ra từ run_pipeline() (08/2026, xem docstring
    _handle_existing_job() ở trên để biết lý do tách chung), nguyên bản
    là bước (3b) trong vòng lặp chính. Hành vi không đổi, chỉ thêm việc đóng
    transaction đọc trước khi chờ mạng tải trang công ty (xem chú thích ở dưới).

    "probe" chỉ tra cứu THEO TÊN để BIẾT trước công ty này đã đủ
    thông tin (tax_id + website) chưa — không dùng để match chính
    thức, vì tên có thể trùng/lệch giữa các lần đăng tin. Việc
    match chính thức nằm ở get_or_create_company_by_profile()
    bên dưới, ưu tiên theo tax_id.

    Trả về company_id (str)."""
    probe = db.find_company_probe(conn, company_name)
    profile = {}
    if raw.company_url and db.probe_needs_enrichment(probe):
        # Tải trang công ty là chờ mạng (vài giây tới hàng chục giây) nên đóng
        # transaction đọc trước, nếu không kết nối đứng "idle in transaction"
        # suốt thời gian chờ (cùng lý do với _process_job()). AN TOÀN vì tới đây
        # transaction của job này mới chỉ có SELECT: tra mã job, tỉnh
        # (get_or_create_province chỉ là bí danh của get_province_id, không
        # INSERT từ 08/2026), cấp bậc và find_company_probe ở trên. Các ghi
        # (get_or_create_company_by_profile...) đều nằm SAU điểm này. Nếu sau
        # này thêm một lệnh GHI trước _resolve_company() thì phải commit nó
        # trước, nếu không rollback ở đây sẽ huỷ mất.
        _release_read_transaction(conn)
        profile = adapter.fetch_company_profile(raw.company_url) or {}

    company_id = db.get_or_create_company_by_profile(
        conn, company_name, province_id, tax_id=profile.get("tax_id", "")
    )

    # LUÔN ghi lại source_profile_url khi có raw.company_url, KỂ
    # CẢ KHI probe_needs_enrichment() ở trên trả False (công ty đã
    # đủ 4 field, không cần fetch_company_profile() lần này) — xem
    # docstring update_company_profile() mục "source_profile_url"
    # để biết lý do: đây là "địa chỉ để backfill sau này", không
    # phải nội dung cần đủ/thiếu, nên tách hẳn khỏi điều kiện
    # if profile (vốn chỉ true khi VỪA enrich xong 4 field kia).
    # Thiếu bước này thì công ty đã "đủ" từ sớm (crawl trước khi
    # có cột này, hoặc crawl trước khi sửa parser Brand Pro) sẽ
    # KHÔNG BAO GIỜ có source_profile_url dù job vẫn đang active
    # -> mất luôn khả năng backfill dù đang có sẵn URL đúng ngay
    # trong tay ở request này.
    if raw.company_url:
        db.update_company_profile(
            conn, company_id, source_profile_url=raw.company_url,
        )

    if profile:
        db.update_company_profile(
            conn, company_id,
            tax_id=profile.get("tax_id", ""),
            website=profile.get("real_website", ""),
            products_services=profile.get("description", ""),
            industry=profile.get("industry", ""),
            company_size=profile.get("company_size", ""),
            address=profile.get("address", ""),
        )

    return company_id


def _release_read_transaction(conn) -> None:
    """Đóng transaction mà một câu SELECT vừa mở, TRƯỚC khi chờ mạng.

    Kết nối chạy autocommit=False nên chỉ một câu SELECT cũng mở transaction và
    giữ nó tới khi commit/rollback. Nếu ngay sau đó pipeline đi fetch trang chi
    tiết (vài giây tới hàng chục giây), kết nối đứng ở trạng thái
    "idle in transaction" suốt thời gian chờ: giữ snapshot, chặn vacuum dọn và
    chiếm 1 slot kết nối vô ích.

    CHỈ gọi khi chắc chắn transaction hiện tại chưa có ghi nào (rollback sẽ huỷ
    cả ghi chưa commit). Ở đầu mỗi job thì đúng như vậy: job trước đã commit
    hoặc rollback xong, chỉ còn câu SELECT tra cứu trùng URL."""
    conn.rollback()


def _make_known_url_checker(conn, on_known_skipped=None):
    """Hàm kiểm tra cho BaseAdapter.set_known_url_checker(): True khi URL
    đã có trong DB VÀ không cần vá nữa (đủ work_type/deadline/
    parsed_content) — dùng cùng tiêu chí db.job_needs_detail_enrichment()
    như _handle_existing_job(), nên job cũ còn thiếu field vẫn được adapter
    yield và vá như trước, không bị bỏ sót.

    Lỗi DB -> rollback rồi trả False (coi như chưa biết, adapter fetch bình
    thường): tối đa tốn thêm 1 request, tốt hơn bỏ nhầm 1 job mới.

    on_known_skipped: gọi mỗi lần trả True. Job bị bỏ qua kiểu này không bao
    giờ tới vòng lặp run_pipeline() nên không kích hoạt heartbeat ở đó; nếu
    cả trang toàn job đã biết (crawl theo max_jobs có thể đi qua rất nhiều
    trang như vậy) thì progress.last_update đứng yên và watchdog sẽ tưởng
    lượt crawl bị treo."""
    def checker(source_url: str) -> bool:
        try:
            probe = db.get_job_probe_by_source_url(conn, source_url)
        except Exception:  # noqa: BLE001
            conn.rollback()
            logger.exception("Tra cứu URL đã biết lỗi (%s), coi như chưa có", source_url)
            return False
        # Checker chạy giữa 2 job (adapter gọi khi quét listing), lúc này không
        # còn ghi dở nào. Adapter sắp đi fetch mạng nên đóng transaction đọc.
        _release_read_transaction(conn)
        known = probe is not None and not db.job_needs_detail_enrichment(probe)
        if known and on_known_skipped is not None:
            on_known_skipped()
        return known

    return checker


def _import_repost(conn, raw, candidate: dict, deadline, raw_jd_content,
                    stats: PipelineStats) -> None:
    """Nhánh "tin đăng lại" (bước 3c): URL mới nhưng trùng company + title + province
    (không xét level, xét cả job đã CLOSED, xem db.find_repost_candidate) với job đã có
    -> KHÔNG insert job mới, ghi source_url mới vào job cũ như nguồn phụ. `candidate` là
    dict db.find_repost_candidate() trả về.

    Trước đây bỏ hẳn mà không ghi gì, nên lượt crawl sau URL này vẫn "chưa từng
    thấy": fetch chi tiết + xử lý công ty rồi lại bỏ, lặp mãi. Có dòng log thì lần sau
    URL đi nhánh "job đã có" và không tốn request nếu job cũ đã đủ field.

    Xử lý theo trạng thái job cũ:
      - OPEN: hạn của job theo listing mới (C4 phần 2/3): db.link_repost_source ghi listing mang hạn của tin
        mới rồi đồng bộ job, nên hạn muộn hơn thì dời hạn job ra sau (RepostLink.deadline_extended, đếm
        repost_deadline_extended); job đã quá hạn được đăng lại sẽ sống lại.
      - CLOSED với closed_reason='expired_auto' (do check_expired_source_jobs tự đóng): MỞ LẠI. Việc đó do
        db.link_repost_source làm (C4 phần 1/3): listing của URL mới sinh ra OPEN kèm hạn mới (luật 1 của
        db.listing_state), job suy ra OPEN từ listing đó (hạn và source_url theo listing, nên
        check_expired_source_jobs không kiểm URL cũ đã chết) và ghi audit REOPEN_JOB. Hạn của tin mới đã qua thì
        listing sinh ra CLOSED nên job giữ CLOSED. Hàm này chỉ đọc kết quả (RepostLink.reopened) để đếm và ghi log.
      - CLOSED với closed_reason khác (staff, unknown, merged; cột job_postings.closed_reason) hoặc
        không mở lại được: giữ nguyên CLOSED, chỉ ghi URL mới làm nguồn phụ.
    Nội dung (parsed_content, work_type) thì KHÔNG vá từ tin đăng lại. raw_jd_content của tin
    đăng lại được giữ lại để còn dữ liệu xem lại các trường hợp gộp nhầm.

    Commit ở cuối để chốt cả phần ghi công ty ở bước trước (nhánh này không
    đi qua commit của bước insert)."""
    duplicate_job_id = candidate["job_id"]
    stats.skipped_duplicate_repost += 1
    link = db.link_repost_source(
        conn, duplicate_job_id,
        source_name=raw.source_name, source_url=raw.source_url,
        raw_jd_content=raw_jd_content, salary_raw_text=raw.salary_text, deadline=deadline,
    )
    action = ""
    if candidate["job_status"] == "CLOSED":
        reason = candidate["closed_reason"]
        if link.reopened:
            stats.repost_reopened += 1
            action = f", MỞ LẠI job đã đóng (hạn {deadline})"
        else:
            stats.repost_kept_closed += 1
            if reason in AUTO_REOPEN_REASONS:
                action = ", giữ nguyên CLOSED (hạn mới đã qua hoặc job đã đổi)"
            else:
                action = f", giữ nguyên CLOSED (closed_reason={reason})"
    elif link.deadline_extended:
        stats.repost_deadline_extended += 1
        action = f", dời deadline sang {deadline}"
    conn.commit()
    logger.info(
        "Tin đăng lại (trùng company/title/province với "
        "job_id=%s), không tạo job mới, đã ghi URL làm nguồn phụ%s: "
        "%s @ %s",
        duplicate_job_id, action, raw.job_title, raw.source_url,
    )


def _jd_looks_truncated(parsed_content) -> bool:
    """True nếu mô tả/yêu cầu kết thúc bằng "..." — dấu hiệu bản JD bị API search
    của VietnamWorks cắt ngắn (đúng tiêu chí scripts/backfill/backfill_vnw_detail.py dùng để tìm
    JD cắt). Xảy ra khi trang chi tiết không giải mã được và adapter phải dùng
    dữ liệu search. Bản như vậy không được đè lên JD đầy đủ đã lưu."""
    if not parsed_content:
        return False
    return any(
        (parsed_content.get(key) or "").rstrip().endswith("...")
        for key in ("job_description", "requirements")
    )


def _find_job_by_job_code(adapter: BaseAdapter, conn, raw, stats: PipelineStats):
    """Tìm job đã lưu CÙNG MÃ JOB với URL mới này, để cập nhật thay vì tạo job trùng.

    Bối cảnh (VietnamWorks): nhà tuyển dụng sửa tiêu đề tin thì URL đổi (phần chữ
    của slug) nhưng mã số cuối URL giữ nguyên. Với pipeline đó là URL chưa từng
    thấy, và vì tiêu đề đã đổi nên bước chống trùng "đăng lại" (company + title +
    level + province) cũng không bắt được -> trước đây luôn tạo job mới, để lại
    job cũ mang tiêu đề cũ.

    Chỉ nguồn nào adapter khai báo job_code_url_regex() mới đi vào đây; nguồn
    khác (TopCV, CareerViet) trả None ngay và không tốn câu SQL nào.

    Quy tắc chọn (đã chốt):
      - chỉ xét job còn OPEN (job đã CLOSED là do người dùng chủ động đóng, tin
        đăng lại thì tạo job mới, giống cách xử lý tin đăng lại);
      - phải có tiêu đề còn gần giống (normalize.titles_similar, ngưỡng 0,5). Nếu
        có job cùng mã nhưng tiêu đề khác hẳn thì nhà tuyển dụng đã đổi sang vị trí
        khác: KHÔNG đụng job cũ, đếm job_code_title_mismatch + log WARNING để xem
        tay, rồi chạy tiếp như thường (tạo job mới, không bao giờ tệ hơn trước);
      - nhiều dòng cùng mã (dữ liệu cũ có sẵn các cặp trùng): chọn dòng giống
        tiêu đề nhất; hoà điểm thì lấy dòng tạo sớm nhất (db trả cũ nhất trước,
        max() giữ phần tử đầu khi hoà).

    Trả (job_id, job_title, job_status, updated_by, created_at) của job khớp, hoặc
    None. Khi trả None thì đóng transaction đọc của câu tra cứu: chưa có ghi nào
    trong job này, mà ngay sau đó pipeline có thể fetch trang công ty (chờ mạng)."""
    # getattr: adapter không kế thừa BaseAdapter (không có hook tuỳ chọn này) vẫn
    # phải chạy được, giống set_known_url_checker ở run_pipeline().
    get_url_regex = getattr(adapter, "job_code_url_regex", None)
    url_regex = get_url_regex(raw.source_url) if get_url_regex is not None else None
    if not url_regex:
        return None
    rows = db.find_jobs_by_source_url_regex(
        conn, source_name=raw.source_name, url_regex=url_regex,
    )
    open_rows = [row for row in rows if row[2] == "OPEN"]
    similar = [row for row in open_rows if normalize.titles_similar(row[1], raw.job_title)]
    if similar:
        return max(similar, key=lambda row: normalize.title_overlap(row[1], raw.job_title))
    if open_rows:
        stats.job_code_title_mismatch += 1
        logger.warning(
            "Cùng mã job nhưng tiêu đề khác hẳn, KHÔNG cập nhật job cũ, tạo job mới "
            "(nhà tuyển dụng có thể đã đổi sang vị trí khác; cần xem tay): "
            "đã lưu %s, trên trang %r | %s",
            [row[1] for row in open_rows], raw.job_title, raw.source_url,
        )
    _release_read_transaction(conn)
    return None


def _update_job_by_job_code(conn, raw, match, *, level_code: str, level_source: str,
                             level_signals: dict, salary, work_type, deadline, parsed_content, raw_jd_content,
                             stats: PipelineStats) -> None:
    """Nhánh "cùng mã job, tiêu đề còn gần giống" (xem _find_job_by_job_code): cập
    nhật job cũ và ghi URL mới làm nguồn phụ, KHÔNG tạo job mới. Commit đúng 1 lần
    ở cuối (quy tắc 1 job = 1 transaction).

    Luôn ghi URL mới vào job_sources_log trước, kể cả khi không sửa gì: nhờ đó lượt
    crawl sau URL này đi nhánh "job đã có" và không bị fetch lại (cùng lý do với
    _import_repost). Nhánh này KHÔNG đổi job_postings.source_url: cột đó là URL mà
    check_expired_source_jobs kiểm tra còn sống hay không (không phải "nguồn gốc bất biến",
    link_repost_source, khi job sống lại, và merge-duplicates vẫn có thể ghi đè); URL mới chỉ vào
    job_sources_log.

    Job đã có người sửa tay (updated_by khác rỗng) thì chỉ dừng ở bước ghi URL.
    Còn lại cập nhật: tiêu đề; level; mô tả/yêu cầu (parsed_content); lương; hình
    thức làm việc. KHÔNG đụng công ty, tỉnh, ngành. Hạn nộp KHÔNG vá trực tiếp: link_repost_source ghi listing
    mới mang hạn của chính nó rồi đồng bộ job, nên hạn job là hạn muộn nhất trong các listing OPEN, kể cả job
    đã có người sửa tay (C4 phần 2/3, bạn chọn 09/10).

    Hai chỗ chủ động làm ít hơn để không làm dữ liệu tệ đi:
      - lương chỉ ghi khi nguồn thật sự có chuỗi lương (chuỗi rỗng nghĩa là ẩn
        lương hoặc không lấy được, không phân biệt được nên giữ lương cũ);
      - JD bị cắt ngắn ("...", xem _jd_looks_truncated) thì không ghi JD và level
        (level suy từ số năm trong JD, JD cắt thì level cũng kém tin cậy); tiêu đề,
        lương, hình thức làm việc, hạn nộp vẫn cập nhật vì lấy từ danh sách tìm kiếm.

    update_job_from_recrawl chỉ ghi khi job còn OPEN và chưa ai sửa tay, nên nếu
    trong lúc xử lý có người vừa sửa/đóng job thì không ghi đè, và tính vào
    linked_by_job_code_only."""
    job_id, old_title, _status, updated_by, _created_at = match
    db.link_repost_source(
        conn, job_id,
        source_name=raw.source_name, source_url=raw.source_url,
        raw_jd_content=raw_jd_content, salary_raw_text=raw.salary_text, deadline=deadline,
    )
    updated = False
    if not updated_by:
        content_reliable = not _jd_looks_truncated(parsed_content)
        if not content_reliable:
            logger.warning(
                "JD lấy được bị cắt ngắn, giữ nguyên JD và level cũ của job %s: %s",
                job_id, raw.source_url,
            )
        recrawl_level_id = db.get_level_id(conn, level_code) if content_reliable else None
        updated = db.update_job_from_recrawl(
            conn, job_id,
            job_title=raw.job_title,
            level_id=recrawl_level_id,
            level_source=level_source if recrawl_level_id is not None else None,
            level_rule_version=(
                normalize.LEVEL_RULE_VERSION if recrawl_level_id is not None else None
            ),
            level_signals=level_signals if recrawl_level_id is not None else None,
            work_type=work_type,
            parsed_content=parsed_content if content_reliable else None,
            salary=asdict(salary) if (raw.salary_text or "").strip() else None,
        )
    conn.commit()
    if updated:
        stats.updated_by_job_code += 1
        logger.info(
            "Cùng mã job với job_id=%s, đã cập nhật tiêu đề/nội dung và ghi URL làm "
            "nguồn phụ, không tạo job mới: %r -> %r @ %s",
            job_id, old_title, raw.job_title, raw.source_url,
        )
    else:
        stats.linked_by_job_code_only += 1
        logger.info(
            "Cùng mã job với job_id=%s nhưng không sửa nội dung (đã có người sửa tay "
            "hoặc vừa bị sửa/đóng), chỉ ghi URL làm nguồn phụ: %r -> %r @ %s",
            job_id, old_title, raw.job_title, raw.source_url,
        )


def _insert_new_job(conn, raw, *, company_id, level_id, province_id, work_type, salary,
                     deadline, parsed_content, raw_jd_content, level_code: str,
                     level_source: str, level_signals: dict, company_name: str,
                     stats: PipelineStats) -> None:
    """Bước 4: insert job mới (content_hash tự tính bởi trigger Postgres) rồi
    commit. Tách ra từ _process_jobs() (đợt B2), KHÔNG đổi hành vi."""
    db.insert_job(
        conn,
        company_id=company_id,
        job_title=raw.job_title,
        matching_industry=raw.matching_industry,
        level_id=level_id,
        province_id=province_id,
        work_type=work_type,
        currency=salary.currency,
        salary_min=salary.salary_min,
        salary_max=salary.salary_max,
        salary_type=salary.salary_type,
        salary_period=salary.salary_period,
        source_url=raw.source_url,
        source_name=raw.source_name,
        salary_raw_text=raw.salary_text,
        deadline=deadline,
        parsed_content=parsed_content,
        raw_jd_content=raw_jd_content,
        detail_fetched=True,
        level_source=level_source if level_id is not None else None,
        level_rule_version=normalize.LEVEL_RULE_VERSION if level_id is not None else None,
        level_signals=level_signals if level_id is not None else None,
    )
    conn.commit()
    stats.inserted += 1
    logger.info("Đã lưu: [%s] %s @ %s", level_code, raw.job_title, company_name)


# --------------------------------------------------------------------------
# Chống trùng cho job MỚI: danh sách resolver theo thứ tự (B1, kế hoạch backend)
# --------------------------------------------------------------------------
# Trước B1 hai bước chống trùng (mã job, tin đăng lại) viết cứng trong _import_new_job().
# Giờ mỗi bước là một resolver có tên, adapter khai báo nó dùng những bước nào và theo thứ
# tự nào (BaseAdapter.dedup_resolvers()). Thêm một kiểu chống trùng mới = thêm một resolver
# vào DEDUP_RESOLVERS và ghi tên nó ở adapter cần, không sửa _import_new_job().
#
# Hai giai đoạn (stage) vì có resolver cần tỉnh/cấp bậc/công ty đã tra hoặc tạo, có resolver
# thì không được tạo chúng (nhánh khớp mã job không được sinh tỉnh/công ty thừa):
#   STAGE_BEFORE_COMPANY  chạy ngay sau khi fetch chi tiết, trước tỉnh/công ty.
#   STAGE_AFTER_COMPANY   chạy sau khi đã có province_id, level_id, company_id.
# Trong cùng giai đoạn chạy theo thứ tự adapter khai báo; giai đoạn BEFORE luôn trước AFTER
# dù adapter ghi tên theo thứ tự nào. Resolver khớp thì tự xử lý xong job (kể cả commit)
# và trả True, các resolver sau không chạy. Không khớp trả False; transaction vẫn mở và
# KHÔNG được rollback ở đây vì khoá advisory của "repost" phải giữ tới commit của bước insert.

STAGE_BEFORE_COMPANY = "before_company"
STAGE_AFTER_COMPANY = "after_company"


@dataclass
class _DedupContext:
    """Mọi thứ một resolver có thể cần cho job đang xử lý. Các trường company_id,
    province_id, level_id là None ở giai đoạn BEFORE_COMPANY, có giá trị ở AFTER_COMPANY."""
    adapter: BaseAdapter
    conn: object
    raw: object
    stats: PipelineStats
    level_code: str
    level_source: str
    level_signals: dict
    salary: object
    work_type: object
    deadline: object
    parsed_content: object
    raw_jd_content: str
    company_id: Optional[str] = None
    province_id: Optional[int] = None
    level_id: Optional[int] = None


@dataclass(frozen=True)
class _DedupResolver:
    name: str
    stage: str
    resolve: Callable[[_DedupContext], bool]


def _resolve_by_job_code(ctx: _DedupContext) -> bool:
    """Cùng mã job với một job đã lưu -> cập nhật job đó. Xem _find_job_by_job_code."""
    existing_job = _find_job_by_job_code(ctx.adapter, ctx.conn, ctx.raw, ctx.stats)
    if existing_job is None:
        return False
    _update_job_by_job_code(
        ctx.conn, ctx.raw, existing_job, level_code=ctx.level_code,
        level_source=ctx.level_source, level_signals=ctx.level_signals, salary=ctx.salary,
        work_type=ctx.work_type, deadline=ctx.deadline, parsed_content=ctx.parsed_content,
        raw_jd_content=ctx.raw_jd_content, stats=ctx.stats,
    )
    return True


def _resolve_by_repost(ctx: _DedupContext) -> bool:
    """Tin đăng lại dưới URL khác (cùng công ty + tiêu đề + tỉnh) -> ghi URL làm nguồn phụ.

    A4 (10/2026): giành khoá advisory theo khoá chống trùng TRƯỚC câu tra. Các nguồn (TopCV,
    VietnamWorks, CareerViet) và nhập tay có thể chạy song song, hai bên cùng tra thấy "chưa có"
    rồi cùng insert sẽ ra hai job trùng. Khoá cấp transaction giữ tới commit/rollback của nhánh
    này (nhánh đăng lại và nhánh insert đều commit ở cuối), bên đến sau chờ rồi tra lại và thấy
    job vừa tạo. Hết thời gian chờ thì raise JobDedupLockTimeout, vòng lặp job rollback và đếm lỗi.
    Vì vậy khi KHÔNG khớp, hàm này để nguyên khoá cho bước insert phía sau.

    Dùng db.find_repost_candidate() (không phải find_manual_job_duplicate() của POST /jobs):
    khoá cũ hụt ở hai chỗ khi đo trên 230 job trùng thật: bỏ qua job đã CLOSED (~88%) và đòi
    cùng level (~34%, level suy từ số năm kinh nghiệm nên hai lần đăng hay ra level khác nhau).
    Cái giá: hai vị trí cùng tên, công ty, tỉnh nhưng khác cấp bị coi là một (dữ liệu gốc của tin
    bị gộp vẫn nằm trong job_sources_log). Xem _import_repost cho cách xử lý theo trạng thái."""
    db.lock_job_dedup_key(
        ctx.conn, company_id=ctx.company_id, job_title=ctx.raw.job_title,
        province_id=ctx.province_id,
    )
    candidate = db.find_repost_candidate(
        ctx.conn, company_id=ctx.company_id, job_title=ctx.raw.job_title,
        province_id=ctx.province_id, level_id=ctx.level_id,
    )
    if candidate is None:
        return False
    _import_repost(ctx.conn, ctx.raw, candidate, ctx.deadline, ctx.raw_jd_content, ctx.stats)
    return True


DEDUP_RESOLVERS = {
    resolver.name: resolver for resolver in (
        _DedupResolver("job_code", STAGE_BEFORE_COMPANY, _resolve_by_job_code),
        _DedupResolver("repost", STAGE_AFTER_COMPANY, _resolve_by_repost),
    )
}


def _resolvers_for(adapter) -> "list[_DedupResolver]":
    """Danh sách resolver của adapter, theo thứ tự adapter khai báo. Adapter không có hook
    dedup_resolvers() (không kế thừa BaseAdapter) hoặc trả giá trị không phải tuple/list thì
    dùng DEFAULT_DEDUP_RESOLVERS, giống set_known_url_checker ở run_pipeline(). Tên không có
    trong DEDUP_RESOLVERS hoặc khai báo lặp thì raise ValueError (lỗi cấu hình, run_pipeline()
    gọi hàm này đầu lượt để lỗi hiện ngay, không bị nuốt thành lỗi từng job)."""
    declare = getattr(adapter, "dedup_resolvers", None)
    names = declare() if callable(declare) else None
    if not isinstance(names, (tuple, list)):
        names = DEFAULT_DEDUP_RESOLVERS
    unknown = [name for name in names if name not in DEDUP_RESOLVERS]
    if unknown:
        raise ValueError(
            f"Adapter {type(adapter).__name__} khai báo resolver chống trùng không tồn tại: "
            f"{unknown} (có: {sorted(DEDUP_RESOLVERS)})"
        )
    if len(set(names)) != len(names):
        raise ValueError(
            f"Adapter {type(adapter).__name__} khai báo resolver chống trùng bị lặp: {list(names)}"
        )
    return [DEDUP_RESOLVERS[name] for name in names]


def _run_dedup_stage(resolvers, stage: str, ctx: _DedupContext) -> bool:
    """Chạy các resolver thuộc `stage` theo thứ tự; True nếu có resolver đã xử lý xong job."""
    return any(r.resolve(ctx) for r in resolvers if r.stage == stage)


def _import_new_job(adapter: BaseAdapter, conn, raw, stats: PipelineStats,
                     field_counter: EmptyFieldCounter) -> None:
    """Xử lý job CHƯA từng crawl (source_url chưa có trong DB): chuẩn hoá, lọc
    nhà tuyển dụng ẩn danh, fetch chi tiết, tìm/tạo company, rồi hoặc ghi
    nhận tin đăng lại hoặc insert job mới. Tách ra từ _process_jobs() (đợt B2,
    10/2026), KHÔNG đổi hành vi — `return` ở đây thay cho `continue` của vòng
    lặp cũ. Ngoại lệ KHÔNG bắt ở đây: _process_jobs() rollback + đếm lỗi."""
    # 2) Chuẩn hóa (phần DÙNG CHUNG, không quan tâm nguồn)
    salary = normalize.normalize_salary(raw.salary_text)
    # derive_level trả level KÈM căn cứ; căn cứ + LEVEL_RULE_VERSION được lưu cùng
    # level để lệnh tính lại sau này biết job nào tính theo quy tắc nào.
    level_decision = normalize.derive_level(raw.experience_text, raw.job_title, raw.level_hint)
    level_code = level_decision.level
    # Lưu luôn tín hiệu thô derive_level vừa đọc (cột level_signals) để tính lại level
    # sau này (khi quy tắc đổi) không phải tải lại trang.
    level_signals = normalize.build_level_signals(raw.experience_text, raw.level_hint)
    company_name = normalize.clean_company_name(raw.company_name)

    # 2a) Nhà tuyển dụng ẨN DANH (site tự điền placeholder thay tên công ty
    # thật, vd "Vietnamworks' Client") — bỏ hẳn job này TRƯỚC khi
    # fetch_job_full_detail() (đỡ tốn 1 request thật ra ngoài cho job chắc
    # chắn sẽ bị vứt), không tạo company/job rác. Xem
    # normalize.is_anonymous_employer_name().
    if normalize.is_anonymous_employer_name(company_name):
        stats.skipped_anonymous_employer += 1
        logger.info(
            "Bỏ qua job (nhà tuyển dụng ẩn danh, company_name='%s'): %s @ %s",
            company_name, raw.job_title, raw.source_url,
        )
        return

    # 2b) Crawl sâu vào trang chi tiết JD để lấy work_type/deadline + nội dung
    # mô tả đầy đủ (job_description/requirements/perks/required_skills) — các
    # field này KHÔNG có trên trang listing, chỉ hiển thị ở trang chi tiết job
    # (giống cách fetch_company_profile() crawl sâu vào trang công ty).
    #
    # QUYẾT ĐỊNH: nếu fetch_job_full_detail() THẤT BẠI THẬT SỰ (trả None —
    # network error/bị chặn, KHÁC với dict rỗng khi trang fetch OK nhưng thiếu
    # field), BỎ HẲN job này — không insert. Lý do: thà thiếu 1 job (sẽ được
    # nhặt lại ở lần crawl sau, vì job chưa từng insert nên vẫn được coi là
    # "job mới") còn hơn insert job với work_type/deadline/parsed_content =
    # NULL một cách âm thầm, dễ nhầm tưởng "trang JD thật sự không có dữ liệu
    # này" trong khi thực ra là bị chặn lúc crawl.
    job_detail = adapter.fetch_job_full_detail(raw.source_url)
    field_counter.record_detail(job_detail)
    if job_detail is None:
        stats.skipped_fetch_failed += 1
        logger.warning(
            "Bỏ qua job (fetch chi tiết thất bại): %s @ %s",
            raw.job_title, raw.source_url,
        )
        return

    work_type = normalize.normalize_work_type(
        job_detail.get("work_type") or raw.work_type_text
    )
    deadline = normalize.normalize_deadline(job_detail.get("deadline_text", ""))
    parsed_content, raw_jd_content = _build_parsed_content_and_raw(job_detail)

    # 2c) Chống trùng GIAI ĐOẠN TRƯỚC công ty (resolver "job_code": cùng MÃ JOB với một job
    # đã lưu, nhà tuyển dụng sửa tiêu đề nên URL đổi -> cập nhật job đó thay vì tạo job
    # trùng). Đặt TRƯỚC bước tỉnh/công ty vì nhánh này không cần tới chúng và không được tạo
    # công ty/tỉnh thừa. Resolver nào chạy, theo thứ tự nào: adapter.dedup_resolvers().
    resolvers = _resolvers_for(adapter)
    ctx = _DedupContext(
        adapter=adapter, conn=conn, raw=raw, stats=stats, level_code=level_code,
        level_source=level_decision.source, level_signals=level_signals, salary=salary,
        work_type=work_type, deadline=deadline, parsed_content=parsed_content,
        raw_jd_content=raw_jd_content,
    )
    if _run_dedup_stage(resolvers, STAGE_BEFORE_COMPANY, ctx):
        return

    # 3) Map sang khóa ngoại thật trong DB
    province_id = db.get_or_create_province(conn, raw.province_text)
    level_id = db.get_level_id(conn, level_code)

    # 3b) Quyết định có cần crawl sâu vào trang công ty không + tìm/tạo
    # company tương ứng — xem docstring _resolve_company().
    company_id = _resolve_company(adapter, conn, raw, company_name, province_id)

    # 3c) Chống trùng GIAI ĐOẠN SAU công ty (resolver "repost": "đăng lại dưới URL khác").
    # job_probe ở bước (1) (_process_job) chỉ bắt được trùng THEO source_url, không bắt được
    # trường hợp TopCV/VietnamWorks cấp source_url MỚI cho job đã đăng trước đó (cùng
    # company + title + province, thường do nhà tuyển dụng "làm mới" tin để đẩy lên top tìm
    # kiếm, đã xác nhận thực tế 08/2026: 2 tin "Fullstack Developer" cùng công ty, cùng nội
    # dung, khác job_id/URL, đăng cách nhau ~1 phút). Chi tiết khoá và cách xử lý: xem
    # _resolve_by_repost. Không resolver nào khớp thì khoá advisory vẫn được giữ cho bước insert.
    ctx.province_id, ctx.level_id, ctx.company_id = province_id, level_id, company_id
    if _run_dedup_stage(resolvers, STAGE_AFTER_COMPANY, ctx):
        return

    # 4) Insert job mới
    _insert_new_job(
        conn, raw, company_id=company_id, level_id=level_id, province_id=province_id,
        work_type=work_type, salary=salary, deadline=deadline,
        parsed_content=parsed_content, raw_jd_content=raw_jd_content,
        level_code=level_code, level_source=level_decision.source,
        level_signals=level_signals, company_name=company_name, stats=stats,
    )


def _process_job(adapter: BaseAdapter, conn, raw, stats: PipelineStats,
                  field_counter: EmptyFieldCounter) -> None:
    """Xử lý 1 job adapter trả về. Tách ra từ _process_jobs() (đợt B2,
    10/2026), KHÔNG đổi hành vi.

    1) Chống trùng theo link JD gốc. Job đã crawl trước đó thì KHÔNG insert
    lại, nhưng job cũ có thể còn thiếu work_type/deadline/parsed_content (nếu
    được crawl từ trước khi các field này tồn tại) -> vá thêm rồi bỏ qua phần
    insert, không dừng cả job này như 1 lỗi. Chưa có thì đi tiếp
    _import_new_job()."""
    job_probe = db.get_job_probe_by_source_url(conn, raw.source_url)
    # Cả 2 nhánh bên dưới đều fetch trang chi tiết ngay (chờ mạng) nên đóng
    # transaction đọc của câu probe trước. Các ghi của job bắt đầu sau điểm này.
    _release_read_transaction(conn)
    if job_probe is not None:
        _handle_existing_job(adapter, conn, raw, job_probe, stats, field_counter)
        return
    _import_new_job(adapter, conn, raw, stats, field_counter)


def _process_jobs(adapter: BaseAdapter, conn, category_key: str, max_pages: int,
                   max_jobs: "int | None", stats: PipelineStats,
                   field_counter: EmptyFieldCounter, _emit_progress) -> None:
    """Vòng lặp xử lý từng job adapter trả về — tách ra từ run_pipeline() (đợt
    3, 10/2026) để run_pipeline() bọc được 1 try/except CrawlBlockedError quanh
    TOÀN BỘ vòng lặp. Đợt B2 (10/2026): phần xử lý từng job đã chuyển xuống
    _process_job() và các hàm nó gọi; ở đây chỉ còn vòng lặp, giới hạn
    max_jobs, xử lý lỗi từng job và heartbeat.
    Cập nhật trực tiếp `stats`/`field_counter` do run_pipeline() truyền vào.

    QUY TẮC TRANSACTION (đợt B3, 10/2026) — 1 job = 1 transaction:
      - Các hàm db.* KHÔNG tự commit/rollback, người gọi (pipeline) quyết định.
      - Mỗi nhánh CÓ ghi DB tự commit đúng 1 lần ở CUỐI nhánh thành công:
        _handle_existing_job (sau khi vá job cũ), _import_repost,
        _update_job_by_job_code, _insert_new_job. Commit chốt luôn các ghi phụ
        trước đó của cùng job (tỉnh, công ty).
      - Nhánh KHÔNG ghi gì (job trùng đã đủ field, ẩn danh, fetch chi tiết
        thất bại) không commit.
      - Mọi lỗi (kể cả CrawlBlockedError) -> rollback ở vòng lặp dưới, huỷ
        toàn bộ phần CHƯA commit của job đó. Job đã commit không bị ảnh hưởng.
      - Rollback "đóng transaction đọc" (_release_read_transaction): ngay sau
        câu probe ở đầu job, sau câu probe của _make_known_url_checker() và
        sau câu tra cứu mã job khi không có job nào khớp
        (_find_job_by_job_code), để kết nối không đứng "idle in transaction"
        lúc chờ fetch mạng. Chỉ làm lúc chưa có ghi nào.
      - Chỗ rollback khác: _make_known_url_checker() khi tra cứu URL lỗi.
    Thêm nhánh mới có ghi DB thì phải tự commit ở cuối nhánh đó. Quên thì
    tests/test_pipeline_transactions.py báo đỏ (và buộc phân loại mọi hàm db.*
    mới pipeline gọi là đọc hay ghi)."""
    for raw in adapter.fetch_jobs(category_key, max_pages):
        if max_jobs is not None and stats.fetched >= max_jobs:
            logger.info("Đã đạt giới hạn --max-jobs=%d, dừng crawl.", max_jobs)
            break
        stats.fetched += 1
        field_counter.record_listing(raw)
        try:
            _process_job(adapter, conn, raw, stats, field_counter)

        except CrawlBlockedError:
            # Ngắt mạch (adapters/base.py::_note_fetch_failure) raise ngay
            # TRONG fetch_job_full_detail()/fetch_company_profile() ở giữa 1
            # job — nằm trong try của từng job nên except Exception bên dưới
            # sẽ NUỐT mất, và vòng lặp lại tiếp tục đập vào site đang chặn.
            # Rollback phần dở dang của job này (execute() sẽ commit khi ghi
            # trạng thái lỗi, không để lọt company/province mồ côi) rồi cho
            # lan lên run_pipeline().
            conn.rollback()
            raise
        except Exception as exc:  # noqa: BLE001 - log rồi tiếp tục, không dừng cả pipeline
            conn.rollback()
            stats.errors += 1
            logger.error("Lỗi xử lý job '%s': %s", raw.job_title, exc)
        finally:
            # Đặt trong `finally` (10/2026) để heartbeat chạy cho MỌI job xử
            # lý xong, kể cả các nhánh return sớm (trùng URL, ẩn danh, đăng
            # lại, fetch chi tiết thất bại). Trước đây đoạn này nằm sau khối
            # try nên mỗi `continue` bỏ qua heartbeat: 1 chuỗi dài job bị bỏ
            # qua (mỗi job vẫn có thể tốn 1 request fetch) khiến
            # progress.last_update đứng yên, và watchdog tính theo tiến độ
            # (db.reconcile_stale_runs) sẽ tưởng lượt crawl đang treo.
            _emit_progress()


def run_pipeline(adapter: BaseAdapter, conn, category_key: str, max_pages: int,
                  max_jobs: "int | None" = None, on_progress=None) -> dict:
    """
    max_jobs: giới hạn TỔNG SỐ JD sẽ crawl (đếm theo raw record nhận được
    từ adapter, không phân biệt sau đó có insert được hay không) — dùng
    khi muốn crawl 1 lượng nhỏ để test/lấy mẫu mà không cần quan tâm mỗi
    trang có bao nhiêu job. None (mặc định) -> không giới hạn, crawl hết
    max_pages như cũ.

    on_progress: callback tùy chọn, kiểu fn(dict) -> None, gọi lại SAU
    MỖI JOB xử lý xong (không phải sau mỗi trang — adapter.fetch_jobs()
    không lộ ranh giới trang ra ngoài dạng dễ bắt, còn "sau mỗi job" đã
    đủ mịn cho mục đích hiển thị tiến độ real-time, xem lịch sử trao đổi
    "phương án Heartbeat"). Callback nhận dict {"fetched", "inserted"} —
    y hệt 2 khóa tương ứng trong `stats` tại THỜI ĐIỂM gọi (không phải
    bản sao, không được sửa dict này). Lỗi bên trong callback (vd DB
    tạm mất kết nối lúc ghi progress) KHÔNG được để làm hỏng cả lượt
    crawl đang chạy tốt — bọc try/except, chỉ log lại. None (mặc định)
    -> không heartbeat gì cả, giữ hành vi cũ nguyên vẹn (vd khi gọi từ
    main.py CLI, không cần heartbeat).

    Cách dừng: adapter.fetch_jobs() là generator sinh job THEO TỪNG TRANG
    (xem adapters/topcv.py, adapters/vietnamworks.py) — dừng vòng lặp
    for ở đây (break) trước khi gọi next() lần nữa sẽ tự động khiến
    adapter KHÔNG fetch thêm trang mới nữa, không tốn request thừa ra
    ngoài internet. Không cần sửa gì trong adapter.

    Đợt 2 (10/2026):
    - Đặt hook adapter.set_known_url_checker() để adapter fetch chi tiết
      sớm (CareerViet) bỏ qua job đã có đủ field, không fetch lại. Job bị
      bỏ qua kiểu này KHÔNG tới được vòng lặp dưới nên không tính vào
      stats.fetched/max_jobs; số lượng nằm ở stats.skipped_known_url.
    - stats.field_empty: tỷ lệ trường rỗng của dữ liệu adapter trả về,
      xem field_stats.py. Chỉ có khi đã ghi nhận ít nhất 1 record.

    Đợt 3 (10/2026):
    - Bị chặn (CrawlBlockedError: trang đầu thất bại, hoặc ngắt mạch giữa
      chừng khi N lần fetch liên tiếp thất bại) KHÔNG bị nuốt vào except
      từng job: rollback job dở dang, gắn stats tạm (kèm "blocked": True) vào
      exc.stats rồi raise tiếp để execute() ghi status='error'.
    - stats.degraded = {"reasons": [...]}: lượt vẫn 'done' nhưng field bắt
      buộc rỗng gần hết, hoặc trang listing đầu parse ra 0 job. Chỉ có khi có
      lý do; xem _finalize_stats().

    Đợt B1 (10/2026): bên trong pipeline thống kê nằm trong PipelineStats
    (pipeline_stats.py); run_pipeline() vẫn TRẢ VỀ dict (to_dict()), cùng tập
    khoá như trước — các chú thích stats.xxx ở trên là tên khoá của dict đó."""
    _resolvers_for(adapter)  # cấu hình chống trùng sai thì báo ngay, trước khi crawl
    stats = PipelineStats()
    field_counter = EmptyFieldCounter()

    def _emit_progress() -> None:
        if on_progress is None:
            return
        try:
            on_progress(stats.progress())
        except Exception:  # noqa: BLE001 - heartbeat lỗi không được làm hỏng crawl
            logger.exception("on_progress callback lỗi, bỏ qua và crawl tiếp tục")

    set_checker = getattr(adapter, "set_known_url_checker", None)
    if callable(set_checker):
        set_checker(_make_known_url_checker(conn, on_known_skipped=_emit_progress))

    try:
        _process_jobs(adapter, conn, category_key, max_pages, max_jobs,
                      stats, field_counter, _emit_progress)
    except CrawlBlockedError as exc:
        # Bị chặn (trang đầu thất bại, hoặc ngắt mạch giữa chừng). Giữ lại
        # số liệu ĐÃ CÓ (vd đã lưu bao nhiêu job trước khi bị chặn) thay vì
        # mất hết khi execute() ghi status='error'.
        stats.blocked = True
        _finalize_stats(adapter, stats, field_counter)
        exc.stats = stats.to_dict()
        raise
    except Exception as exc:
        # Lỗi bất ngờ phát ra từ chính adapter.fetch_jobs() (generator, nằm NGOÀI
        # try của từng job): lỗi từng job đã được bắt ở _process_jobs() rồi, đây
        # là lỗi làm chết cả lượt (parser nổ, bug adapter...). Giữ số liệu đã có
        # (đã lưu bao nhiêu job) thay vì ghi status='error' với stats trống. Lỗi
        # khi dựng stats không được che mất lỗi gốc.
        try:
            conn.rollback()
            _finalize_stats(adapter, stats, field_counter)
            exc.stats = stats.to_dict()
        except Exception:  # noqa: BLE001
            logger.exception("Không dựng được stats tạm khi lượt crawl lỗi, bỏ qua")
        raise

    _finalize_stats(adapter, stats, field_counter)
    return stats.to_dict()


def _finalize_stats(adapter: BaseAdapter, stats: PipelineStats, field_counter: EmptyFieldCounter) -> None:
    """Điền các chỉ số tổng hợp cuối lượt: skipped_known_url, field_empty và
    cờ "degraded" (đợt 3). Dùng chung cho nhánh chạy xong và nhánh bị chặn."""
    skipped_known = getattr(adapter, "skipped_known_count", 0)
    stats.skipped_known_url = skipped_known if isinstance(skipped_known, int) else 0
    dropped = getattr(adapter, "skipped_detail_unavailable_count", 0)
    stats.skipped_detail_unavailable = dropped if isinstance(dropped, int) else 0

    field_empty = field_counter.summary()
    if field_empty:
        stats.field_empty = field_empty
        field_counter.log_summary()

    # "degraded": lượt vẫn status='done' nhưng dữ liệu nhiều khả năng sai vì
    # selector hỏng. KHÔNG đổi status (cột enum, frontend đang phụ thuộc 4 giá
    # trị queued/running/done/error) — cờ nằm trong stats để UI/người xem log
    # nhận ra mà không phá hợp đồng API. Chỉ phản ánh lý do, không chặn insert.
    reasons = degraded_reasons(field_empty, rate_threshold=DEGRADED_EMPTY_RATE)
    anomalies = getattr(adapter, "listing_anomalies", None)
    if isinstance(anomalies, list):
        reasons.extend({"type": code} for code in anomalies)
    # Adapter bỏ quá nhiều job vì không lấy được trang chi tiết: 1-2 job lẻ là
    # bình thường (tin đã gỡ...), nhưng gần hết thì gần như chắc chắn trang
    # chi tiết đã đổi cấu trúc, và circuit breaker KHÔNG bắt được trường hợp
    # này (tải được trang, chỉ là parse ra không có gì).
    attempted = stats.fetched + stats.skipped_detail_unavailable
    if (stats.skipped_detail_unavailable >= WARN_MIN_SAMPLES
            and stats.skipped_detail_unavailable / attempted >= DEGRADED_EMPTY_RATE):
        reasons.append({
            "type": "jobs_dropped", "dropped": stats.skipped_detail_unavailable,
            "total": attempted,
            "rate": round(stats.skipped_detail_unavailable / attempted, 3),
        })
    if reasons:
        stats.degraded = {"reasons": reasons}
        logger.warning(
            "Lượt crawl bị đánh dấu DEGRADED (dữ liệu có thể sai do selector/"
            "cấu trúc trang đổi): %s", reasons,
        )
