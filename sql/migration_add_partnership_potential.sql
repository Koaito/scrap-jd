-- Thêm cột partnership_potential vào bảng companies để staff tự đánh giá
-- mức độ tiềm năng hợp tác của từng công ty (đánh tay qua UI add/edit
-- company, không có rule tự động gán).
--
-- Giá trị: HIGH / MEDIUM / LOW / UNVERIFIED.
-- Mặc định UNVERIFIED cho company hiện có và company mới thêm — nghĩa là
-- "chưa đánh giá", không phải "tiềm năng thấp". Staff cần chủ động đổi
-- sang HIGH/MEDIUM/LOW sau khi review.
--
-- An toàn để chạy lại nhiều lần.
--
-- Cách chạy:
--   psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_partnership_potential.sql

DO $$ BEGIN
    CREATE TYPE partnership_potential_enum AS ENUM (
        'HIGH', 'MEDIUM', 'LOW', 'UNVERIFIED'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE companies
    ADD COLUMN IF NOT EXISTS partnership_potential partnership_potential_enum
    NOT NULL DEFAULT 'UNVERIFIED';

CREATE INDEX IF NOT EXISTS idx_companies_partnership_potential
    ON companies(partnership_potential);
