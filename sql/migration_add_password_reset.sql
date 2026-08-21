-- Thêm "Quên mật khẩu" — thêm 08/2026. Mirror ĐÚNG cơ chế email_verify_token/
-- email_verify_expires đã có (xem sql/migration_add_email_verification.sql),
-- chỉ khác tên cột + thời hạn ngắn hơn (1h thay vì 24h — reset mật khẩu
-- nhạy cảm hơn xác thực email, không cần cho người dùng nhiều thời gian).
--
-- An toàn để chạy lại nhiều lần (IF NOT EXISTS).
--
-- Cách chạy:
--   psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_password_reset.sql

ALTER TABLE app_users ADD COLUMN IF NOT EXISTS password_reset_token VARCHAR(255);
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS password_reset_expires TIMESTAMPTZ;

-- Tra cứu theo token khi user bấm link trong email — cần index vì đây
-- là truy vấn WHERE trên cột không phải khoá chính/unique tự nhiên.
CREATE INDEX IF NOT EXISTS idx_app_users_password_reset_token ON app_users(password_reset_token)
    WHERE password_reset_token IS NOT NULL;
