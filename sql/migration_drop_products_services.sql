-- Migration bổ sung 08/2026 (3 việc độc lập, gộp 1 file cho gọn — xem
-- lịch sử trao đổi trước khi đọc file này):
--
-- 1. KHÔNG cần đổi schema — job_postings.parsed_content (JSONB) đã tồn
--    tại từ đầu, chỉ thiếu chỗ ghi ở JobCreate/JobUpdate (sửa ở code,
--    xem api/schemas.py + api/routers/jobs.py). Ghi chú ở đây để không
--    ai nhầm tưởng cần ALTER TABLE cho việc này.
--
-- 2. Xoá hẳn companies.products_services — field mô tả sản phẩm/dịch vụ
--    công ty, do crawl (profile.get("description")) lẫn form nhập tay
--    (thật ra CHƯA từng có form nào gửi field này — chỉ pipeline crawl
--    dùng) ghi vào. Xác nhận xoá theo yêu cầu, KHÔNG giữ lại dữ liệu cũ.
--
-- 3. Cho phép huỷ ứng tuyển — job_applications trước đây cố ý không có
--    DELETE (coi là "sự kiện lịch sử"), giờ đổi ý: học viên bấm nhầm/
--    muốn rút đơn thì cần rút được. KHÔNG cần ALTER TABLE (DELETE thẳng
--    dùng UNIQUE constraint sẵn có) — chỉ cần route mới
--    (DELETE /me/applications/{job_id}), ghi chú ở đây cho đủ bộ.
--
-- An toàn để chạy lại nhiều lần.
--
-- Cách chạy (SAU migration_add_applications_saved_jobs.sql):
--   psql "$DATABASE_URL" -f sql/migration_drop_products_services.sql

ALTER TABLE companies DROP COLUMN IF EXISTS products_services;

-- ============================================================
-- HẾT FILE
-- ============================================================
