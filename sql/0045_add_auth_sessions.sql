-- Bảng phiên đăng nhập "mờ" lưu trong DB (Mục 3, Pha A) — thay dần JWT + refresh token xoay vòng.
--
-- Mô hình: cookie phía frontend chứa một session token NGẪU NHIÊN, không có nội dung (dạng `s1.<48 byte
-- urlsafe>`). DB chỉ lưu SHA-256 hex của token (token_hash), KHÔNG lưu token thô. Mỗi request tra token_hash,
-- kiểm tra còn hạn, chưa thu hồi và người dùng còn is_active.
--
-- session_id dùng CHUNG với app_users.active_session_id và claim `sid` của JWT cùng lần đăng nhập: trong Pha A
-- (backend chấp nhận cả JWT cũ lẫn session token mới) một lần đăng nhập sinh MỘT session_id, và thu hồi bằng
-- cách nào cũng làm chết cả hai loại credential. Nhờ vậy single-session giữ nguyên ý nghĩa.
--
-- Hai hạn riêng, cả hai đều bắt buộc:
--   expires_at      hạn TUYỆT ĐỐI, tính từ lúc tạo, không bao giờ kéo dài.
--   idle_expires_at hạn TRƯỢT, đẩy lên mỗi khi phiên được dùng (có giới hạn tần suất ghi), luôn <= expires_at.
-- Giá trị mặc định (30 ngày và 7 ngày) do ứng dụng đặt (SESSION_ABSOLUTE_DAYS, SESSION_IDLE_DAYS), không nằm
-- trong DB, nên đổi hạn không cần migration.
--
-- revoked_reason: logout | replaced | password_changed | admin | inactive. `replaced` (đăng nhập ở nơi khác) và
-- các lý do còn lại cho ra hai mã lỗi khác nhau ở API (session_replaced, session_revoked), nên phải lưu lý do.
-- Dòng đã thu hồi được GIỮ tới hết hạn tuyệt đối (cộng một ngày) để client cũ còn nhận đúng thông báo, rồi
-- mới bị dọn (xem scrapjd/api/services/session_cleanup.py).
--
-- ip_address là VARCHAR(45) chứ không phải INET, cùng lý do với auth_refresh_tokens: giá trị lấy từ request
-- không đảm bảo là IP hợp lệ (vd "testclient"), kiểu INET sẽ làm đăng nhập lỗi.
--
-- Migration CHỈ THÊM (bảng + index), chạy TRƯỚC khi push code dùng nó. Idempotent. Bảng mới nên không cần
-- khoá bảng nào đang có (khoá khoá ngoại ngắn trên app_users, vài mili giây).

SET LOCAL lock_timeout = '10s';

CREATE TABLE IF NOT EXISTS auth_sessions (
    session_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ss_user_id       UUID NOT NULL REFERENCES app_users(ss_user_id),

    -- SHA-256 hex (64 ký tự thường). CHECK chặn việc lỡ ghi token thô vào đây.
    token_hash       VARCHAR(64) NOT NULL UNIQUE,

    created_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    last_seen_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    expires_at       TIMESTAMPTZ NOT NULL,
    idle_expires_at  TIMESTAMPTZ NOT NULL,

    revoked_at       TIMESTAMPTZ,
    revoked_reason   VARCHAR(30),

    user_agent       TEXT,
    ip_address       VARCHAR(45),

    CONSTRAINT ck_auth_sessions_token_hash_sha256
        CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_auth_sessions_revoked_pair
        CHECK ((revoked_at IS NULL) = (revoked_reason IS NULL)),
    CONSTRAINT ck_auth_sessions_revoked_reason
        CHECK (revoked_reason IS NULL
               OR revoked_reason IN ('logout', 'replaced', 'password_changed', 'admin', 'inactive')),
    CONSTRAINT ck_auth_sessions_idle_within_absolute
        CHECK (idle_expires_at <= expires_at)
);

CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
    ON auth_sessions(ss_user_id);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user_active
    ON auth_sessions(ss_user_id) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry
    ON auth_sessions(expires_at);
