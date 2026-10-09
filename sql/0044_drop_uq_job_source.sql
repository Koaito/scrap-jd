-- Gỡ ràng buộc uq_job_source UNIQUE (job_id, source_url) của job_sources_log (C2, nửa 2/2).
--
-- Vì sao gỡ được: UNIQUE (source_url) (uq_job_sources_log_source_url, migration 0041, D2) đã bao nó, vì một
-- URL chỉ thuộc một job thì cặp (job_id, source_url) đương nhiên không trùng. uq_job_source chỉ còn được
-- giữ vì insert_job dùng ON CONFLICT (job_id, source_url); C2 nửa 1/2 đã bỏ ON CONFLICT đó (INSERT thường), và
-- không còn câu SQL nào trong code nhắc tới ràng buộc này. Giữ thêm một UNIQUE thừa chỉ tốn chỗ và tốn thời
-- gian mỗi lần INSERT.
--
-- THỨ TỰ TRIỂN KHAI (khác các migration chỉ thêm cột): đây là thay đổi PHÁ. Chạy SAU khi code C2 nửa 1/2 trở
-- lên đã Live trên Render: bản code cũ hơn còn ON CONFLICT (job_id, source_url), và Postgres từ chối câu
-- INSERT đó (\"there is no unique or exclusion constraint matching the ON CONFLICT specification\") khi ràng
-- buộc không còn, làm hỏng mọi lần tạo job mới của crawl.
--
-- Nếu UNIQUE (source_url) chưa có (DB chưa chạy 0041) thì migration DỪNG với thông báo, không gỡ gì: gỡ
-- uq_job_source lúc đó sẽ để job_sources_log không còn ràng buộc nào chống trùng URL.
--
-- Khoá job_sources_log (ACCESS EXCLUSIVE) trong lúc gỡ, vài mili giây; lock_timeout 10 giây như 0042 và 0043:
-- nếu crawl đang giữ khoá thì migration báo lỗi rồi dừng, chạy lại sau. Cả file là một transaction.
-- Idempotent: chạy lại khi ràng buộc đã mất thì không làm gì.

SET LOCAL lock_timeout = '10s';

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint
                    WHERE conname = 'uq_job_sources_log_source_url'
                      AND conrelid = 'job_sources_log'::regclass) THEN
        RAISE EXCEPTION
            'Thiếu UNIQUE (source_url) trên job_sources_log (uq_job_sources_log_source_url, migration 0041): '
            'không gỡ uq_job_source vì bảng sẽ không còn ràng buộc nào chống trùng URL. Chạy migrate đủ 0041 trước.';
    END IF;

    ALTER TABLE job_sources_log DROP CONSTRAINT IF EXISTS uq_job_source;
END
$$;
