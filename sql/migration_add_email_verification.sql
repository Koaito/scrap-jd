-- Phần 2/2 của việc mở rộng phân quyền + đăng ký (08/2026) — xem lịch
-- sử trao đổi trước khi đọc file này. Thêm cột cho luồng ĐĂNG KÝ CÔNG
-- KHAI (POST /auth/register, role mặc định 'user') + xác thực email
-- qua Resend TRƯỚC khi tài khoản dùng được (login bị chặn nếu
-- email_verified=false — xem api/routers/auth.py).
--
-- Khác POST /auth/users (admin tạo hộ, must_change_password=true,
-- KHÔNG cần xác thực email vì admin đã đích thân xác nhận danh tính
-- người được tạo hộ) — 2 luồng tạo tài khoản độc lập, cùng ghi vào 1
-- bảng ss_team_members.
--
-- An toàn để chạy lại nhiều lần.
--
-- Cách chạy (SAU migration_add_role_hierarchy.sql, TRƯỚC khi deploy
-- code Phần 2):
--   psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_email_verification.sql

ALTER TABLE ss_team_members ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT false;

-- Token xác thực — chuỗi ngẫu nhiên (giống refresh_token, xem
-- security.generate_refresh_token()), DB lưu bản THÔ (không hash) vì
-- token này CHỈ dùng 1 lần rồi bị xoá ngay sau khi verify thành công
-- (đặt lại NULL, xem db.verify_user_email()) — không có giá trị lâu dài
-- để cần bảo vệ như refresh_token (vốn sống hàng tháng, phải hash đề
-- phòng rò rỉ DB).
ALTER TABLE ss_team_members ADD COLUMN IF NOT EXISTS email_verify_token VARCHAR(255);
ALTER TABLE ss_team_members ADD COLUMN IF NOT EXISTS email_verify_expires TIMESTAMPTZ;

-- Tra cứu nhanh theo token khi user bấm link trong email (GET
-- /auth/verify-email?token=...) — bảng này quy mô nhỏ (vài chục -
-- vài trăm dòng, team nội bộ) nên index không quan trọng bằng đúng
-- tính, nhưng thêm cho rõ ý định và phòng khi số lượng user tăng.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ss_team_members_email_verify_token
    ON ss_team_members(email_verify_token)
    WHERE email_verify_token IS NOT NULL;

-- Tài khoản ĐÃ CÓ TỪ TRƯỚC (tạo qua POST /auth/users, hoặc admin đầu
-- tiên qua CLI create-admin) coi như ĐÃ xác thực — chỉ áp dụng luồng
-- xác thực email cho tài khoản đăng ký công khai TỪ NAY TRỞ ĐI, không
-- hồi tố bắt các tài khoản cũ (không có email hợp lệ để verify lại,
-- và admin đã đích thân xác nhận danh tính lúc tạo).
UPDATE ss_team_members SET email_verified = true WHERE email_verified = false;

-- ============================================================
-- HẾT FILE
-- ============================================================
