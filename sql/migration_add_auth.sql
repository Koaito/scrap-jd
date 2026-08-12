-- Thêm hệ thống đăng nhập THẬT cho từng thành viên team (08/2026) —
-- KHÁC với API_KEY tĩnh (api/auth.py, dùng chung cho mọi client kiểu
-- "máy gọi máy"). Đây là lớp đăng nhập TỪNG NGƯỜI qua frontend (JWT
-- access token ngắn hạn + refresh token xoay vòng).
--
-- Thiết kế: mở rộng THẲNG bảng ss_team_members đã có (không tạo bảng
-- users riêng) — bảng này vốn đã đại diện đúng "người trong team", và
-- đã được job_contact_interactions/company_contacts tham chiếu tới qua
-- assigned_ss_user. Đánh đổi: trộn "thông tin nghiệp vụ" và "thông tin
-- bảo mật" vào 1 bảng — chấp nhận được ở quy mô team nhỏ (2-5 người),
-- xem phân tích ưu/nhược đầy đủ trong lịch sử trao đổi trước khi code.
--
-- An toàn để chạy lại nhiều lần (IF NOT EXISTS ở mọi bước).
--
-- Cách chạy:
--   psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_auth.sql

-- ============================================================
-- 1. Mở rộng ss_team_members
-- ============================================================

ALTER TABLE ss_team_members ADD COLUMN IF NOT EXISTS password_hash TEXT;

-- role: quy ước 'admin' (tạo user khác, reset mật khẩu hộ người khác)
-- hoặc 'member' (mặc định, thành viên thường) — xem require_admin() ở
-- api/deps.py. Không dùng ENUM riêng vì chỉ 2 giá trị, ít khả năng đổi,
-- VARCHAR đơn giản hơn cho quy mô này.
ALTER TABLE ss_team_members ADD COLUMN IF NOT EXISTS role VARCHAR(20) NOT NULL DEFAULT 'member';

-- Ép đổi mật khẩu ở lần đăng nhập kế tiếp — dùng khi: (a) admin tạo tài
-- khoản mới bằng mật khẩu tạm, hoặc (b) admin reset mật khẩu hộ người
-- quên. Mặc định true cho tài khoản mới tạo.
ALTER TABLE ss_team_members ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT true;

-- Đếm số lần đăng nhập sai LIÊN TIẾP — reset về 0 khi đăng nhập đúng
-- (xem db.reset_failed_login()). Dùng cùng locked_until để khoá tạm
-- thời sau N lần sai (xem db.record_failed_login()).
ALTER TABLE ss_team_members ADD COLUMN IF NOT EXISTS failed_login_count INT NOT NULL DEFAULT 0;

-- NULL = không bị khoá. Có giá trị + còn ở tương lai (so với now()) =
-- đang bị khoá tạm thời. Tự hết hạn, không cần cron job dọn dẹp.
ALTER TABLE ss_team_members ADD COLUMN IF NOT EXISTS locked_until TIMESTAMPTZ;

ALTER TABLE ss_team_members ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ;

-- is_active đã có sẵn từ schema.sql gốc (BOOLEAN NOT NULL DEFAULT TRUE)
-- — tái dùng để "khoá vĩnh viễn" 1 tài khoản (khác locked_until là khoá
-- TẠM THỜI do sai mật khẩu nhiều lần). Không thêm cột mới.

-- ============================================================
-- 2. Bảng refresh token — hỗ trợ xoay vòng (rotation) + phát hiện tái
-- sử dụng token đã bị thu hồi (dấu hiệu token bị đánh cắp).
-- ============================================================

CREATE TABLE IF NOT EXISTS auth_refresh_tokens (
    refresh_token_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ss_user_id          UUID NOT NULL REFERENCES ss_team_members(ss_user_id),

    -- KHÔNG lưu refresh token thô — chỉ lưu SHA-256 hex (64 ký tự) của
    -- nó (xem api/security.py hash_refresh_token()). Nếu DB bị lộ, kẻ
    -- tấn công vẫn không có token thật để dùng, giống cách lưu
    -- password_hash chứ không lưu mật khẩu thô.
    token_hash          VARCHAR(64) NOT NULL UNIQUE,

    expires_at           TIMESTAMPTZ NOT NULL,
    revoked_at            TIMESTAMPTZ,

    -- Khi 1 token bị xoay vòng (dùng để lấy access token mới), token CŨ
    -- bị revoke và trỏ replaced_by_token_id sang token MỚI. Nếu sau đó
    -- có ai dùng LẠI token cũ đã bị revoke này (revoked_at IS NOT NULL)
    -- -> dấu hiệu rõ ràng token bị đánh cắp (người hợp lệ không có lý do
    -- dùng lại token đã đổi) -> route tự động thu hồi TOÀN BỘ token của
    -- user này (xem db.revoke_all_refresh_tokens_for_user()).
    replaced_by_token_id  UUID REFERENCES auth_refresh_tokens(refresh_token_id),

    -- Ghi lại để dễ audit/debug (vd user báo "tôi không đăng nhập ở
    -- đâu khác" nhưng có token lạ) — KHÔNG dùng để tự động chặn gì.
    user_agent             TEXT,
    ip_address               VARCHAR(45),  -- đủ cho IPv6

    created_at                TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_auth_refresh_tokens_user   ON auth_refresh_tokens(ss_user_id);
CREATE INDEX IF NOT EXISTS idx_auth_refresh_tokens_expiry ON auth_refresh_tokens(expires_at);

-- ============================================================
-- HẾT FILE
-- ============================================================
