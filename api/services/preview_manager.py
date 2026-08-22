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
                          | "pending_company_resolution",
      "existing_record": {...} | null,
      "company_resolution": {                # CHỈ có ở entity job/contact
        "status": "resolved" | "needs_resolution",
        "company_id": "<uuid>" | null,
        "company_is_active": true/false | null,
        "suggestions": [{"company_id":..., "company_name":..., "tax_id":...,
                          "is_active":..., "similarity":...}, ...]
      }
    }
  ],
  "summary": {"total_rows": N, "new_records": N, "conflicts": N,
              "conflicts_inactive": N, "pending_company_resolution": N,
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
from api.services.entity_specs import get_spec
from api.services.validation_engine import ValidationResult

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
        "pending_level_resolution": 0,
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

        status = entry["conflict_status"]
        if status == "no_conflict":
            summary["new_records"] += 1
        elif status == "conflict":
            summary["conflicts"] += 1
        elif status == "conflict_inactive":
            summary["conflicts_inactive"] += 1
        elif status == "pending_company_resolution":
            summary["pending_company_resolution"] += 1

        # Cộng dồn ĐỘC LẬP với conflict_status ở trên (xem comment
        # needs_level_resolve phía trên) — 1 dòng "no_conflict" vẫn có
        # thể cần chọn lại level, nên đếm bằng if riêng, KHÔNG phải
        # elif nối vào chuỗi if/elif conflict_status.
        if entry.get("needs_level_resolve"):
            summary["pending_level_resolution"] += 1

        rows_out.append(entry)

    return {"rows": rows_out, "summary": summary}


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
