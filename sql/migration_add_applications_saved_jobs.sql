-- Thêm 2 bảng cho học viên (role='user') tương tác với job: ứng tuyển
-- (job_applications) và lưu để xem lại sau (saved_jobs) — 08/2026, xem
-- lịch sử trao đổi trước khi đọc file này.
--
-- Cả 2 bảng dùng ss_user_id tham chiếu THẲNG app_users(ss_user_id) —
-- bảng này giờ đại diện chung cho MỌI tài khoản (học viên role='user'
-- lẫn team SS role='ss_team'/'admin'), không phải bảng riêng cho học
-- viên (xem sql/migration_add_role_hierarchy.sql). Tên bảng đổi từ
-- ss_team_members -> app_users ở migration_rename_ss_team_members.sql
-- (đổi tên phản ánh đúng vai trò hiện tại — không còn riêng "team SS"
-- nữa). ss_user_id ở đây có thể là 1 trong 3 role, nhưng route
-- (api/routers/me.py) chỉ cho phép role='user' trở lên tự thao tác
-- trên chính mình qua get_current_user() — không hạn chế cứng ở tầng
-- DB vì staff cũng có thể cần test/ứng tuyển thử.
--
-- An toàn để chạy lại nhiều lần (IF NOT EXISTS ở mọi bước).
--
-- ⚠️ THỨ TỰ CHẠY: phải chạy SAU migration_rename_ss_team_members.sql
-- (FK REFERENCES app_users bên dưới sẽ lỗi "relation không tồn tại"
-- nếu bảng chưa được đổi tên trước đó). Và tất nhiên vẫn sau
-- migration_add_email_verification.sql như cũ.
--   psql "$DATABASE_URL" -f sql/migration_rename_ss_team_members.sql
--   psql "$DATABASE_URL" -f sql/migration_add_applications_saved_jobs.sql

-- ============================================================
-- 1. job_applications — học viên bấm "Ứng tuyển", staff xem ai đã nộp
-- ============================================================

CREATE TABLE IF NOT EXISTS job_applications (
    application_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ss_user_id        UUID NOT NULL REFERENCES app_users(ss_user_id),
    job_id            UUID NOT NULL REFERENCES job_postings(job_id),
    note              TEXT,
    applied_at        TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- 1 học viên chỉ ứng tuyển 1 job đúng 1 lần — bấm lại nút "Ứng
    -- tuyển" trên job đã nộp trước đó sẽ báo lỗi 409 ở tầng router,
    -- không tạo dòng trùng.
    CONSTRAINT uq_job_applications_user_job UNIQUE (ss_user_id, job_id)
);

CREATE INDEX IF NOT EXISTS idx_job_applications_user ON job_applications(ss_user_id);
CREATE INDEX IF NOT EXISTS idx_job_applications_job  ON job_applications(job_id);

-- ============================================================
-- 2. saved_jobs — bookmark, KHÁC ứng tuyển.
--
-- LƯU Ý (08/2026, ĐÃ ĐỔI QUYẾT ĐỊNH): lúc tạo bảng này ban đầu cố ý
-- KHÔNG cho staff xem ("không staff nào cần thấy học viên đã lưu job
-- gì, đây là danh sách cá nhân"). Sau đó phát hiện SS team/admin không
-- có cách nào theo dõi học viên đang quan tâm/lưu JD nào để chủ động
-- hỗ trợ -> đã thêm route staff-only GET /jobs/{job_id}/saved-jobs và
-- GET /auth/users/{ss_user_id}/saved-jobs (xem db.list_saved_jobs_for_job()
-- và api/routers/jobs.py, api/routers/auth.py). KHÔNG đổi schema bảng
-- này khi đảo ngược quyết định — chỉ thêm đường query mới.
-- ============================================================

CREATE TABLE IF NOT EXISTS saved_jobs (
    saved_job_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ss_user_id         UUID NOT NULL REFERENCES app_users(ss_user_id),
    job_id             UUID NOT NULL REFERENCES job_postings(job_id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT uq_saved_jobs_user_job UNIQUE (ss_user_id, job_id)
);

CREATE INDEX IF NOT EXISTS idx_saved_jobs_user ON saved_jobs(ss_user_id);
CREATE INDEX IF NOT EXISTS idx_saved_jobs_job  ON saved_jobs(job_id);

-- ============================================================
-- HẾT FILE
-- ============================================================
