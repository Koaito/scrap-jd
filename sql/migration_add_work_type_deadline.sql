-- Đảm bảo job_postings có đủ cột work_type (kiểu enum) và deadline (DATE).
-- An toàn để chạy lại nhiều lần, xử lý cả 3 trường hợp:
--   1. Cột work_type/deadline chưa tồn tại (DB tạo từ schema rất cũ)
--      -> thêm mới đúng kiểu ngay từ đầu.
--   2. Cột work_type đã tồn tại nhưng là VARCHAR (DB tạo trước khi có enum)
--      -> convert sang work_type_enum, map text cũ sang đúng giá trị,
--         text không khớp -> NULL (không làm mất job).
--   3. Đã đúng kiểu enum từ trước -> không làm gì thêm.
--
-- Cách chạy:
--   psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_work_type_deadline.sql

DO $$ BEGIN
    CREATE TYPE work_type_enum AS ENUM (
        'FULL_TIME', 'PART_TIME', 'INTERNSHIP', 'OTHER'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- deadline: chỉ cần thêm cột nếu chưa có, không có gì để convert
ALTER TABLE job_postings ADD COLUMN IF NOT EXISTS deadline DATE;

-- work_type: thêm mới đúng kiểu enum nếu cột chưa tồn tại, hoặc convert
-- nếu đang là VARCHAR từ code cũ.
DO $$
DECLARE
    current_type text;
BEGIN
    SELECT data_type INTO current_type
    FROM information_schema.columns
    WHERE table_name = 'job_postings' AND column_name = 'work_type';

    IF current_type IS NULL THEN
        -- Cột chưa tồn tại
        ALTER TABLE job_postings ADD COLUMN work_type work_type_enum;
    ELSIF current_type IS DISTINCT FROM 'USER-DEFINED' THEN
        -- Cột đang là VARCHAR (từ code cũ) -> convert, map text -> enum
        ALTER TABLE job_postings
            ALTER COLUMN work_type TYPE work_type_enum
            USING (
                CASE trim(work_type)
                    WHEN 'Toàn thời gian' THEN 'FULL_TIME'
                    WHEN 'Bán thời gian' THEN 'PART_TIME'
                    WHEN 'Thực tập' THEN 'INTERNSHIP'
                    WHEN 'Khác' THEN 'OTHER'
                    ELSE NULL
                END
            )::work_type_enum;
    END IF;
    -- Nếu current_type = 'USER-DEFINED': đã là enum từ trước, không làm gì.
END $$;
