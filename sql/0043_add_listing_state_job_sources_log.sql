-- Thêm trạng thái của từng listing (từng URL tin đăng) vào job_sources_log (C1, nửa 1/2).
--
-- TẠI SAO CẦN: hiện "tin này còn sống không, hạn nộp khi nào, thấy lần đầu và lần cuối lúc nào" chỉ có ở
-- cấp JOB (job_postings.job_status, deadline, source_url), trong khi sự thật nằm ở cấp LISTING: một job có
-- thể có nhiều URL (tin đăng lại, nguồn khác), mỗi URL sống chết và hết hạn theo cách riêng. Hậu quả là các
-- ca đặc biệt mọc dần: reopen_job_for_repost phải GHI ĐÈ job_postings.source_url sang URL mới (để
-- check_expired_source_jobs khỏi đóng lại ngay), extend_job_deadline phải dời hạn của job, find_repost_candidate
-- phải đoán job đóng do ai. Đưa trạng thái xuống listing là bước đầu để job trở thành giá trị tổng hợp từ
-- các listing của nó (C2 ghi kép, C3 chuyển người đọc, C4 gỡ ca đặc biệt). Migration này CHỈ thêm cột và
-- backfill; chưa có đoạn code nào đọc hay ghi chúng, nên chạy được TRƯỚC khi push code.
--
-- CỘT MỚI của job_sources_log:
--   listing_status  OPEN | CLOSED | UNKNOWN (NOT NULL, mặc định UNKNOWN).
--                   OPEN     listing còn sống ở nguồn theo lần kiểm gần nhất (hoặc theo job, với dữ liệu cũ).
--                   CLOSED   đã chết hoặc hết hạn, hoặc job của nó đã bị đóng.
--                   UNKNOWN  CHƯA KẾT LUẬN được. Mặc định cho dòng ghi bởi code cũ (chưa biết cột này) để không
--                   tuyên bố "còn sống" khi chưa kiểm: nguyên tắc "thà thiếu còn hơn sai" của dự án.
--   deadline        hạn nộp của CHÍNH listing này (NULL = không biết hoặc tin không ghi hạn).
--   first_seen_at   lần đầu hệ thống thấy listing (NOT NULL, mặc định now()).
--   last_seen_at    lần gần nhất hệ thống thấy listing còn tồn tại ở nguồn (NOT NULL, mặc định now(), luôn
--                   >= first_seen_at). Rộng hơn detail_checked_at (chỉ tính lần fetch thành công trang chi
--                   tiết); detail_checked_at GIỮ NGUYÊN vì job_needs_detail_enrichment đang dùng.
--   closed_reason   staff | expired_auto | merged | unknown: CÙNG bộ giá trị với job_postings.closed_reason để
--                   ánh xạ một đối một khi job trở thành giá trị tổng hợp. NULL khi listing không CLOSED.
--   closed_at       lúc listing chuyển sang CLOSED. NULL khi không CLOSED, hoặc đóng từ trước migration mà
--                   không biết giờ (NULL = không biết, không phải "chưa đóng").
--
-- BẤT BIẾN (CHECK): listing CLOSED thì closed_reason NOT NULL; listing không CLOSED thì closed_reason và
-- closed_at đều NULL (cùng luật chk_job_postings_closed_state); last_seen_at >= first_seen_at; listing_status
-- và closed_reason chỉ nhận các giá trị trên.
--
-- BACKFILL (chỉ dòng chưa có listing_status, nên chạy lại không ghi đè gì), dựa trên job của listing:
--   "listing hiện hành" = listing có source_url trùng job_postings.source_url (URL mà
--   check_expired_source_jobs đang kiểm tra). Dữ liệu thật: mọi job có source_url đều có listing như vậy.
--   - job CLOSED : MỌI listing của job là CLOSED, chép closed_reason và closed_at của job.
--   - job OPEN   : listing hiện hành là OPEN; listing khác (tin đăng lại, URL cũ) là UNKNOWN vì chưa ai kiểm
--                  riêng URL đó, và URL cũ của job vừa được mở lại thường đã chết.
--   - deadline   : chép job_postings.deadline CHO LISTING HIỆN HÀNH (hạn đó vốn là hạn của URL hiện hành);
--                  listing khác để NULL, không đoán.
--   - first_seen_at = 00:00 giờ VN của collected_date (collected_date chỉ có ngày, nên giờ là xấp xỉ, sai
--                  tối đa vài giờ về phía sớm); last_seen_at = detail_checked_at nếu có và không sớm hơn
--                  first_seen_at, ngược lại = first_seen_at. Dòng tạo sau migration có giờ chính xác.
--   Backfill KHÔNG đụng job_postings.
--
-- An toàn: khoá job_sources_log (ACCESS EXCLUSIVE) trong lúc thêm cột và đặt NOT NULL; lock_timeout 10 giây
-- để không xếp hàng vô hạn nếu crawl đang giữ khoá (gặp lỗi thì chờ crawl xong rồi chạy lại). Cả file là
-- một transaction nên không có dòng nào do code cũ chèn lọt vào giữa lúc thêm cột và lúc đặt NOT NULL.
-- Idempotent.

SET LOCAL lock_timeout = '10s';

ALTER TABLE job_sources_log ADD COLUMN IF NOT EXISTS listing_status VARCHAR(10);
ALTER TABLE job_sources_log ADD COLUMN IF NOT EXISTS deadline       DATE;
ALTER TABLE job_sources_log ADD COLUMN IF NOT EXISTS first_seen_at  TIMESTAMPTZ;
ALTER TABLE job_sources_log ADD COLUMN IF NOT EXISTS last_seen_at   TIMESTAMPTZ;
ALTER TABLE job_sources_log ADD COLUMN IF NOT EXISTS closed_reason  VARCHAR(20);
ALTER TABLE job_sources_log ADD COLUMN IF NOT EXISTS closed_at      TIMESTAMPTZ;

-- Backfill dòng chưa có trạng thái. Tính sẵn các giá trị dẫn xuất trong subquery x (UPDATE ... FROM không cho
-- subquery tham chiếu lại bảng đích), first_seen_at tính một lần rồi dùng lại cho last_seen_at.
UPDATE job_sources_log l
   SET listing_status = CASE
           WHEN x.job_status = 'CLOSED' THEN 'CLOSED'
           WHEN x.is_current            THEN 'OPEN'
           ELSE 'UNKNOWN'
       END,
       closed_reason  = CASE WHEN x.job_status = 'CLOSED' THEN x.job_closed_reason END,
       closed_at      = CASE WHEN x.job_status = 'CLOSED' THEN x.job_closed_at END,
       deadline       = CASE WHEN x.is_current THEN x.job_deadline END,
       first_seen_at  = x.first_seen,
       last_seen_at   = GREATEST(x.first_seen, COALESCE(x.detail_checked_at, x.first_seen))
  FROM (
        SELECT s.log_id,
               j.job_status,
               j.closed_reason AS job_closed_reason,
               j.closed_at     AS job_closed_at,
               j.deadline      AS job_deadline,
               -- NULL (một trong hai URL NULL) coi như không phải listing hiện hành.
               COALESCE(s.source_url = j.source_url, false) AS is_current,
               (s.collected_date::timestamp AT TIME ZONE 'Asia/Ho_Chi_Minh') AS first_seen,
               s.detail_checked_at
          FROM job_sources_log s
          JOIN job_postings j ON j.job_id = s.job_id
         WHERE s.listing_status IS NULL
       ) x
 WHERE l.log_id = x.log_id;

ALTER TABLE job_sources_log ALTER COLUMN listing_status SET DEFAULT 'UNKNOWN';
ALTER TABLE job_sources_log ALTER COLUMN listing_status SET NOT NULL;
ALTER TABLE job_sources_log ALTER COLUMN first_seen_at  SET DEFAULT now();
ALTER TABLE job_sources_log ALTER COLUMN first_seen_at  SET NOT NULL;
ALTER TABLE job_sources_log ALTER COLUMN last_seen_at   SET DEFAULT now();
ALTER TABLE job_sources_log ALTER COLUMN last_seen_at   SET NOT NULL;

DO $$ BEGIN
    ALTER TABLE job_sources_log ADD CONSTRAINT chk_job_sources_log_listing_status CHECK (
        listing_status IN ('OPEN', 'CLOSED', 'UNKNOWN')
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    ALTER TABLE job_sources_log ADD CONSTRAINT chk_job_sources_log_closed_reason CHECK (
        closed_reason IS NULL OR closed_reason IN ('staff', 'expired_auto', 'merged', 'unknown')
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- CASE (không phải AND/OR): CHECK coi NULL là "đạt".
DO $$ BEGIN
    ALTER TABLE job_sources_log ADD CONSTRAINT chk_job_sources_log_closed_state CHECK (
        CASE
            WHEN listing_status = 'CLOSED' THEN closed_reason IS NOT NULL
            ELSE closed_reason IS NULL AND closed_at IS NULL
        END
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

DO $$ BEGIN
    ALTER TABLE job_sources_log ADD CONSTRAINT chk_job_sources_log_seen_order CHECK (
        last_seen_at >= first_seen_at
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

COMMENT ON COLUMN job_sources_log.listing_status IS
    'Trạng thái của listing (URL tin đăng) này: OPEN | CLOSED | UNKNOWN (chưa kết luận, mặc định cho dòng ghi bởi code cũ).';
COMMENT ON COLUMN job_sources_log.deadline IS
    'Hạn nộp của chính listing này. NULL = không biết hoặc tin không ghi hạn.';
COMMENT ON COLUMN job_sources_log.first_seen_at IS
    'Lần đầu hệ thống thấy listing. Dòng tạo trước migration 0043: 00:00 giờ VN của collected_date (xấp xỉ).';
COMMENT ON COLUMN job_sources_log.last_seen_at IS
    'Lần gần nhất hệ thống thấy listing còn tồn tại ở nguồn; luôn >= first_seen_at. Khác detail_checked_at (chỉ tính lần fetch thành công trang chi tiết).';
COMMENT ON COLUMN job_sources_log.closed_reason IS
    'Vì sao listing CLOSED: staff | expired_auto | merged | unknown (cùng bộ giá trị job_postings.closed_reason). NULL khi không CLOSED.';
COMMENT ON COLUMN job_sources_log.closed_at IS
    'Lúc listing chuyển sang CLOSED. NULL khi không CLOSED, hoặc đóng từ trước migration mà không biết giờ.';
