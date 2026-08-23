"""
Preview Manager — orchestrator nối file_parser -> validation_engine ->
company_resolver (chỉ Job/Contact) -> conflict_detector, rồi lưu kết quả
vào bảng import_previews (JSONB). Cũng lo việc đọc lại preview theo
preview_id kèm check quyền sở hữu (user_id) + hết hạn (Requirement 4,
8.4, 8.5).

Cấu trúc JSONB preview_data lưu trong DB:
{
  "rows": [
    {
      "row_index": 0,
      "data": {...cleaned fields từ validation_engine...},
      "conflict_status": "no_conflict" | "conflict" | "conflict_inactive"
                          | "pending_company_resolution" | "conflict_in_batch",
      "existing_record": {...} | null,
      "duplicate_match": {"match_score":..., "matched_fields":[...]} | null,
                          # (chỉ Contact) match mờ với DB — chỉ có giá trị
                          # khi conflict_status chuyển "conflict" NGAY TẠI
                          # apply_field_fix() (xem hàm đó), null nếu chưa
                          # từng re-check hoặc conflict do build_preview.
      "duplicate_in_batch": {"match_score":..., "matched_fields":[...],
                              "other_row_index": N} | null,
                          # (chỉ Contact, thêm 08/2026) match mờ với 1
                          # dòng KHÁC trong CHÍNH file này (không phải
                          # DB) — xem conflict_detector.
                          # find_duplicate_rows_in_batch(). Chỉ có giá
                          # trị khi conflict_status == "conflict_in_batch".
      "company_resolution": {                # CHỈ có ở entity job/contact
        "status": "resolved" | "needs_resolution",
        "company_id": "<uuid>" | null,
        "company_is_active": true/false | null,
        "suggestions": [{"company_id":..., "company_name":..., "tax_id":...,
                          "is_active":..., "similarity":...}, ...]
      },
      "needs_field_fix": true/false,   # thêm 08/2026, xem validation_engine.py
      "field_errors": {                # {} nếu needs_field_fix=false
        "<field_name>": {
          "rule": "required" | "type_date" | "type_number" | "type_email"
                  | "business_rule_enum" | "business_rule_non_negative"
                  | "business_rule_salary_range",
          "message": "...",
          "raw_value": "<chuỗi gốc staff đã gõ trong file>" | null,
          "widget_type": "enum" | "date" | "number" | "email" | "text",
          "options": ["..."] | null   # chỉ có giá trị khi widget_type == "enum"
        }
      }
    }
  ],
  "summary": {"total_rows": N, "new_records": N, "conflicts": N,
              "conflicts_inactive": N, "pending_company_resolution": N,
              "conflicts_in_batch": N, "pending_level_resolution": N,
              "pending_field_fix": N,
              "id_field": "job_id" | "company_id" | "contact_id"}
}
"""

import json
import uuid as uuid_module
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import psycopg2.extras

from api.services import conflict_detector, company_resolver
from api.services.entity_specs import field_options, field_widget_type, get_spec
from api.services.validation_engine import ValidationResult, validate_single_field

# Contact: field nào (khi sửa) cần re-check trùng mờ ngay — xem
# conflict_detector.find_duplicate_contacts() + apply_field_fix() bên
# dưới. company_name KHÔNG nằm trong set này vì đổi company_name kéo
# theo re-resolve company_id (khác flow), xử lý riêng, chưa làm ở bản
# này (xem ghi chú "việc số 4" trong trao đổi thiết kế).
_CONTACT_DUPLICATE_CHECK_FIELDS = {"work_email", "social_link", "phone_number"}

PREVIEW_TTL = timedelta(hours=1)


class PreviewNotFoundError(Exception):
    """preview_id không tồn tại — đã bị xoá (confirm xong, hoặc cleanup
    task dọn quá hạn) hoặc chưa từng tồn tại (gõ sai)."""


class PreviewExpiredError(Exception):
    """Preview quá 1 giờ — Requirement 4.3, message cố định "Preview
    expired, please re-upload file" (Requirement 10.5)."""


class PreviewOwnershipError(Exception):
    """preview_id tồn tại nhưng KHÔNG thuộc về user đang gọi (Requirement
    8.4/8.5) — trả 403, KHÔNG lộ preview đó có tồn tại hay không (thông
    điệp giống hệt PreviewNotFoundError ở tầng router)."""


def build_preview(conn, entity_type: str, validation_result: ValidationResult) -> dict:
    """Chạy company resolution (nếu cần) + conflict detection cho toàn bộ
    cleaned_rows, trả về dict preview_data đúng cấu trúc mô tả ở đầu file
    (CHƯA lưu DB — save_preview() lo phần lưu)."""
    rows_out = []
    summary = {
        "total_rows": len(validation_result.cleaned_rows),
        "new_records": 0,
        "conflicts": 0,
        "conflicts_inactive": 0,
        "pending_company_resolution": 0,
        "conflicts_in_batch": 0,
        "pending_level_resolution": 0,
        "pending_field_fix": 0,
        # id_field: tên cột PK thật của entity (vd "job_id") — thêm
        # 08/2026 để FE tra tên field id đúng từ response thay vì tự
        # hardcode map entity_type -> tên cột id riêng phía client (xem
        # EntitySpec.id_field, api/services/entity_specs.py).
        "id_field": get_spec(entity_type).id_field,
    }

    for row in validation_result.cleaned_rows:
        row_index = row["_row_index"]
        # "_row_index" (khoá nội bộ) và mọi "_<field>_raw" (giá trị gốc
        # của field strict_enum_fields không khớp — xem validation_engine.
        # py::validate_dataframe nhánh strict_enum_fields) đều KHÔNG được
        # coi là 1 cột dữ liệu thật -> tách khỏi `data` (nếu để lẫn vào,
        # FE sẽ render thành 1 cột rác "_level_code_raw" trên bảng preview
        # cùng hàng các cột company_name/job_title/...).
        data = {k: v for k, v in row.items() if k != "_row_index" and not k.startswith("_")}
        entry = {"row_index": row_index, "data": _jsonable(data)}

        # needs_level_resolve (chỉ Job, 08/2026): validate_dataframe() đã
        # set cleaned["level_code"] = None + cleaned["_level_code_raw"] =
        # <giá trị gốc trong file> khi giá trị không khớp (dù đã chuẩn
        # hoá case-insensitive) 1 trong 7 level hợp lệ. Tách riêng thành
        # field top-level giống company_resolution, KHÔNG gộp vào
        # conflict_status hiện có (no_conflict/conflict/conflict_inactive/
        # pending_company_resolution) vì đây là 2 trục độc lập — 1 dòng
        # CÓ THỂ vừa "no_conflict" (job chưa từng tồn tại) vừa "cần chọn
        # lại level" cùng lúc, khác company_resolution vốn quyết định
        # thẳng conflict_status vì company_id ảnh hưởng tới việc detect
        # trùng job (job trùng được match theo company_id).
        if entity_type == "job" and row.get("_level_code_raw") is not None:
            entry["needs_level_resolve"] = True
            entry["level_code_raw"] = row["_level_code_raw"]
        else:
            entry["needs_level_resolve"] = False
            entry["level_code_raw"] = None

        # needs_field_fix (08/2026, mọi entity): validate_dataframe() gắn
        # cleaned["_field_errors"] = {field: {"rule","message"}} cho dòng
        # có field lỗi type/required/business-rule (KHÔNG còn reject cả
        # file — xem validation_engine.py). Gắn kèm raw_value (giá trị
        # gốc staff đã gõ, None cho rule "required" vì ô đó vốn để trống)
        # + widget_type/options (tra từ entity_specs.py, KHÔNG hardcode ở
        # đây hay ở FE) để FE render đúng loại ô sửa (select cho enum,
        # input type=date cho ngày, input cho số/chữ) ngay trên bảng
        # preview.
        field_errors_raw = row.get("_field_errors") or {}
        if field_errors_raw:
            entry["needs_field_fix"] = True
            entry["field_errors"] = {
                fname: {
                    "rule": err["rule"],
                    "message": err["message"],
                    "raw_value": row.get(f"_{fname}_raw"),
                    "widget_type": field_widget_type(entity_type, fname),
                    "options": field_options(entity_type, fname),
                }
                for fname, err in field_errors_raw.items()
            }
        else:
            entry["needs_field_fix"] = False
            entry["field_errors"] = {}

        if entity_type == "company":
            result = conflict_detector.detect_company_conflict(
                conn, data.get("company_name"), data.get("tax_id")
            )
            entry.update(result)

        elif entity_type == "job":
            resolution = company_resolver.resolve_company(conn, data.get("company_name"))
            entry["company_resolution"] = _resolution_to_dict(resolution)
            if resolution.status == "needs_resolution":
                entry["conflict_status"] = "pending_company_resolution"
            else:
                result = conflict_detector.detect_job_conflict(
                    conn, data.get("company_name"), data.get("job_title"), data.get("deadline")
                )
                entry.update(result)

        elif entity_type == "contact":
            resolution = company_resolver.resolve_company(conn, data.get("company_name"))
            entry["company_resolution"] = _resolution_to_dict(resolution)
            if resolution.status == "needs_resolution":
                entry["conflict_status"] = "pending_company_resolution"
            else:
                result = conflict_detector.detect_contact_conflict(
                    conn, resolution.company_id, data.get("contact_name"), data.get("work_email")
                )
                entry.update(result)

        entry.setdefault("existing_record", None)
        entry["existing_record"] = _jsonable(entry.get("existing_record"))
        # duplicate_match/duplicate_in_batch (08/2026): mặc định None cho
        # MỌI dòng ngay lúc build — build_preview() KHÔNG BAO GIỜ tự gán
        # 2 field này (chỉ apply_field_fix() mới gán, xem hàm đó), nhưng
        # cần có mặt sẵn với giá trị None để FE luôn có key để đọc, và để
        # apply_field_fix() không phải tự kiểm tra "key có tồn tại chưa"
        # mỗi lần đọc/ghi (row.get(...) vẫn an toàn dù thiếu key, nhưng
        # để tường minh cấu trúc JSONB đồng nhất mọi dòng ngay từ đầu).
        entry.setdefault("duplicate_match", None)
        entry.setdefault("duplicate_in_batch", None)

        rows_out.append(entry)

    summary.update(_count_summary_fields(rows_out))
    return {"rows": rows_out, "summary": summary}


def _count_summary_fields(rows: list[dict]) -> dict:
    """Đếm các field summary phụ thuộc trực tiếp vào nội dung `rows`
    (conflict_status + needs_level_resolve/needs_field_fix) — TÁCH RIÊNG
    khỏi build_preview() để apply_field_fix() dùng LẠI được nguyên hàm
    này khi cần tính lại summary sau khi sửa 1 ô (thay vì tự cộng/trừ
    tay ở nhiều điểm rẽ nhánh trong apply_field_fix, dễ lệch số khi 1
    lần sửa ảnh hưởng conflict_status của CẢ dòng đang sửa lẫn dòng kia
    bị match batch — xem apply_field_fix()). Quét lại toàn bộ rows mỗi
    lần gọi (preview tối đa 5000 dòng, chi phí không đáng kể) thay vì
    cộng dồn tăng-dần — LUÔN ĐÚNG, không rủi ro lệch số do quên nhánh
    nào đó.

    KHÔNG bao gồm "total_rows"/"id_field" (2 field đó không đổi khi sửa
    field, không cần tính lại) — caller tự set/giữ nguyên."""
    counts = {
        "new_records": 0,
        "conflicts": 0,
        "conflicts_inactive": 0,
        "pending_company_resolution": 0,
        "conflicts_in_batch": 0,
        "pending_level_resolution": 0,
        "pending_field_fix": 0,
    }
    for entry in rows:
        status = entry["conflict_status"]
        if status == "no_conflict":
            counts["new_records"] += 1
        elif status == "conflict":
            counts["conflicts"] += 1
        elif status == "conflict_inactive":
            counts["conflicts_inactive"] += 1
        elif status == "pending_company_resolution":
            counts["pending_company_resolution"] += 1
        elif status == "conflict_in_batch":
            counts["conflicts_in_batch"] += 1

        # Cộng dồn ĐỘC LẬP với conflict_status ở trên (xem comment
        # needs_level_resolve trong build_preview) — 1 dòng "no_conflict"
        # vẫn có thể cần chọn lại level, nên đếm bằng if riêng, KHÔNG
        # phải elif nối vào chuỗi if/elif conflict_status.
        if entry.get("needs_level_resolve"):
            counts["pending_level_resolution"] += 1

        if entry.get("needs_field_fix"):
            counts["pending_field_fix"] += 1

    return counts


def save_preview(conn, *, user_id: str, entity_type: str, preview_data: dict) -> str:
    now = datetime.now(timezone.utc)
    expires_at = now + PREVIEW_TTL
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO import_previews (user_id, entity_type, preview_data, created_at, expires_at)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING preview_id
            """,
            (user_id, entity_type, json.dumps(preview_data, default=str), now, expires_at),
        )
        return str(cur.fetchone()[0])


def get_preview(conn, preview_id: str, *, requesting_user_id: str) -> dict:
    """Trả row đầy đủ (dict) của import_previews, đã check TTL + ownership.
    Raise PreviewNotFoundError/PreviewExpiredError/PreviewOwnershipError
    tương ứng — router quyết định mã lỗi HTTP."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM import_previews WHERE preview_id = %s", (preview_id,))
        row = cur.fetchone()

    if row is None:
        raise PreviewNotFoundError(preview_id)

    if str(row["user_id"]) != str(requesting_user_id):
        raise PreviewOwnershipError(preview_id)

    expires_at = row["expires_at"]
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        raise PreviewExpiredError(preview_id)

    return dict(row)


def apply_field_fix(
    conn, preview_row: dict, *, row_index: int, field_name: str, raw_value: str,
) -> dict:
    """Sửa TẠI CHỖ 1 field của 1 dòng trong preview_data đang lưu DB, rồi
    lưu lại ngay (KHÔNG đợi staff bấm "Xác nhận nhập dữ liệu" ở bước
    confirm) — dùng cho nút "Xác nhận" cạnh mỗi ô sửa trên bảng preview
    (xem trao đổi thiết kế "việc số 2": staff sửa xong 1 ô, bấm xác nhận
    ngay tại đó, biết lỗi/nghi ngờ trùng NGAY, không phải đợi tới cuối).

    Làm 2 việc, THEO ĐÚNG THỨ TỰ:
    1. Re-validate format field_name bằng validate_single_field() — Y HỆT
       hàm dùng lúc build preview lẫn lúc confirm thật (import_executor.
       _apply_field_fixes), để 3 nơi không lệch logic convert theo type.
       Sai format -> trả lỗi ngay, KHÔNG lưu gì, KHÔNG re-check trùng
       (dữ liệu chưa hợp lệ thì chưa có gì để so khớp).
    2. Field hợp lệ -> ghi vào data, xoá khỏi field_errors/needs_field_fix
       của dòng. Nếu field_name là 1 trong 3 cột định danh contact
       (_CONTACT_DUPLICATE_CHECK_FIELDS) VÀ entity_type == "contact" ->
       chạy 2 loại match, THEO THỨ TỰ ƯU TIÊN (DB trước, batch sau —
       tránh 2 loại conflict chồng lên nhau gây rối UI, vì DB-match đã
       có existing_record thật để Update ngay, "chắc" hơn 1 dòng khác
       trong file mà bản thân cũng chưa chắc đúng):
         a. conflict_detector.find_duplicate_contacts() — match mờ VỚI
            DB, company_id lấy từ company_resolution đã resolve lúc
            build preview (KHÔNG re-resolve lại company_name ở đây).
            Có match -> conflict_status "conflict" (tái dùng UI
            Skip/Update/Create có sẵn) + existing_record/duplicate_match.
         b. CHỈ khi (a) không có gì: conflict_detector.
            find_duplicate_rows_in_batch() — match mờ với 1 dòng KHÁC
            TRONG CHÍNH file này (quét toàn bộ rows, KHÔNG loại trừ
            dòng đã Skip — xem docstring hàm đó). Có match -> XỬ LÝ 2
            CHIỀU: conflict_status của dòng đang sửa VÀ dòng bị match
            đều chuyển "conflict_in_batch" (nếu dòng kia CHƯA có gì
            "nặng" hơn — conflict/conflict_inactive/pending_company_
            resolution từ trước, DB/company-resolution vẫn ưu tiên hơn
            batch-match ở dòng kia), gắn duplicate_in_batch cho CẢ 2
            dòng (other_row_index trỏ chéo nhau), lưu preview_data 1
            LẦN duy nhất cho cả 2 thay đổi.
       Hết match ở CẢ (a) và (b) -> tự revert conflict_status về
       "no_conflict" NẾU trạng thái hiện tại là do CHÍNH lần re-check
       trước gây ra (không đụng tới nếu dòng vốn conflict/conflict_inactive/
       conflict_in_batch từ build_preview hoặc từ 1 lần sửa field KHÁC).
       Nếu dòng ĐANG trỏ batch-match sang 1 dòng khác (trước khi sửa)
       mà giờ không còn match nữa (đổi field, hoặc DB-match giờ ưu tiên
       hơn) -> gỡ liên kết NGƯỢC ở dòng kia luôn (_clear_batch_link),
       tránh để dòng kia trỏ treo sang dữ liệu CŨ của dòng này.

    Trả full row entry (dict) đã cập nhật của DÒNG ĐANG SỬA — router
    build FieldVerifyResponse trực tiếp từ đây. LƯU Ý: nếu case (b) xảy
    ra, dòng KIA cũng bị đổi trong preview_data đã lưu DB nhưng KHÔNG
    nằm trong response này — FE phải tự nhận biết (vd load lại preview,
    hoặc BE trả thêm ở lần mở rộng sau) rằng 1 dòng khác cũng vừa đổi.

    LƯU Ý: hàm này TỰ COMMIT (conn.commit()) vì lưu ngay khi staff bấm
    "Xác nhận" tại ô, không gộp chung transaction với bước confirm cuối
    — nếu raise lỗi validate thì KHÔNG commit gì (giữ nguyên preview cũ)."""
    preview_data = preview_row["preview_data"]
    rows = preview_data["rows"]
    row = next((r for r in rows if r["row_index"] == row_index), None)
    if row is None:
        raise ValueError(f"row_index {row_index} không có trong preview này.")

    entity_type = preview_row["entity_type"]
    spec = get_spec(entity_type)

    raw = (raw_value or "").strip()
    if raw == "":
        return {
            "row": row,
            "field_error": {
                "rule": "required",
                "message": f"Cột '{field_name}' là bắt buộc, không được để trống.",
            },
        }

    value, err = validate_single_field(spec, field_name, raw)
    if err is not None:
        return {"row": row, "field_error": err}

    # Field hợp lệ -> ghi vào data, xoá lỗi cũ của field này (nếu có).
    row["data"][field_name] = value
    field_errors = row.get("field_errors") or {}
    field_errors.pop(field_name, None)
    row["field_errors"] = field_errors
    row["needs_field_fix"] = bool(field_errors)

    if entity_type == "contact" and field_name in _CONTACT_DUPLICATE_CHECK_FIELDS:
        was_conflict_from_db_check = row.get("duplicate_match") is not None
        was_conflict_in_batch = row.get("conflict_status") == "conflict_in_batch"
        prev_batch_link = row.get("duplicate_in_batch")

        # Dòng đang sửa TRƯỚC ĐÓ trỏ batch-match sang 1 dòng khác -> gỡ
        # liên kết NGƯỢC ở dòng kia trước khi tính lại (nếu vẫn còn
        # match sau khi tính lại, sẽ được set lại ở nhánh (b) bên dưới —
        # đơn giản hơn cố giữ lại liên kết cũ rồi so sánh diff).
        if prev_batch_link:
            _clear_batch_link(rows, prev_batch_link["other_row_index"], row_index)

        company_id = (row.get("company_resolution") or {}).get("company_id")
        work_email = row["data"].get("work_email")
        social_link = row["data"].get("social_link")
        phone_number = row["data"].get("phone_number")

        # (a) DB-match — ưu tiên trước.
        db_matches = conflict_detector.find_duplicate_contacts(
            conn, company_id=company_id, work_email=work_email,
            social_link=social_link, phone_number=phone_number,
        )
        if db_matches:
            best = db_matches[0]
            row["conflict_status"] = "conflict"
            row["existing_record"] = _jsonable(best["existing_record"])
            row["duplicate_match"] = {
                "match_score": best["match_score"], "matched_fields": best["matched_fields"],
            }
            row["duplicate_in_batch"] = None
        else:
            # Hết DB-match -> revert phần do DB-match gây ra trước đó
            # (nếu có), rồi mới xét batch-match.
            if was_conflict_from_db_check:
                row["conflict_status"] = "no_conflict"
                row["existing_record"] = None
                row["duplicate_match"] = None

            # (b) Batch-match — CHỈ chạy khi (a) không có gì.
            batch_matches = conflict_detector.find_duplicate_rows_in_batch(
                rows, row_index=row_index, company_id=company_id,
                work_email=work_email, social_link=social_link, phone_number=phone_number,
            )
            if batch_matches:
                best = batch_matches[0]
                other_index = best["row_index"]
                other_row = next(r for r in rows if r["row_index"] == other_index)

                link_for_this = {
                    "match_score": best["match_score"], "matched_fields": best["matched_fields"],
                    "other_row_index": other_index,
                }
                row["duplicate_in_batch"] = link_for_this
                if row["conflict_status"] not in ("conflict", "conflict_inactive"):
                    row["conflict_status"] = "conflict_in_batch"

                other_row["duplicate_in_batch"] = {
                    "match_score": best["match_score"], "matched_fields": best["matched_fields"],
                    "other_row_index": row_index,
                }
                # Dòng KIA giữ nguyên conflict_status nếu đang "nặng" hơn
                # batch-match (conflict/conflict_inactive/pending_company_
                # resolution từ build_preview hoặc từ 1 lần sửa khác) —
                # batch-match chỉ HẠ xuống conflict_in_batch cho dòng
                # đang "no_conflict".
                if other_row["conflict_status"] == "no_conflict":
                    other_row["conflict_status"] = "conflict_in_batch"
            else:
                row["duplicate_in_batch"] = None
                if was_conflict_in_batch:
                    row["conflict_status"] = "no_conflict"

    preview_data["summary"].update(_count_summary_fields(rows))
    _save_preview_data(conn, preview_row["preview_id"], preview_data)
    conn.commit()

    return {"row": row, "field_error": None}


def _clear_batch_link(rows: list[dict], other_row_index: int, expect_pointer_to: int) -> None:
    """Gỡ duplicate_in_batch phía DÒNG KIA khi dòng đang sửa KHÔNG còn
    trỏ tới nó nữa (dữ liệu vừa đổi khác đi, hoặc DB-match giờ được ưu
    tiên thay batch-match) — CHỈ gỡ nếu dòng kia ĐANG THẬT SỰ trỏ ngược
    lại đúng dòng này (`expect_pointer_to`), tránh xoá nhầm liên kết
    dòng kia đã kịp đổi sang match 1 dòng thứ 3 khác từ 1 lần sửa khác.
    Nếu conflict_status của dòng kia là DO CHÍNH batch-match này gây ra
    (đang "conflict_in_batch") -> revert về "no_conflict" luôn, tránh
    treo trạng thái không còn đúng. GIỚI HẠN: chỉ xử lý đúng 1 cặp
    (2 dòng) — trường hợp 3+ dòng cùng match nhau (A-B-C) không được xử
    lý triệt để ở mức revert này, staff cần tự re-check lại các dòng
    liên quan nếu gặp case hiếm này."""
    other_row = next((r for r in rows if r["row_index"] == other_row_index), None)
    if other_row is None:
        return
    link = other_row.get("duplicate_in_batch")
    if not link or link.get("other_row_index") != expect_pointer_to:
        return
    other_row["duplicate_in_batch"] = None
    if other_row.get("conflict_status") == "conflict_in_batch":
        other_row["conflict_status"] = "no_conflict"


def _save_preview_data(conn, preview_id: str, preview_data: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE import_previews SET preview_data = %s WHERE preview_id = %s",
            (json.dumps(preview_data, default=str), preview_id),
        )


def delete_preview(conn, preview_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM import_previews WHERE preview_id = %s", (preview_id,))


def cleanup_expired_previews(conn) -> int:
    """Xoá mọi preview đã hết hạn — dùng cho scheduled cleanup task
    (Requirement 9). Trả số dòng đã xoá (để log)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM import_previews WHERE expires_at < now()")
        return cur.rowcount


def _resolution_to_dict(resolution) -> dict:
    return {
        "status": resolution.status,
        "company_id": resolution.company_id,
        "company_is_active": resolution.company_is_active,
        "suggestions": [
            {
                "company_id": s.company_id,
                "company_name": s.company_name,
                "tax_id": s.tax_id,
                "is_active": s.is_active,
                "similarity": s.similarity,
            }
            for s in (resolution.suggestions or [])
        ],
    }


def _jsonable(value):
    """Convert đệ quy value từ psycopg2/pandas (date, Decimal, UUID...)
    sang kiểu JSON-serializable thuần, để json.dumps() ở save_preview()
    không lỗi TypeError."""
    if value is None:
        return None
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (pd.Timestamp,)):
        return value.date().isoformat()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, uuid_module.UUID):
        return str(value)
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)
