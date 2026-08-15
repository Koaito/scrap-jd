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

CREATE TABLE IF NOT EXISTS ss_team_members (
    ss_user_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name       VARCHAR(255) NOT NULL,
    email           VARCHAR(255) NOT NULL UNIQUE,
    role            VARCHAR(50),
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMP NOT NULL DEFAULT now()
);

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
    created_at       TIMESTAMP NOT NULL DEFAULT now(),
    updated_at       TIMESTAMP NOT NULL DEFAULT now()
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
    deadline          DATE,
    job_status        job_status_enum NOT NULL DEFAULT 'OPEN',
    ss_team_notes     TEXT,
    content_hash      VARCHAR(64),
    source_url        VARCHAR(500),
    created_at        TIMESTAMP NOT NULL DEFAULT now(),
    updated_at        TIMESTAMP NOT NULL DEFAULT now(),

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
    assigned_ss_user  UUID REFERENCES ss_team_members(ss_user_id),
    last_contacted_date DATE,
    contact_status    contact_status_enum NOT NULL DEFAULT 'UNCONTACTED',
    created_at        TIMESTAMP NOT NULL DEFAULT now(),
    updated_at        TIMESTAMP NOT NULL DEFAULT now()
);

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
    assigned_ss_user  UUID REFERENCES ss_team_members(ss_user_id),
    interaction_type  VARCHAR(50),
    interaction_status interaction_status_enum,
    note              TEXT,
    interaction_date  DATE NOT NULL DEFAULT CURRENT_DATE
);

-- ============================================================
-- 3. INDEXES
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_companies_province        ON companies(province_id);
CREATE INDEX IF NOT EXISTS idx_job_postings_company       ON job_postings(company_id);
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
