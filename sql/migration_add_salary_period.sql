-- Thêm cột salary_period (MONTH / YEAR) vào job_postings.
--
-- Bug đã sửa (08/2026): normalize_salary() trước đây chỉ trích số ra khỏi
-- text lương rồi suy luận đơn vị TIỀN TỆ (triệu/nghìn đồng) theo độ lớn
-- con số, hoàn toàn không đọc CHU KỲ trả lương ("/tháng" hay "/năm") —
-- code mặc định coi MỌI mức lương crawl được là lương/tháng. Vì vậy input
-- kiểu "200tr-500tr ₫/năm" (lương NĂM) bị lưu y hệt như lương/tháng
-- (salary_min/max = 200,000,000 / 500,000,000 — sai lệch 12 lần).
--
-- Không tự chia 12 để quy đổi ra "lương/tháng tương đương": salary_min/
-- salary_max giữ NGUYÊN con số gốc theo đúng chu kỳ đã detect (nếu là
-- lương năm thì salary_min/max LÀ mức lương năm) — cột salary_period cho
-- biết con số đó đang ở chu kỳ nào. Lý do không chia 12: chia sẽ tạo ra
-- số lẻ không khớp với salary_raw_content gốc, gây khó đối chiếu ngược
-- lại khi audit; đồng thời "quy đổi tương đương" là 1 phép biến đổi có ý
-- kiến (12 tháng lương/năm có chắc đúng cho mọi trường hợp thưởng/phụ
-- cấp không?) nên để tầng hiển thị/tầng lọc dữ liệu tự quyết định cách
-- quy đổi khi cần, thay vì áp đặt sẵn lúc ghi vào DB.
--
-- An toàn để chạy lại nhiều lần.
--
-- Cách chạy:
--   psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_salary_period.sql

DO $$ BEGIN
    CREATE TYPE salary_period_enum AS ENUM ('MONTH', 'YEAR');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- Mặc định 'MONTH' cho dữ liệu cũ: mọi job crawl trước migration này đều
-- được normalize_salary() (bản cũ, không đọc chu kỳ) coi là lương/tháng,
-- nên set default khớp với hành vi cũ -> không làm lệch dữ liệu đã có.
-- 2 record sai đã biết (iOS Dev, Vendor Development) sẽ được backfill
-- riêng (ngoài phạm vi migration này), không tự động sửa ở đây.
ALTER TABLE job_postings
    ADD COLUMN IF NOT EXISTS salary_period salary_period_enum NOT NULL DEFAULT 'MONTH';
