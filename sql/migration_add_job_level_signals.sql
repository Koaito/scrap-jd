-- Thêm job_postings.level_signals — tín hiệu THÔ mà normalize.derive_level() đã đọc
-- để suy ra level của job (10/2026). Đi cùng level_source / level_rule_version
-- (sql/migration_add_job_level_source.sql).
--
-- TẠI SAO CẦN: derive_level() ngoài tiêu đề còn đọc nhãn kinh nghiệm của nguồn
-- (experience_text, vd "2 năm", "Trên 5 năm") và nhãn cấp bậc (level_hint, vd
-- jobLevel của VietnamWorks). Hai chuỗi này chỉ có lúc crawl và trước đây bị bỏ
-- sau khi suy xong, nên khi quy tắc đổi chỉ còn cách tải lại từng trang. Lưu lại
-- thì lệnh tính lại level chạy hoàn toàn trong DB, không cần mạng.
--
-- ĐỊNH DẠNG: JSONB object đúng hai khoá (xem normalize.LEVEL_SIGNAL_KEYS):
--   {"experience_text": "<chuỗi>", "level_hint": "<chuỗi>"}
--   chuỗi rỗng = nguồn không có tín hiệu đó (khác với NULL cả cột).
-- Tiêu đề không lưu ở đây vì đã có cột job_title.
--
-- QUY ƯỚC GIÁ TRỊ:
--   level_signals IS NULL = CHƯA TỪNG LƯU (job có từ trước migration này, hoặc level
--                           được ghi bởi đường không biết tín hiệu). Với job này chỉ
--                           tính lại được phần suy từ tiêu đề.
--   object (kể cả rỗng)   = đã lưu đúng những gì derive_level đọc lúc suy level.
-- Cột này đi theo level do MÁY suy: mỗi lần level tự động được ghi lại thì tín hiệu
-- ghi lại cùng lúc (không có tín hiệu thì thành NULL); dòng 'manual' không bị đụng.
--
-- KHÔNG điền dữ liệu cũ: tín hiệu thật không còn ở đâu để điền, đoán là bịa.
-- Migration không UPDATE dòng nào nên không đụng updated_at / content_hash.
-- Idempotent.
ALTER TABLE job_postings
    ADD COLUMN IF NOT EXISTS level_signals JSONB;

DO $$ BEGIN
    ALTER TABLE job_postings ADD CONSTRAINT chk_job_postings_level_signals CHECK (
        level_signals IS NULL OR jsonb_typeof(level_signals) = 'object'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
