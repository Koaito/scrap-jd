-- Migration: Thêm cột cv_url vào bảng job_applications
-- Lưu ý: cv_url là dạng TEXT lưu path nội bộ trong bucket (ví dụ: cv-files/user_id/app_id.pdf)
-- Nullable để các đơn ứng tuyển cũ trước đây không bị lỗi.

ALTER TABLE job_applications
    ADD COLUMN IF NOT EXISTS cv_url TEXT;
