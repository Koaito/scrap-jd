-- job_sources_log.source_url: NOT NULL và không được rỗng (E3).
--
-- Mỗi dòng job_sources_log là một listing, định danh bằng URL của nó. Mọi câu tra (mark_listing_seen,
-- mark_listing_detail_checked, close_listing_dead, lock_job_of_listing...) tìm listing theo source_url, nên
-- một dòng NULL hay '' là listing không ai tìm ra được và không ai đóng được. UNIQUE (source_url) (0041) không
-- chặn NULL (Postgres coi các NULL là khác nhau, nên nhiều dòng NULL vẫn qua), còn '' chỉ chặn được MỘT dòng.
-- Code hiện tại không sinh cả hai: job crawl luôn có URL, job nhập tay dùng manual://<uuid>. Migration này chuyển
-- "không có đường nào sinh" thành "DB không cho sinh".
--
--   - ALTER COLUMN source_url SET NOT NULL.
--   - CHECK chk_job_sources_log_source_url_not_blank (source_url <> '').
--
-- KHÔNG tự sửa hay xoá dòng nào. Nếu có dòng NULL/rỗng thì migration DỪNG với thông báo kèm số lượng, một ví dụ
-- và câu liệt kê; xử lý tay (dòng đó là listing thật của job nào, có nên gán URL hay đóng/gộp job) rồi chạy lại
-- migrate. Trước khi chạy trên dữ liệu thật, đếm bằng:
--   SELECT count(*) FILTER (WHERE source_url IS NULL) AS null_urls,
--          count(*) FILTER (WHERE source_url = '')    AS blank_urls
--     FROM job_sources_log;
--
-- Chỉ chuỗi rỗng bị chặn, không chặn chuỗi toàn khoảng trắng (chưa thấy đường nào sinh ra, và thêm btrim làm CHECK
-- khác với cách mọi câu tra so khớp URL). Cột vẫn VARCHAR(500).
--
-- THỨ TỰ TRIỂN KHAI: migration SIẾT ràng buộc, không cần code mới đi kèm; code hiện tại không ghi NULL hay ''.
-- Backup Supabase trước. Chạy được trước hoặc sau khi push (không có bản code nào cần ràng buộc này).
--
-- Khoá job_sources_log (ACCESS EXCLUSIVE) trong lúc quét kiểm tra, vài mili giây với vài nghìn dòng;
-- lock_timeout 10 giây như 0044. Idempotent (chạy lại không làm gì).

SET LOCAL lock_timeout = '10s';

DO $$
DECLARE
    n_null   BIGINT;
    n_blank  BIGINT;
    sample   TEXT;
BEGIN
    SELECT count(*) FILTER (WHERE source_url IS NULL),
           count(*) FILTER (WHERE source_url = ''),
           min(log_id::text) FILTER (WHERE source_url IS NULL OR source_url = '')
      INTO n_null, n_blank, sample
      FROM job_sources_log;

    IF n_null > 0 OR n_blank > 0 THEN
        RAISE EXCEPTION
            'job_sources_log có % dòng source_url NULL và % dòng source_url rỗng, ví dụ log_id: %. '
            'Không thể thêm NOT NULL / CHECK. Liệt kê: SELECT log_id, job_id, source_name, listing_status '
            'FROM job_sources_log WHERE source_url IS NULL OR source_url = '''';',
            n_null, n_blank, sample;
    END IF;

    ALTER TABLE job_sources_log ALTER COLUMN source_url SET NOT NULL;

    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'chk_job_sources_log_source_url_not_blank'
                      AND conrelid = 'job_sources_log'::regclass) THEN
        ALTER TABLE job_sources_log
            ADD CONSTRAINT chk_job_sources_log_source_url_not_blank CHECK (source_url <> '');
    END IF;
END
$$;
