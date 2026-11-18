-- Đóng các listing UNKNOWN di sản của backfill 0043 (C1) và đồng bộ job theo đó.
--
-- VẤN ĐỀ. 0043 gắn UNKNOWN ("chưa kết luận") cho mọi listing KHÁC listing hiện hành của một job, và cố ý không
-- chép hạn cho chúng (thà thiếu còn hơn sai). Luật suy ra (db.job_derivation) coi job còn OPEN chừng nào còn một
-- listing OPEN hoặc UNKNOWN. UNKNOWN thì không có hạn nên không bao giờ hết hạn, và kiểm mạng (C3b) chỉ đóng được
-- khi URL trả 404/410, trong khi 3 nguồn trả 200 cho tin đã hết hạn (đã thấy ngày 26/10/2026: cả 31 listing UNKNOWN
-- trả 2xx dù hạn đã qua 18 ngày). Kết quả: job OPEN mãi dù mọi listing có hạn đều đã hết hạn.
-- UNKNOWN chỉ do backfill này sinh ra (code hiện tại chỉ có một đường INSERT listing và luôn đặt trạng thái rõ
-- ràng), nên tập này đóng kín; migration 0049 bỏ DEFAULT 'UNKNOWN' để nó không sinh thêm.
--
-- LUẬT XỬ LÝ (chỉ listing UNKNOWN của job đang OPEN):
--   1. Job KHÔNG có listing OPEN nào và hạn job (hạn muộn nhất của mọi listing) đã qua (< CURRENT_DATE):
--      mọi bằng chứng còn lại đều là "đã hết hạn", listing UNKNOWN không đưa ra được bằng chứng nào ngược lại.
--      Đóng listing 'expired_auto', closed_at = now(), rồi job thành CLOSED 'expired_auto'. Chọn expired_auto (không
--      phải 'unknown') có chủ đích: đây là lý do duy nhất db.listing_state tự mở lại khi tin được đăng lại, nên
--      nếu tin thực ra còn sống thì lần crawl sau job tự mở lại (luật 1), việc đóng nhầm tự lành.
--   2. Job CÓ listing OPEN: job sống nhờ listing OPEN đó, listing UNKNOWN chỉ là bẫy hẹn giờ (khi listing OPEN hết
--      hạn thì chính nó giữ job OPEN mãi, y hệt luật 1 ở trên nhưng muộn hơn). Đóng listing 'unknown' (lý do
--      chưa rõ, không bịa expired_auto khi không có hạn nào). Hạn, URL và trạng thái suy ra của job không đổi vì
--      suy ra lấy từ các listing OPEN.
--   Listing UNKNOWN còn lại (job không có OPEN và hạn job NULL hoặc chưa qua) KHÔNG bị đụng; số lượng được báo
--   bằng NOTICE để xử lý tay nếu có (dữ liệu thật tại thời điểm viết: không có dòng nào như vậy).
--
-- ĐỒNG BỘ JOB giống db.job_sync.sync_job_from_listings: chỉ job_status và closed_reason đổi (job_postings.closed_at
-- do trigger set_job_closed_state đặt = now()), hạn và source_url suy ra không đổi vì tập listing giữ nguyên,
-- chỉ đổi trạng thái; updated_at giữ nguyên (cờ app.skip_updated_at) như mọi lần đồng bộ do máy. Sau migration,
-- `python main.py check-listing-derivation --strict` phải báo 0 job lệch.
--
-- THỨ TỰ TRIỂN KHAI: không cần code mới đi kèm. Backup Supabase trước. Chạy được trước hoặc sau khi push.
-- Idempotent (chạy lại không còn dòng nào khớp). Phụ thuộc ngày chạy (CURRENT_DATE) nên trên DB không có dữ liệu
-- tương ứng thì không làm gì. Khoá dòng job_sources_log/job_postings theo UPDATE thường; lock_timeout 10 giây như 0044.

SET LOCAL lock_timeout = '10s';

DO $$
DECLARE
    n_expired   BIGINT;
    n_jobs      BIGINT;
    n_trap      BIGINT;
    n_left      BIGINT;
BEGIN
    -- Cờ giữ nguyên updated_at, chỉ trong transaction này (cùng cờ trg_set_updated_at đọc).
    PERFORM set_config('app.skip_updated_at', 'on', true);

    -- Luật 1, MỘT câu lệnh với CTE ghi dữ liệu: các CTE cùng thấy một snapshot nên tập job được chọn một lần
    -- duy nhất, listing và job đóng cùng nhau (không cần bảng tạm, chạy lại trong cùng transaction vẫn an toàn).
    WITH target_jobs AS (
        SELECT jp.job_id
          FROM job_postings jp
         WHERE jp.job_status = 'OPEN'
           AND jp.deadline IS NOT NULL
           AND jp.deadline < CURRENT_DATE
           AND EXISTS (SELECT 1 FROM job_sources_log u
                        WHERE u.job_id = jp.job_id AND u.listing_status = 'UNKNOWN')
           AND NOT EXISTS (SELECT 1 FROM job_sources_log o
                            WHERE o.job_id = jp.job_id AND o.listing_status = 'OPEN')
    ),
    closed_listings AS (
        UPDATE job_sources_log l
           SET listing_status = 'CLOSED', closed_reason = 'expired_auto', closed_at = now()
         WHERE l.listing_status = 'UNKNOWN'
           AND l.job_id IN (SELECT job_id FROM target_jobs)
        RETURNING l.log_id
    ),
    closed_jobs AS (
        UPDATE job_postings
           SET job_status = 'CLOSED', closed_reason = 'expired_auto'
         WHERE job_id IN (SELECT job_id FROM target_jobs)
        RETURNING job_id
    )
    SELECT (SELECT count(*) FROM closed_listings), (SELECT count(*) FROM closed_jobs)
      INTO n_expired, n_jobs;

    -- Luật 2: listing UNKNOWN của job còn listing OPEN.
    UPDATE job_sources_log l
       SET listing_status = 'CLOSED', closed_reason = 'unknown', closed_at = now()
     WHERE l.listing_status = 'UNKNOWN'
       AND EXISTS (SELECT 1 FROM job_sources_log o
                    WHERE o.job_id = l.job_id AND o.listing_status = 'OPEN');
    GET DIAGNOSTICS n_trap = ROW_COUNT;

    SELECT count(*) INTO n_left FROM job_sources_log WHERE listing_status = 'UNKNOWN';

    RAISE NOTICE '0048: đóng % listing UNKNOWN (expired_auto) và % job; đóng % listing UNKNOWN của job còn listing OPEN; '
                 'còn % listing UNKNOWN chưa xử lý (cần xem tay nếu > 0).',
                 n_expired, n_jobs, n_trap, n_left;
END
$$;
