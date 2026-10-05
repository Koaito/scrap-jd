-- Thêm job_postings.level_source + level_rule_version — level của job được suy
-- ra bằng cách nào, theo bộ quy tắc phiên bản nào (10/2026).
--
-- TẠI SAO CẦN: level là giá trị SUY RA (từ tiêu đề / nhãn kinh nghiệm / số năm /
-- nhãn cấp bậc của nguồn) nhưng trước đây được lưu như sự thật, không kèm cách
-- suy. Hệ quả: đổi quy tắc thì phải chạy backfill riêng, mỗi script tự đoán tập
-- job cần vá, không ai biết job nào đã tính theo quy tắc nào, và không phân biệt
-- được level do máy suy với level do người sửa tay (backfill có thể đè lên).
--
-- Hai cột:
--   level_source        'title' | 'label' | 'years' | 'title_range' | 'hint' |
--                       'default' | 'manual'   (danh sách khớp normalize.LEVEL_SOURCES,
--                       có test giữ hai bên đồng bộ)
--   level_rule_version  SMALLINT = normalize.LEVEL_RULE_VERSION lúc suy level.
--                       'manual' thì để NULL (người sửa không thuộc phiên bản quy tắc nào).
--
-- QUY ƯỚC GIÁ TRỊ:
--   level_source IS NULL  = CHƯA BIẾT (job có từ trước migration này, hoặc ghi bởi
--                           code chưa đóng dấu) -> lệnh tính lại level phải xử lý.
--                           KHÔNG có nghĩa "mặc định" hay "đã đúng".
--   'manual'              = có người đặt/sửa level; việc ghi TỰ ĐỘNG không bao giờ đè.
--   còn lại               = do hàm normalize.derive_level() suy, kèm level_rule_version.
-- Lệnh tính lại chọn job theo "level_source IS NULL OR level_rule_version < hiện
-- tại", không đoán theo giá trị level.
--
-- Điền sẵn 'manual' CHỈ cho job chắc chắn do người đặt level:
--   (a) job có created_by (nhập tay hoặc import; job crawl luôn created_by NULL);
--   (b) job có dòng audit_logs UPDATE_JOB đổi level_code.
-- Job khác để NULL, kể cả job có updated_by (người sửa field nào đó chưa chắc đã
-- sửa level; lệnh tính lại tự quyết định có đụng vào không).
--
-- Điền 'manual' ở đây tắt trigger updated_at trong đúng transaction này để
-- "cập nhật lần cuối" của job không nhảy vì một lần di trú. Idempotent.
ALTER TABLE job_postings
    ADD COLUMN IF NOT EXISTS level_source       VARCHAR(20),
    ADD COLUMN IF NOT EXISTS level_rule_version SMALLINT;

DO $$ BEGIN
    ALTER TABLE job_postings ADD CONSTRAINT chk_job_postings_level_source CHECK (
        level_source IS NULL OR level_source IN (
            'title', 'label', 'years', 'title_range', 'hint', 'default', 'manual'
        )
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Hình dạng hợp lệ của cặp (level_source, level_rule_version, level_id):
--   chưa biết: cả hai NULL          manual: version NULL
--   máy suy:   version NOT NULL VÀ có level_id (không suy ra "không có level")
-- Viết bằng CASE (không dùng chuỗi AND/OR) vì CHECK coi NULL là "đạt": với
-- level_source NULL, biểu thức kiểu "level_source IN (...) AND ..." ra NULL chứ
-- không phải false, và (NULL, version=1) lọt qua. CASE luôn ra true/false.
DO $$ BEGIN
    ALTER TABLE job_postings ADD CONSTRAINT chk_job_postings_level_stamp CHECK (
        CASE
            WHEN level_source IS NULL THEN level_rule_version IS NULL
            WHEN level_source = 'manual' THEN level_rule_version IS NULL
            ELSE level_rule_version IS NOT NULL AND level_id IS NOT NULL
        END
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE job_postings DISABLE TRIGGER set_updated_at_job_postings;

UPDATE job_postings jp
SET level_source = 'manual'
WHERE jp.level_source IS NULL
  AND jp.level_id IS NOT NULL
  AND (
        jp.created_by IS NOT NULL
        OR EXISTS (
            SELECT 1 FROM audit_logs a
            WHERE a.entity_type = 'JOB'
              AND a.entity_id = jp.job_id
              AND a.action_type = 'UPDATE_JOB'
              AND a.changes ? 'level_code'
        )
  );

ALTER TABLE job_postings ENABLE TRIGGER set_updated_at_job_postings;
