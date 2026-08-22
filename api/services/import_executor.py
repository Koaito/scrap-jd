"""
Import Executor — nhận preview_data (đã lưu trong import_previews) +
resolution map từ staff (Skip/Update/Create mỗi dòng, company_id đã chọn
cho dòng "pending_company_resolution", xác nhận reactivate cho dòng
"conflict_inactive") -> thực thi INSERT/UPDATE thật trong 1 DATABASE
TRANSACTION (Requirement 6.1: tất cả hoặc không gì cả).

KHÔNG tự conn.commit()/rollback() ở đây — router (nơi có transaction
boundary rõ ràng, giống mọi route ghi khác trong codebase) chịu trách
nhiệm commit sau khi execute_import() trả về thành công, hoặc để
exception tự nổi lên cho router rollback + trả lỗi (Requirement 6.2,
6.8: giữ nguyên Preview_Session nếu fail để retry).
"""

from dataclasses import dataclass
from typing import Optional

import db as db_module
from api.services import conflict_detector
from api.services.entity_specs import get_spec
from constants import LEVEL_CODE_VALUES


class RowResolutionError(Exception):
    """1 dòng thiếu thông tin bắt buộc để thực thi (vd action=Update cho
    dòng pending_company_resolution mà staff chưa chọn company_id nào,
    hoặc action=Update cho dòng conflict_inactive mà chưa xác nhận
    confirm_reactivate) — router bắt exception này, trả 422 rõ nguyên
    nhân TRƯỚC KHI chạm transaction (thực ra được raise NGAY BÊN TRONG
    transaction nên vẫn rollback sạch, không cần router tự kiểm tra
    trước — chỉ cần bắt và convert sang HTTPException)."""


@dataclass
class ImportSummary:
    created: int = 0
    updated: int = 0
    skipped: int = 0


def execute_import(
    conn,
    *,
    entity_type: str,
    preview_rows: list[dict],
    resolutions: dict[str, dict],
    actor_id: str,
) -> ImportSummary:
    """resolutions: {str(row_index): {"action": "skip"|"update"|"create",
    "company_id": "<uuid>" (optional, cho dòng pending_company_resolution),
    "confirm_reactivate": bool (optional, cho dòng conflict_inactive),
    "level_code": "<Intern|Fresher|...|Manager>" (optional, BẮT BUỘC nếu
    dòng needs_level_resolve=true và action != "skip" — xem check ngay
    đầu vòng lặp bên dưới)}}.

    Requirement 5.6: dòng conflict KHÔNG có resolution -> mặc định Skip.
    Dòng no_conflict KHÔNG cần resolution -> LUÔN tạo mới (Requirement 6.3).
    """
    summary = ImportSummary()

    for row in preview_rows:
        row_index = row["row_index"]
        data = dict(row["data"])
        status = row["conflict_status"]
        resolution = resolutions.get(str(row_index), {})
        action = resolution.get("action", "skip")

        # needs_level_resolve (chỉ Job, 08/2026 — xem preview_manager.py::
        # build_preview): level_code trong file không khớp danh sách hợp
        # lệ (dù đã chuẩn hoá hoa/thường) -> validation_engine.py đã set
        # data["level_code"] = None ngay từ preview. Bắt buộc staff chọn
        # lại qua resolution["level_code"] TRƯỚC KHI tạo/sửa job — action
        # "skip" thì bỏ qua check này (đằng nào cũng không ghi gì).
        # KHÔNG default âm thầm về None/1 giá trị nào đó: level sai/thiếu
        # là lỗi dữ liệu staff cần xác nhận tay, khác company (không chọn
        # gì -> tự hiểu "tạo company mới theo tên trong file", vì company
        # LUÔN có thể tạo mới hợp lệ; còn level chỉ có đúng 7 giá trị cố
        # định, "level rỗng" không phải 1 lựa chọn nghiệp vụ hợp lệ).
        if entity_type == "job" and row.get("needs_level_resolve") and action != "skip":
            chosen_level = resolution.get("level_code")
            if not chosen_level or chosen_level not in LEVEL_CODE_VALUES:
                raise RowResolutionError(
                    f"Dòng {row_index + 1}: level_code trong file "
                    f"({row.get('level_code_raw')!r}) không khớp danh sách hợp lệ "
                    f"{LEVEL_CODE_VALUES} — cần chọn lại 1 giá trị trước khi xác nhận."
                )
            data["level_code"] = chosen_level

        # Job/Contact: company đã resolve XONG lúc build preview (mọi
        # status TRỪ "pending_company_resolution") -> gắn company_id đã
        # biết vào data ngay từ đây, để _create_row/_update_row dùng
        # thẳng thay vì phải đọc lại company_resolution mỗi nơi.
        if entity_type in ("job", "contact") and status != "pending_company_resolution":
            company_resolution = row.get("company_resolution") or {}
            if company_resolution.get("company_id"):
                data["company_id"] = company_resolution["company_id"]

        if status == "no_conflict":
            _create_row(conn, entity_type, data, row, resolution, actor_id)
            summary.created += 1
            continue

        if status == "pending_company_resolution":
            # Staff đã chọn 1 company trong danh sách gợi ý -> dùng
            # company_id đó. Không chọn gì (bỏ trống) -> hiểu là "không
            # công ty gợi ý nào đúng, tạo company mới theo company_name
            # trong file" (theo đúng quyết định thiết kế), KHÔNG chặn
            # lại bắt staff phải chọn.
            company_id = resolution.get("company_id") or _resolve_company_id_for_create(
                conn, data, actor_id
            )
            data["company_id"] = company_id
            # Re-check conflict NGAY LÚC NÀY với company_id thật vừa chọn
            # (lúc build preview chưa biết company_id nên chưa detect được
            # — xem docstring conflict_detector.py).
            real_status, existing = _recheck_conflict(conn, entity_type, data)
            if real_status == "no_conflict":
                _create_row(conn, entity_type, data, row, resolution, actor_id)
                summary.created += 1
            else:
                _apply_conflict_action(
                    conn, entity_type, data, existing, real_status,
                    action, resolution, actor_id, summary,
                )
            continue

        # status in ("conflict", "conflict_inactive")
        existing = row.get("existing_record")
        _apply_conflict_action(
            conn, entity_type, data, existing, status, action, resolution, actor_id, summary,
        )

    return summary


def _apply_conflict_action(conn, entity_type, data, existing, status, action, resolution, actor_id, summary):
    if action == "skip":
        summary.skipped += 1
        return

    if action == "create":
        _create_row(conn, entity_type, data, {"row_index": None}, resolution, actor_id)
        summary.created += 1
        return

    if action == "update":
        if status == "conflict_inactive" and not resolution.get("confirm_reactivate"):
            raise RowResolutionError(
                "Dòng trùng với record đang ở trạng thái ngừng hoạt động (inactive) — "
                "cần xác nhận confirm_reactivate=true để ghi đè và kích hoạt lại."
            )
        _update_row(conn, entity_type, data, existing, resolution, actor_id,
                    reactivate=(status == "conflict_inactive"))
        summary.updated += 1
        return

    raise RowResolutionError(f"action '{action}' không hợp lệ (chỉ nhận skip/update/create)")


def _recheck_conflict(conn, entity_type, data) -> tuple[str, Optional[dict]]:
    if entity_type == "job":
        result = conflict_detector.detect_job_conflict(
            conn, data.get("company_name"), data.get("job_title"), data.get("deadline")
        )
    elif entity_type == "contact":
        result = conflict_detector.detect_contact_conflict(
            conn, data.get("company_id"), data.get("contact_name"), data.get("work_email")
        )
    else:
        result = {"conflict_status": "no_conflict"}
    return result["conflict_status"], result.get("existing_record")


def _create_row(conn, entity_type, data, row, resolution, actor_id):
    if entity_type == "company":
        province_id = (
            db_module.get_or_create_province(conn, data["province_name"])
            if data.get("province_name") else None
        )
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO companies
                    (company_name, tax_id, website, industry, company_size, address,
                     province_id, fanpage_url, linkedin_url, partnership_potential,
                     created_by, updated_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING company_id
                """,
                (
                    data["company_name"], data.get("tax_id"), data.get("website"),
                    data.get("industry"), data.get("company_size"), data.get("address"),
                    province_id, data.get("fanpage_url"), data.get("linkedin_url"),
                    data.get("partnership_potential") or "UNVERIFIED",
                    actor_id, actor_id,
                ),
            )
            return str(cur.fetchone()[0])

    if entity_type == "job":
        company_id = data.get("company_id") or _resolve_company_id_for_create(conn, data, actor_id)
        level_id = db_module.get_level_id(conn, data["level_code"]) if data.get("level_code") else None
        province_id = (
            db_module.get_or_create_province(conn, data["province_name"])
            if data.get("province_name") else None
        )
        return db_module.create_manual_job(
            conn,
            job_title=data["job_title"],
            company_id=company_id,
            matching_industry=data.get("matching_industry") or "",
            level_id=level_id,
            province_id=province_id,
            work_type=data.get("work_type"),
            currency=data.get("currency") or "VNĐ",
            salary_min=data.get("salary_min"),
            salary_max=data.get("salary_max"),
            salary_type=data.get("salary_type") or "NEGOTIABLE",
            salary_period=data.get("salary_period") or "MONTH",
            deadline=data.get("deadline"),
            created_by=actor_id,
        )

    if entity_type == "contact":
        company_id = data.get("company_id") or _resolve_company_id_for_create(conn, data, actor_id)
        return db_module.create_company_contact(
            conn,
            company_id=company_id,
            contact_name=data["contact_name"],
            job_title=data.get("job_title"),
            work_email=data.get("work_email"),
            social_link=data.get("social_link"),
            phone_number=data.get("phone_number"),
            found_source=data.get("found_source") or "IMPORT",
            created_by=actor_id,
        )

    raise ValueError(f"entity_type không hợp lệ: {entity_type!r}")


def _resolve_company_id_for_create(conn, data, actor_id) -> str:
    """Dòng Job/Contact ở trạng thái no_conflict — company đã resolved
    (exact match tax_id/tên) lúc build preview, company_id nằm trong
    company_resolution chứ không phải data['company_id'] trực tiếp (xem
    preview_manager.build_preview) — hàm này KHÔNG được gọi tới nếu
    router truyền company_id đúng cách; giữ lại làm lớp phòng thủ, tự
    tạo company mới theo company_name nếu vì lý do gì đó vẫn thiếu."""
    return db_module.get_or_create_company_by_profile(
        conn, data.get("company_name", ""), province_id=None, created_by=actor_id,
    )


def _update_row(conn, entity_type, data, existing, resolution, actor_id, *, reactivate: bool):
    if entity_type == "company":
        company_id = existing[get_spec(entity_type).id_field]
        province_id = (
            db_module.get_or_create_province(conn, data["province_name"])
            if data.get("province_name") else None
        )
        db_module.patch_company_profile(
            conn, company_id,
            company_name=data.get("company_name"),
            tax_id=data.get("tax_id"),
            website=data.get("website"),
            industry=data.get("industry"),
            company_size=data.get("company_size"),
            address=data.get("address"),
            province_id=province_id,
            fanpage_url=data.get("fanpage_url"),
            linkedin_url=data.get("linkedin_url"),
            partnership_potential=data.get("partnership_potential"),
            updated_by=actor_id,
        )
        if reactivate:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE companies SET is_active = true WHERE company_id = %s",
                    (company_id,),
                )
        return company_id

    if entity_type == "job":
        job_id = existing[get_spec(entity_type).id_field]
        level_id = db_module.get_level_id(conn, data["level_code"]) if data.get("level_code") else None
        province_id = (
            db_module.get_or_create_province(conn, data["province_name"])
            if data.get("province_name") else None
        )
        db_module.update_job(
            conn, job_id,
            job_title=data.get("job_title"),
            matching_industry=data.get("matching_industry"),
            level_id=level_id,
            province_id=province_id,
            work_type=data.get("work_type"),
            currency=data.get("currency"),
            salary_min=data.get("salary_min"),
            salary_max=data.get("salary_max"),
            salary_type=data.get("salary_type"),
            salary_period=data.get("salary_period"),
            deadline=data.get("deadline"),
            # reactivate: job cũ CLOSED/EXPIRED -> mở lại OPEN, KỂ CẢ KHI
            # file import không có cột job_status (mặc định OPEN vì đây
            # là job "còn hiệu lực" theo file mới import).
            job_status=(data.get("job_status") or ("OPEN" if reactivate else None)),
            updated_by=actor_id,
        )
        return job_id

    if entity_type == "contact":
        contact_id = existing[get_spec(entity_type).id_field]
        db_module.update_company_contact(
            conn, contact_id,
            contact_name=data.get("contact_name"),
            job_title=data.get("job_title"),
            work_email=data.get("work_email"),
            social_link=data.get("social_link"),
            phone_number=data.get("phone_number"),
            updated_by=actor_id,
        )
        if reactivate:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE company_contacts SET is_active = true WHERE contact_id = %s",
                    (contact_id,),
                )
        return contact_id

    raise ValueError(f"entity_type không hợp lệ: {entity_type!r}")
