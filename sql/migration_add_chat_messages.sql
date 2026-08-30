-- Hệ thống nhắn tin — học viên ↔ SS / SS ↔ SS (08/2026).
-- Xem kế hoạch đầy đủ: backend-scrap-jd-nhan-tin.md.
--
-- Chỉ tạo 2 bảng dữ liệu (Việc 1). Các câu UPDATE/INSERT state machine
-- (Việc 2) KHÔNG nằm trong file này — đó là logic tầng application
-- (api/routers/messages.py), chạy với tham số theo từng request, không
-- phải thay đổi schema. Xem chi tiết 7 transition ở
-- backend-scrap-jd-nhan-tin.md §2.
--
-- Idempotent — an toàn chạy lại nhiều lần (dùng IF NOT EXISTS /
-- EXCEPTION WHEN duplicate_object cho CHECK constraint).
--
-- Cách chạy: python main.py migrate
-- (Đã cập nhật song song vào sql/schema.sql cho DB mới — xem
-- README_MIGRATIONS.md mục "Quy trình thêm 1 thay đổi schema mới".)

-- ============================================================
-- 1. Quan hệ nhắn tin học viên ↔ SS (state machine)
-- KHÔNG có bảng này cho cặp SS↔SS — cặp đó luôn được phép, không
-- cần gate (xem router, không cần schema riêng).
--
-- Tên bảng người dùng thật là app_users (đổi từ ss_team_members, xem
-- migration_rename_ss_team_members.sql), role học viên là 'user'
-- (không phải 'student') — student_id/ss_id dưới đây chỉ là tên cột
-- cho dễ đọc.
-- ============================================================
CREATE TABLE IF NOT EXISTS chat_relationships (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    student_id      UUID NOT NULL REFERENCES app_users(ss_user_id),
    ss_id           UUID NOT NULL REFERENCES app_users(ss_user_id),
    status          TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'declined', 'blocked')),
    initiated_by    UUID NOT NULL REFERENCES app_users(ss_user_id),
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    decided_at      TIMESTAMPTZ,
    declined_at     TIMESTAMPTZ,   -- dùng để tính cooldown 7 ngày trước khi cho gửi lại request
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (student_id, ss_id)
);

-- Tăng tốc COUNT(*) đếm số pending hiện tại của 1 học viên (chặn spam
-- request tới hàng loạt SS khác nhau — xem §2(a) trong kế hoạch).
CREATE INDEX IF NOT EXISTS idx_chat_relationships_student_status
    ON chat_relationships (student_id, status);

-- Tăng tốc GET /messages/conversations phần "Yêu cầu đang chờ" của SS.
CREATE INDEX IF NOT EXISTS idx_chat_relationships_ss_status
    ON chat_relationships (ss_id, status);

-- ============================================================
-- 2. Tin nhắn — dùng chung cho MỌI cặp (học viên-SS lẫn SS-SS).
-- id dùng BIGSERIAL (không phải UUID) để so sánh "id > last_seen"
-- / "before_id" tự nhiên, không cần cột thời gian phụ cho cursor.
-- ============================================================
CREATE TABLE IF NOT EXISTS messages (
    id              BIGSERIAL PRIMARY KEY,
    sender_id       UUID NOT NULL REFERENCES app_users(ss_user_id),
    receiver_id     UUID NOT NULL REFERENCES app_users(ss_user_id),
    content         TEXT NOT NULL CHECK (char_length(btrim(content)) BETWEEN 1 AND 2000),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    read_at         TIMESTAMPTZ,
    CHECK (sender_id != receiver_id)
);

-- Index cho query "lịch sử giữa A và B" — CẦN CẢ 2 CHIỀU, 1 index
-- (sender_id, receiver_id, id) chỉ tối ưu 1 chiều, nửa còn lại vẫn
-- table-scan. Postgres gộp 2 index này bằng Bitmap OR.
CREATE INDEX IF NOT EXISTS idx_messages_sender_receiver ON messages (sender_id, receiver_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_receiver_sender ON messages (receiver_id, sender_id, id);

-- Partial index cho đếm unread — chỉ index dòng read_at IS NULL,
-- nhỏ gọn, không phình theo lịch sử tin đã đọc.
CREATE INDEX IF NOT EXISTS idx_messages_unread ON messages (receiver_id, id) WHERE read_at IS NULL;

-- ============================================================
-- HẾT FILE
-- ============================================================
