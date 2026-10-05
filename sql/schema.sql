-- ============================================================
-- SCHEMA: Student Success — Job Postings & Company Contacts
-- Target: PostgreSQL 14+
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================
-- 0. ENUM TYPES
-- ============================================================

DO $$ BEGIN
    CREATE TYPE salary_type_enum AS ENUM (
        'RANGE', 'EXACT', 'UPTO', 'STARTING_FROM', 'NEGOTIABLE', 'UNPAID'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Chu kỳ trả lương của salary_min/salary_max (xem
-- sql/migration_add_salary_period.sql để biết lý do tách riêng, không tự
-- quy đổi ra "tháng tương đương"). Mặc định MONTH khớp hành vi
-- normalize_salary() khi text gốc không có tín hiệu chu kỳ rõ ràng.
DO $$ BEGIN
    CREATE TYPE salary_period_enum AS ENUM ('MONTH', 'YEAR');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- EXPIRED đã bị loại khỏi enum này qua
-- sql/migration_remove_expired_job_status.sql (08/2026) — job hết hạn
-- tự nhiên giờ gộp chung CLOSED, không tách riêng nữa. schema.sql là
-- snapshot cho DB TẠO MỚI nên phải khớp trạng thái sau migration, không
-- phải trạng thái gốc lúc đầu.
DO $$ BEGIN
    CREATE TYPE job_status_enum AS ENUM (
        'OPEN', 'CLOSED'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE contact_status_enum AS ENUM (
        'UNCONTACTED', 'EMAIL_SENT', 'RESPONDED', 'IN_PARTNERSHIP'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Loại hình làm việc — khớp đúng 4 lựa chọn cố định trong bộ lọc
-- "Loại hình làm việc" trên TopCV (Toàn thời gian / Bán thời gian /
-- Thực tập / Khác). Giá trị parse ra không khớp enum này (TopCV đổi
-- wording, hoặc parser lỗi) sẽ được map về NULL/OTHER ở tầng normalize,
-- không insert thẳng text thô để tránh rác dữ liệu kiểu trùng tên công ty.
DO $$ BEGIN
    CREATE TYPE work_type_enum AS ENUM (
        'FULL_TIME', 'PART_TIME', 'INTERNSHIP', 'OTHER'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    CREATE TYPE interaction_status_enum AS ENUM (
        'SENT', 'RESPONDED', 'REJECTED', 'PENDING'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Đánh giá tiềm năng hợp tác của công ty — staff tự chấm tay qua UI
-- add/edit company, không có rule tự động gán. UNVERIFIED = mặc định,
-- nghĩa là "chưa đánh giá" (không phải "tiềm năng thấp"). Khác với
-- contact_status_enum ở trên: contact_status theo dõi tiến độ LIÊN HỆ
-- (đã gửi mail/đã phản hồi/đang hợp tác), còn cột này là nhận định
-- CHỦ QUAN của staff về mức độ đáng hợp tác của công ty, độc lập với
-- việc đã liên hệ hay chưa.
DO $$ BEGIN
    CREATE TYPE partnership_potential_enum AS ENUM (
        'HIGH', 'MEDIUM', 'LOW', 'UNVERIFIED'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- ============================================================
-- 1. BẢNG LOOKUP TĨNH (SERIAL INT PK)
-- ============================================================

CREATE TABLE IF NOT EXISTS provinces (
    province_id     SERIAL PRIMARY KEY,
    province_name   VARCHAR(100) NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS levels (
    level_id        SERIAL PRIMARY KEY,
    level_code      VARCHAR(50) NOT NULL UNIQUE,
    level_order     INT NOT NULL,
    level_group     VARCHAR(50)
);

-- ============================================================
-- 2. BẢNG NGHIỆP VỤ (UUID PK)
-- ============================================================

-- Đổi tên từ ss_team_members -> app_users (08/2026, xem
-- migration_rename_ss_team_members.sql) — bảng này KHÔNG còn chỉ chứa
-- team SS, mà dùng CHUNG cho mọi tài khoản trong hệ thống: học viên
-- (role='user'), team SS (role='ss_team'), quản trị (role='admin').
-- Cột ss_user_id GIỮ NGUYÊN tên cũ dù bảng đã đổi tên (xem lý do trong
-- migration_rename_ss_team_members.sql — đổi sẽ kéo theo sửa hàng trăm
-- chỗ, rủi ro cao hơn lợi ích).
CREATE TABLE IF NOT EXISTS app_users (
    ss_user_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name       VARCHAR(255) NOT NULL,
    email           VARCHAR(255) NOT NULL UNIQUE,

    -- role: 3 giá trị phân cấp (xem migration_add_role_hierarchy.sql):
    --   'user'    — học viên, chỉ xem/lọc job, không thấy HR contact.
    --   'ss_team' — CRUD job/company/contact, xem danh sách tài khoản.
    --   'admin'   — như ss_team + trigger crawl + đổi role user khác.
    role                    VARCHAR(50) NOT NULL DEFAULT 'user',
    is_active               BOOLEAN NOT NULL DEFAULT TRUE,

    -- Đăng nhập từng người qua JWT (xem migration_add_auth.sql) —
    -- KHÁC với API_KEY tĩnh dùng chung cho client kiểu máy gọi máy.
    password_hash           TEXT,
    must_change_password    BOOLEAN NOT NULL DEFAULT true,
    failed_login_count      INT NOT NULL DEFAULT 0,
    locked_until             TIMESTAMPTZ,
    last_login_at             TIMESTAMPTZ,

    -- Đăng ký công khai + xác thực email qua Resend (xem
    -- migration_add_email_verification.sql).
    email_verified           BOOLEAN NOT NULL DEFAULT false,
    email_verify_token        VARCHAR(255),
    email_verify_expires        TIMESTAMPTZ,

    -- Quên mật khẩu (xem migration_add_password_reset.sql) — mirror
    -- cơ chế email_verify_token, thời hạn ngắn hơn (1h thay vì 24h).
    password_reset_token          VARCHAR(255),
    password_reset_expires          TIMESTAMPTZ,

    -- Số điện thoại + định hướng ngành học viên (xem
    -- migration_add_phone_track.sql) — nhập ở form /register frontend,
    -- dùng cho team SS liên hệ trực tiếp + giới thiệu job phù hợp.
    phone                             VARCHAR(30),
    track                               VARCHAR(100),

    created_at      TIMESTAMP NOT NULL DEFAULT now(),

    CONSTRAINT chk_ss_team_members_role
        CHECK (role IN ('user', 'ss_team', 'admin'))
);

-- Tra token xác thực email khi user bấm link trong email.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ss_team_members_email_verify_token
    ON app_users(email_verify_token)
    WHERE email_verify_token IS NOT NULL;

-- Tra token đặt lại mật khẩu khi user bấm link "quên mật khẩu".
CREATE INDEX IF NOT EXISTS idx_app_users_password_reset_token
    ON app_users(password_reset_token)
    WHERE password_reset_token IS NOT NULL;

-- Refresh token — hỗ trợ xoay vòng (rotation) + phát hiện tái sử dụng
-- token đã bị thu hồi (dấu hiệu token bị đánh cắp). Xem
-- migration_add_auth.sql.
CREATE TABLE IF NOT EXISTS auth_refresh_tokens (
    refresh_token_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ss_user_id          UUID NOT NULL REFERENCES app_users(ss_user_id),

    -- KHÔNG lưu refresh token thô — chỉ lưu SHA-256 hex (64 ký tự).
    token_hash          VARCHAR(64) NOT NULL UNIQUE,

    expires_at           TIMESTAMPTZ NOT NULL,
    revoked_at            TIMESTAMPTZ,

    -- Token cũ bị revoke khi xoay vòng, trỏ sang token mới. Nếu token
    -- cũ đã revoke bị dùng lại -> dấu hiệu bị đánh cắp -> thu hồi toàn
    -- bộ token của user này.
    replaced_by_token_id  UUID REFERENCES auth_refresh_tokens(refresh_token_id),

    user_agent             TEXT,
    ip_address               VARCHAR(45),  -- đủ cho IPv6

    created_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_auth_refresh_tokens_user   ON auth_refresh_tokens(ss_user_id);
CREATE INDEX IF NOT EXISTS idx_auth_refresh_tokens_expiry ON auth_refresh_tokens(expires_at);

CREATE TABLE IF NOT EXISTS companies (
    company_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_name     VARCHAR(255) NOT NULL,
    tax_id           VARCHAR(20),
    website          VARCHAR(255),
    industry         VARCHAR(255),
    company_size     VARCHAR(50),
    address          TEXT,
    province_id      INT REFERENCES provinces(province_id),
    fanpage_url      VARCHAR(255),
    linkedin_url     VARCHAR(255),

    -- Mô tả ngắn sản phẩm/dịch vụ chính công ty cung cấp — do pipeline
    -- crawl (fetch_company_profile()) hoặc enrich_company_profile_from_
    -- website.py (Gemini, đọc website riêng công ty) ghi vào, KHÔNG có
    -- form nhập tay nào gửi field này. Cột này bị bỏ sót khỏi schema gốc
    -- dù code đã ghi vào từ trước 08/2026 — thêm lại vào đây để DB mới
    -- tạo từ schema.sql không thiếu cột (trước đó chỉ tồn tại nếu DB đã
    -- được ALTER TABLE tay ngoài luồng migration).
    products_services TEXT,

    -- URL trang hồ sơ công ty trên nguồn crawl gốc (TopCV/VietnamWorks) —
    -- xem sql/migration_add_source_profile_url.sql để biết lý do cần cột
    -- này (backfill lại industry/company_size/address/website sau này mà
    -- không phụ thuộc công ty còn job đang active trên listing hay không).
    source_profile_url VARCHAR(500),

    -- Đánh giá tiềm năng hợp tác, staff chấm tay qua UI
    -- (xem migration_add_partnership_potential.sql).
    partnership_potential partnership_potential_enum NOT NULL DEFAULT 'UNVERIFIED',

    -- Xoá mềm (xem sql/migration_add_company_soft_delete.sql) — xoá qua
    -- API là UPDATE is_active=false, KHÔNG DELETE thật (JD/HR contact cũ
    -- vẫn tham chiếu company_id này qua FK, xoá cứng sẽ vỡ FK/mất lịch sử).
    is_active        BOOLEAN NOT NULL DEFAULT true,

    created_at       TIMESTAMP NOT NULL DEFAULT now(),
    updated_at       TIMESTAMP NOT NULL DEFAULT now(),

    -- Audit trail "ai tạo/sửa" (xem migration_add_audit_columns.sql).
    created_by       UUID REFERENCES app_users(ss_user_id),
    updated_by       UUID REFERENCES app_users(ss_user_id)
);

-- Mã số thuế là định danh doanh nghiệp thật, ổn định theo pháp luật VN —
-- dùng để match công ty chính xác hơn nhiều so với so tên (tên hay bị viết
-- khác nhau giữa các lần đăng tin). NULL được phép trùng nhiều dòng (dùng
-- cho công ty chưa lấy được mã số thuế), nhưng nếu đã có giá trị thì phải
-- duy nhất.
CREATE UNIQUE INDEX IF NOT EXISTS uq_companies_tax_id
    ON companies(tax_id) WHERE tax_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS job_postings (
    job_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id        UUID NOT NULL REFERENCES companies(company_id),
    job_title         VARCHAR(255) NOT NULL,
    matching_industry VARCHAR(100),
    level_id          INT REFERENCES levels(level_id),
    -- Level được suy từ đâu / theo bộ quy tắc phiên bản nào (xem
    -- sql/migration_add_job_level_source.sql). NULL = chưa biết, cần tính lại;
    -- 'manual' = có người sửa, ghi tự động không bao giờ đè.
    level_source       VARCHAR(20),
    level_rule_version SMALLINT,
    -- Tín hiệu thô derive_level() đã đọc ({"experience_text", "level_hint"}), để tính
    -- lại level mà không tải lại trang (xem sql/migration_add_job_level_signals.sql).
    -- NULL = chưa từng lưu.
    level_signals      JSONB,
    province_id       INT REFERENCES provinces(province_id),
    work_type         work_type_enum,
    parsed_content    JSONB,
    currency          VARCHAR(3),
    salary_min        BIGINT,
    salary_max        BIGINT,
    salary_type       salary_type_enum,
    salary_period     salary_period_enum NOT NULL DEFAULT 'MONTH',
    deadline          DATE,
    job_status        job_status_enum NOT NULL DEFAULT 'OPEN',
    ss_team_notes     TEXT,
    content_hash      VARCHAR(64),
    source_url        VARCHAR(500),
    created_at        TIMESTAMP NOT NULL DEFAULT now(),
    updated_at        TIMESTAMP NOT NULL DEFAULT now(),

    -- Audit trail "ai tạo/sửa" (xem migration_add_audit_columns.sql).
    -- NULL = job tạo qua crawl pipeline tự động, không phải lỗi.
    created_by        UUID REFERENCES app_users(ss_user_id),
    updated_by        UUID REFERENCES app_users(ss_user_id),

    CONSTRAINT chk_salary_range CHECK (
        salary_min IS NULL OR salary_max IS NULL OR salary_min <= salary_max
    ),
    CONSTRAINT chk_job_postings_level_source CHECK (
        level_source IS NULL OR level_source IN (
            'title', 'label', 'years', 'title_range', 'hint', 'default', 'manual'
        )
    ),
    -- CASE (không phải AND/OR): CHECK coi NULL là "đạt", xem migration.
    CONSTRAINT chk_job_postings_level_stamp CHECK (
        CASE
            WHEN level_source IS NULL THEN level_rule_version IS NULL
            WHEN level_source = 'manual' THEN level_rule_version IS NULL
            ELSE level_rule_version IS NOT NULL AND level_id IS NOT NULL
        END
    ),
    CONSTRAINT chk_job_postings_level_signals CHECK (
        level_signals IS NULL OR jsonb_typeof(level_signals) = 'object'
    )
);

CREATE TABLE IF NOT EXISTS company_contacts (
    contact_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id        UUID NOT NULL REFERENCES companies(company_id),
    contact_name      VARCHAR(255) NOT NULL,
    job_title         VARCHAR(100),
    work_email        VARCHAR(255),
    social_link       VARCHAR(255),
    phone_number      VARCHAR(50),
    found_source      VARCHAR(100),
    collected_date    DATE,
    assigned_ss_user  UUID REFERENCES app_users(ss_user_id),
    last_contacted_date DATE,
    contact_status    contact_status_enum NOT NULL DEFAULT 'UNCONTACTED',
    created_at        TIMESTAMP NOT NULL DEFAULT now(),
    updated_at        TIMESTAMP NOT NULL DEFAULT now(),

    -- Soft-delete — xoá qua API là UPDATE is_active=false, KHÔNG DELETE
    -- thật, giữ lại lịch sử liên hệ (xem migration_add_role_hierarchy.sql).
    is_active         BOOLEAN NOT NULL DEFAULT true,

    -- Audit trail "ai tạo/sửa".
    created_by        UUID REFERENCES app_users(ss_user_id),
    updated_by        UUID REFERENCES app_users(ss_user_id)
);

CREATE INDEX IF NOT EXISTS idx_company_contacts_is_active ON company_contacts(is_active);

-- Mẫu email liên hệ doanh nghiệp (thêm 08/2026) — trước đây 6 mẫu hardcode
-- cứng trong public/app.js phía frontend, không sửa/thêm được mà không
-- đụng code. XOÁ HẲN (hard delete, không có is_active) — khác
-- company_contacts/companies ở trên — lịch sử vẫn giữ qua audit_logs.
-- Xem sql/migration_add_email_templates.sql cho đầy đủ ghi chú thiết kế.
CREATE TABLE IF NOT EXISTS email_templates (
    template_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    title           VARCHAR(255) NOT NULL,
    description     VARCHAR(500),
    body            TEXT NOT NULL,
    recommended_for contact_status_enum[] NOT NULL DEFAULT '{}',
    display_order   INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      UUID REFERENCES app_users(ss_user_id),
    updated_by      UUID REFERENCES app_users(ss_user_id)
);

CREATE INDEX IF NOT EXISTS idx_email_templates_display_order ON email_templates(display_order);

CREATE TABLE IF NOT EXISTS job_sources_log (
    log_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id            UUID NOT NULL REFERENCES job_postings(job_id),
    source_name       VARCHAR(100),
    source_url        VARCHAR(500),
    raw_jd_content    TEXT,
    salary_raw_content VARCHAR(255),
    collected_date    DATE NOT NULL DEFAULT CURRENT_DATE,
    -- Lần gần nhất pipeline fetch THÀNH CÔNG trang chi tiết của URL này. NULL =
    -- chưa từng ghi nhận (job cũ, trước khi có cột). Xem
    -- db/jobs.py::job_needs_detail_enrichment.
    detail_checked_at TIMESTAMPTZ,

    CONSTRAINT uq_job_source UNIQUE (job_id, source_url)
);

CREATE TABLE IF NOT EXISTS job_contact_links (
    link_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id            UUID NOT NULL REFERENCES job_postings(job_id),
    contact_id        UUID NOT NULL REFERENCES company_contacts(contact_id),
    interaction_status VARCHAR(50),
    created_at        TIMESTAMP NOT NULL DEFAULT now(),
    updated_at        TIMESTAMP NOT NULL DEFAULT now(),

    CONSTRAINT uq_job_contact UNIQUE (job_id, contact_id)
);

CREATE TABLE IF NOT EXISTS job_contact_interactions (
    interaction_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    link_id           UUID NOT NULL REFERENCES job_contact_links(link_id),
    assigned_ss_user  UUID REFERENCES app_users(ss_user_id),
    interaction_type  VARCHAR(50),
    interaction_status interaction_status_enum,
    note              TEXT,
    interaction_date  DATE NOT NULL DEFAULT CURRENT_DATE
);

-- ============================================================
-- 2b. TƯƠNG TÁC CỦA HỌC VIÊN VỚI JOB (role='user')
-- Xem migration_add_applications_saved_jobs.sql.
-- ============================================================

-- job_applications — học viên bấm "Ứng tuyển", staff xem ai đã nộp.
CREATE TABLE IF NOT EXISTS job_applications (
    application_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ss_user_id        UUID NOT NULL REFERENCES app_users(ss_user_id),
    job_id            UUID NOT NULL REFERENCES job_postings(job_id),
    note              TEXT,
    applied_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- 1 học viên chỉ ứng tuyển 1 job đúng 1 lần.
    CONSTRAINT uq_job_applications_user_job UNIQUE (ss_user_id, job_id)
);

CREATE INDEX IF NOT EXISTS idx_job_applications_user ON job_applications(ss_user_id);
CREATE INDEX IF NOT EXISTS idx_job_applications_job  ON job_applications(job_id);

-- saved_jobs — bookmark riêng tư, KHÁC ứng tuyển (danh sách cá nhân,
-- không staff nào cần thấy học viên đã lưu job gì).
CREATE TABLE IF NOT EXISTS saved_jobs (
    saved_job_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ss_user_id         UUID NOT NULL REFERENCES app_users(ss_user_id),
    job_id             UUID NOT NULL REFERENCES job_postings(job_id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_saved_jobs_user_job UNIQUE (ss_user_id, job_id)
);

CREATE INDEX IF NOT EXISTS idx_saved_jobs_user ON saved_jobs(ss_user_id);
CREATE INDEX IF NOT EXISTS idx_saved_jobs_job  ON saved_jobs(job_id);

-- ============================================================
-- 2c. LỊCH SỬ THAO TÁC (audit_logs) — xem
-- sql/migration_add_audit_logs.sql để biết đầy đủ lý do thiết kế.
--
-- 1 BẢNG DUY NHẤT phục vụ CẢ "log tự động" (mọi thao tác, không note)
-- LẪN "log thủ công" (tập con action nhạy cảm, có note) — 2 view này
-- chỉ là filter is_manual_log khác nhau ở tầng API, không phải 2 bảng.
-- ============================================================

DO $$ BEGIN
    CREATE TYPE audit_action_enum AS ENUM (
        'CREATE_JOB', 'UPDATE_JOB', 'DELETE_JOB',
        'CREATE_COMPANY', 'UPDATE_COMPANY', 'DELETE_COMPANY',
        'CREATE_CONTACT', 'UPDATE_CONTACT', 'DELETE_CONTACT', 'ASSIGN_CONTACT',
        'CREATE_EMAIL_TEMPLATE', 'UPDATE_EMAIL_TEMPLATE', 'DELETE_EMAIL_TEMPLATE'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS audit_logs (
    log_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_id        UUID REFERENCES app_users(ss_user_id),
    action_type     audit_action_enum NOT NULL,
    entity_type     VARCHAR(20) NOT NULL,
    entity_id       UUID NOT NULL,
    entity_label    VARCHAR(255),
    company_id      UUID REFERENCES companies(company_id),
    changes         JSONB,
    is_manual_log   BOOLEAN NOT NULL,
    note_required   BOOLEAN NOT NULL DEFAULT false,
    note            TEXT,
    note_updated_by UUID REFERENCES app_users(ss_user_id),
    note_updated_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_audit_logs_note_required
        CHECK (NOT note_required OR note IS NOT NULL)
);

-- ============================================================
-- 3. INDEXES
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_companies_province        ON companies(province_id);
CREATE INDEX IF NOT EXISTS idx_companies_created_by       ON companies(created_by);
CREATE INDEX IF NOT EXISTS idx_companies_partnership_potential ON companies(partnership_potential);
CREATE INDEX IF NOT EXISTS idx_companies_is_active ON companies(is_active);
CREATE INDEX IF NOT EXISTS idx_job_postings_company       ON job_postings(company_id);
CREATE INDEX IF NOT EXISTS idx_job_postings_created_by    ON job_postings(created_by);
CREATE INDEX IF NOT EXISTS idx_job_postings_level         ON job_postings(level_id);
CREATE INDEX IF NOT EXISTS idx_job_postings_province      ON job_postings(province_id);
CREATE INDEX IF NOT EXISTS idx_job_postings_status        ON job_postings(job_status);
CREATE INDEX IF NOT EXISTS idx_job_postings_content_hash  ON job_postings(content_hash);
CREATE INDEX IF NOT EXISTS idx_company_contacts_company   ON company_contacts(company_id);
CREATE INDEX IF NOT EXISTS idx_company_contacts_status    ON company_contacts(contact_status);
CREATE INDEX IF NOT EXISTS idx_job_sources_log_job        ON job_sources_log(job_id);
CREATE INDEX IF NOT EXISTS idx_job_contact_links_job      ON job_contact_links(job_id);
CREATE INDEX IF NOT EXISTS idx_job_contact_links_contact  ON job_contact_links(contact_id);
CREATE INDEX IF NOT EXISTS idx_job_contact_interactions_link ON job_contact_interactions(link_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_entity     ON audit_logs(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_company    ON audit_logs(company_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_actor      ON audit_logs(actor_id);
CREATE INDEX IF NOT EXISTS idx_audit_logs_manual     ON audit_logs(is_manual_log, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_logs_pending_note ON audit_logs(is_manual_log)
    WHERE note_required = true AND note IS NULL;

-- ============================================================
-- 4. TRIGGERS — updated_at tự động
-- ============================================================

CREATE OR REPLACE FUNCTION trg_set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS set_updated_at_companies ON companies;
CREATE TRIGGER set_updated_at_companies
BEFORE UPDATE ON companies
FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();

DROP TRIGGER IF EXISTS set_updated_at_job_postings ON job_postings;
CREATE TRIGGER set_updated_at_job_postings
BEFORE UPDATE ON job_postings
FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();

DROP TRIGGER IF EXISTS set_updated_at_company_contacts ON company_contacts;
CREATE TRIGGER set_updated_at_company_contacts
BEFORE UPDATE ON company_contacts
FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();

DROP TRIGGER IF EXISTS set_updated_at_job_contact_links ON job_contact_links;
CREATE TRIGGER set_updated_at_job_contact_links
BEFORE UPDATE ON job_contact_links
FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();

DROP TRIGGER IF EXISTS trg_email_templates_updated_at ON email_templates;
CREATE TRIGGER trg_email_templates_updated_at
BEFORE UPDATE ON email_templates
FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();

-- ============================================================
-- 5. TRIGGER — content_hash tự động cho job_postings
-- ============================================================

CREATE OR REPLACE FUNCTION generate_job_hash(
    p_company_id  UUID,
    p_job_title   TEXT,
    p_level_id    INT,
    p_province_id INT
) RETURNS VARCHAR(64) AS $$
BEGIN
    RETURN encode(
        digest(
            p_company_id::text || '|' ||
            lower(regexp_replace(trim(p_job_title), '\s+', ' ', 'g')) || '|' ||
            COALESCE(p_level_id::text, '') || '|' ||
            COALESCE(p_province_id::text, ''),
            'sha256'
        ),
        'hex'
    );
END;
$$ LANGUAGE plpgsql IMMUTABLE;

CREATE OR REPLACE FUNCTION trg_set_job_hash() RETURNS TRIGGER AS $$
BEGIN
    NEW.content_hash := generate_job_hash(
        NEW.company_id, NEW.job_title, NEW.level_id, NEW.province_id
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS set_job_hash ON job_postings;
CREATE TRIGGER set_job_hash
BEFORE INSERT OR UPDATE ON job_postings
FOR EACH ROW EXECUTE FUNCTION trg_set_job_hash();

-- ============================================================
-- 6. VIEW hỗ trợ — tìm job nghi ngờ trùng lặp
-- ============================================================

CREATE OR REPLACE VIEW v_duplicate_job_candidates AS
SELECT
    content_hash,
    array_agg(job_id ORDER BY created_at) AS job_ids,
    array_agg(job_title ORDER BY created_at) AS job_titles,
    count(*) AS num_duplicates
FROM job_postings
GROUP BY content_hash
HAVING count(*) > 1;

-- ============================================================
-- 7. SEED DATA — tỉnh thành + level cơ bản
-- ============================================================

-- Danh sách 34 tỉnh/thành sau sáp nhập (Nghị quyết 202/2025/QH15, Quốc
-- hội thông qua 12/6/2025, hiệu lực từ 01/7/2025 — cả nước giảm từ 63
-- xuống 34 đơn vị hành chính cấp tỉnh, gồm 28 tỉnh + 6 thành phố trực
-- thuộc Trung ương). Xem thêm sql/migration_update_provinces_2025.sql
-- để vá bổ sung cho DB đã tạo từ trước đợt cập nhật này.
INSERT INTO provinces (province_name) VALUES
    ('Tuyên Quang'), ('Cao Bằng'), ('Lai Châu'), ('Lào Cai'), ('Thái Nguyên'),
    ('Điện Biên'), ('Lạng Sơn'), ('Sơn La'), ('Phú Thọ'), ('Bắc Ninh'),
    ('Quảng Ninh'), ('Hà Nội'), ('Hải Phòng'), ('Hưng Yên'), ('Ninh Bình'),
    ('Thanh Hóa'), ('Nghệ An'), ('Hà Tĩnh'), ('Quảng Trị'), ('Huế'),
    ('Đà Nẵng'), ('Quảng Ngãi'), ('Gia Lai'), ('Đắk Lắk'), ('Khánh Hòa'),
    ('Lâm Đồng'), ('Đồng Nai'), ('Tây Ninh'), ('Hồ Chí Minh'), ('Đồng Tháp'),
    ('An Giang'), ('Vĩnh Long'), ('Cần Thơ'), ('Cà Mau'),
    ('Khác'), ('Remote')
ON CONFLICT (province_name) DO NOTHING;

INSERT INTO levels (level_code, level_order, level_group) VALUES
    ('Intern',   1, 'Entry Level'),
    ('Fresher',  2, 'Entry Level'),
    ('Junior',   3, 'Entry Level'),
    ('Middle',   4, 'Mid Level'),
    ('Senior',   5, 'Mid Level'),
    ('Lead',     6, 'Advance Level'),
    ('Manager',  7, 'Advance Level')
ON CONFLICT (level_code) DO NOTHING;

-- 6 mẫu email liên hệ doanh nghiệp gốc (thêm 08/2026) — nội dung đầy đủ
-- + lý do dùng NOT EXISTS thay vì ON CONFLICT xem
-- sql/migration_add_email_templates.sql (bảng không có cột UNIQUE tự
-- nhiên để làm khoá ON CONFLICT).
INSERT INTO email_templates (title, description, body, recommended_for, display_order)
SELECT 'Giới thiệu MindX',
       'Mở lời làm quen lần đầu, đặt vấn đề hợp tác tuyển dụng Intern/Fresher.',
       E'Tiêu đề: MindX kết nối cơ hội thực tập/fresher cùng {{TEN_CONG_TY}}\n\n{{LOI_CHAO}}\n\nEm là {{TEN_STAFF}}, phụ trách kết nối doanh nghiệp của MindX — đơn vị đào tạo lập trình, Data Analysis và Business Analysis cho học viên trẻ, định hướng đi thực tập/fresher ngay sau khoá học.\n\nEm thấy {{TEN_CONG_TY}} là một trong những doanh nghiệp em rất muốn kết nối, nên xin phép chủ động liên hệ để tìm hiểu xem hiện tại công ty có đang có nhu cầu tuyển Intern/Fresher ở mảng nào không ạ. Nếu có, em rất mong được trao đổi thêm để giới thiệu những học viên phù hợp từ MindX.\n\nEm cảm ơn {{TEN_NGUOI_LIEN_HE}} đã dành thời gian đọc email, rất mong nhận được phản hồi ạ.\n\nTrân trọng,\n{{TEN_STAFF}}\nStudent Success — MindX',
       ARRAY['UNCONTACTED']::contact_status_enum[], 1
WHERE NOT EXISTS (SELECT 1 FROM email_templates WHERE title = 'Giới thiệu MindX');

INSERT INTO email_templates (title, description, body, recommended_for, display_order)
SELECT 'Xin JD Intern/Fresher',
       'Hỏi xin mô tả công việc cụ thể để giới thiệu đúng học viên.',
       E'Tiêu đề: Xin thông tin tuyển dụng Intern/Fresher từ {{TEN_CONG_TY}}\n\n{{LOI_CHAO}}\n\nEm là {{TEN_STAFF}} bên MindX, trước đó có liên hệ giới thiệu về chương trình kết nối việc làm cho học viên ạ.\n\nKhông biết hiện tại {{TEN_CONG_TY}} có JD (mô tả công việc) nào đang tuyển Intern/Fresher không ạ? Nếu có, {{TEN_NGUOI_LIEN_HE}} gửi giúp em JD chi tiết (vị trí, yêu cầu, mức lương/trợ cấp nếu có, deadline) để em lọc và giới thiệu đúng học viên phù hợp nhất bên MindX ạ.\n\nEm cảm ơn {{TEN_NGUOI_LIEN_HE}} nhiều ạ!\n\nTrân trọng,\n{{TEN_STAFF}}\nStudent Success — MindX',
       ARRAY['EMAIL_SENT']::contact_status_enum[], 2
WHERE NOT EXISTS (SELECT 1 FROM email_templates WHERE title = 'Xin JD Intern/Fresher');

INSERT INTO email_templates (title, description, body, recommended_for, display_order)
SELECT 'Giới thiệu học viên phù hợp',
       'Gửi kèm profile/CV học viên ứng với JD đã có.',
       E'Tiêu đề: MindX giới thiệu ứng viên cho vị trí Intern/Fresher tại {{TEN_CONG_TY}}\n\n{{LOI_CHAO}}\n\nEm là {{TEN_STAFF}} bên MindX. Dựa trên JD {{TEN_CONG_TY}} đang tuyển, em xin giới thiệu (các) học viên sau đây — CV/profile chi tiết em đính kèm trong email này ạ:\n\n- [Tên học viên] — [Kỹ năng/thế mạnh nổi bật, liên quan trực tiếp tới JD]\n\nCác bạn đều đã hoàn thành chương trình đào tạo tại MindX và sẵn sàng phỏng vấn/đi làm theo lịch phía công ty. {{TEN_NGUOI_LIEN_HE}} xem giúp em, có gì cần trao đổi thêm em luôn sẵn sàng hỗ trợ ạ.\n\nTrân trọng,\n{{TEN_STAFF}}\nStudent Success — MindX',
       ARRAY['IN_PARTNERSHIP']::contact_status_enum[], 3
WHERE NOT EXISTS (SELECT 1 FROM email_templates WHERE title = 'Giới thiệu học viên phù hợp');

INSERT INTO email_templates (title, description, body, recommended_for, display_order)
SELECT 'Follow-up sau khi gửi profile học viên',
       'Nhắc nhẹ khi chưa thấy phản hồi sau khi đã giới thiệu học viên.',
       E'Tiêu đề: Follow-up — hồ sơ học viên MindX gửi {{TEN_CONG_TY}}\n\n{{LOI_CHAO}}\n\nEm là {{TEN_STAFF}} bên MindX. Tuần trước em có gửi {{TEN_NGUOI_LIEN_HE}} profile một số học viên ứng với vị trí Intern/Fresher bên {{TEN_CONG_TY}} đang tuyển ạ.\n\nEm xin phép follow-up lại xem {{TEN_NGUOI_LIEN_HE}} đã có dịp xem qua chưa, và bên mình có cần em bổ sung thêm hồ sơ hay thông tin gì không ạ. Nếu vị trí đã tuyển đủ hoặc chưa phù hợp, {{TEN_NGUOI_LIEN_HE}} phản hồi giúp em 1 câu để em chủ động cập nhật lại phía học viên ạ.\n\nEm cảm ơn {{TEN_NGUOI_LIEN_HE}} nhiều!\n\nTrân trọng,\n{{TEN_STAFF}}\nStudent Success — MindX',
       ARRAY[]::contact_status_enum[], 4
WHERE NOT EXISTS (SELECT 1 FROM email_templates WHERE title = 'Follow-up sau khi gửi profile học viên');

INSERT INTO email_templates (title, description, body, recommended_for, display_order)
SELECT 'Cảm ơn sau khi doanh nghiệp phản hồi',
       'Ghi nhận + giữ nhịp trao đổi sau khi phía công ty trả lời.',
       E'Tiêu đề: Cảm ơn {{TEN_CONG_TY}} đã phản hồi\n\n{{LOI_CHAO}}\n\nEm là {{TEN_STAFF}} bên MindX, cảm ơn {{TEN_NGUOI_LIEN_HE}} đã dành thời gian phản hồi email trước của em ạ.\n\n[Điền nội dung theo đúng những gì phía công ty vừa phản hồi — ví dụ: xác nhận lịch trao đổi tiếp theo, thông tin JD sẽ gửi sau, hoặc bước tiếp theo hai bên đã thống nhất.]\n\nEm sẽ theo sát và phối hợp chặt chẽ với {{TEN_NGUOI_LIEN_HE}} trong các bước tiếp theo ạ. Rất mong được đồng hành cùng {{TEN_CONG_TY}} trong việc kết nối các bạn học viên tiềm năng.\n\nTrân trọng,\n{{TEN_STAFF}}\nStudent Success — MindX',
       ARRAY['RESPONDED']::contact_status_enum[], 5
WHERE NOT EXISTS (SELECT 1 FROM email_templates WHERE title = 'Cảm ơn sau khi doanh nghiệp phản hồi');

INSERT INTO email_templates (title, description, body, recommended_for, display_order)
SELECT 'Hỏi nhu cầu tuyển dụng tháng/quý',
       'Chủ động hỏi thăm định kỳ với các đối tác đã từng hợp tác.',
       E'Tiêu đề: {{TEN_CONG_TY}} có đang cần tuyển Intern/Fresher không ạ?\n\n{{LOI_CHAO}}\n\nEm là {{TEN_STAFF}} bên MindX. Lâu rồi em chưa có dịp cập nhật lại với {{TEN_NGUOI_LIEN_HE}}, không biết thời gian tới {{TEN_CONG_TY}} có kế hoạch tuyển thêm Intern/Fresher ở mảng nào không ạ?\n\nNếu có, em rất mong được {{TEN_NGUOI_LIEN_HE}} chia sẻ sớm để em chuẩn bị và giới thiệu học viên phù hợp kịp tiến độ tuyển dụng bên mình ạ. Em luôn sẵn sàng hỗ trợ bất cứ khi nào công ty cần ạ.\n\nTrân trọng,\n{{TEN_STAFF}}\nStudent Success — MindX',
       ARRAY[]::contact_status_enum[], 6
WHERE NOT EXISTS (SELECT 1 FROM email_templates WHERE title = 'Hỏi nhu cầu tuyển dụng tháng/quý');

-- ============================================================
-- Hệ thống nhắn tin — học viên ↔ SS / SS ↔ SS (08/2026).
-- Xem sql/migration_add_chat_messages.sql cho DB đã có sẵn dữ liệu,
-- và backend-scrap-jd-nhan-tin.md cho toàn bộ kế hoạch/state machine.
-- ============================================================

CREATE TABLE IF NOT EXISTS chat_relationships (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id      UUID NOT NULL REFERENCES app_users(ss_user_id),
    ss_id           UUID NOT NULL REFERENCES app_users(ss_user_id),
    status          TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'declined', 'blocked')),
    initiated_by    UUID NOT NULL REFERENCES app_users(ss_user_id),
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at      TIMESTAMPTZ,
    declined_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (student_id, ss_id)
);

CREATE INDEX IF NOT EXISTS idx_chat_relationships_student_status
    ON chat_relationships (student_id, status);
CREATE INDEX IF NOT EXISTS idx_chat_relationships_ss_status
    ON chat_relationships (ss_id, status);

CREATE TABLE IF NOT EXISTS messages (
    id              BIGSERIAL PRIMARY KEY,
    sender_id       UUID NOT NULL REFERENCES app_users(ss_user_id),
    receiver_id     UUID NOT NULL REFERENCES app_users(ss_user_id),
    content         TEXT NOT NULL CHECK (char_length(btrim(content)) BETWEEN 1 AND 2000),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at         TIMESTAMPTZ,
    CHECK (sender_id != receiver_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_sender_receiver ON messages (sender_id, receiver_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_receiver_sender ON messages (receiver_id, sender_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_unread ON messages (receiver_id, id) WHERE read_at IS NULL;

-- ============================================================
-- Lịch sử lượt crawl (crawl_runs)
-- ============================================================

-- Thêm bảng crawl_runs — lưu bền lịch sử + trạng thái từng lượt crawl
-- (08/2026, xem lịch sử trao đổi "phương án 2: bảng crawl_runs riêng").
--
-- THAY THẾ HẲN _RUNS (dict trong RAM của api/crawl_runner.py) — không
-- chạy song song 2 nguồn dữ liệu. Giải quyết đồng thời 2 giới hạn cũ
-- đã ghi trong docstring crawl_runner.py:
--   - Mất lịch sử khi server restart (Render sleep dậy/deploy mới).
--   - Không đồng bộ nếu chạy nhiều worker uvicorn (mỗi worker RAM
--     riêng) — giờ mọi worker đọc/ghi chung 1 bảng Postgres.
--
-- ĐÃ CHỐT (xem lịch sử trao đổi):
--   - Giới hạn "1 lượt crawl/nguồn tại 1 thời điểm" enforce Ở TẦNG DB
--     bằng UNIQUE INDEX có điều kiện bên dưới (idx_crawl_runs_one_active_
--     per_source) — KHÔNG chỉ dựa vào disable nút ở frontend như trước.
--     TopCV và VietnamWorks (2 nguồn khác nhau) vẫn được chạy song song
--     bình thường.
--   - GET /crawl/{run_id} và GET /crawl (mới) đều yêu cầu tối thiểu
--     role 'ss_team' — xem api/routers/crawl.py (trước đây GET
--     /crawl/{run_id} không yêu cầu đăng nhập).
--
-- An toàn để chạy lại nhiều lần (IF NOT EXISTS ở mọi bước, trừ CREATE
-- TYPE dùng khối DO $$ bắt lỗi duplicate_object — cùng pattern
-- migration_add_audit_logs.sql).

DO $$ BEGIN
    CREATE TYPE crawl_status_enum AS ENUM ('queued', 'running', 'done', 'error');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS crawl_runs (
    run_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Khớp _SOURCE_ADAPTERS / _CATEGORIES_BY_SOURCE trong
    -- api/crawl_runner.py, api/routers/crawl.py — không dùng ENUM vì
    -- danh sách nguồn/category còn có thể thêm (vd careerviet) mà
    -- không muốn phải ALTER TYPE mỗi lần, khác trường hợp status (cố
    -- định, ít khả năng đổi).
    source        VARCHAR(50)  NOT NULL,
    category      VARCHAR(100) NOT NULL,
    pages         INT NOT NULL,
    max_jobs      INT,

    status        crawl_status_enum NOT NULL DEFAULT 'queued',

    -- Điền khi status='done' — dict trả về từ pipeline.run_pipeline():
    -- fetched, inserted, skipped_duplicate, skipped_duplicate_repost,
    -- updated_existing, skipped_fetch_failed, errors,
    -- skipped_anonymous_employer.
    stats         JSONB,

    -- Điền khi status='error' (str(exception) bắt được trong execute()).
    error         TEXT,

    -- Admin nào bấm crawl — NULL đã CHỦ ĐÍCH dành sẵn cho crawl tự
    -- động theo lịch sau này (APScheduler/cron, xem "NÂNG CẤP SAU"
    -- trong crawl_runner.py cũ), không phải lỗi dữ liệu. KHÔNG dùng
    -- ON DELETE CASCADE/SET NULL tường minh — giữ mặc định RESTRICT
    -- như mọi FK khác trong schema.sql (admin có lượt crawl đã chạy
    -- thì không xoá cứng tài khoản được, phải soft-delete qua
    -- is_active nếu sau này thêm, giống app_users nói chung).
    triggered_by  UUID REFERENCES app_users(ss_user_id),

    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);

-- Tra nhanh "lượt nào đang chạy" — dùng cả cho check chặn trùng nguồn
-- (db/crawl_runs.py::create_run) lẫn filter ?status= ở GET /crawl.
CREATE INDEX IF NOT EXISTS idx_crawl_runs_status ON crawl_runs(status);

-- Trang lịch sử, sắp mới nhất trước — query chính của GET /crawl.
CREATE INDEX IF NOT EXISTS idx_crawl_runs_started_at ON crawl_runs(started_at DESC);

CREATE INDEX IF NOT EXISTS idx_crawl_runs_source       ON crawl_runs(source);
CREATE INDEX IF NOT EXISTS idx_crawl_runs_triggered_by ON crawl_runs(triggered_by);

-- ENFORCE Ở TẦNG DB: mỗi nguồn (source) chỉ được TỐI ĐA 1 dòng đang
-- 'queued' hoặc 'running' tại 1 thời điểm — UNIQUE INDEX có điều kiện
-- (partial index) trên riêng cột `source`, WHERE lọc đúng 2 trạng thái
-- "đang sống". Khi đã 'done'/'error', dòng đó không còn nằm trong index
-- này nữa nên nguồn đó lại được crawl tiếp bình thường.
--
-- db/crawl_runs.py::create_run() PHẢI tự SELECT kiểm tra trước và raise
-- ActiveCrawlExistsError để router trả 409 rõ ràng (giống pattern
-- NoteRequiredError ở audit_logs) — UNIQUE INDEX này chỉ là LỚP CHẶN
-- THỨ 2 phòng race condition (2 request POST /crawl cùng nguồn tới
-- gần như đồng thời, cả 2 đều pass qua SELECT check trước khi kịp
-- INSERT), không phải lớp chặn chính.
CREATE UNIQUE INDEX IF NOT EXISTS idx_crawl_runs_one_active_per_source
    ON crawl_runs(source)
    WHERE status IN ('queued', 'running');

-- ============================================================
-- Nhóm lượt crawl theo batch (crawl_batches)
-- ============================================================

-- Thêm "crawl nhiều category liên tục" (08/2026, xem lịch sử trao đổi
-- "Hướng A — batch job ở tầng backend").
--
-- Ý TƯỞNG: 1 request POST /crawl/batch (source + list category) tạo ra:
--   - 1 dòng crawl_batches (metadata chung: source, danh sách category
--     theo ĐÚNG THỨ TỰ sẽ crawl, pages/max_jobs áp dụng chung).
--   - N dòng crawl_runs con (MỖI category 1 run RIÊNG, y hệt crawl đơn
--     lẻ hiện có — không đổi gì ở crawl_runs/pipeline.py cho từng run),
--     nối vào batch qua batch_id + batch_position (thứ tự 0..N-1).
--
-- CHẠY TUẦN TỰ, KHÔNG SONG SONG: api/crawl_runner.py::execute() tự tạo
-- + chạy run kế tiếp trong batch NGAY SAU KHI run hiện tại xong (done
-- HOẶC error đều tính là "xong", batch vẫn tiếp tục category kế) —
-- đúng hành vi vòng for tuần tự người dùng đang gõ tay, và tự động giữ
-- đúng UNIQUE INDEX idx_crawl_runs_one_active_per_source đã có sẵn (chỉ
-- tạo run tiếp theo sau khi run trước đã đổi khỏi 'queued'/'running').
--
-- KHÔNG cần bảng/queue riêng: chỉ cần bảng crawl_batches (metadata) +
-- 2 cột nối trên crawl_runs — mọi cơ chế heartbeat/log live/watchdog
-- đã có sẵn cho crawl_runs áp dụng nguyên vẹn cho từng run con của
-- batch, không cần sửa gì thêm ở đó.
--
-- An toàn để chạy lại nhiều lần (IF NOT EXISTS mọi bước).

CREATE TABLE IF NOT EXISTS crawl_batches (
    batch_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    source        VARCHAR(50) NOT NULL,

    -- Danh sách category ĐÚNG THỨ TỰ sẽ crawl, vd
    -- ["data-analyst","data-engineer","business-analyst"] — batch_position
    -- ở crawl_runs chính là index trong mảng này (0-based).
    categories    JSONB NOT NULL,

    pages         INT NOT NULL,
    max_jobs      INT,

    -- Dùng LẠI crawl_status_enum đã có (queued/running/done/error) —
    -- batch chỉ thật sự dùng 'running' (đang crawl dở category nào đó)
    -- và 'done' (hết category); 'error' dành cho trường hợp HIẾM tự
    -- advance sang category kế thất bại giữa chừng (vd ActiveCrawlExistsError
    -- do có ai đó crawl tay đúng lúc source này rảnh — xem
    -- api/crawl_runner.py::execute()), KHÁC với 1 category lỗi bình
    -- thường (category đó vẫn tính "xong", batch vẫn tiếp tục category
    -- sau, không rơi vào 'error' ở đây).
    status        crawl_status_enum NOT NULL DEFAULT 'running',
    error         TEXT,

    triggered_by  UUID REFERENCES app_users(ss_user_id),

    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_crawl_batches_status       ON crawl_batches(status);
CREATE INDEX IF NOT EXISTS idx_crawl_batches_created_at   ON crawl_batches(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_crawl_batches_triggered_by ON crawl_batches(triggered_by);

-- Nối crawl_runs -> crawl_batches. NULL ở cả 2 cột = run đơn lẻ như
-- trước giờ (không đổi hành vi gì cho crawl không qua batch).
ALTER TABLE crawl_runs ADD COLUMN IF NOT EXISTS batch_id       UUID REFERENCES crawl_batches(batch_id) ON DELETE CASCADE;
ALTER TABLE crawl_runs ADD COLUMN IF NOT EXISTS batch_position INT;

-- Tra "các run của batch X, đúng thứ tự" — dùng cho GET /crawl/batch/{batch_id}.
CREATE INDEX IF NOT EXISTS idx_crawl_runs_batch_id ON crawl_runs(batch_id, batch_position);

-- ============================================================
-- Log tiến độ từng lượt crawl (crawl_run_logs)
-- ============================================================

-- Thêm heartbeat/tiến độ real-time + log live cho crawl_runs (08/2026,
-- xem lịch sử trao đổi "phương án Heartbeat / tiến độ theo thời gian
-- thực"). 2 phần TÁCH RIÊNG có chủ đích:
--
--   1. crawl_runs.progress (JSONB) — snapshot GỌN {page, fetched,
--      inserted, last_update} do execute() ghi đè liên tục trong lúc
--      chạy pipeline (không phải lịch sử, chỉ trạng thái mới nhất) —
--      dùng cho khu "Hiện tại" ở trang /crawl (số trang đang crawl,
--      đã lấy bao nhiêu job) VÀ cho watchdog (crawl_watchdog.py) so
--      sánh last_update để phát hiện "treo quá lâu không cập nhật".
--
--   2. crawl_run_logs (bảng riêng, KHÔNG gộp vào crawl_runs) — TỪNG
--      DÒNG log (giống log thấy trong cmd khi crawl chạy ở máy local),
--      phục vụ khu "Xem log live" ở trang /crawl. Tách bảng riêng
--      (không phải 1 cột TEXT nối chuỗi trong crawl_runs) vì:
--      - Ghi liên tục nhiều dòng/giây lúc crawl chạy -> UPDATE nối
--        chuỗi ngày càng dài trên CHÍNH dòng crawl_runs sẽ khoá
--        (row lock) cả dòng đó, đụng độ với các UPDATE status/progress
--        khác đang cần ghi đồng thời lên cùng 1 dòng.
--      - INSERT có thể set primary key tăng dần (id) -> tự nhiên đã có
--        thứ tự để ORDER BY, không cần thêm cột "line_number" riêng.
--      - Dọn dẹp dễ hơn (xem cron dọn log cũ ở cuối file) khi tách
--        riêng khỏi bảng chính, không đụng tới lịch sử crawl_runs.
--
-- An toàn để chạy lại nhiều lần (IF NOT EXISTS mọi bước).

ALTER TABLE crawl_runs ADD COLUMN IF NOT EXISTS progress JSONB;

CREATE TABLE IF NOT EXISTS crawl_run_logs (
    id         BIGSERIAL PRIMARY KEY,
    run_id     UUID NOT NULL REFERENCES crawl_runs(run_id) ON DELETE CASCADE,
    -- Khớp logging.LEVELNAME chuẩn của Python (INFO/WARNING/ERROR) —
    -- dùng để tô màu dòng log khác nhau ở frontend (giống terminal thật
    -- có màu vàng/đỏ cho warning/error), xem crawl.html.
    level      VARCHAR(10) NOT NULL DEFAULT 'INFO',
    message    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Trang /crawl poll GET /crawl/{run_id}/logs?after_id=N để chỉ lấy dòng
-- MỚI kể từ lần poll trước (không tải lại toàn bộ log mỗi 2-3 giây) —
-- cần cả (run_id, id) để query "id > after_id AND run_id = X" nhanh.
CREATE INDEX IF NOT EXISTS idx_crawl_run_logs_run_id_id ON crawl_run_logs(run_id, id);

-- ============================================================
-- Snapshot HTML/JSON gốc để debug parser (crawl_snapshots)
-- ============================================================

-- Snapshot HTML/JSON gốc của các lượt crawl để debug khi parser hỏng (đợt 3,
-- 10/2026, xem snapshots.py + db/crawl_snapshots.py).
--
-- Vì sao cần: site đổi giao diện thì adapter thường KHÔNG báo lỗi mà chỉ trả 0
-- record hoặc field rỗng hàng loạt; không có HTML gốc của đúng thời điểm đó
-- thì không biết selector hỏng ở đâu, và fixture trong tests/ vẫn là dữ liệu
-- dựng tay. Mỗi lượt chỉ giữ vài mẫu (xem SNAPSHOT_MAX_PER_RUN trong
-- config.py): trang listing đầu, trang chi tiết đầu, và các trang bất thường
-- (listing parse ra 0 job, chi tiết không có nội dung).
--
-- content_gz là gzip của body text (UTF-8): HTML/JSON nén được ~5-10 lần nên
-- 1 mẫu thường chỉ vài chục KB. Bản ghi cũ hơn SNAPSHOT_RETENTION_DAYS bị xoá
-- cơ hội mỗi lần lưu snapshot mới (db/crawl_snapshots.py), không cần cron.
--
-- ON DELETE CASCADE: xoá lượt crawl thì snapshot đi theo (cùng lý do
-- crawl_run_logs). An toàn để chạy lại nhiều lần (IF NOT EXISTS mọi bước).

CREATE TABLE IF NOT EXISTS crawl_snapshots (
    id          BIGSERIAL PRIMARY KEY,
    run_id      UUID NOT NULL REFERENCES crawl_runs(run_id) ON DELETE CASCADE,
    -- Chép từ crawl_runs để tra "mẫu mới nhất của nguồn X" không cần JOIN.
    source      VARCHAR(50) NOT NULL,
    -- 'listing' | 'detail' | ...
    kind        VARCHAR(30) NOT NULL,
    -- 'sample' hoặc mã bất thường: 'listing_empty', 'detail_blank',
    -- 'detail_unparsable'.
    reason      VARCHAR(50) NOT NULL DEFAULT 'sample',
    url         TEXT NOT NULL DEFAULT '',
    content_gz  BYTEA NOT NULL,
    -- Kích thước text TRƯỚC khi nén (byte), để biết trang lớn cỡ nào.
    raw_bytes   INT NOT NULL,
    -- TRUE nếu body bị cắt theo SNAPSHOT_MAX_CHARS.
    truncated   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_crawl_snapshots_run_id ON crawl_snapshots(run_id);
-- Dọn bản ghi cũ + tra "mẫu mới nhất của nguồn X".
CREATE INDEX IF NOT EXISTS idx_crawl_snapshots_source_created
    ON crawl_snapshots(source, created_at DESC);

-- ============================================================
-- Import/export CSV-XLSX (import_previews)
-- ============================================================

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
-- Chạy bảo trì dữ liệu (maintenance_runs, maintenance_run_logs)
-- ============================================================

-- Thêm bảng maintenance_runs — chạy nền + theo dõi 5 script bảo trì dữ
-- liệu (backfill_company_profiles.py, enrich_company_profile_from_website.py,
-- enrich_company_web_info.py, get_company_fb_linkedin_link.py,
-- check_expired_source_jobs.py) từ trang web dưới quyền admin, KHÔNG cần
-- chạy tay CLI từ máy local nữa (08/2026, xem lịch sử trao đổi "phương án
-- B — 1 bảng generic dùng chung", đối xứng crawl_runs/crawl_run_logs
-- nhưng generic hoá qua cột job_type + params thay vì source/category
-- riêng cho crawl).
--
-- KHÁC crawl_runs ở 2 điểm CHỦ Ý:
--   1. Khoá đồng thời theo job_type (không phải source) — 5 job này
--      không có khái niệm "nguồn", mỗi LOẠI job tối đa 1 lượt
--      queued/running tại 1 thời điểm, nhưng 2 job_type khác nhau (vd
--      đang backfill + đang check_expired_jobs) vẫn chạy song song
--      được, không đụng dữ liệu nhau theo cách gây race condition rõ
--      ràng (khác 2 lượt CÙNG job_type dễ chọn trùng batch job/company
--      để xử lý).
--   2. params (JSONB) thay cho các cột riêng — mỗi job_type có tập
--      tham số khác nhau (limit; hoặc limit+dry_run+check_deadline_only
--      ở check_expired_jobs) — xem api/schemas/maintenance.py để biết
--      đúng shape từng job_type. Không ép kiểu ở tầng DB (khác cột
--      thường), validate ở tầng Pydantic trước khi tới đây.
--
-- An toàn để chạy lại nhiều lần (IF NOT EXISTS mọi bước, trừ CREATE TYPE
-- dùng khối DO $$ bắt lỗi duplicate_object — cùng pattern
-- migration_add_crawl_runs.sql).

DO $$ BEGIN
    CREATE TYPE maintenance_job_type_enum AS ENUM (
        'backfill_company_profiles',
        'enrich_profile_from_website',
        'enrich_web_info',
        'get_fb_linkedin',
        'check_expired_jobs'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS maintenance_runs (
    run_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    job_type      maintenance_job_type_enum NOT NULL,

    -- Tham số truyền vào run() của script tương ứng — vd
    -- {"limit": 50} hoặc {"limit": 100, "dry_run": true,
    -- "check_deadline_only": false}. '{}' nghĩa là chạy KHÔNG giới hạn
    -- (limit=None), giống chạy CLI không kèm --limit.
    params        JSONB NOT NULL DEFAULT '{}',

    -- Dùng LẠI crawl_status_enum đã có (queued/running/done/error) —
    -- cùng ý nghĩa, không cần tạo enum trạng thái riêng.
    status        crawl_status_enum NOT NULL DEFAULT 'queued',

    -- Điền khi status='done' — dict `stats` trả về từ run() của từng
    -- script (shape khác nhau tuỳ job_type, xem docstring run() từng
    -- file — vd checked/updated/unchanged/errors ở backfill, hoặc
    -- checked/expired_by_deadline/... ở check_expired_jobs).
    stats         JSONB,

    -- Điền khi status='error' (str(exception) bắt được trong execute()).
    error         TEXT,

    -- Admin nào bấm — NULL dành sẵn cho trường hợp chạy tự động theo
    -- lịch sau này (APScheduler/cron), không phải lỗi dữ liệu, cùng quy
    -- ước triggered_by ở crawl_runs.
    triggered_by  UUID REFERENCES app_users(ss_user_id),

    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_maintenance_runs_status       ON maintenance_runs(status);
CREATE INDEX IF NOT EXISTS idx_maintenance_runs_job_type     ON maintenance_runs(job_type);
CREATE INDEX IF NOT EXISTS idx_maintenance_runs_started_at   ON maintenance_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_maintenance_runs_triggered_by ON maintenance_runs(triggered_by);

-- ENFORCE Ở TẦNG DB: mỗi job_type chỉ được TỐI ĐA 1 dòng đang
-- 'queued' hoặc 'running' tại 1 thời điểm — UNIQUE INDEX có điều kiện,
-- đối xứng idx_crawl_runs_one_active_per_source nhưng khoá theo
-- job_type thay vì source. db/maintenance_runs.py::create_run() PHẢI
-- tự SELECT kiểm tra trước và raise ActiveMaintenanceRunExistsError để
-- router trả 409 rõ ràng — UNIQUE INDEX này là LỚP CHẶN THỨ 2 phòng
-- race condition, không phải lớp chặn chính (cùng lý do đã ghi ở
-- migration_add_crawl_runs.sql).
CREATE UNIQUE INDEX IF NOT EXISTS idx_maintenance_runs_one_active_per_job_type
    ON maintenance_runs(job_type)
    WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS maintenance_run_logs (
    id         BIGSERIAL PRIMARY KEY,
    run_id     UUID NOT NULL REFERENCES maintenance_runs(run_id) ON DELETE CASCADE,
    -- Khớp logging.LEVELNAME chuẩn của Python — dùng để tô màu dòng log
    -- khác nhau ở frontend, giống crawl_run_logs.level.
    level      VARCHAR(10) NOT NULL DEFAULT 'INFO',
    message    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Trang web poll GET /maintenance/{run_id}/logs?after_id=N để chỉ lấy
-- dòng MỚI — cùng lý do cần (run_id, id) như idx_crawl_run_logs_run_id_id.
CREATE INDEX IF NOT EXISTS idx_maintenance_run_logs_run_id_id ON maintenance_run_logs(run_id, id);

-- ============================================================
-- Cột cv_url của hồ sơ ứng tuyển
-- ============================================================

-- Migration: Thêm cột cv_url vào bảng job_applications
-- Lưu ý: cv_url là dạng TEXT lưu path nội bộ trong bucket (ví dụ: cv-files/user_id/app_id.pdf)
-- Nullable để các đơn ứng tuyển cũ trước đây không bị lỗi.

ALTER TABLE job_applications
    ADD COLUMN IF NOT EXISTS cv_url TEXT;

-- ============================================================
-- Cột phục vụ đăng nhập một phiên
-- ============================================================

-- Thêm cơ chế SINGLE ACTIVE SESSION (08/2026) — mục tiêu: 1 tài khoản
-- không được đăng nhập ĐỒNG THỜI ở 2 nơi khác nhau. Nhiều người thay
-- phiên dùng chung 1 tài khoản ở các thời điểm KHÁC nhau vẫn được (đó
-- là lựa chọn có chủ đích, không phải lỗ hổng) — chỉ chặn việc 2 phiên
-- cùng active song song.
--
-- Vấn đề trước đây: hệ thống cho phép vô hạn refresh token active song
-- song trên 1 tài khoản (xem docstring auth_refresh_tokens trong
-- migration_add_auth.sql) — không có gì ngăn 2 người tự login riêng
-- biệt rồi cùng dùng 1 lúc.
--
-- Thiết kế: mỗi lần login() thành công, sinh 1 session_id (UUID) MỚI,
-- ghi vào app_users.active_session_id VÀ nhúng vào JWT access token
-- (claim "sid") + gắn với refresh token của phiên đó. Mọi access token
-- CŨ (thuộc session_id khác) sẽ bị từ chối NGAY LẬP TỨC ở
-- get_current_user() (api/deps.py) — khác hành vi cũ là JWT chỉ verify
-- chữ ký, không query DB, nên token cũ vẫn sống tới khi tự hết hạn (tối
-- đa 30 phút). Đánh đổi: get_current_user() giờ cần 1 lượt query DB
-- mỗi request (tra theo primary key, rất rẻ) — chấp nhận được để đổi
-- lấy khả năng chặn NGAY LẬP TỨC thay vì có cửa sổ chồng lấn tới 30
-- phút giữa 2 phiên.
--
-- refresh() KHÔNG sinh session_id mới (chỉ login() mới sinh) — vì
-- refresh là xoay vòng token của CÙNG 1 phiên đang dùng, không phải
-- phiên mới; session_id được giữ nguyên từ phiên gốc.
--
-- NULL = tài khoản chưa từng login lần nào sau khi có cột này (dữ liệu
-- cũ trước migration), hoặc phiên đã bị logout/đổi mật khẩu/reset mật
-- khẩu (xem db.clear_active_session_id() — dùng chung 1 hàm
-- set_active_session_id() để set/clear).
--
-- An toàn để chạy lại nhiều lần (IF NOT EXISTS).

ALTER TABLE app_users ADD COLUMN IF NOT EXISTS active_session_id UUID;

-- ============================================================
-- Giá trị enum bổ sung và ghi chú cột
-- ============================================================

-- Audit log cho luồng ứng tuyển.
ALTER TYPE audit_action_enum ADD VALUE IF NOT EXISTS 'APPLY_JOB';
ALTER TYPE audit_action_enum ADD VALUE IF NOT EXISTS 'WITHDRAW_JOB_APPLICATION';

-- Hình thức làm việc linh hoạt.
ALTER TYPE work_type_enum ADD VALUE IF NOT EXISTS 'FLEXIBLE';

COMMENT ON COLUMN companies.source_profile_url IS
    'URL trang hồ sơ công ty trên nguồn crawl gốc (TopCV/VietnamWorks/CareerViet) — dùng để backfill lại industry/company_size/address/website sau này mà không cần công ty còn job đang active.';

-- ============================================================
-- HẾT FILE
-- ============================================================
