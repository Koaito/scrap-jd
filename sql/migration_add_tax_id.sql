-- Chạy đoạn này 1 lần trên database đã tạo sẵn (student_success) để thêm
-- cột tax_id vào bảng companies mà không cần xóa/tạo lại DB.
--
-- Cách chạy:
--   psql -U postgres -d student_success -f sql/migration_add_tax_id.sql
-- Hoặc copy-paste trực tiếp vào psql.

ALTER TABLE companies ADD COLUMN IF NOT EXISTS tax_id VARCHAR(20);

CREATE UNIQUE INDEX IF NOT EXISTS uq_companies_tax_id
    ON companies(tax_id) WHERE tax_id IS NOT NULL;
