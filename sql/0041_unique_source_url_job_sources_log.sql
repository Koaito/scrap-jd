-- Một URL nguồn chỉ thuộc MỘT job: UNIQUE (source_url) trên job_sources_log (D2).
--
-- Trước: chỉ có uq_job_source UNIQUE (job_id, source_url). Ràng buộc đó chặn trùng URL TRONG MỘT job
-- nhưng cho phép cùng một URL nằm ở hai job khác nhau, và index của nó có job_id đứng đầu nên không
-- giúp được câu tra theo riêng source_url. Mọi câu tra "URL này đã crawl chưa" đều tra theo riêng
-- source_url: job_exists_by_source_url, get_job_probe_by_source_url, mark_source_detail_checked.
-- Chúng đang quét tuần tự cả bảng mỗi lần.
--
-- Sau: UNIQUE (source_url) vừa là ràng buộc nghiệp vụ ("một URL thuộc một job", dữ liệu thật hiện
-- không có URL nào vi phạm) vừa là index cho ba câu tra trên.
--
-- source_url NULL vẫn được phép nhiều dòng (Postgres coi các NULL là khác nhau). Hiện không có đường
-- ghi nào sinh NULL: job nhập tay dùng manual://<uuid> (duy nhất theo cấu tạo), job crawl luôn có URL.
--
-- GIỮ uq_job_source (job_id, source_url): giờ nó thừa về mặt ràng buộc (UNIQUE (source_url) đã bao
-- nó), nhưng insert_job còn dùng ON CONFLICT (job_id, source_url). Gỡ ở đợt C (tách job và listing),
-- khi chỗ ghi log được viết lại.
--
-- KHÔNG đổi VARCHAR(500) sang TEXT: URL dài nhất trong dữ liệu thật là 212 ký tự, và
-- job_postings.source_url cũng VARCHAR(500), đổi một cột không giúp gì.
--
-- Nếu DB đang có URL nằm ở nhiều dòng, migration DỪNG với thông báo kèm số lượng và một ví dụ, không
-- tự xoá gì. Khi đó chạy câu liệt kê trong thông báo, quyết định giữ dòng nào (thường là gộp job
-- bằng merge-duplicates) rồi chạy lại migrate.
--
-- Khoá bảng job_sources_log trong lúc tạo index, vài mili giây với vài nghìn dòng. Idempotent.

DO $$
DECLARE
    n_dup    BIGINT;
    n_rows   BIGINT;
    sample   TEXT;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_constraint
                WHERE conname = 'uq_job_sources_log_source_url'
                  AND conrelid = 'job_sources_log'::regclass) THEN
        RETURN;
    END IF;

    SELECT count(*), COALESCE(sum(c), 0), min(source_url)
      INTO n_dup, n_rows, sample
      FROM (SELECT source_url, count(*) AS c
              FROM job_sources_log
             WHERE source_url IS NOT NULL
             GROUP BY source_url
            HAVING count(*) > 1) t;

    IF n_dup > 0 THEN
        RAISE EXCEPTION
            'job_sources_log có % URL nằm ở nhiều dòng (% dòng), ví dụ: %. Không thể thêm UNIQUE (source_url). '
            'Liệt kê: SELECT source_url, array_agg(job_id) FROM job_sources_log '
            'GROUP BY source_url HAVING count(*) > 1;',
            n_dup, n_rows, sample;
    END IF;

    ALTER TABLE job_sources_log
        ADD CONSTRAINT uq_job_sources_log_source_url UNIQUE (source_url);
END
$$;
