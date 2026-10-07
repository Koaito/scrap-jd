-- Thêm khoá chống trùng của job: job_postings.dedup_key và dedup_key_version (A3, nửa 1/2).
--
-- TẠI SAO CẦN: "hai tin là cùng một job" đang được định nghĩa ở bốn nơi, mỗi nơi một kiểu:
--   - content_hash (trigger generate_job_hash): công ty + tiêu đề + LEVEL + tỉnh;
--   - find_repost_candidate (crawler): công ty + tiêu đề + tỉnh, không level;
--   - find_manual_job_duplicate (POST /jobs): công ty + tiêu đề + level + tỉnh, bỏ qua job CLOSED;
--   - duplicate_report: công ty + tiêu đề, rồi chia tầng theo tỉnh và level.
-- Level do máy suy từ số năm kinh nghiệm nên hai lần đăng của cùng một tin hay ra level khác nhau;
-- đưa level vào định danh làm trùng lọt lưới. Migration này chốt MỘT khoá duy nhất, tính trong DB,
-- lưu thành cột có index để mọi nơi cùng tra bằng một câu: WHERE dedup_key = job_dedup_key(...).
--
-- KHOÁ (phiên bản 1): sha256 của  company_id | tiêu đề chuẩn hoá | province_id
--   - tiêu đề chuẩn hoá = lower(trim) rồi gộp mọi dãy khoảng trắng thành một dấu cách, ĐÚNG công thức
--     của generate_job_hash() và của find_repost_candidate, nên migration KHÔNG làm đổi nhóm nào ngoài
--     việc bỏ level ra khỏi khoá;
--   - province_id NULL (không rõ tỉnh) là một giá trị riêng: hai tin cùng thiếu tỉnh thì cùng khoá, tin
--     thiếu tỉnh khác tin có tỉnh (giống IS NOT DISTINCT FROM);
--   - KHÔNG gồm level (quyết định đã chốt), KHÔNG gồm trạng thái OPEN/CLOSED: khoá chỉ nói "cùng một
--     vị trí tuyển", còn có coi là trùng hay là đăng lại hay không do nơi dùng quyết định.
--
-- dedup_key_version: phiên bản công thức đã dùng để tính dedup_key của dòng đó (hiện là 1, lấy từ hàm
-- job_dedup_key_version()). Khi cần đổi công thức (ví dụ chuẩn hoá bỏ dấu), làm trong MỘT migration mới:
-- sửa job_dedup_key(), tăng job_dedup_key_version(), và chạy lại phần backfill bên dưới (nó chọn đúng
-- các dòng có version khác phiên bản hiện hành). Dòng nào còn version cũ chính là dòng chưa tính lại.
--
-- TRIGGER set_job_dedup_key luôn tính lại cả hai cột ở mọi INSERT và UPDATE, nên không đường ghi nào
-- (pipeline, API, SQL tay, gộp job) có thể để khoá lệch với tiêu đề, công ty, tỉnh của dòng. Hai cột là
-- NOT NULL: trigger bị tắt mà vẫn chèn dòng thì lỗi to, không để lọt dòng không có khoá.
--
-- KHÔNG đổi: content_hash và trigger set_job_hash (code cũ còn dùng), view v_duplicate_job_candidates
-- (vẫn gom theo content_hash; chuyển sang dedup_key ở migration riêng cùng đợt đổi code), mọi truy vấn
-- đang chạy. Nên migration này chạy được TRƯỚC khi push code, và code đang chạy trên Render không bị
-- ảnh hưởng (INSERT không liệt kê hai cột mới, trigger điền trước khi kiểm NOT NULL).
--
-- Backfill bật app.skip_updated_at nên KHÔNG đổi updated_at của dòng nào. Idempotent. Giữ khoá bảng
-- job_postings trong lúc chạy (ADD COLUMN, SET NOT NULL, tạo index): chạy lúc không crawl.

ALTER TABLE job_postings
    ADD COLUMN IF NOT EXISTS dedup_key VARCHAR(64);
ALTER TABLE job_postings
    ADD COLUMN IF NOT EXISTS dedup_key_version SMALLINT;

-- Khoá chống trùng của job (A3, xem sql/0039_add_job_dedup_key.sql): công ty + tiêu đề chuẩn hoá +
-- tỉnh, KHÔNG gồm level. Tiêu đề chuẩn hoá giống generate_job_hash(): lower + gộp khoảng trắng.
-- job_dedup_key_version() là phiên bản của công thức; đổi công thức thì tăng số này (xem migration).
CREATE OR REPLACE FUNCTION job_dedup_key_version() RETURNS SMALLINT AS $$
    SELECT 1::smallint;
$$ LANGUAGE sql IMMUTABLE;

CREATE OR REPLACE FUNCTION job_dedup_key(
    p_company_id  UUID,
    p_job_title   TEXT,
    p_province_id INT
) RETURNS VARCHAR(64) AS $$
BEGIN
    RETURN encode(
        digest(
            p_company_id::text || '|' ||
            lower(regexp_replace(trim(p_job_title), '\s+', ' ', 'g')) || '|' ||
            COALESCE(p_province_id::text, ''),
            'sha256'
        ),
        'hex'
    );
END;
$$ LANGUAGE plpgsql IMMUTABLE;

CREATE OR REPLACE FUNCTION trg_set_job_dedup_key() RETURNS TRIGGER AS $$
BEGIN
    NEW.dedup_key := job_dedup_key(NEW.company_id, NEW.job_title, NEW.province_id);
    NEW.dedup_key_version := job_dedup_key_version();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS set_job_dedup_key ON job_postings;
CREATE TRIGGER set_job_dedup_key
BEFORE INSERT OR UPDATE ON job_postings
FOR EACH ROW EXECUTE FUNCTION trg_set_job_dedup_key();

-- Backfill (không đổi updated_at). Chỉ chọn dòng chưa có khoá hoặc đang giữ khoá của phiên bản cũ.
SELECT set_config('app.skip_updated_at', 'on', true);

UPDATE job_postings
   SET dedup_key = job_dedup_key(company_id, job_title, province_id),
       dedup_key_version = job_dedup_key_version()
 WHERE dedup_key IS NULL
    OR dedup_key_version IS DISTINCT FROM job_dedup_key_version();

ALTER TABLE job_postings ALTER COLUMN dedup_key SET NOT NULL;
ALTER TABLE job_postings ALTER COLUMN dedup_key_version SET NOT NULL;

-- Không UNIQUE: dữ liệu thật có nhóm trùng, và job đã CLOSED cùng khoá với job đang OPEN là hợp lệ.
CREATE INDEX IF NOT EXISTS idx_job_postings_dedup_key ON job_postings(dedup_key);

COMMENT ON COLUMN job_postings.dedup_key IS
    'Khoá chống trùng: sha256(company_id | tiêu đề chuẩn hoá | province_id), không gồm level. Do trigger set_job_dedup_key tính, xem hàm job_dedup_key().';
COMMENT ON COLUMN job_postings.dedup_key_version IS
    'Phiên bản công thức đã dùng để tính dedup_key (job_dedup_key_version()). Khác phiên bản hiện hành = chưa tính lại.';
