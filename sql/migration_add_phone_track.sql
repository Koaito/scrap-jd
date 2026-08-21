-- Thêm cột phone/track vào app_users (08/2026) — xem lịch sử trao đổi:
-- frontend (mindx-jobs) đã có sẵn 2 ô nhập này ở form /register từ
-- trước (số điện thoại + định hướng ngành Code/Data Analysis/Business
-- Analysis), gửi kèm lên POST /auth/register, nhưng backend chưa có
-- cột lưu nên Pydantic tự bỏ qua field lạ — dữ liệu học viên nhập mất
-- hoàn toàn, không ai xem lại được.
--
-- Mục đích 2 field này (đúng luồng nghiệp vụ, không phải trang trí):
--   phone — để team SS liên hệ trực tiếp (gọi/nhắn) học viên khi có
--           job phù hợp, không chỉ email.
--   track — định hướng ngành học viên quan tâm, để team SS biết nên
--           giới thiệu job nào cho ai khi chủ động liên hệ.
--
-- An toàn để chạy lại nhiều lần.
--
-- Cách chạy:
--   psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_phone_track.sql

ALTER TABLE app_users ADD COLUMN IF NOT EXISTS phone VARCHAR(30);

-- Không dùng ENUM riêng — track chỉ hiển thị/lọc, không phải khoá logic
-- gì (giống matching_industry ở job_postings, cũng VARCHAR tự do), và
-- danh sách ngành (Code/Data Analysis/Business Analysis) có thể đổi ở
-- phía frontend (INDUSTRIES trong app.py) mà không cần sửa lại DB.
ALTER TABLE app_users ADD COLUMN IF NOT EXISTS track VARCHAR(100);

-- ============================================================
-- HẾT FILE
-- ============================================================
