"""
Import/Export — schema request/response cho luồng import CSV/XLSX
(POST /import/{entity_type}/preview, GET .../preview/{preview_id},
POST .../confirm, POST .../verify-field, POST .../resolve-company) +
gợi ý công ty (suggest-companies). Tách từ api/schemas.py (08/2026) —
xem docstring api/schemas/__init__.py.
"""

from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


# ------------------------------------------------------------------
# Import/Export — Company resolution schemas
# ------------------------------------------------------------------

class CompanySuggestionOut(BaseModel):
    """Gợi ý công ty tương tự cho import resolution — dùng trong
    GET /import/{entity_type}/preview/{preview_id}/rows/{row_index}/suggest-companies"""
    company_id: str
    company_name: str
    tax_id: Optional[str] = None
    is_active: bool
    similarity: float = Field(
        ..., 
        description="Độ tương đồng tên công ty (pg_trgm similarity, 0-1)"
    )


class CompanySuggestionsResponse(BaseModel):
    """Response wrapper cho danh sách gợi ý công ty"""
    suggestions: list[CompanySuggestionOut] = Field(default_factory=list)


class ImportUploadResponse(BaseModel):
    """Response cho POST /import/{entity_type}/preview và
    GET /import/{entity_type}/preview/{preview_id}
    
    Chứa preview_id, entity_type, summary (tổng hợp số dòng), 
    và rows (chi tiết từng dòng với trạng thái ready/needs_resolution)
    """
    preview_id: str
    entity_type: str
    summary: dict  # {"total_rows", "new_records", "conflicts", "conflicts_inactive",
                    #  "pending_company_resolution", "pending_level_resolution",
                    #  "id_field"} — xem
                    # api/services/preview_manager.py::build_preview() cho cấu
                    # trúc đầy đủ + comment đầu file (nguồn sự thật thật sự,
                    # dict comment ở đây chỉ để đọc lướt nhanh).
    rows: list[dict]  # Chi tiết từng dòng import


class RowResolution(BaseModel):
    """Resolution cho 1 dòng cần xử lý thủ công - dùng trong ImportConfirmRequest.

    Sửa 08/2026 (fix bug reactivate không hoạt động): action enum ở đây
    TRƯỚC ĐÂY là "create_new | use_existing | skip" và field
    "confirm_reactivate" hoàn toàn không tồn tại — trong khi
    api/services/import_executor.py (nơi thực thi thật) lại đọc
    action là "skip"|"create"|"update" và đọc resolution.get(
    "confirm_reactivate") cho dòng conflict_inactive (xem
    RowResolutionError docstring + dòng 121-124 file đó). Vì router
    convert RowResolution -> dict bằng .model_dump() (xem
    api/routers/import_export.py), field lạ "confirm_reactivate" gửi
    từ frontend bị Pydantic ÂM THẦM loại bỏ — nghĩa là flow "ghi đè +
    kích hoạt lại record inactive" không bao giờ chạy được, dù cả
    frontend lẫn import_executor.py đều đã code đúng phần của mình.
    Sửa lại enum + bổ sung field cho khớp đúng những gì
    import_executor.py thực sự đọc.

    Thêm 08/2026 (action lan truyền cho conflict_in_batch): dòng
    conflict_status="conflict_in_batch" (trùng với 1 dòng KHÁC trong
    CHÍNH file import, không phải DB — xem BatchDuplicateMatchOut) giờ
    nhận thêm 3 giá trị action, mỗi giá trị áp dụng cho CẢ CẶP 2 dòng
    trùng nhau CHỈ TỪ 1 resolution duy nhất (backend tự điền resolution
    cho dòng kia qua duplicate_in_batch.other_row_index — xem
    import_executor.BATCH_PROPAGATING_ACTIONS +
    _expand_conflict_in_batch_resolutions()):
      - "keep_this"   : giữ dòng đang gửi resolution này, bỏ dòng kia
                        (2 dòng là CÙNG 1 người, dòng này đúng).
      - "keep_other"  : ngược lại — bỏ dòng đang gửi, giữ dòng kia.
      - "import_both" : xác nhận 2 dòng là 2 người KHÁC NHAU, giữ cả 2.
    Vẫn có thể tiếp tục gửi resolution RIÊNG cho từng dòng bằng
    skip/create như trước (không bắt buộc dùng action lan truyền) — nếu
    gửi cả 2 kiểu cho 1 cặp mà mâu thuẫn nhau, backend raise 422 rõ
    nguyên nhân thay vì tự đoán."""
    model_config = ConfigDict(extra="forbid")
    
    action: str = Field(
        ..., 
        description="skip | create | update. Riêng dòng conflict_status="
                     "'conflict_in_batch' (thêm 08/2026, trùng với 1 dòng KHÁC "
                     "trong CHÍNH file import, không phải DB — xem "
                     "BatchDuplicateMatchOut): nhận skip/create như trên "
                     "(resolve RIÊNG từng dòng trong cặp) HOẶC 1 trong 3 action "
                     "LAN TRUYỀN 'keep_this'/'keep_other'/'import_both' (áp dụng "
                     "1 lần cho CẢ CẶP, chỉ cần gửi cho 1 trong 2 dòng — backend "
                     "tự điền resolution cho dòng kia, xem import_executor."
                     "BATCH_PROPAGATING_ACTIONS); 'update' không hợp lệ (không có "
                     "existing_record để update) và resolution cho dòng này là "
                     "BẮT BUỘC tường minh (trực tiếp hoặc do lan truyền từ dòng "
                     "kia), KHÔNG được để mặc định (xem "
                     "import_executor.execute_import)."
    )
    company_id: Optional[str] = Field(
        None, 
        description="Bắt buộc nếu dòng needs_company_resolve và action='create'/'update'"
    )
    confirm_reactivate: bool = Field(
        False,
        description="Bắt buộc =true nếu action='update' cho dòng conflict_status="
                     "'conflict_inactive' (ghi đè + kích hoạt lại record đã ngừng "
                     "hoạt động) — xem import_executor.RowResolutionError.",
    )
    level_code: Optional[str] = Field(
        None,
        description="Bắt buộc (1 trong LEVEL_CODE_VALUES — xem constants.py) nếu "
                     "dòng needs_level_resolve=true (Job, level_code trong file "
                     "không khớp danh sách hợp lệ dù đã chuẩn hoá hoa/thường) — "
                     "staff chọn lại qua dropdown tĩnh ở FE, xem "
                     "import_executor.RowResolutionError.",
    )
    field_fixes: Optional[dict[str, str]] = Field(
        None,
        description="Thêm 08/2026: map field_name -> giá trị staff đã sửa trực "
                     "tiếp trên bảng preview, BẮT BUỘC chứa đủ mọi field còn "
                     "trong needs_field_fix/field_errors của dòng này nếu "
                     "action != 'skip' (xem preview_manager.build_preview -> "
                     "entry['field_errors']). Giá trị LUÔN là string thô "
                     "(giống format trong file gốc, vd ngày 'YYYY-MM-DD') — "
                     "import_executor.py::_apply_field_fixes() re-validate lại "
                     "bằng đúng validate_single_field() dùng lúc build preview, "
                     "không tin ngầm dữ liệu FE gửi lên.",
    )


class FieldVerifyRequest(BaseModel):
    """Body cho POST /import/{entity_type}/preview/{preview_id}/rows/
    {row_index}/verify-field — staff sửa 1 ô trên bảng preview rồi bấm
    nút "Xác nhận" cạnh ô đó (thêm 08/2026, xem trao đổi thiết kế
    "cảnh báo trùng contact sau khi sửa field lỗi").

    Re-validate format field_name NGAY (dùng đúng validate_single_field()
    dùng lúc build preview) + với contact, re-check trùng mờ theo
    company_id + tối thiểu 1/3 trong (work_email, social_link,
    phone_number) — xem preview_manager.apply_field_fix()."""
    model_config = ConfigDict(extra="forbid")

    field_name: str = Field(..., description="Tên field vừa sửa, vd 'work_email'")
    value: str = Field(..., description="Giá trị mới (string thô, giống format trong file gốc)")


class DuplicateMatchOut(BaseModel):
    """Kết quả match mờ khi phát hiện dòng vừa sửa trùng với 1 contact đã
    có trong DB — chỉ có giá trị khi conflict_status chuyển sang
    "conflict" NGAY TẠI apply_field_fix() (khác conflict phát hiện lúc
    build preview ban đầu, vốn không có field này)."""
    match_score: float = Field(
        ..., description="Số cột khớp / 3 (0.33 / 0.67 / 1.0) — càng cao càng chắc trùng."
    )
    matched_fields: list[str] = Field(
        ..., description="Các cột khớp, trong (work_email, social_link, phone_number)."
    )


class BatchDuplicateMatchOut(BaseModel):
    """Kết quả match mờ khi phát hiện dòng vừa sửa trùng với 1 dòng KHÁC
    TRONG CHÍNH file đang import (thêm 08/2026) — khác DuplicateMatchOut
    ở trên vốn so với record ĐÃ CÓ trong DB. Xem conflict_detector.
    find_duplicate_rows_in_batch() + preview_manager.apply_field_fix().

    Chỉ có giá trị khi conflict_status của dòng chuyển "conflict_in_batch"
    — dòng này KHÔNG có existing_record thật (existing_record vẫn null),
    other_row_index mới là thứ FE cần để biết đang trùng với dòng nào
    trong bảng preview (không phải record DB). FE dùng other_row_index
    này để tự gửi resolution cho dòng kia theo cách cũ (skip/create
    riêng từng dòng), HOẶC (thêm 08/2026) chỉ cần gửi 1 resolution với
    action lan truyền (keep_this/keep_other/import_both — xem
    RowResolution.action) cho MỘT trong 2 dòng, backend tự áp dụng cho
    dòng kia."""
    match_score: float = Field(
        ..., description="Số cột khớp / 3 (0.33 / 0.67 / 1.0) — càng cao càng chắc trùng."
    )
    matched_fields: list[str] = Field(
        ..., description="Các cột khớp, trong (work_email, social_link, phone_number)."
    )
    other_row_index: int = Field(
        ...,
        description="row_index của dòng KIA trong CÙNG file bị phát hiện trùng "
                     "(khớp key trong preview_data['rows']) — FE dùng để "
                     "highlight/liên kết sang dòng đó trên bảng preview. Khi 1 "
                     "dòng nhận duplicate_in_batch mới, dòng có row_index này "
                     "CŨNG bị cập nhật duplicate_in_batch/conflict_status trong "
                     "preview đã lưu DB (xử lý 2 chiều), dù response của "
                     "verify-field chỉ trả về đúng dòng vừa sửa — FE cần tự "
                     "biết dòng kia cũng vừa đổi (vd tải lại preview nếu cần "
                     "hiển thị chính xác ngay lập tức)."
    )


class FieldVerifyResponse(BaseModel):
    """Response cho POST .../verify-field.

    field_error != None -> field vẫn KHÔNG hợp lệ sau khi sửa (giữ
    nguyên field_errors/needs_field_fix cũ trong preview, KHÔNG lưu gì
    mới) — FE hiện lỗi ngay tại ô, không cho staff tưởng đã xác nhận
    thành công.

    field_error == None -> đã lưu field mới vào preview. row trả về là
    TOÀN BỘ entry của dòng đó sau khi cập nhật (đúng cấu trúc 1 phần tử
    trong ImportUploadResponse.rows) — FE ghi đè PREVIEW_DATA[row_index]
    bằng row này, tự cập nhật lại UI (field_errors còn lại, conflict_status
    mới nếu có, duplicate_match nếu phát hiện trùng DB, duplicate_in_batch
    (xem BatchDuplicateMatchOut) nếu phát hiện trùng với 1 dòng KHÁC
    trong CHÍNH file — LƯU Ý: case duplicate_in_batch ảnh hưởng 2 CHIỀU,
    dòng other_row_index cũng vừa đổi trong preview đã lưu DB dù KHÔNG
    nằm trong response này)."""
    row: dict
    field_error: Optional[dict] = Field(
        None, description="{'rule','message'} nếu vẫn lỗi, None nếu đã hợp lệ và đã lưu."
    )


class ResolveCompanyRequest(BaseModel):
    """Body cho POST /import/{entity_type}/preview/{preview_id}/rows/
    {row_index}/resolve-company — staff chọn 1 công ty (hoặc "Tạo công ty
    mới") trong modal chọn công ty ở bước preview, cho dòng
    conflict_status="pending_company_resolution" (chỉ job/contact — xem
    api/services/preview_manager.py::resolve_company_selection()).

    Re-check conflict NGAY với company_id thật vừa chọn, thay vì để treo
    tới lúc confirm (xem trao đổi thiết kế "vấn đề 2 & 3", 08/2026)."""
    model_config = ConfigDict(extra="forbid")

    company_id: Optional[str] = Field(
        None,
        description="UUID công ty staff chọn trong danh sách gợi ý. "
        "None (hoặc '__new__') = staff xác nhận không công ty nào đúng, "
        "sẽ tạo công ty mới theo company_name trong file.",
    )


class ResolveCompanyResponse(BaseModel):
    """Response cho POST .../resolve-company — row trả về là TOÀN BỘ entry
    của dòng đó sau khi cập nhật (đúng cấu trúc 1 phần tử trong
    ImportUploadResponse.rows, cùng shape FieldVerifyResponse.row) — FE ghi
    đè PREVIEW_DATA[row_index] bằng row này rồi renderPage() lại."""
    row: dict


class ImportConfirmRequest(BaseModel):
    """Body cho POST /import/{entity_type}/confirm
    
    Staff xác nhận import sau khi đã xem preview và resolve các dòng cần xử lý
    """
    model_config = ConfigDict(extra="forbid")
    
    preview_id: str
    note: str = Field(
        ..., 
        min_length=1,
        description="Ghi chú về lần import này (bắt buộc cho audit log)"
    )
    resolutions: dict[str, RowResolution] = Field(
        default_factory=dict,
        description="Map row_index -> resolution cho các dòng needs_resolution"
    )


class ImportConfirmResult(BaseModel):
    """Response cho POST /import/{entity_type}/confirm
    
    Tổng kết số bản ghi đã tạo mới, cập nhật, và bỏ qua
    """
    created: int
    updated: int
    skipped: int
