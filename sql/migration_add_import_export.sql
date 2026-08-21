-- Thêm tính năng Import/Export CSV/XLSX cho Job/Company/Contact (staff).
--
-- 1. Bảng import_previews — lưu tạm dữ liệu đã parse + validate + detect
--    conflict từ file upload, chờ staff review và confirm (2 bước
--    preview -> confirm, xem requirements.md/design.md tính năng
--    import/export). Dùng DATABASE thay vì file system/Redis vì Render
--    deploy container ephemeral (mất file khi restart) + có thể chạy
--    nhiều instance backend cùng lúc (JSONB trong Postgres là nơi duy
--    nhất mọi instance đều thấy chung).
--
--    TTL 1 giờ (expires_at), dọn định kỳ bằng cleanup task (APScheduler,
--    xem api/services/preview_cleanup.py) — KHÔNG dùng DELETE CASCADE gì
--    đặc biệt, chỉ là 1 bảng độc lập, xoá quá hạn là xong, không có FK
--    nào trỏ NGƯỢC vào bảng này.
--
-- 2. Thêm 3 giá trị mới vào audit_action_enum (BULK_IMPORT_JOB/COMPANY/
--    CONTACT) — mỗi lần staff confirm 1 lượt import ghi ĐÚNG 1 dòng
--    audit_logs (không ghi từng dòng con), note BẮT BUỘC (giải thích lý
--    do import, xem ACTION_LOG_RULES trong db.py) — entity_id của dòng
--    log này là preview_id (không phải id của 1 record nghiệp vụ cụ thể,
--    vì 1 lượt import có thể tạo/sửa NHIỀU record cùng lúc).
--
-- An toàn để chạy lại nhiều lần.
--
-- Cách chạy:
--   psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_import_export.sql

-- ============================================================
-- 1. import_previews
-- ============================================================

CREATE TABLE IF NOT EXISTS import_previews (
    preview_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Chủ sở hữu preview — CHỈ user này được xem/confirm preview_id này
    -- (xem api/deps.py::require_role + check ownership ở router), staff
    -- khác dù cùng role ss_team/admin cũng KHÔNG được đụng vào preview
    -- của người khác.
    user_id         UUID NOT NULL REFERENCES app_users(ss_user_id),

    entity_type     VARCHAR(20) NOT NULL CHECK (entity_type IN ('job', 'company', 'contact')),

    -- Toàn bộ rows đã parse + trạng thái conflict + summary — xem cấu
    -- trúc chi tiết trong docstring api/services/preview_manager.py.
    preview_data    JSONB NOT NULL,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,

    CONSTRAINT chk_import_previews_expires CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS idx_import_previews_user    ON import_previews(user_id);
CREATE INDEX IF NOT EXISTS idx_import_previews_expires ON import_previews(expires_at);

-- ============================================================
-- 2. audit_action_enum — thêm BULK_IMPORT_JOB/COMPANY/CONTACT
-- ============================================================

DO $$ BEGIN
    ALTER TYPE audit_action_enum ADD VALUE IF NOT EXISTS 'BULK_IMPORT_JOB';
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    ALTER TYPE audit_action_enum ADD VALUE IF NOT EXISTS 'BULK_IMPORT_COMPANY';
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    ALTER TYPE audit_action_enum ADD VALUE IF NOT EXISTS 'BULK_IMPORT_CONTACT';
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ============================================================
-- 3. pg_trgm — cần cho gợi ý company tương tự (fuzzy match tên công ty
--    khi resolve company_name trong file import Job/Contact, xem
--    api/services/company_resolver.py::suggest_companies). Extension
--    chuẩn của Postgres, không phải bên thứ 3.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE INDEX IF NOT EXISTS idx_companies_name_trgm
    ON companies USING gin (company_name gin_trgm_ops);

-- ============================================================
-- HẾT FILE
-- ============================================================
