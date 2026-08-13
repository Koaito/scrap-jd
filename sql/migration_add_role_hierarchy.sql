-- Phần 1/2 của việc mở rộng phân quyền (08/2026) — xem lịch sử trao đổi
-- đầy đủ trước khi đọc file này. Đổi từ 2 role ('admin'/'member') sang
-- 3 role phân cấp:
--   'user'     — chỉ xem/lọc job, không thấy HR contact, không sửa gì.
--   'ss_team'  — như 'user' + CRUD job/company/contact + xem danh sách
--                tài khoản. KHÔNG trigger crawl, KHÔNG tạo/đổi role user
--                khác.
--   'admin'    — như 'ss_team' + trigger crawl + tạo/đổi role user khác.
--
-- 'member' (giá trị cũ) được ĐỔI THÀNH 'ss_team' bên dưới (không phải
-- xoá đi tạo lại) — giữ đúng ý nghĩa cũ: người dùng có role 'member' từ
-- trước vốn đã có toàn quyền CRUD job/company (POST/PATCH /jobs, POST
-- /companies chỉ cần Depends(get_current_user), không phân biệt), tức
-- ĐÚNG bằng quyền 'ss_team' mới, không phải 'user' (quyền thấp nhất,
-- nghĩa hoàn toàn khác — chỉ xem).
--
-- An toàn để chạy lại nhiều lần.
--
-- Cách chạy (SAU migration_add_auth.sql, TRƯỚC khi deploy code Phần 1):
--   psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_role_hierarchy.sql

-- ============================================================
-- 1. Đổi giá trị role cũ sang tên mới
-- ============================================================

UPDATE ss_team_members SET role = 'ss_team' WHERE role = 'member';

-- Đổi default cho tài khoản tạo mới không truyền role rõ ràng — DB
-- KHÔNG tự cho quyền CRUD (khác hành vi cũ), phải admin nâng cấp thủ
-- công đúng theo luồng "ss_team gửi email nhờ admin nâng quyền" đã
-- thống nhất.
ALTER TABLE ss_team_members ALTER COLUMN role SET DEFAULT 'user';

-- Chặn CỨNG ở DB (không chỉ ở code) mọi giá trị role nằm ngoài 3 giá trị
-- hợp lệ — phòng trường hợp có script/tay ai đó lỡ ghi sai giá trị.
-- DROP CONSTRAINT trước để script chạy lại được nhiều lần không lỗi.
ALTER TABLE ss_team_members DROP CONSTRAINT IF EXISTS chk_ss_team_members_role;
ALTER TABLE ss_team_members ADD CONSTRAINT chk_ss_team_members_role
    CHECK (role IN ('user', 'ss_team', 'admin'));

-- ============================================================
-- 2. Soft-delete cho company_contacts (HR contact) — xoá qua API sẽ là
-- UPDATE is_active=false, KHÔNG DELETE thật, giữ lại lịch sử liên hệ
-- (last_contacted_date, contact_status...) cho báo cáo/đối chiếu sau
-- này. GET mặc định chỉ trả is_active=true, có thể xem lại contact đã
-- ẩn qua query param riêng (xem router mới).
-- ============================================================

ALTER TABLE company_contacts ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT true;

CREATE INDEX IF NOT EXISTS idx_company_contacts_is_active ON company_contacts(is_active);

-- Audit trail cho contact — cùng lý do với job_postings/companies
-- (migration_add_audit_columns.sql), thêm ở đây vì contact CHƯA có cột
-- này (chưa từng có route CRUD nào cho bảng này trước Phần 1).
ALTER TABLE company_contacts ADD COLUMN IF NOT EXISTS created_by UUID REFERENCES ss_team_members(ss_user_id);
ALTER TABLE company_contacts ADD COLUMN IF NOT EXISTS updated_by UUID REFERENCES ss_team_members(ss_user_id);

-- ============================================================
-- HẾT FILE
-- ============================================================
