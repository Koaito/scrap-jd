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

DO $$ BEGIN
    CREATE TYPE job_status_enum AS ENUM (
        'OPEN', 'EXPIRED', 'CLOSED'
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

CREATE TABLE IF NOT EXISTS job_sources_log (
    log_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id            UUID NOT NULL REFERENCES job_postings(job_id),
    source_name       VARCHAR(100),
    source_url        VARCHAR(500),
    raw_jd_content    TEXT,
    salary_raw_content VARCHAR(255),
    collected_date    DATE NOT NULL DEFAULT CURRENT_DATE,

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
        'CREATE_CONTACT', 'UPDATE_CONTACT', 'DELETE_CONTACT', 'ASSIGN_CONTACT'
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
-- 2d. LỊCH SỬ + TRẠNG THÁI CRAWL (crawl_runs) — xem
-- sql/migration_add_crawl_runs.sql để biết đầy đủ lý do thiết kế.
--
-- THAY THẾ _RUNS (dict RAM cũ trong api/crawl_runner.py) — sống bền qua
-- restart server, đồng bộ được nếu chạy nhiều worker uvicorn.
-- ============================================================

DO $$ BEGIN
    CREATE TYPE crawl_status_enum AS ENUM ('queued', 'running', 'done', 'error');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS crawl_runs (
    run_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source        VARCHAR(50)  NOT NULL,
    category      VARCHAR(100) NOT NULL,
    pages         INT NOT NULL,
    max_jobs      INT,
    status        crawl_status_enum NOT NULL DEFAULT 'queued',
    stats         JSONB,
    error         TEXT,
    triggered_by  UUID REFERENCES app_users(ss_user_id),
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);

-- Mỗi source tối đa 1 dòng 'queued'/'running' cùng lúc — xem
-- migration_add_crawl_runs.sql để biết đầy đủ lý do (LỚP CHẶN THỨ 2,
-- lớp chính là SELECT check ở db/crawl_runs.py::create_run()).
CREATE UNIQUE INDEX IF NOT EXISTS idx_crawl_runs_one_active_per_source
    ON crawl_runs(source)
    WHERE status IN ('queued', 'running');

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
CREATE INDEX IF NOT EXISTS idx_crawl_runs_status       ON crawl_runs(status);
CREATE INDEX IF NOT EXISTS idx_crawl_runs_started_at   ON crawl_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_crawl_runs_source       ON crawl_runs(source);
CREATE INDEX IF NOT EXISTS idx_crawl_runs_triggered_by ON crawl_runs(triggered_by);

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

-- ============================================================
-- HẾT FILE
-- ============================================================
