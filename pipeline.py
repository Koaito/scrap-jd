"""
Pipeline LÕI — phần dùng chung 100% cho mọi nguồn crawl.
Chỉ nói chuyện với BaseAdapter / RawJobRecord, không biết TopCV hay
ITviec là gì (đúng kiến trúc "1 khung chung + N adapter riêng" đã bàn).
"""

import logging

from adapters.base import BaseAdapter
import db
import normalize

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

    parsed_content = {
        "job_description": job_detail.get("job_description", ""),
        "requirements": job_detail.get("requirements", ""),
        "perks": job_detail.get("perks", ""),
        "required_skills": job_detail.get("required_skills", []),
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


def run_pipeline(adapter: BaseAdapter, conn, category_key: str, max_pages: int,
                  max_jobs: "int | None" = None) -> dict:
    """
    max_jobs: giới hạn TỔNG SỐ JD sẽ crawl (đếm theo raw record nhận được
    từ adapter, không phân biệt sau đó có insert được hay không) — dùng
    khi muốn crawl 1 lượng nhỏ để test/lấy mẫu mà không cần quan tâm mỗi
    trang có bao nhiêu job. None (mặc định) -> không giới hạn, crawl hết
    max_pages như cũ.

    Cách dừng: adapter.fetch_jobs() là generator sinh job THEO TỪNG TRANG
    (xem adapters/topcv.py, adapters/vietnamworks.py) — dừng vòng lặp
    for ở đây (break) trước khi gọi next() lần nữa sẽ tự động khiến
    adapter KHÔNG fetch thêm trang mới nữa, không tốn request thừa ra
    ngoài internet. Không cần sửa gì trong adapter."""
    stats = {
        "fetched": 0, "inserted": 0, "skipped_duplicate": 0,
        "updated_existing": 0, "skipped_fetch_failed": 0, "errors": 0,
    }

    for raw in adapter.fetch_jobs(category_key, max_pages):
        if max_jobs is not None and stats["fetched"] >= max_jobs:
            logger.info("Đã đạt giới hạn --max-jobs=%d, dừng crawl.", max_jobs)
            break
        stats["fetched"] += 1
        try:
            # 1) Chống trùng theo link JD gốc. Job đã crawl trước đó thì KHÔNG
            # insert lại, nhưng job cũ có thể còn thiếu work_type/deadline/
            # parsed_content (nếu được crawl từ trước khi các field này tồn
            # tại) -> vá thêm rồi bỏ qua phần insert, không dừng cả job này
            # như 1 lỗi.
            job_probe = db.get_job_probe_by_source_url(conn, raw.source_url)
            if job_probe is not None:
                existing_job_id = job_probe[0]
                if db.job_needs_detail_enrichment(job_probe):
                    detail = adapter.fetch_job_full_detail(raw.source_url)
                    if detail is None:
                        # Fetch thất bại thật sự — KHÔNG update gì cả (khác
                        # với "update bằng rỗng"), để job này vẫn được
                        # job_needs_detail_enrichment() nhận diện là còn
                        # thiếu và tự thử lại ở lần crawl kế tiếp.
                        stats["skipped_fetch_failed"] += 1
                        logger.warning(
                            "Bỏ qua vá job cũ (fetch chi tiết thất bại): %s @ %s",
                            raw.job_title, raw.source_url,
                        )
                    else:
                        new_work_type = normalize.normalize_work_type(detail.get("work_type", ""))
                        new_deadline = normalize.normalize_deadline(detail.get("deadline_text", ""))
                        new_parsed_content, _ = _build_parsed_content_and_raw(detail)

                        db.update_job_fields(
                            conn, existing_job_id,
                            work_type=new_work_type,
                            deadline=new_deadline,
                            parsed_content=new_parsed_content,
                        )
                        conn.commit()
                        if new_work_type or new_deadline or new_parsed_content:
                            stats["updated_existing"] += 1
                            logger.info(
                                "Đã vá work_type/deadline/parsed_content cho job cũ: %s",
                                raw.job_title,
                            )
                stats["skipped_duplicate"] += 1
                continue

            # 2) Chuẩn hóa (phần DÙNG CHUNG, không quan tâm nguồn)
            salary = normalize.normalize_salary(raw.salary_text)
            level_code = normalize.infer_level(raw.experience_text, raw.job_title)
            company_name = normalize.clean_company_name(raw.company_name)

            # 2b) Crawl sâu vào trang chi tiết JD để lấy work_type/deadline
            # + nội dung mô tả đầy đủ (job_description/requirements/perks/
            # required_skills) — các field này KHÔNG có trên trang listing,
            # chỉ hiển thị ở trang chi tiết job (giống cách
            # fetch_company_profile() crawl sâu vào trang công ty).
            #
            # QUYẾT ĐỊNH: nếu fetch_job_full_detail() THẤT BẠI THẬT SỰ (trả
            # None — network error/bị chặn, KHÁC với dict rỗng khi trang
            # fetch OK nhưng thiếu field), BỎ HẲN job này — không insert.
            # Lý do: thà thiếu 1 job (sẽ được nhặt lại ở lần crawl sau, vì
            # job chưa từng insert nên vẫn được coi là "job mới") còn hơn
            # insert job với work_type/deadline/parsed_content = NULL một
            # cách âm thầm, dễ nhầm tưởng "trang JD thật sự không có dữ
            # liệu này" trong khi thực ra là bị chặn lúc crawl.
            job_detail = adapter.fetch_job_full_detail(raw.source_url)
            if job_detail is None:
                stats["skipped_fetch_failed"] += 1
                logger.warning(
                    "Bỏ qua job (fetch chi tiết thất bại): %s @ %s",
                    raw.job_title, raw.source_url,
                )
                continue

            work_type = normalize.normalize_work_type(
                job_detail.get("work_type") or raw.work_type_text
            )
            deadline = normalize.normalize_deadline(job_detail.get("deadline_text", ""))
            parsed_content, raw_jd_content = _build_parsed_content_and_raw(job_detail)

            # 3) Map sang khóa ngoại thật trong DB
            province_id = db.get_or_create_province(conn, raw.province_text)
            level_id = db.get_level_id(conn, level_code)

            # 3b) Quyết định có cần crawl sâu vào trang công ty không.
            # "probe" chỉ tra cứu THEO TÊN để BIẾT trước công ty này đã đủ
            # thông tin (tax_id + website) chưa — không dùng để match chính
            # thức, vì tên có thể trùng/lệch giữa các lần đăng tin. Việc
            # match chính thức nằm ở get_or_create_company_by_profile()
            # bên dưới, ưu tiên theo tax_id.
            probe = db.find_company_probe(conn, company_name)
            profile = {}
            if raw.company_url and db.probe_needs_enrichment(probe):
                profile = adapter.fetch_company_profile(raw.company_url) or {}

            company_id = db.get_or_create_company_by_profile(
                conn, company_name, province_id, tax_id=profile.get("tax_id", "")
            )

            if profile:
                db.update_company_profile(
                    conn, company_id,
                    tax_id=profile.get("tax_id", ""),
                    website=profile.get("real_website", ""),
                    industry=profile.get("industry", ""),
                    company_size=profile.get("company_size", ""),
                    address=profile.get("address", ""),
                    products_services=profile.get("description", ""),
                )

            # 4) Insert (content_hash tự tính bởi trigger Postgres)
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
                source_url=raw.source_url,
                source_name=raw.source_name,
                salary_raw_text=raw.salary_text,
                deadline=deadline,
                parsed_content=parsed_content,
                raw_jd_content=raw_jd_content,
            )
            conn.commit()
            stats["inserted"] += 1
            logger.info("Đã lưu: [%s] %s @ %s", level_code, raw.job_title, company_name)

        except Exception as exc:  # noqa: BLE001 - log rồi tiếp tục, không dừng cả pipeline
            conn.rollback()
            stats["errors"] += 1
            logger.error("Lỗi xử lý job '%s': %s", raw.job_title, exc)

    return stats
