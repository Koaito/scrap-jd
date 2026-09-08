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

CƠ CHẾ TEMPLATE BIẾN SỐ (09/2026, đợt 1/2 — chỉ áp dụng cho error_code
CHỈ có đúng 1 giá trị runtime chèn vào message qua f-string; nhóm có
≥2 giá trị để dành đợt 2): các raise HTTPException của nhóm này giờ có
thêm field "params": {"value": <biến gốc>} cạnh "error_code"/"message",
VD:
    detail={
        "error_code": error_codes.JOB_JOB_ID_INVALID_UUID,
        "message": f"job_id '{job_id}' không đúng định dạng UUID.",
        "params": {"value": job_id},
    }
"message" vẫn giữ nguyên y hệt như trước (không đổi hành vi cho
locale=vi, vẫn dùng thẳng "message"). "params" chỉ được Next.js
(Giai đoạn 3) dùng khi locale=en VÀ error_code đã có template dạng
"{value}" trong errors.en.json — nếu chưa có, "params" bị bỏ qua vô
hại, fallback về "message" gốc như cũ.
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
AUTH_SERVER_MISSING_API_KEY = "auth_server_chua_cau_hinh_api"
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
AUDIT_LOG_ONLY_ACTOR_CAN_EDIT_NOTE = "audit_log_nguoi_thuc_hien_thao_tac"
AUDIT_LOG_REQUIRED = "audit_log_required"

# ---------------------------------------------------------------
# api/routers/auth_registration.py
# ---------------------------------------------------------------
AUTH_EMAIL_ALREADY_REGISTERED = "auth_email_tai_khoan"
AUTH_INVALID = "auth_invalid"
AUTH_EXPIRED = "auth_expired"

# ---------------------------------------------------------------
# api/routers/auth_session.py
# ---------------------------------------------------------------
AUTH_WRONG_CREDENTIALS = "auth_wrong_credentials"
AUTH_DEACTIVATED = "auth_deactivated"
AUTH_EMAIL_NOT_VERIFIED = "auth_email_not_verified"
AUTH_LOCKED = "auth_locked"
AUTH_TOO_MANY_FAILED_ATTEMPTS = "auth_locked_2"
AUTH_REFRESH_TOKEN_INVALID = "auth_invalid_2"
AUTH_REFRESH_TOKEN_ALREADY_REVOKED = "auth_refresh_token_thu_hoi_truoc"
AUTH_REFRESH_TOKEN_EXPIRED = "auth_expired_2"
AUTH_ACCOUNT_INACTIVE = "auth_deactivated_2"
AUTH_ACCOUNT_NOT_FOUND = "auth_account_not_found"
AUTH_OLD_PASSWORD_INCORRECT = "auth_mat_khau_cu_dung"

# ---------------------------------------------------------------
# api/routers/auth_users.py
# ---------------------------------------------------------------
USER_ROLE_INVALID = "user_role_1_user_ss_team"
USER_EMAIL_ALREADY_REGISTERED = "user_email_tai_khoan"
USER_SS_USER_ID_INVALID_UUID = "user_ss_user_id_invalid_uuid"
USER_ACCOUNT_NOT_FOUND = "user_account_not_found"
USER_FORBIDDEN = "user_forbidden"
USER_CANNOT_MODIFY_SELF = "user_forbidden_2"

# ---------------------------------------------------------------
# api/routers/companies.py
# ---------------------------------------------------------------
COMPANY_CREATED_BY_INVALID_UUID = "company_created_by_invalid_uuid"
COMPANY_COMPANY_ID_INVALID_UUID = "company_company_id_invalid_uuid"
COMPANY_COMPANY_NOT_FOUND = "company_company_not_found"
COMPANY_TAX_ID_ALREADY_USED = "company_ma_so_thue_dung_boi"

# ---------------------------------------------------------------
# api/routers/contacts.py
# ---------------------------------------------------------------
CONTACT_ASSIGNED_SS_USER_INVALID_UUID = "contact_assigned_ss_user_invalid_uuid"
CONTACT_ASSIGNED_USER_NOT_FOUND = "contact_assigned_user_not_found"
CONTACT_ASSIGNED_USER_ROLE_INVALID = "contact_assigned_ss_user_tai_khoan"
CONTACT_STATUS_INVALID = "contact_contact_status_invalid"
CONTACT_COMPANY_ID_INVALID_UUID = "contact_company_id_invalid_uuid"
CONTACT_COMPANY_NOT_FOUND = "contact_company_not_found"
CONTACT_CREATED_BY_INVALID_UUID = "contact_created_by_invalid_uuid"
CONTACT_CONTACT_ID_INVALID_UUID = "contact_contact_id_invalid_uuid"
CONTACT_NOT_FOUND_IN_COMPANY = "contact_company_not_found_2"
CONTACT_REQUIRED = "contact_required"
CONTACT_CONTACT_NOT_FOUND = "contact_contact_not_found"
CONTACT_ASSIGNEE_REQUIRED = "contact_required_2"
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
IMPORT_DATE_FIELD_INVALID = "import_date_field_nhan_created_at"
IMPORT_FROM_DATE_TO_DATE = "import_from_date_to_date"
IMPORT_LIMIT_MUST_BE_POSITIVE = "import_limit_0"
IMPORT_FILE_FORMAT_UNSUPPORTED = "import_unsupported_file_format_please_upload"
IMPORT_FILE_ROW_LIMIT_EXCEEDED = "import_file_exceeds_maximum_of_5000"
IMPORT_ROW_INDEX_NOT_IN_PREVIEW = "import_row_index_not_in_preview"
IMPORT_PREVIEW_ENTITY_TYPE_MISMATCH = "import_preview_id_thuoc_entity_type"
IMPORT_FIELD_VERIFY_FAILED = "import_field_verify_failed"
IMPORT_ENTITY_TYPE_NO_COMPANY_STEP = "import_entity_type_no_company_step"
IMPORT_RESOLVE_COMPANY_FAILED = "import_resolve_company_failed"
IMPORT_ROW_RESOLUTION_FAILED = "import_row_resolution_failed"
IMPORT_INTERNAL_ERROR = "import_internal_error"
IMPORT_PREVIEW_ID_INVALID_UUID = "import_preview_id_invalid_uuid"
IMPORT_NOT_FOUND = "import_not_found"
IMPORT_PREVIEW_EXPIRED = "import_preview_expired_please_re_upload"

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
MAINTENANCE_DRY_RUN_JOB_TYPE_MISMATCH = "maintenance_dry_run_check_deadline_only"
MAINTENANCE_ALREADY_ACTIVE = "maintenance_already_active"
MAINTENANCE_STATUS_INVALID = "maintenance_status_invalid"
MAINTENANCE_TRIGGERED_BY_INVALID_UUID = "maintenance_triggered_by_invalid_uuid"
MAINTENANCE_RUN_IDS_AFTER_IDS_LENGTH_MISMATCH = "maintenance_run_ids_muc_after_ids"
MAINTENANCE_RUN_ID_INVALID_UUID = "maintenance_run_id_invalid_uuid"
MAINTENANCE_AFTER_ID_INVALID = "maintenance_after_id_ung_run_id"
MAINTENANCE_RUN_NOT_FOUND = "maintenance_run_not_found"

# ---------------------------------------------------------------
# api/routers/me.py
# ---------------------------------------------------------------
PROFILE_JOB_ID_INVALID_UUID = "profile_job_id_invalid_uuid"
PROFILE_JOB_NOT_FOUND = "profile_job_not_found"
PROFILE_JOB_STATUS_NOT_APPLICABLE = "profile_job_status_not_applicable"
PROFILE_CV_FORMAT_INVALID = "profile_chap_nhan_file_cv_dinh"
PROFILE_CV_FILE_TOO_LARGE = "profile_dung_luong_file_cv_toi"
PROFILE_ALREADY_APPLIED = "profile_ban_ung_tuyen_job_roi"
PROFILE_CV_UPLOAD_FAILED = "profile_cv_upload_failed"
PROFILE_INVALID = "profile_invalid"
PROFILE_CV_NOT_SUBMITTED = "profile_hoc_vien_chua_nop_cv"
PROFILE_CANNOT_CREATE = "profile_cannot_create"
PROFILE_NOT_APPLIED_YET = "profile_ban_chua_ung_tuyen_job"
PROFILE_JOB_ALREADY_SAVED = "profile_job_luu_roi"
PROFILE_JOB_NOT_SAVED = "profile_job_chua_luu"

# ---------------------------------------------------------------
# api/routers/messages.py
# ---------------------------------------------------------------
MESSAGE_FORBIDDEN = "message_forbidden"
MESSAGE_NOT_FOUND = "message_not_found"
MESSAGE_STUDENT_CANNOT_MESSAGE_STUDENT = "message_hoc_vien_nhan_tin_hoc"
MESSAGE_TOO_MANY_PENDING_REQUESTS = "message_too_many_pending_requests"
MESSAGE_BLOCKED_BY_RECIPIENT = "message_ban_chan_nhan_tin_nguoi"
MESSAGE_REQUEST_PENDING_SS_REVIEW = "message_yeu_cau_nhan_tin_ss"
MESSAGE_PREVIOUS_REQUEST_REJECTED_COOLDOWN = "message_previous_request_rejected_cooldown"
MESSAGE_YOU_BLOCKED_THIS_STUDENT = "message_ban_chan_hoc_vien_bam"
MESSAGE_ONLY_SS_ADMIN_CAN_VIEW_LIST = "message_ss_admin_moi_xem_danh"
MESSAGE_INVALID = "message_invalid"
MESSAGE_ONLY_STUDENT_CAN_CANCEL_REQUEST = "message_hoc_vien_moi_huy_yeu"
MESSAGE_NOT_FOUND_2 = "message_not_found_2"
MESSAGE_REQUEST_ALREADY_PROCESSED = "message_yeu_cau_vua_xu_ly"
MESSAGE_ONLY_SS_ADMIN_CAN_ACCEPT = "message_ss_admin_moi_quyen_chap"
MESSAGE_NOT_FOUND_3 = "message_not_found_3"
MESSAGE_ONLY_SS_ADMIN_CAN_REJECT = "message_ss_admin_moi_quyen_choi"
MESSAGE_NOT_FOUND_4 = "message_not_found_4"
MESSAGE_ONLY_SS_ADMIN_CAN_BLOCK = "message_ss_admin_moi_quyen_chan"
MESSAGE_RELATIONSHIP_NOT_FOUND = "message_relationship_not_found"
MESSAGE_STUDENT_NOT_FOUND = "message_student_not_found"
MESSAGE_ONLY_SS_ADMIN_CAN_UNBLOCK = "message_ss_admin_moi_quyen_bo"
MESSAGE_RELATIONSHIP_NOT_FOUND_2 = "message_relationship_not_found_2"

