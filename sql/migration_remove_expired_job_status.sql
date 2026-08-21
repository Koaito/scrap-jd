-- Migration 08/2026: bỏ giá trị 'EXPIRED' khỏi job_status_enum, chỉ còn
-- 'OPEN' / 'CLOSED'. Quyết định gộp: job hết hạn tự nhiên (deadline qua
-- hạn / link nguồn 404-410) giờ coi như CLOSED luôn, không phân biệt
-- "job chết tự nhiên" vs "SS chủ động đóng" ở tầng job_status nữa
-- (xem lịch sử trao đổi — trước đây check_expired_source_jobs.py cố
-- tình tách 2 status này, giờ đổi ý, gộp lại cho đơn giản).
--
-- Postgres KHÔNG cho DROP 1 value khỏi enum trực tiếp (ALTER TYPE ...
-- DROP VALUE không tồn tại) — cách an toàn duy nhất là: tạo enum mới
-- không có EXPIRED, đổi cột job_postings.job_status sang enum mới
-- (ép kiểu qua text, map EXPIRED -> CLOSED), xoá enum cũ, đổi tên enum
-- mới lại đúng tên cũ.
--
-- An toàn để chạy lại nhiều lần (kiểm tra tồn tại trước khi tạo/xoá).
-- Tại thời điểm viết migration này, KHÔNG có job nào đang ở job_status
-- = 'EXPIRED' trong production — bước UPDATE bên dưới vẫn giữ lại
-- phòng trường hợp có dữ liệu mới phát sinh trước khi migration chạy.
--
-- Cách chạy:
--   psql "$DATABASE_URL" -f sql/migration_remove_expired_job_status.sql

BEGIN;

-- 1. Phòng hờ: nếu có job nào đang EXPIRED tại thời điểm chạy migration,
--    chuyển sang CLOSED trước khi đổi kiểu cột (bước 3 sẽ ép kiểu qua
--    text nên về mặt kỹ thuật không bắt buộc bước này, nhưng làm tường
--    minh cho rõ ý định + backfill notes nếu cần).
UPDATE job_postings SET job_status = 'CLOSED' WHERE job_status::text = 'EXPIRED';

-- 2. Enum mới, không có EXPIRED.
DO $$ BEGIN
    CREATE TYPE job_status_enum_new AS ENUM ('OPEN', 'CLOSED');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- 3. Đổi kiểu cột: ép qua text rồi map EXPIRED -> CLOSED, còn lại giữ
--    nguyên (OPEN/CLOSED đã hợp lệ ở enum mới).
ALTER TABLE job_postings
    ALTER COLUMN job_status DROP DEFAULT,
    ALTER COLUMN job_status TYPE job_status_enum_new
        USING (
            CASE WHEN job_status::text = 'EXPIRED' THEN 'CLOSED' ELSE job_status::text END
        )::job_status_enum_new,
    ALTER COLUMN job_status SET DEFAULT 'OPEN';

-- 4. Xoá enum cũ, đổi tên enum mới về đúng tên cũ (job_status_enum) để
--    không phải sửa lại chỗ nào khác tham chiếu tên type này.
DROP TYPE job_status_enum;
ALTER TYPE job_status_enum_new RENAME TO job_status_enum;

COMMIT;

-- ============================================================
-- HẾT FILE
-- ============================================================
