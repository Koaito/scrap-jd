-- Thêm cột companies.source_profile_url — lưu lại URL trang hồ sơ công
-- ty trên nguồn crawl (TopCV/VietnamWorks), vd:
--   https://www.topcv.vn/cong-ty/lg-electronics-development-vietnam-lgedv/149958.html
--   https://www.topcv.vn/brand/tuyendungvietabank?id=26756
--
-- TẠI SAO CẦN CỘT NÀY (08/2026, sau khi phát hiện qua debug thật):
-- TRƯỚC ĐÂY, company_url (link trang công ty) chỉ tồn tại TẠM THỜI trong
-- RawJobRecord lúc đang crawl 1 job cụ thể (adapters/topcv.py ->
-- pipeline.py), dùng ngay để gọi fetch_company_profile() rồi VỨT BỎ —
-- không lưu vào DB ở đâu cả. Hệ quả: muốn vá lại industry/company_size/
-- address/website cho 1 công ty đã có sẵn trong DB (vd parser cũ đọc sai
-- label trên layout Brand Pro, đã sửa ở adapters/topcv.py), CHỈ vá lại
-- được nếu công ty đó VẪN CÒN ÍT NHẤT 1 JOB ĐANG ACTIVE trên listing —
-- job hết hạn/gỡ khỏi listing = mất vĩnh viễn khả năng crawl lại, dù
-- trang hồ sơ công ty trên TopCV vẫn còn nguyên đó.
--
-- Có cột này: mỗi lần thấy company_url mới cho 1 công ty, LUÔN ghi lại
-- vào đây (xem pipeline.py, db.py: update_company_profile /
-- get_or_create_company_by_profile) — độc lập với việc job đó còn active
-- hay không. Sau này muốn backfill lại field nào, chỉ cần đọc URL đã lưu
-- ở đây, gọi lại fetch_company_profile(), không phụ thuộc job listing.
--
-- Không unique, không NOT NULL — công ty tạo tay qua POST /companies
-- (chưa từng crawl từ nguồn nào) sẽ có giá trị NULL, hợp lệ.
--
-- Cách chạy:
--   psql -U postgres -d student_success -f sql/migration_add_source_profile_url.sql
-- Hoặc copy-paste trực tiếp vào psql.

ALTER TABLE companies ADD COLUMN IF NOT EXISTS source_profile_url VARCHAR(500);

COMMENT ON COLUMN companies.source_profile_url IS
    'URL trang hồ sơ công ty trên nguồn crawl gốc (TopCV/VietnamWorks) — dùng để backfill lại industry/company_size/address/website sau này mà không cần công ty còn job đang active.';
