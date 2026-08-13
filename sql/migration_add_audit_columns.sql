-- Thêm audit trail "ai tạo/sửa" cho job_postings và companies (08/2026).
--
-- Bối cảnh: hạ tầng JWT (sql/migration_add_auth.sql) đã có sẵn từ trước
-- (ss_team_members có role admin/member, có bảng auth_refresh_tokens),
-- nhưng job_postings/companies chưa có chỗ ghi lại NGƯỜI nào vừa
-- POST/PATCH — mọi request ghi trước đây chỉ cần API_KEY dùng chung,
-- không phân biệt được thành viên nào trong team thực hiện thao tác.
--
-- Migration này CHỈ thêm cột — không tự bắt buộc JWT ở tầng DB (việc
-- đó nằm ở tầng API, xem Depends(get_current_user) trong
-- api/routers/jobs.py và api/routers/companies.py). Job/company tạo
-- qua crawl pipeline (main.py, không đi qua JWT) vẫn insert bình
-- thường với created_by = NULL — NULL nghĩa là "hệ thống/crawl tự
-- động", không phải lỗi.
--
-- An toàn để chạy lại nhiều lần (IF NOT EXISTS ở mọi bước).
--
-- QUAN TRỌNG — chạy migration này TRƯỚC KHI deploy code có
-- Depends(get_current_user) ở route ghi, nếu không mọi POST/PATCH sẽ
-- lỗi 500 vì cột created_by/updated_by chưa tồn tại:
--   psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_audit_columns.sql

-- ============================================================
-- job_postings
-- ============================================================

ALTER TABLE job_postings
    ADD COLUMN IF NOT EXISTS created_by UUID REFERENCES ss_team_members(ss_user_id);

ALTER TABLE job_postings
    ADD COLUMN IF NOT EXISTS updated_by UUID REFERENCES ss_team_members(ss_user_id);

-- Job crawl (source_name != 'MANUAL') sẽ luôn có created_by NULL — không
-- FOREIGN KEY nào yêu cầu NOT NULL nên không cần xử lý gì thêm cho dữ
-- liệu cũ đã có sẵn trong bảng.

-- ============================================================
-- companies
-- ============================================================

ALTER TABLE companies
    ADD COLUMN IF NOT EXISTS created_by UUID REFERENCES ss_team_members(ss_user_id);

ALTER TABLE companies
    ADD COLUMN IF NOT EXISTS updated_by UUID REFERENCES ss_team_members(ss_user_id);

-- ============================================================
-- Index — tra "job/công ty do ai tạo" (dashboard quản trị sau này,
-- vd "xem tất cả job admin X tự nhập tay") mà không phải full scan.
-- Không đánh index updated_by (ít khi lọc theo field này, ghi đè
-- thường xuyên hơn created_by nên index thêm không đáng chi phí ghi).
-- ============================================================

CREATE INDEX IF NOT EXISTS idx_job_postings_created_by ON job_postings(created_by);
CREATE INDEX IF NOT EXISTS idx_companies_created_by ON companies(created_by);
