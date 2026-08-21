-- Đổi tên bảng ss_team_members -> app_users (08/2026).
--
-- LÝ DO: bảng này lúc đầu chỉ chứa team SS (member/admin), nhưng từ
-- migration_add_role_hierarchy.sql đã mở rộng thành bảng tài khoản DÙNG
-- CHUNG cho MỌI người dùng hệ thống — học viên (role='user') lẫn team
-- SS (role='ss_team'/'admin'). Tên cũ "ss_team_members" gây hiểu lầm
-- (tưởng chỉ có team SS trong đó). Đổi tên phản ánh đúng vai trò hiện
-- tại, không đổi cấu trúc/dữ liệu.
--
-- ⚠️ CHỈ ĐỔI TÊN BẢNG — KHÔNG đổi tên cột ss_user_id. Cột này được
-- tham chiếu ở rất nhiều nơi trong code (api/schemas.py, api/routers/*,
-- db.py, các bảng con qua FK). Đổi luôn tên cột sẽ kéo theo sửa hàng
-- trăm chỗ, rủi ro cao hơn hẳn lợi ích — không làm trong migration này.
--
-- AN TOÀN: RENAME TABLE trong Postgres không phá FK — Postgres theo dõi
-- quan hệ khoá ngoại qua OID nội bộ, không theo tên, nên mọi FK từ
-- job_postings, applications (assigned_ss_user), company_contacts,
-- job_applications, saved_jobs (created_by/updated_by/ss_user_id) vẫn
-- hoạt động bình thường sau khi đổi tên, KHÔNG cần sửa lại các migration
-- cũ đã chạy trước đó.
--
-- Tên constraint/index cũ (vd chk_ss_team_members_role,
-- idx_ss_team_members_email_verify_token) vẫn giữ nguyên tên cũ sau khi
-- đổi tên bảng — đây chỉ là vấn đề thẩm mỹ (tên không khớp bảng mới),
-- KHÔNG ảnh hưởng chức năng. Cố tình không đổi trong migration này để
-- giảm rủi ro; có thể đổi riêng sau nếu muốn triệt để.
--
-- ⚠️ THỨ TỰ CHẠY: PHẢI chạy TRƯỚC migration_add_applications_saved_jobs.sql
-- (file đó REFERENCES app_users — sẽ lỗi "relation không tồn tại" nếu
-- chạy trước migration này). Chạy sau mọi migration khác đã có
-- (migration_add_email_verification.sql trở về trước).
--
-- Cách chạy:
--   psql "$DATABASE_URL" -f sql/migration_rename_ss_team_members.sql

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'ss_team_members')
       AND NOT EXISTS (SELECT 1 FROM information_schema.tables WHERE table_name = 'app_users') THEN
        ALTER TABLE ss_team_members RENAME TO app_users;
    END IF;
END $$;

-- ============================================================
-- HẾT FILE
-- ============================================================
