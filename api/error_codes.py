"""
Hằng số error_code dùng xuyên suốt API layer (api/routers/*.py,
api/auth.py, api/deps.py) — Giai đoạn 1 kế hoạch i18n (09/2026).

MỤC ĐÍCH: mỗi raise HTTPException(detail={"error_code": ..., "message": ...})
dùng 1 hằng số ở đây thay vì gõ tay chuỗi rải rác — tránh gõ sai/trùng
giữa các router, và là single source of truth để Next.js (Giai đoạn 3)
tra bảng dịch theo đúng error_code thay vì so khớp message tiếng Việt
(so khớp message là anti-pattern đã phát hiện ở Flask, xem
mindx-jobs: BackendAuthError.email_not_verified).

QUY ƯỚC ĐẶT TÊN: {domain}_{tình_huống}, domain lấy theo router
(contact, company, job, crawl, maintenance, message, profile,
audit_log, email_template, auth, user, meta, import) — đảm bảo
không trùng error_code giữa 2 router khác nhau dù tình huống giống hệt.
"""

# ---------------------------------------------------------------
# api/deps.py — lỗi xác thực token (đã có sẵn TRƯỚC Giai đoạn 1, giữ
# nguyên, liệt kê lại ở đây cho đủ bộ hằng số).
# ---------------------------------------------------------------
MISSING_AUTH_HEADER = "missing_auth_header"
SESSION_REPLACED = "session_replaced"
SESSION_REVOKED = "session_revoked"
TOKEN_EXPIRED = "token_expired"

# ---------------------------------------------------------------
# api/auth.py
# ---------------------------------------------------------------
AUTH_SERVER_CHUA_CAU_HINH_API = "auth_server_chua_cau_hinh_api"
AUTH_MISSING = "auth_missing"

# ---------------------------------------------------------------
# api/routers/audit_logs.py
# ---------------------------------------------------------------
AUDIT_LOG_ENTITY_TYPE_INVALID = "audit_log_entity_type_invalid"
AUDIT_LOG_ACTION_TYPE_INVALID = "audit_log_action_type_invalid"
AUDIT_LOG_COMPANY_ID_INVALID_UUID = "audit_log_company_id_invalid_uuid"
AUDIT_LOG_ACTOR_ID_INVALID_UUID = "audit_log_actor_id_invalid_uuid"
AUDIT_LOG_LOG_ID_INVALID_UUID = "audit_log_log_id_invalid_uuid"
AUDIT_LOG_LOG_NOT_FOUND = "audit_log_log_not_found"
AUDIT_LOG_NGUOI_THUC_HIEN_THAO_TAC = "audit_log_nguoi_thuc_hien_thao_tac"
AUDIT_LOG_REQUIRED = "audit_log_required"

# ---------------------------------------------------------------
# api/routers/auth_registration.py
# ---------------------------------------------------------------
AUTH_EMAIL_TAI_KHOAN = "auth_email_tai_khoan"
AUTH_INVALID = "auth_invalid"
AUTH_EXPIRED = "auth_expired"

# ---------------------------------------------------------------
# api/routers/auth_session.py
# ---------------------------------------------------------------
AUTH_WRONG_CREDENTIALS = "auth_wrong_credentials"
AUTH_DEACTIVATED = "auth_deactivated"
AUTH_EMAIL_NOT_VERIFIED = "auth_email_not_verified"
AUTH_LOCKED = "auth_locked"
AUTH_LOCKED_2 = "auth_locked_2"
AUTH_INVALID_2 = "auth_invalid_2"
AUTH_REFRESH_TOKEN_THU_HOI_TRUOC = "auth_refresh_token_thu_hoi_truoc"
AUTH_EXPIRED_2 = "auth_expired_2"
AUTH_DEACTIVATED_2 = "auth_deactivated_2"
AUTH_ACCOUNT_NOT_FOUND = "auth_account_not_found"
AUTH_MAT_KHAU_CU_DUNG = "auth_mat_khau_cu_dung"

# ---------------------------------------------------------------
# api/routers/auth_users.py
# ---------------------------------------------------------------
USER_ROLE_1_USER_SS_TEAM = "user_role_1_user_ss_team"
USER_EMAIL_TAI_KHOAN = "user_email_tai_khoan"
USER_SS_USER_ID_INVALID_UUID = "user_ss_user_id_invalid_uuid"
USER_ACCOUNT_NOT_FOUND = "user_account_not_found"
USER_FORBIDDEN = "user_forbidden"
USER_FORBIDDEN_2 = "user_forbidden_2"

# ---------------------------------------------------------------
# api/routers/companies.py
# ---------------------------------------------------------------
COMPANY_CREATED_BY_INVALID_UUID = "company_created_by_invalid_uuid"
COMPANY_COMPANY_ID_INVALID_UUID = "company_company_id_invalid_uuid"
COMPANY_COMPANY_NOT_FOUND = "company_company_not_found"
COMPANY_MA_SO_THUE_DUNG_BOI = "company_ma_so_thue_dung_boi"

# ---------------------------------------------------------------
# api/routers/contacts.py
# ---------------------------------------------------------------
CONTACT_ASSIGNED_SS_USER_INVALID_UUID = "contact_assigned_ss_user_invalid_uuid"
CONTACT_ASSIGNED_USER_NOT_FOUND = "contact_assigned_user_not_found"
CONTACT_ASSIGNED_SS_USER_TAI_KHOAN = "contact_assigned_ss_user_tai_khoan"
CONTACT_CONTACT_STATUS_INVALID = "contact_contact_status_invalid"
CONTACT_COMPANY_ID_INVALID_UUID = "contact_company_id_invalid_uuid"
CONTACT_COMPANY_NOT_FOUND = "contact_company_not_found"
CONTACT_CREATED_BY_INVALID_UUID = "contact_created_by_invalid_uuid"
CONTACT_CONTACT_ID_INVALID_UUID = "contact_contact_id_invalid_uuid"
CONTACT_COMPANY_NOT_FOUND_2 = "contact_company_not_found_2"
CONTACT_REQUIRED = "contact_required"
CONTACT_CONTACT_NOT_FOUND = "contact_contact_not_found"
CONTACT_REQUIRED_2 = "contact_required_2"
CONTACT_STILL_ACTIVE = "contact_still_active"
CONTACT_HAS_LINKS = "contact_has_links"

# ---------------------------------------------------------------
# api/routers/crawl.py
# ---------------------------------------------------------------
CRAWL_NOT_FOUND = "crawl_not_found"
CRAWL_NOT_FOUND_2 = "crawl_not_found_2"
CRAWL_ALREADY_ACTIVE = "crawl_already_active"
CRAWL_NOT_FOUND_3 = "crawl_not_found_3"
CRAWL_STATUS_INVALID = "crawl_status_invalid"
CRAWL_TRIGGERED_BY_INVALID_UUID = "crawl_triggered_by_invalid_uuid"
CRAWL_BATCH_ID_INVALID_UUID = "crawl_batch_id_invalid_uuid"
CRAWL_BATCH_NOT_FOUND = "crawl_batch_not_found"
CRAWL_RUN_ID_INVALID_UUID = "crawl_run_id_invalid_uuid"
CRAWL_RUN_NOT_FOUND = "crawl_run_not_found"

# ---------------------------------------------------------------
# api/routers/email_templates.py
# ---------------------------------------------------------------
EMAIL_TEMPLATE_TEMPLATE_ID_INVALID_UUID = "email_template_template_id_invalid_uuid"
EMAIL_TEMPLATE_TEMPLATE_NOT_FOUND = "email_template_template_not_found"
EMAIL_TEMPLATE_REQUIRED = "email_template_required"

# ---------------------------------------------------------------
# api/routers/import_export.py
# ---------------------------------------------------------------
IMPORT_ENTITY_TYPE_INVALID = "import_entity_type_invalid"
IMPORT_ENTITY_TYPE_FILTER_STATUS = "import_entity_type_filter_status"
IMPORT_STATUS_INVALID = "import_status_invalid"
IMPORT_ENTITY_TYPE_JOB_FILTER_IS = "import_entity_type_job_filter_is"
IMPORT_ENTITY_TYPE_COMPANY_FILTER_COMPANY = "import_entity_type_company_filter_company"
IMPORT_DATE_FIELD_NHAN_CREATED_AT = "import_date_field_nhan_created_at"
IMPORT_FROM_DATE_TO_DATE = "import_from_date_to_date"
IMPORT_LIMIT_0 = "import_limit_0"
IMPORT_UNSUPPORTED_FILE_FORMAT_PLEASE_UPLOAD = "import_unsupported_file_format_please_upload"
IMPORT_FILE_EXCEEDS_MAXIMUM_OF_5000 = "import_file_exceeds_maximum_of_5000"
IMPORT_ROW_INDEX_PREVIEW = "import_row_index_preview"
IMPORT_PREVIEW_ID_THUOC_ENTITY_TYPE = "import_preview_id_thuoc_entity_type"
IMPORT_FIELD_VERIFY_FAILED = "import_field_verify_failed"
IMPORT_ENTITY_TYPE_BUOC_CHON_CONG = "import_entity_type_buoc_chon_cong"
IMPORT_RESOLVE_COMPANY_FAILED = "import_resolve_company_failed"
IMPORT_ROW_RESOLUTION_FAILED = "import_row_resolution_failed"
IMPORT_INTERNAL_ERROR = "import_internal_error"
IMPORT_PREVIEW_ID_INVALID_UUID = "import_preview_id_invalid_uuid"
IMPORT_NOT_FOUND = "import_not_found"
IMPORT_PREVIEW_EXPIRED_PLEASE_RE_UPLOAD = "import_preview_expired_please_re_upload"

# ---------------------------------------------------------------
# api/routers/jobs.py
# ---------------------------------------------------------------
JOB_CREATED_BY_INVALID_UUID = "job_created_by_invalid_uuid"
JOB_JOB_ID_INVALID_UUID = "job_job_id_invalid_uuid"
JOB_JOB_NOT_FOUND = "job_job_not_found"
JOB_COMPANY_ID_INVALID_UUID = "job_company_id_invalid_uuid"
JOB_COMPANY_NOT_FOUND = "job_company_not_found"

# ---------------------------------------------------------------
# api/routers/maintenance.py
# ---------------------------------------------------------------
MAINTENANCE_JOB_NOT_FOUND = "maintenance_job_not_found"
MAINTENANCE_REQUIRED = "maintenance_required"
MAINTENANCE_DRY_RUN_CHECK_DEADLINE_ONLY = "maintenance_dry_run_check_deadline_only"
MAINTENANCE_ALREADY_ACTIVE = "maintenance_already_active"
MAINTENANCE_STATUS_INVALID = "maintenance_status_invalid"
MAINTENANCE_TRIGGERED_BY_INVALID_UUID = "maintenance_triggered_by_invalid_uuid"
MAINTENANCE_RUN_IDS_MUC_AFTER_IDS = "maintenance_run_ids_muc_after_ids"
MAINTENANCE_RUN_ID_INVALID_UUID = "maintenance_run_id_invalid_uuid"
MAINTENANCE_AFTER_ID_UNG_RUN_ID = "maintenance_after_id_ung_run_id"
MAINTENANCE_RUN_NOT_FOUND = "maintenance_run_not_found"

# ---------------------------------------------------------------
# api/routers/me.py
# ---------------------------------------------------------------
PROFILE_JOB_ID_INVALID_UUID = "profile_job_id_invalid_uuid"
PROFILE_JOB_NOT_FOUND = "profile_job_not_found"
PROFILE_JOB_TRANG_THAI_UNG_TUYEN = "profile_job_trang_thai_ung_tuyen"
PROFILE_CHAP_NHAN_FILE_CV_DINH = "profile_chap_nhan_file_cv_dinh"
PROFILE_DUNG_LUONG_FILE_CV_TOI = "profile_dung_luong_file_cv_toi"
PROFILE_BAN_UNG_TUYEN_JOB_ROI = "profile_ban_ung_tuyen_job_roi"
PROFILE_CV_UPLOAD_FAILED = "profile_cv_upload_failed"
PROFILE_INVALID = "profile_invalid"
PROFILE_HOC_VIEN_CHUA_NOP_CV = "profile_hoc_vien_chua_nop_cv"
PROFILE_CANNOT_CREATE = "profile_cannot_create"
PROFILE_BAN_CHUA_UNG_TUYEN_JOB = "profile_ban_chua_ung_tuyen_job"
PROFILE_JOB_LUU_ROI = "profile_job_luu_roi"
PROFILE_JOB_CHUA_LUU = "profile_job_chua_luu"

# ---------------------------------------------------------------
# api/routers/messages.py
# ---------------------------------------------------------------
MESSAGE_FORBIDDEN = "message_forbidden"
MESSAGE_NOT_FOUND = "message_not_found"
MESSAGE_HOC_VIEN_NHAN_TIN_HOC = "message_hoc_vien_nhan_tin_hoc"
MESSAGE_BAN_QUA_NHIEU_YEU_CAU = "message_ban_qua_nhieu_yeu_cau"
MESSAGE_BAN_CHAN_NHAN_TIN_NGUOI = "message_ban_chan_nhan_tin_nguoi"
MESSAGE_YEU_CAU_NHAN_TIN_SS = "message_yeu_cau_nhan_tin_ss"
MESSAGE_YEU_CAU_TRUOC_CHOI_VUI = "message_yeu_cau_truoc_choi_vui"
MESSAGE_BAN_CHAN_HOC_VIEN_BAM = "message_ban_chan_hoc_vien_bam"
MESSAGE_SS_ADMIN_MOI_XEM_DANH = "message_ss_admin_moi_xem_danh"
MESSAGE_INVALID = "message_invalid"
MESSAGE_HOC_VIEN_MOI_HUY_YEU = "message_hoc_vien_moi_huy_yeu"
MESSAGE_NOT_FOUND_2 = "message_not_found_2"
MESSAGE_YEU_CAU_VUA_XU_LY = "message_yeu_cau_vua_xu_ly"
MESSAGE_SS_ADMIN_MOI_QUYEN_CHAP = "message_ss_admin_moi_quyen_chap"
MESSAGE_NOT_FOUND_3 = "message_not_found_3"
MESSAGE_SS_ADMIN_MOI_QUYEN_CHOI = "message_ss_admin_moi_quyen_choi"
MESSAGE_NOT_FOUND_4 = "message_not_found_4"
MESSAGE_SS_ADMIN_MOI_QUYEN_CHAN = "message_ss_admin_moi_quyen_chan"
MESSAGE_RELATIONSHIP_NOT_FOUND = "message_relationship_not_found"
MESSAGE_STUDENT_NOT_FOUND = "message_student_not_found"
MESSAGE_SS_ADMIN_MOI_QUYEN_BO = "message_ss_admin_moi_quyen_bo"
MESSAGE_RELATIONSHIP_NOT_FOUND_2 = "message_relationship_not_found_2"

