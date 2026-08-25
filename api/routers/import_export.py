"""
Router Import/Export CSV/XLSX cho Job/Company/Contact — xem
requirements.md + design.md tính năng import/export (08/2026).

TOÀN BỘ route yêu cầu require_role("ss_team") — cả 'ss_team' và 'admin'
đều dùng được (quyết định thiết kế: import/export không cần riêng
quyền admin, giống mọi thao tác CRUD job/company/contact khác trong
codebase này).

Luồng Import 2 bước (preview -> confirm) — xem docstring
api/services/preview_manager.py để hiểu đầy đủ cấu trúc preview_data.
"""

from datetime import date, datetime, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile

import db as db_module
from api.deps import get_db, require_role
from api.schemas import (
    CompanySuggestionOut,
    CompanySuggestionsResponse,
    ExportPreviewResponse,
    FieldVerifyRequest,
    FieldVerifyResponse,
    ImportConfirmRequest,
    ImportConfirmResult,
    ImportUploadResponse,
    ResolveCompanyRequest,
    ResolveCompanyResponse,
)
from api.services import company_resolver, export_query, file_parser, import_executor, preview_manager
from api.services.entity_specs import get_spec
from api.services.export_query import ExportFilters
from api.services.validation_engine import validate_dataframe

router = APIRouter(tags=["import-export"])

_VALID_ENTITY_TYPES = {"job", "company", "contact"}


def _check_entity_type(entity_type: str) -> None:
    if entity_type not in _VALID_ENTITY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"entity_type '{entity_type}' không hợp lệ — chỉ nhận job/company/contact.",
        )


# ------------------------------------------------------------------
# Export — Requirement 1
#
# Thêm 08/2026 (filter + preview trước khi tải, xem trao đổi thiết kế
# "export chưa chọn được lọc gì, chưa xem trước"): TRƯỚC ĐÂY export_entity
# là route DUY NHẤT, luôn lấy 100% record không filter gì, không có bước
# xem trước. Giờ tách 2 route DÙNG CHUNG _build_export_filters() (validate
# status theo entity_specs.py + date_field hợp lệ) để preview và file tải
# thật LUÔN khớp nhau:
#   - GET /export/{entity_type}/preview -> tổng số dòng khớp filter + tối
#     đa 20 dòng mẫu, KHÔNG sinh file (FE hiện bảng preview cho staff xem
#     trước khi bấm "Tải file").
#   - GET /export/{entity_type} -> giữ nguyên hành vi tải file, nhận THÊM
#     cùng bộ query params filter (mọi param optional, không truyền gì =
#     lấy toàn bộ, giữ đúng hành vi cũ cho FE/script cũ chưa cập nhật).
# ------------------------------------------------------------------

def _build_export_filters(
    entity_type: str,
    status: Optional[str],
    is_active: Optional[bool],
    company_id: Optional[str],
    date_field: str,
    from_date: Optional[date],
    to_date: Optional[date],
    limit: Optional[int],
) -> ExportFilters:
    """Validate filter theo đúng field entity đó THỰC SỰ có (job không có
    is_active, company không có status/company_id) — báo lỗi 400 rõ ràng
    thay vì âm thầm bỏ qua filter staff tưởng đã áp dụng. status hợp lệ
    tra theo entity_specs.py (1 nguồn sự thật duy nhất, khớp enum dùng
    cho import), KHÔNG hardcode danh sách riêng ở đây."""
    if status is not None:
        spec = get_spec(entity_type)
        status_field = "job_status" if entity_type == "job" else "contact_status"
        valid_values = spec.enum_fields.get(status_field)
        if valid_values is None:
            raise HTTPException(
                status_code=400,
                detail=f"entity_type '{entity_type}' không có filter status.",
            )
        if status not in valid_values:
            raise HTTPException(
                status_code=400,
                detail=f"status '{status}' không hợp lệ cho '{entity_type}' — chỉ nhận {valid_values}.",
            )

    if is_active is not None and entity_type == "job":
        raise HTTPException(
            status_code=400,
            detail="entity_type 'job' không có filter is_active.",
        )

    if company_id is not None and entity_type == "company":
        raise HTTPException(
            status_code=400,
            detail="entity_type 'company' không có filter company_id (company export theo chính nó).",
        )

    if date_field not in ("created_at", "updated_at"):
        raise HTTPException(
            status_code=400,
            detail="date_field chỉ nhận 'created_at' hoặc 'updated_at'.",
        )

    if from_date is not None and to_date is not None and from_date > to_date:
        raise HTTPException(
            status_code=400,
            detail="from_date phải <= to_date.",
        )

    if limit is not None and limit <= 0:
        raise HTTPException(status_code=400, detail="limit phải > 0.")

    return ExportFilters(
        status=status,
        is_active=is_active,
        company_id=company_id,
        date_field=date_field,
        from_date=from_date,
        to_date=to_date,
        limit=limit,
    )


def _export_filter_params(
    status: Optional[str] = Query(
        None, description="Lọc theo trạng thái (job: job_status, contact: contact_status). "
                           "Bỏ trống = mọi trạng thái."
    ),
    is_active: Optional[bool] = Query(
        None, description="Lọc theo còn hoạt động / đã ngừng (company/contact). "
                           "Bỏ trống = cả 2."
    ),
    company_id: Optional[str] = Query(
        None, description="Chỉ xuất record thuộc 1 công ty cụ thể (job/contact)."
    ),
    date_field: Literal["created_at", "updated_at"] = Query(
        "created_at", description="Cột áp from_date/to_date."
    ),
    from_date: Optional[date] = Query(None, description="Từ ngày (inclusive), theo date_field."),
    to_date: Optional[date] = Query(None, description="Đến ngày (inclusive), theo date_field."),
    limit: Optional[int] = Query(
        None, gt=0,
        description="Không lọc gì, chỉ muốn N dòng MỚI NHẤT? Truyền limit — "
                     "áp SAU khi lọc theo các filter khác (nếu có), sắp theo "
                     "created_at giảm dần.",
    ),
) -> dict:
    """Gom mọi query param filter thành 1 dict — dùng làm dependency chung
    cho cả 2 route preview/export bên dưới, tránh khai trùng 7 Query(...)
    y hệt nhau 2 lần (dễ lệch default/description nếu sửa 1 chỗ quên chỗ kia)."""
    return {
        "status": status,
        "is_active": is_active,
        "company_id": company_id,
        "date_field": date_field,
        "from_date": from_date,
        "to_date": to_date,
        "limit": limit,
    }


_PREVIEW_SAMPLE_SIZE = 20


@router.get("/export/{entity_type}/preview", response_model=ExportPreviewResponse)
def export_preview(
    entity_type: str,
    filter_params: dict = Depends(_export_filter_params),
    conn=Depends(get_db),
    user: dict = Depends(require_role("ss_team")),
):
    _check_entity_type(entity_type)
    filters = _build_export_filters(entity_type, **filter_params)

    total_matching = export_query.count_rows_for_export(conn, entity_type, filters)

    # Mẫu preview: áp CÙNG filters (kể cả limit thật nếu staff đã chọn),
    # nhưng không lấy quá _PREVIEW_SAMPLE_SIZE dòng để hiện trên bảng —
    # nếu limit của staff nhỏ hơn ngưỡng preview thì dùng đúng limit đó
    # (preview khớp CHÍNH XÁC những gì sẽ có trong file).
    sample_limit = _PREVIEW_SAMPLE_SIZE
    if filters.limit is not None:
        sample_limit = min(filters.limit, _PREVIEW_SAMPLE_SIZE)
    sample_filters = ExportFilters(**{**filters.__dict__, "limit": sample_limit})

    query_fn = export_query.QUERY_FUNCS[entity_type]
    sample_rows = query_fn(conn, sample_filters)

    will_export = min(filters.limit, total_matching) if filters.limit is not None else total_matching

    return ExportPreviewResponse(
        entity_type=entity_type,
        total_matching=total_matching,
        will_export=will_export,
        columns=get_spec(entity_type).export_columns,
        sample_rows=sample_rows,
    )


@router.get("/export/{entity_type}")
def export_entity(
    entity_type: str,
    format: Literal["csv", "xlsx"] = Query("csv", description="csv | xlsx"),
    filter_params: dict = Depends(_export_filter_params),
    conn=Depends(get_db),
    user: dict = Depends(require_role("ss_team")),
):
    _check_entity_type(entity_type)
    filters = _build_export_filters(entity_type, **filter_params)

    query_fn = export_query.QUERY_FUNCS[entity_type]
    rows = query_fn(conn, filters)
    columns = get_spec(entity_type).export_columns

    buffer = file_parser.generate_export_file(rows, columns, format)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{entity_type}_export_{timestamp}.{format}"

    from fastapi.responses import StreamingResponse
    return StreamingResponse(
        buffer,
        media_type=file_parser.content_type_for_format(format),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------------------------------------------
# Import — bước 1: upload + preview — Requirement 2, 3, 4
# ------------------------------------------------------------------

@router.post("/import/{entity_type}/preview", response_model=ImportUploadResponse)
async def import_preview(
    entity_type: str,
    file: UploadFile = File(...),
    conn=Depends(get_db),
    user: dict = Depends(require_role("ss_team")),
):
    _check_entity_type(entity_type)

    raw_bytes = await file.read()

    try:
        df = file_parser.parse_file(file, raw_bytes)
    except file_parser.UnsupportedFileFormatError:
        raise HTTPException(status_code=400, detail="Unsupported file format. Please upload CSV or XLSX")
    except file_parser.FileTooLargeError:
        raise HTTPException(status_code=400, detail="File exceeds maximum of 5000 rows")

    validation_result = validate_dataframe(df, entity_type)
    if not validation_result.is_valid:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "File có dòng không hợp lệ, không import gì cả — sửa lỗi rồi upload lại.",
                "errors": [
                    {
                        "row_number": e.row_number,
                        "field_name": e.field_name,
                        "rule": e.rule,
                        "message": e.message,
                    }
                    for e in validation_result.errors
                ],
            },
        )

    preview_data = preview_manager.build_preview(conn, entity_type, validation_result)
    preview_id = preview_manager.save_preview(
        conn, user_id=user["sub"], entity_type=entity_type, preview_data=preview_data,
    )
    conn.commit()

    return ImportUploadResponse(
        preview_id=preview_id,
        entity_type=entity_type,
        summary=preview_data["summary"],
        rows=preview_data["rows"],
    )


@router.get("/import/{entity_type}/preview/{preview_id}", response_model=ImportUploadResponse)
def get_import_preview(
    entity_type: str,
    preview_id: str,
    conn=Depends(get_db),
    user: dict = Depends(require_role("ss_team")),
):
    _check_entity_type(entity_type)
    preview_row = _load_owned_preview(conn, preview_id, user["sub"])

    return ImportUploadResponse(
        preview_id=str(preview_row["preview_id"]),
        entity_type=preview_row["entity_type"],
        summary=preview_row["preview_data"]["summary"],
        rows=preview_row["preview_data"]["rows"],
    )


@router.get(
    "/import/{entity_type}/preview/{preview_id}/company-suggestions",
    response_model=CompanySuggestionsResponse,
)
def get_company_suggestions(
    entity_type: str,
    preview_id: str,
    row_index: int = Query(..., ge=0),
    conn=Depends(get_db),
    user: dict = Depends(require_role("ss_team")),
):
    """Gợi ý company tương tự cho 1 dòng cụ thể — dùng khi staff muốn xem
    lại/đổi ý ở dòng đã ở trạng thái pending_company_resolution (danh
    sách gợi ý đã lưu sẵn lúc build preview, route này chủ yếu tiện cho
    FE gọi lại riêng lẻ 1 dòng thay vì tải lại nguyên preview)."""
    _check_entity_type(entity_type)
    preview_row = _load_owned_preview(conn, preview_id, user["sub"])

    matched = next(
        (r for r in preview_row["preview_data"]["rows"] if r["row_index"] == row_index), None,
    )
    if matched is None:
        raise HTTPException(status_code=404, detail=f"row_index {row_index} không có trong preview này.")

    company_name = matched["data"].get("company_name", "")
    suggestions = company_resolver.suggest_companies(conn, company_name)
    return CompanySuggestionsResponse(
        suggestions=[
            CompanySuggestionOut(
                company_id=s.company_id, company_name=s.company_name,
                tax_id=s.tax_id, is_active=s.is_active, similarity=s.similarity,
            )
            for s in suggestions
        ]
    )


@router.post(
    "/import/{entity_type}/preview/{preview_id}/rows/{row_index}/verify-field",
    response_model=FieldVerifyResponse,
)
def verify_field(
    entity_type: str,
    preview_id: str,
    row_index: int,
    payload: FieldVerifyRequest,
    conn=Depends(get_db),
    user: dict = Depends(require_role("ss_team")),
):
    """Staff sửa 1 ô lỗi trên bảng preview, bấm nút "Xác nhận" cạnh ô đó
    -> re-validate format field_name NGAY + (contact) re-check trùng mờ
    ngay tại đó, KHÔNG đợi tới bước confirm cuối cùng mới biết (xem
    preview_manager.apply_field_fix() cho toàn bộ logic + lý do thiết
    kế)."""
    _check_entity_type(entity_type)
    preview_row = _load_owned_preview(conn, preview_id, user["sub"])

    if preview_row["entity_type"] != entity_type:
        raise HTTPException(
            status_code=400,
            detail=f"preview_id này thuộc entity_type '{preview_row['entity_type']}', không phải '{entity_type}'.",
        )

    try:
        result = preview_manager.apply_field_fix(
            conn,
            preview_row,
            row_index=row_index,
            field_name=payload.field_name,
            raw_value=payload.value,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return FieldVerifyResponse(row=result["row"], field_error=result["field_error"])


@router.post(
    "/import/{entity_type}/preview/{preview_id}/rows/{row_index}/resolve-company",
    response_model=ResolveCompanyResponse,
)
def resolve_company(
    entity_type: str,
    preview_id: str,
    row_index: int,
    payload: ResolveCompanyRequest,
    conn=Depends(get_db),
    user: dict = Depends(require_role("ss_team")),
):
    """Staff chọn 1 công ty (hoặc "Tạo công ty mới") trong modal chọn công
    ty ở bước preview -> re-check conflict NGAY với company_id thật vừa
    chọn (xem preview_manager.resolve_company_selection() cho toàn bộ
    logic + lý do thiết kế). Route generic theo entity_type — áp dụng
    chung cho cả job lẫn contact (khác verify-field, hiện chỉ contact)."""
    _check_entity_type(entity_type)
    if entity_type not in ("job", "contact"):
        raise HTTPException(
            status_code=400,
            detail=f"entity_type '{entity_type}' không có bước chọn công ty — chỉ job/contact.",
        )
    preview_row = _load_owned_preview(conn, preview_id, user["sub"])

    if preview_row["entity_type"] != entity_type:
        raise HTTPException(
            status_code=400,
            detail=f"preview_id này thuộc entity_type '{preview_row['entity_type']}', không phải '{entity_type}'.",
        )

    try:
        row = preview_manager.resolve_company_selection(
            conn, preview_row, row_index=row_index, company_id=payload.company_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return ResolveCompanyResponse(row=row)


# ------------------------------------------------------------------
# Import — bước 2: confirm — Requirement 5, 6
# ------------------------------------------------------------------

@router.post("/import/{entity_type}/confirm", response_model=ImportConfirmResult)
def import_confirm(
    entity_type: str,
    payload: ImportConfirmRequest,
    conn=Depends(get_db),
    user: dict = Depends(require_role("ss_team")),
):
    _check_entity_type(entity_type)
    preview_row = _load_owned_preview(conn, payload.preview_id, user["sub"])

    if preview_row["entity_type"] != entity_type:
        raise HTTPException(
            status_code=400,
            detail=f"preview_id này thuộc entity_type '{preview_row['entity_type']}', không phải '{entity_type}'.",
        )

    resolutions = {k: v.model_dump() for k, v in payload.resolutions.items()}

    try:
        summary = import_executor.execute_import(
            conn,
            entity_type=entity_type,
            preview_rows=preview_row["preview_data"]["rows"],
            resolutions=resolutions,
            actor_id=user["sub"],
        )
    except import_executor.RowResolutionError as exc:
        conn.rollback()
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception:
        # Requirement 6.2/6.8: rollback toàn bộ + GIỮ preview để retry,
        # KHÔNG lộ chi tiết lỗi DB thật ra ngoài (Requirement 10.6).
        conn.rollback()
        raise HTTPException(status_code=500, detail="Import failed due to database error")

    action_type = {
        "job": "BULK_IMPORT_JOB",
        "company": "BULK_IMPORT_COMPANY",
        "contact": "BULK_IMPORT_CONTACT",
    }[entity_type]
    entity_label = (
        f"Bulk import {entity_type}: {summary.created} created, "
        f"{summary.updated} updated, {summary.skipped} skipped"
    )
    db_module.log_action(
        conn,
        actor_id=user["sub"],
        action_type=action_type,
        entity_type=entity_type.upper(),
        entity_id=payload.preview_id,
        entity_label=entity_label,
        note=payload.note,
    )

    # Requirement 6.6: xoá Preview_Session khi import THÀNH CÔNG.
    preview_manager.delete_preview(conn, payload.preview_id)
    conn.commit()

    return ImportConfirmResult(created=summary.created, updated=summary.updated, skipped=summary.skipped)


def _load_owned_preview(conn, preview_id: str, requesting_user_id: str) -> dict:
    if not db_module.is_valid_uuid(preview_id):
        raise HTTPException(status_code=400, detail=f"preview_id '{preview_id}' không đúng định dạng UUID.")
    try:
        return preview_manager.get_preview(conn, preview_id, requesting_user_id=requesting_user_id)
    except preview_manager.PreviewNotFoundError:
        raise HTTPException(status_code=404, detail="Preview không tồn tại.")
    except preview_manager.PreviewOwnershipError:
        # Cố ý trả 404 giống hệt "không tồn tại" thay vì 403 — KHÔNG lộ
        # cho biết preview_id này có tồn tại (thuộc người khác) hay
        # không (Requirement 8.5: prevent access to preview_ids created
        # by other users).
        raise HTTPException(status_code=404, detail="Preview không tồn tại.")
    except preview_manager.PreviewExpiredError:
        raise HTTPException(status_code=410, detail="Preview expired, please re-upload file")
