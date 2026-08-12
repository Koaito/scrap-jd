-- Cập nhật bảng `provinces` theo đúng danh sách 34 tỉnh/thành sau sáp nhập
-- (Nghị quyết 202/2025/QH15, Quốc hội thông qua 12/6/2025, hiệu lực từ
-- 01/7/2025 — cả nước giảm từ 63 xuống 34 đơn vị hành chính cấp tỉnh,
-- gồm 28 tỉnh + 6 thành phố trực thuộc Trung ương).
--
-- CHỈ THÊM tỉnh còn thiếu (ON CONFLICT DO NOTHING) — KHÔNG xoá/đổi tên các
-- dòng cũ đã có, vì job_postings.province_id đang tham chiếu tới chúng
-- (xoá sẽ vi phạm foreign key hoặc làm "mồ côi" dữ liệu job đã crawl
-- trước đó dưới tên tỉnh cũ, vd "Bình Dương" — nay đã sáp nhập vào
-- TP. Hồ Chí Minh nhưng vẫn cần giữ lại để không mất dữ liệu lịch sử).
--
-- Cách chạy: dán nguyên file này vào Supabase SQL Editor rồi Run, hoặc
--   psql -U postgres -d <database> -f sql/migration_update_provinces_2025.sql

INSERT INTO provinces (province_name) VALUES
    ('Tuyên Quang'),   -- hợp nhất Hà Giang + Tuyên Quang
    ('Cao Bằng'),      -- giữ nguyên
    ('Lai Châu'),      -- giữ nguyên
    ('Lào Cai'),       -- hợp nhất Lào Cai + Yên Bái
    ('Thái Nguyên'),   -- hợp nhất Bắc Kạn + Thái Nguyên
    ('Điện Biên'),     -- giữ nguyên
    ('Lạng Sơn'),      -- giữ nguyên
    ('Sơn La'),        -- giữ nguyên
    ('Phú Thọ'),       -- hợp nhất Hòa Bình + Vĩnh Phúc + Phú Thọ
    ('Bắc Ninh'),      -- hợp nhất Bắc Giang + Bắc Ninh (đã có sẵn trong seed cũ)
    ('Quảng Ninh'),    -- giữ nguyên
    ('Hà Nội'),        -- giữ nguyên (đã có sẵn trong seed cũ)
    ('Hải Phòng'),     -- hợp nhất Hải Dương + Hải Phòng (đã có sẵn trong seed cũ)
    ('Hưng Yên'),      -- hợp nhất Thái Bình + Hưng Yên
    ('Ninh Bình'),     -- hợp nhất Hà Nam + Ninh Bình + Nam Định
    ('Thanh Hóa'),     -- giữ nguyên
    ('Nghệ An'),       -- giữ nguyên
    ('Hà Tĩnh'),       -- giữ nguyên
    ('Quảng Trị'),     -- hợp nhất Quảng Bình + Quảng Trị
    ('Huế'),           -- giữ nguyên
    ('Đà Nẵng'),       -- hợp nhất Quảng Nam + Đà Nẵng
    ('Quảng Ngãi'),    -- hợp nhất Quảng Ngãi + Kon Tum
    ('Gia Lai'),       -- hợp nhất Gia Lai + Bình Định
    ('Đắk Lắk'),       -- hợp nhất Phú Yên + Đắk Lắk
    ('Khánh Hòa'),     -- hợp nhất Khánh Hòa + Ninh Thuận
    ('Lâm Đồng'),      -- hợp nhất Đắk Nông + Lâm Đồng + Bình Thuận
    ('Đồng Nai'),      -- hợp nhất Bình Phước + Đồng Nai (đã có sẵn trong seed cũ)
    ('Tây Ninh'),      -- hợp nhất Long An + Tây Ninh
    ('Hồ Chí Minh'),   -- hợp nhất Bình Dương + TPHCM + Bà Rịa-Vũng Tàu (đã có sẵn trong seed cũ)
    ('Đồng Tháp'),     -- hợp nhất Tiền Giang + Đồng Tháp
    ('An Giang'),      -- hợp nhất Kiên Giang + An Giang
    ('Vĩnh Long'),     -- hợp nhất Bến Tre + Vĩnh Long + Trà Vinh
    ('Cần Thơ'),       -- hợp nhất Sóc Trăng + Hậu Giang + Cần Thơ (đã có sẵn trong seed cũ)
    ('Cà Mau'),        -- hợp nhất Bạc Liêu + Cà Mau
    -- 2 giá trị đặc biệt TopCV dùng cho filter (không phải đơn vị hành
    -- chính thật, giữ nguyên như seed cũ):
    ('Khác'),
    ('Remote')
ON CONFLICT (province_name) DO NOTHING;
