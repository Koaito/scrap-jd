-- Cho trigger trg_set_updated_at() biết "ghi lần này không phải người/crawl sửa
-- job" qua cờ phiên app.skip_updated_at (10/2026).
--
-- TẠI SAO CẦN: trigger này đặt updated_at = now() mỗi khi dòng bị UPDATE, bất kể
-- ai sửa, và updated_at được API trả ra / xuất file / dùng lọc-sắp xếp. Các lần
-- ghi hàng loạt do máy (lệnh tính lại level `main.py recompute-levels`, sau này là
-- gộp job, enrich công ty) không phải "nội dung vừa được sửa", nhưng nếu để
-- updated_at nhảy thì hàng trăm job cùng hiện "cập nhật hôm nay".
--
-- CÁCH DÙNG (trong transaction của lệnh cần giữ updated_at):
--     SELECT set_config('app.skip_updated_at', 'on', true);   -- true = chỉ trong transaction này
--   Hết transaction (commit/rollback, kể cả bị ngắt giữa chừng) cờ tự mất.
--
-- KHÔNG ĐỔI hành vi mặc định: không có cờ (hoặc cờ khác 'on') thì trigger chạy y như
-- cũ. Không khoá bảng, không ảnh hưởng transaction khác (khác với DISABLE TRIGGER).
-- Hàm dùng chung cho companies, job_postings, company_contacts, job_contact_links,
-- email_templates nên cờ áp dụng cho cả các bảng này.
--
-- Khi bật cờ, updated_at giữ giá trị sẵn có của dòng (không bị trigger đụng tới);
-- câu UPDATE tự gán updated_at = ... thì vẫn được tôn trọng.
-- Idempotent (CREATE OR REPLACE), không UPDATE dòng nào. Rollback: chạy lại hàm
-- cũ trong sql/schema.sql bản trước (chỉ gồm NEW.updated_at = now(); RETURN NEW;).
CREATE OR REPLACE FUNCTION trg_set_updated_at() RETURNS TRIGGER AS $$
BEGIN
    IF current_setting('app.skip_updated_at', true) = 'on' THEN
        RETURN NEW;
    END IF;
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
