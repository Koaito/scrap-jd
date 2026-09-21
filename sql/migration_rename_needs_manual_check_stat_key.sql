-- Đổi key thống kê "cần_kiểm_tra_tay" -> "needs_manual_check" trong
-- maintenance_runs.stats của các lượt check_expired_jobs ĐÃ CHẠY (Phần 5
-- mục 13 của plan Next.js). Code ghi (check_expired_source_jobs.py) đã
-- đổi sang key mới, nhưng lịch sử trong DB vẫn giữ key cũ — nếu không
-- đổi, Next.js (chỉ đọc key mới) sẽ mất số liệu này ở mọi lượt chạy cũ.
--
-- Idempotent: điều kiện `stats ? '...'` chỉ khớp dòng còn key cũ, chạy
-- lại là no-op. Cần deploy CÙNG LÚC với sửa nhãn ở Flask
-- (crawler_client/maintenance.py::MAINTENANCE_STAT_LABELS).
UPDATE maintenance_runs
SET stats = (stats - 'cần_kiểm_tra_tay')
            || jsonb_build_object('needs_manual_check', stats -> 'cần_kiểm_tra_tay')
WHERE stats ? 'cần_kiểm_tra_tay';
