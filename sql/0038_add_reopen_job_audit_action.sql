-- Thêm giá trị REOPEN_JOB vào audit_action_enum (A2, nửa 1/2): ghi lại việc pipeline tự mở lại job
-- đã đóng khi thấy tin được đăng lại (db.reopen_job_for_repost). Nhân viên mở lại bằng PATCH vẫn
-- ghi UPDATE_JOB như trước.
--
-- File này CHỈ chứa câu ADD VALUE: giá trị mới không dùng được ngay trong cùng transaction với
-- câu thêm nó (xem sql/README_MIGRATIONS.md, mục "Thêm giá trị vào enum").
ALTER TYPE audit_action_enum ADD VALUE IF NOT EXISTS 'REOPEN_JOB';
