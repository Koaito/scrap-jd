import psycopg2.errors
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request

import db as db_module
from api.deps import get_db, require_role
from api.rate_limit import limiter
from api.schemas import (
    CompanyCreate,
    CompanyDataHealth,
    CompanyDeleteRequest,
    CompanyDetailOut,
    CompanyOut,
    CompanyUpdate,
    PaginatedCompanies,
    PartnershipSignals,
)

router = APIRouter(prefix="/companies", tags=["companies"])


@router.get("", response_model=PaginatedCompanies)
@limiter.limit("60/minute")
def list_companies(
    request: Request,
    keyword: Optional[str] = Query(None, description="Tìm trong company_name"),
    province: Optional[str] = Query(None, description="Lọc theo tên tỉnh/thành"),
    has_social: Optional[bool] = Query(
        None,
        description="true = chỉ công ty đã có fanpage/linkedin; "
                    "false = chỉ công ty còn thiếu cả hai (ứng viên cho "
                    "get_company_fb_linkedin_link.py)",
    ),
    created_by: Optional[str] = Query(
        None, description="Lọc công ty do 1 thành viên ss_team/admin cụ thể TỰ THÊM TAY (ss_user_id) — công ty crawl tự động (created_by NULL trong DB) không bao giờ khớp filter này."
    ),
    include_inactive: bool = Query(
        False, description="true = xem cả công ty đã xoá mềm qua DELETE /companies/{id} (mặc định ẩn)"
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    conn=Depends(get_db),
):
    """Rate limit 60/minute theo IP (thêm 08/2026) — cùng lý do với
    GET /jobs (xem api/routers/jobs.py::list_jobs)."""
    if created_by is not None and not db_module.is_valid_uuid(created_by):
        raise HTTPException(status_code=400, detail=f"created_by '{created_by}' không đúng định dạng UUID.")
    rows, total = db_module.list_companies(
        conn, keyword=keyword, has_social=has_social, province_name=province,
        created_by=created_by, include_inactive=include_inactive,
        limit=limit, offset=offset,
    )
    return PaginatedCompanies(total=total, limit=limit, offset=offset, items=rows)


@router.get("/partnership-signals", response_model=dict[str, PartnershipSignals])
@limiter.limit("60/minute")
def get_partnership_signals(
    request: Request,
    company_id: Optional[list[str]] = Query(
        None,
        description="Lọc theo 1 hoặc nhiều company_id (?company_id=a&company_id=b). "
                    "Không truyền = tính cho TOÀN BỘ công ty trong DB.",
    ),
    conn=Depends(get_db),
):
    """GET /companies/partnership-signals — thay thế cho việc frontend
    (blueprints/companies.py bên mindx-jobs) từng phải gọi
    list_all_jobs() + list_all_contacts() (kéo TOÀN BỘ job/contact về
    Flask rồi tự group bằng Python, tốn 1 chuỗi round-trip tuần lệ tỉ
    lệ thuận với số job/contact) chỉ để tính gợi ý "Tiềm năng hợp tác"
    (potential_score.suggest_partnership_potential()) ngay trên bảng
    danh sách công ty. Route này tính sẵn 2 tín hiệu cần join
    (has_open_entry_job, matches_target_industry, has_responded) bằng
    SQL GROUP BY (xem db.get_partnership_signals()) — chi phí không
    tăng theo tổng số job/contact toàn hệ thống nữa, chỉ phụ thuộc số
    company_id được lọc (hoặc size DB nếu không lọc, nhưng vẫn RẺ HƠN
    NHIỀU so với kéo full object vì GROUP BY chạy trong Postgres, có
    index company_id, không serialize/deserialize qua network 2 lần).

    PHẢI khai báo route này TRƯỚC GET /{company_id} bên dưới — nếu đặt
    sau, FastAPI sẽ khớp "/companies/partnership-signals" vào path
    param company_id của route đó trước (path cố định luôn ưu tiên hơn
    nhưng thứ tự khai báo trong FastAPI vẫn theo trên-xuống-dưới, xem
    docs Starlette routing), rồi 400 vì "partnership-signals" không phải
    UUID hợp lệ."""
    for cid in company_id or []:
        if not db_module.is_valid_uuid(cid):
            raise HTTPException(status_code=400, detail=f"company_id '{cid}' không đúng định dạng UUID.")
    return db_module.get_partnership_signals(conn, company_ids=company_id)


@router.get("/data-health", response_model=CompanyDataHealth)
@limiter.limit("60/minute")
def get_company_data_health(
    request: Request,
    conn=Depends(get_db),
    user: dict = Depends(require_role("ss_team")),
):
    """GET /companies/data-health — thay thế cho việc frontend
    (blueprints/crawl_status.py bên mindx-jobs, tab "Tình trạng dữ
    liệu") từng phải gọi list_all_companies() + list_all_contacts()
    (kéo TOÀN BỘ company/contact về Flask rồi tự đếm field rỗng bằng
    Python) chỉ để vẽ 2 khối "Company thiếu dữ liệu theo từng trường" +
    "Company chưa có contact". Route này tính sẵn bằng SQL (xem
    db.get_company_data_health()) — chi phí không tăng theo tổng số
    company/contact toàn hệ thống nữa.

    require_role("ss_team") (khác /companies, /companies/{id} — GET
    công khai cho mọi role) vì route này phải JOIN qua company_contacts
    để đếm "chưa có contact" — cùng lý do contacts.py TOÀN BỘ route yêu
    cầu ss_team (thông tin liên hệ nhạy cảm).

    PHẢI khai báo route này TRƯỚC GET /{company_id} bên dưới — cùng lý
    do đã giải thích ở /partnership-signals phía trên (path cố định vs
    path param, thứ tự khai báo trong FastAPI theo trên-xuống-dưới)."""
    return db_module.get_company_data_health(conn)


@router.get("/{company_id}", response_model=CompanyDetailOut)
def get_company(company_id: str, conn=Depends(get_db)):
    if not db_module.is_valid_uuid(company_id):
        raise HTTPException(status_code=400, detail=f"company_id '{company_id}' không đúng định dạng UUID.")
    row = db_module.get_company_by_id(conn, company_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy công ty")
    jobs = db_module.get_jobs_by_company_id(conn, company_id)
    return {**row, "jobs": jobs}


@router.post("", response_model=CompanyOut, status_code=201)
def create_company(
    payload: CompanyCreate,
    conn=Depends(get_db),
    user: dict = Depends(require_role("ss_team")),
):
    """Tạo công ty THỦ CÔNG — dùng trước POST /jobs khi công ty chưa có
    trong DB (GET /companies?keyword= tìm không ra). Nếu tax_id điền vào
    trùng với công ty đã crawl trước đó, tự động DÙNG LẠI company đã có
    (không tạo trùng) — xem docstring db.get_or_create_company_by_profile().

    Trả về company đầy đủ (kể cả khi thực ra là company đã có sẵn từ
    trước do trùng tax_id) — frontend luôn dùng company_id trong response
    này cho bước tạo job tiếp theo, không giả định trùng ID với request.

    BẮT BUỘC đăng nhập VÀ role 'ss_team' trở lên (đổi từ chỉ-cần-đăng-nhập,
    08/2026, xem sql/migration_add_role_hierarchy.sql) — ghi lại
    companies.created_by (nếu company MỚI tạo) và updated_by (kể cả khi
    trùng company đã có, đang vá thêm thông tin)."""
    # get_or_create_company_by_profile() là IDEMPOTENT (dùng LẠI company
    # đã có nếu trùng tax_id/tên — xem docstring) — kiểm tra trùng TRƯỚC
    # bằng đúng logic nó dùng bên trong (tax_id trước, tên sau) để biết
    # company trả về là MỚI hay TÁI SỬ DỤNG, tránh log CREATE_COMPANY sai
    # cho trường hợp thực ra chỉ đang "vá" thêm thông tin công ty cũ.
    was_existing = (
        (payload.tax_id and db_module.find_company_by_tax_id(conn, payload.tax_id))
        or db_module.find_company_probe(conn, payload.company_name)
    ) is not None

    province_id = db_module.get_or_create_province(conn, payload.province_name or "")
    company_id = db_module.get_or_create_company_by_profile(
        conn, payload.company_name, province_id, tax_id=payload.tax_id or "",
        created_by=user["sub"],
    )
    db_module.update_company_profile(
        conn, company_id,
        tax_id=payload.tax_id or "",
        website=payload.website or "",
        industry=payload.industry or "",
        company_size=payload.company_size or "",
        address=payload.address or "",
        partnership_potential=payload.partnership_potential or "",
        updated_by=user["sub"],
    )
    if payload.fanpage_url or payload.linkedin_url:
        db_module.update_company_social_links(
            conn, company_id,
            fanpage_url=payload.fanpage_url or "",
            linkedin_url=payload.linkedin_url or "",
        )

    if not was_existing:
        # CREATE_COMPANY không thuộc log thủ công, không cần note (xem
        # db.ACTION_LOG_RULES) — company "vá thêm thông tin" (trùng
        # tax_id/tên) không log gì ở route này, khác PATCH /companies
        # (route riêng, có UPDATE_COMPANY optional-note của chính nó).
        db_module.log_action(
            conn, actor_id=user["sub"], action_type="CREATE_COMPANY",
            entity_type="COMPANY", entity_id=company_id,
            entity_label=payload.company_name, company_id=company_id,
        )

    conn.commit()

    row = db_module.get_company_by_id(conn, company_id)
    return row


@router.patch("/{company_id}", response_model=CompanyDetailOut)
def patch_company(
    company_id: str,
    payload: CompanyUpdate,
    conn=Depends(get_db),
    user: dict = Depends(require_role("ss_team")),
):
    """Sửa TỰ DO các field của 1 company đã tồn tại (thêm 08/2026, xem
    lịch sử trao đổi). Chỉ field có mặt trong body mới bị ghi đè, field
    không gửi giữ nguyên giá trị cũ — giống PATCH /jobs/{id}.

    KHÔNG có endpoint DELETE — company chưa có is_active/soft-delete
    (việc này để sau, xem lịch sử trao đổi).

    BẮT BUỘC đăng nhập VÀ role 'ss_team' trở lên, giống POST /companies —
    ghi lại companies.updated_by = người vừa sửa."""
    if not db_module.is_valid_uuid(company_id):
        raise HTTPException(status_code=400, detail=f"company_id '{company_id}' không đúng định dạng UUID.")

    existing = db_module.get_company_by_id(conn, company_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy công ty")

    province_id = (
        db_module.get_or_create_province(conn, payload.province_name)
        if payload.province_name is not None else None
    )

    try:
        updated = db_module.patch_company_profile(
            conn, company_id,
            company_name=payload.company_name,
            tax_id=payload.tax_id,
            website=payload.website,
            industry=payload.industry,
            company_size=payload.company_size,
            address=payload.address,
            province_id=province_id,
            fanpage_url=payload.fanpage_url,
            linkedin_url=payload.linkedin_url,
            partnership_potential=payload.partnership_potential,
            updated_by=user["sub"],
        )
    except psycopg2.errors.UniqueViolation:
        conn.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Mã số thuế '{payload.tax_id}' đã được dùng bởi công ty khác.",
        )

    if not updated:
        raise HTTPException(status_code=404, detail="Không tìm thấy công ty")

    # payload_fields: CHỈ field client thực sự gửi lên. province_name so
    # sánh riêng bằng tên hiển thị (existing['province_name'] là chuỗi,
    # không phải id) — giống pattern PATCH /jobs.
    payload_fields = payload.model_dump(exclude_unset=True, exclude={"note", "province_name"})
    if payload.province_name is not None:
        payload_fields["province_name"] = payload.province_name

    if payload_fields:
        changes = db_module.diff_changed_fields(existing, payload_fields)
        if changes:
            # UPDATE_COMPANY — note TUỲ CHỌN (khác PATCH /contacts, khác
            # DELETE /companies bên dưới), xem db.ACTION_LOG_RULES.
            db_module.log_action(
                conn, actor_id=user["sub"], action_type="UPDATE_COMPANY",
                entity_type="COMPANY", entity_id=company_id,
                entity_label=existing["company_name"], company_id=company_id,
                changes=changes, note=payload.note,
            )

    conn.commit()

    row = db_module.get_company_by_id(conn, company_id)
    jobs = db_module.get_jobs_by_company_id(conn, company_id)
    return {**row, "jobs": jobs}


@router.delete("/{company_id}", status_code=204)
def delete_company(
    company_id: str,
    payload: CompanyDeleteRequest,
    conn=Depends(get_db),
    user: dict = Depends(require_role("ss_team")),
):
    """Xoá MỀM company (is_active=false) — thêm 08/2026, xem
    sql/migration_add_company_soft_delete.sql. Trước route này, company
    KHÔNG có cách xoá nào.

    note BẮT BUỘC (khác PATCH /companies) — DELETE_COMPANY thuộc nhóm
    action bị CHẶN CỨNG nếu thiếu note (xem db.ACTION_LOG_RULES): thiếu
    note -> 422 ngay từ Pydantic (CompanyDeleteRequest.note không có
    default, FastAPI tự trả 422 nếu body thiếu field/rỗng), KHÔNG chạm
    tới DB.

    Gọi lại nhiều lần trên company đã xoá vẫn trả 204, KHÔNG lỗi, nhưng
    KHÔNG ghi thêm log mới (xem db.soft_delete_company() — trả False
    nếu company đã is_active=false từ trước, tránh log trùng lặp)."""
    if not db_module.is_valid_uuid(company_id):
        raise HTTPException(status_code=400, detail=f"company_id '{company_id}' không đúng định dạng UUID.")

    existing = db_module.get_company_by_id(conn, company_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy công ty")

    was_active = db_module.soft_delete_company(conn, company_id, updated_by=user["sub"])
    if was_active:
        db_module.log_action(
            conn, actor_id=user["sub"], action_type="DELETE_COMPANY",
            entity_type="COMPANY", entity_id=company_id,
            entity_label=existing["company_name"], company_id=company_id,
            note=payload.note,
        )
    conn.commit()
    return None
