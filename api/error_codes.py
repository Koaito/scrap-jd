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
AUTH_SERVER_MISSING_API_KEY = "auth_server_missing_api_key"
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
AUDIT_LOG_ONLY_ACTOR_CAN_EDIT_NOTE = "audit_log_only_actor_can_edit_note"
AUDIT_LOG_REQUIRED = "audit_log_required"

# ---------------------------------------------------------------
# api/routers/auth_registration.py
# ---------------------------------------------------------------
AUTH_EMAIL_ALREADY_REGISTERED = "auth_email_already_registered"
AUTH_INVALID = "auth_invalid"
AUTH_EXPIRED = "auth_expired"

# ---------------------------------------------------------------
# api/routers/auth_session.py
# ---------------------------------------------------------------
AUTH_WRONG_CREDENTIALS = "auth_wrong_credentials"
AUTH_DEACTIVATED = "auth_deactivated"
AUTH_EMAIL_NOT_VERIFIED = "auth_email_not_verified"
AUTH_LOCKED = "auth_locked"
AUTH_TOO_MANY_FAILED_ATTEMPTS = "auth_too_many_failed_attempts"
AUTH_REFRESH_TOKEN_INVALID = "auth_refresh_token_invalid"
AUTH_REFRESH_TOKEN_ALREADY_REVOKED = "auth_refresh_token_already_revoked"
AUTH_REFRESH_TOKEN_EXPIRED = "auth_refresh_token_expired"
AUTH_ACCOUNT_INACTIVE = "auth_account_inactive"
AUTH_ACCOUNT_NOT_FOUND = "auth_account_not_found"
AUTH_OLD_PASSWORD_INCORRECT = "auth_old_password_incorrect"
# CẬP NHẬT 09/2026 (Phần B audit i18n, phát hiện qua đối chiếu với FE
# job-posting): api/deps.py::require_role() raise HTTPException với
# detail=f"..." dạng STRING THUẦN, không có error_code/message/params
# như chuẩn còn lại — FE không tra bảng dịch được (không có error_code
# để tra), luôn hiện tiếng Việt bất kể locale. Thêm mã này để đồng bộ.
AUTH_INSUFFICIENT_ROLE = "auth_insufficient_role"

# ---------------------------------------------------------------
# api/routers/auth_users.py
# ---------------------------------------------------------------
USER_ROLE_INVALID = "user_role_invalid"
USER_EMAIL_ALREADY_REGISTERED = "user_email_already_registered"
USER_SS_USER_ID_INVALID_UUID = "user_ss_user_id_invalid_uuid"
USER_ACCOUNT_NOT_FOUND = "user_account_not_found"
USER_FORBIDDEN = "user_forbidden"
USER_CANNOT_MODIFY_SELF = "user_cannot_modify_self"

# ---------------------------------------------------------------
# api/routers/companies.py
# ---------------------------------------------------------------
COMPANY_CREATED_BY_INVALID_UUID = "company_created_by_invalid_uuid"
COMPANY_COMPANY_ID_INVALID_UUID = "company_company_id_invalid_uuid"
COMPANY_COMPANY_NOT_FOUND = "company_company_not_found"
COMPANY_TAX_ID_ALREADY_USED = "company_tax_id_already_used"

# ---------------------------------------------------------------
# api/routers/contacts.py
# ---------------------------------------------------------------
CONTACT_ASSIGNED_SS_USER_INVALID_UUID = "contact_assigned_ss_user_invalid_uuid"
CONTACT_ASSIGNED_USER_NOT_FOUND = "contact_assigned_user_not_found"
CONTACT_ASSIGNED_USER_ROLE_INVALID = "contact_assigned_user_role_invalid"
CONTACT_STATUS_INVALID = "contact_status_invalid"
CONTACT_COMPANY_ID_INVALID_UUID = "contact_company_id_invalid_uuid"
CONTACT_COMPANY_NOT_FOUND = "contact_company_not_found"
CONTACT_CREATED_BY_INVALID_UUID = "contact_created_by_invalid_uuid"
CONTACT_CONTACT_ID_INVALID_UUID = "contact_contact_id_invalid_uuid"
CONTACT_NOT_FOUND_IN_COMPANY = "contact_not_found_in_company"
CONTACT_REQUIRED = "contact_required"
CONTACT_CONTACT_NOT_FOUND = "contact_contact_not_found"
CONTACT_ASSIGNEE_REQUIRED = "contact_assignee_required"
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
IMPORT_DATE_FIELD_INVALID = "import_date_field_invalid"
IMPORT_FROM_DATE_TO_DATE = "import_from_date_to_date"
IMPORT_LIMIT_MUST_BE_POSITIVE = "import_limit_must_be_positive"
IMPORT_FILE_FORMAT_UNSUPPORTED = "import_file_format_unsupported"
IMPORT_FILE_ROW_LIMIT_EXCEEDED = "import_file_row_limit_exceeded"
IMPORT_ROW_INDEX_NOT_IN_PREVIEW = "import_row_index_not_in_preview"
IMPORT_PREVIEW_ENTITY_TYPE_MISMATCH = "import_preview_entity_type_mismatch"
IMPORT_FIELD_VERIFY_FAILED = "import_field_verify_failed"
IMPORT_ENTITY_TYPE_NO_COMPANY_STEP = "import_entity_type_no_company_step"
IMPORT_RESOLVE_COMPANY_FAILED = "import_resolve_company_failed"
IMPORT_ROW_RESOLUTION_FAILED = "import_row_resolution_failed"
IMPORT_INTERNAL_ERROR = "import_internal_error"
IMPORT_PREVIEW_ID_INVALID_UUID = "import_preview_id_invalid_uuid"
IMPORT_NOT_FOUND = "import_not_found"
IMPORT_PREVIEW_EXPIRED = "import_preview_expired"
# CẬP NHẬT 09/2026 (Phần B audit i18n): api/routers/import_export.py
# (bước validate file trước khi tạo preview) raise HTTPException với
# detail thiếu hẳn error_code — chỉ có "message" + "errors" (danh sách
# lỗi từng dòng). FE không tra bảng dịch được. Thêm mã này để đồng bộ;
# message tĩnh 100% (không có giá trị runtime chèn vào), không cần params.
IMPORT_ROW_VALIDATION_FAILED = "import_row_validation_failed"

# ---------------------------------------------------------------
# api/routers/jobs.py
# ---------------------------------------------------------------
JOB_CREATED_BY_INVALID_UUID = "job_created_by_invalid_uuid"
JOB_JOB_ID_INVALID_UUID = "job_job_id_invalid_uuid"
JOB_JOB_NOT_FOUND = "job_job_not_found"
JOB_COMPANY_ID_INVALID_UUID = "job_company_id_invalid_uuid"
JOB_COMPANY_NOT_FOUND = "job_company_not_found"
JOB_CURSOR_INVALID = "job_cursor_invalid"
JOB_CURSOR_WITH_OFFSET_NOT_ALLOWED = "job_cursor_with_offset_not_allowed"
JOB_IDS_INVALID_UUID = "job_ids_invalid_uuid"

# ---------------------------------------------------------------
# api/routers/maintenance.py
# ---------------------------------------------------------------
MAINTENANCE_JOB_NOT_FOUND = "maintenance_job_not_found"
MAINTENANCE_REQUIRED = "maintenance_required"
MAINTENANCE_DRY_RUN_JOB_TYPE_MISMATCH = "maintenance_dry_run_job_type_mismatch"
MAINTENANCE_ALREADY_ACTIVE = "maintenance_already_active"
MAINTENANCE_STATUS_INVALID = "maintenance_status_invalid"
MAINTENANCE_TRIGGERED_BY_INVALID_UUID = "maintenance_triggered_by_invalid_uuid"
MAINTENANCE_RUN_IDS_AFTER_IDS_LENGTH_MISMATCH = "maintenance_run_ids_after_ids_length_mismatch"
MAINTENANCE_RUN_ID_INVALID_UUID = "maintenance_run_id_invalid_uuid"
MAINTENANCE_AFTER_ID_INVALID = "maintenance_after_id_invalid"
MAINTENANCE_RUN_NOT_FOUND = "maintenance_run_not_found"

# ---------------------------------------------------------------
# api/routers/me.py
# ---------------------------------------------------------------
PROFILE_JOB_ID_INVALID_UUID = "profile_job_id_invalid_uuid"
PROFILE_JOB_NOT_FOUND = "profile_job_not_found"
PROFILE_JOB_STATUS_NOT_APPLICABLE = "profile_job_status_not_applicable"
PROFILE_CV_FORMAT_INVALID = "profile_cv_format_invalid"
PROFILE_CV_FILE_TOO_LARGE = "profile_cv_file_too_large"
PROFILE_ALREADY_APPLIED = "profile_already_applied"
PROFILE_CV_UPLOAD_FAILED = "profile_cv_upload_failed"
PROFILE_INVALID = "profile_invalid"
PROFILE_CV_NOT_SUBMITTED = "profile_cv_not_submitted"
PROFILE_CANNOT_CREATE = "profile_cannot_create"
PROFILE_NOT_APPLIED_YET = "profile_not_applied_yet"
PROFILE_JOB_ALREADY_SAVED = "profile_job_already_saved"
PROFILE_JOB_NOT_SAVED = "profile_job_not_saved"

# ---------------------------------------------------------------
# api/routers/messages.py
# ---------------------------------------------------------------
MESSAGE_FORBIDDEN = "message_forbidden"
MESSAGE_NOT_FOUND = "message_not_found"
MESSAGE_STUDENT_CANNOT_MESSAGE_STUDENT = "message_student_cannot_message_student"
MESSAGE_TOO_MANY_PENDING_REQUESTS = "message_too_many_pending_requests"
MESSAGE_BLOCKED_BY_RECIPIENT = "message_blocked_by_recipient"
MESSAGE_REQUEST_PENDING_SS_REVIEW = "message_request_pending_ss_review"
MESSAGE_PREVIOUS_REQUEST_REJECTED_COOLDOWN = "message_previous_request_rejected_cooldown"
MESSAGE_YOU_BLOCKED_THIS_STUDENT = "message_you_blocked_this_student"
MESSAGE_ONLY_SS_ADMIN_CAN_VIEW_LIST = "message_only_ss_admin_can_view_list"
MESSAGE_INVALID = "message_invalid"
MESSAGE_ONLY_STUDENT_CAN_CANCEL_REQUEST = "message_only_student_can_cancel_request"
MESSAGE_NOT_FOUND_2 = "message_not_found_2"
MESSAGE_REQUEST_ALREADY_PROCESSED = "message_request_already_processed"
MESSAGE_ONLY_SS_ADMIN_CAN_ACCEPT = "message_only_ss_admin_can_accept"
MESSAGE_NOT_FOUND_3 = "message_not_found_3"
MESSAGE_ONLY_SS_ADMIN_CAN_REJECT = "message_only_ss_admin_can_reject"
MESSAGE_NOT_FOUND_4 = "message_not_found_4"
MESSAGE_ONLY_SS_ADMIN_CAN_BLOCK = "message_only_ss_admin_can_block"
MESSAGE_RELATIONSHIP_NOT_FOUND = "message_relationship_not_found"
MESSAGE_STUDENT_NOT_FOUND = "message_student_not_found"
MESSAGE_ONLY_SS_ADMIN_CAN_UNBLOCK = "message_only_ss_admin_can_unblock"
MESSAGE_RELATIONSHIP_NOT_FOUND_2 = "message_relationship_not_found_2"

