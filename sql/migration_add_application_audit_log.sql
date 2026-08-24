-- Thêm action APPLY_JOB và WITHDRAW_JOB_APPLICATION vào audit_action_enum
-- — ghi nhận log khi học viên ứng tuyển (upload CV) và khi học viên huỷ
-- ứng tuyển. Trước migration này, cả POST /me/applications lẫn
-- DELETE /me/applications/{job_id} (api/routers/me.py) đều thao tác
-- thẳng lên job_applications/storage nhưng KHÔNG ghi audit_logs, nên
-- trang /activity-logs không thấy được 2 thao tác này của học viên
-- (chỉ thấy CREATE/UPDATE/DELETE của ss_team trên JD/company/contact).
--
-- entity_type dùng cho cả 2 log này là 'APPLICATION' (entity_id =
-- application_id trong job_applications) — KHÔNG dùng lại 'JOB' vì
-- entity_id của 1 job KHÔNG đổi mỗi lần có người ứng tuyển/huỷ, trong
-- khi application_id là duy nhất cho từng lượt ứng tuyển, cần track
-- riêng (vd sau này muốn xem "log của đơn X" qua idx_audit_logs_entity).
-- WITHDRAW_JOB_APPLICATION vẫn ghi được entity_id dù application_id đó
-- đã bị xoá thật khỏi job_applications ngay sau (delete_job_application
-- là hard delete) — cột entity_id ở audit_logs KHÔNG có FK ràng buộc
-- tới job_applications, chỉ là snapshot UUID tại thời điểm log.
--
-- is_manual_log=False, note_required=False cho cả 2 (xem
-- db.py::ACTION_LOG_RULES) — cùng nhóm với CREATE_JOB/CREATE_COMPANY:
-- hành động tự động của hệ thống lúc học viên bấm nút, không phải thao
-- tác thủ công của ss_team nên không thuộc tab "log thủ công" và
-- không bắt buộc note.
--
-- An toàn để chạy lại nhiều lần — ALTER TYPE ... ADD VALUE IF NOT EXISTS
-- (PostgreSQL 12+) tự bỏ qua nếu giá trị đã tồn tại.
--
-- LƯU Ý: ALTER TYPE ... ADD VALUE không thể chạy trong cùng 1 transaction
-- block với các câu lệnh dùng giá trị enum đó ngay sau — chạy riêng file
-- migration này trước, không gộp chung với các thay đổi khác.

ALTER TYPE audit_action_enum ADD VALUE IF NOT EXISTS 'APPLY_JOB';
ALTER TYPE audit_action_enum ADD VALUE IF NOT EXISTS 'WITHDRAW_JOB_APPLICATION';

-- ============================================================
-- HẾT FILE
-- ============================================================
