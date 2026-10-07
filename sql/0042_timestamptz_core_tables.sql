-- Đổi 9 cột thời điểm của 5 bảng lõi từ TIMESTAMP sang TIMESTAMPTZ (D3, đợt 2).
--
--   app_users          created_at
--   companies          created_at, updated_at
--   company_contacts   created_at, updated_at
--   job_contact_links  created_at, updated_at
--   job_postings       created_at, updated_at
--
-- Vì sao: cột TIMESTAMP không nhớ múi giờ. Giá trị đang lưu là giờ UTC (Supabase để TimeZone = UTC,
-- code Python ghi bằng datetime.now(timezone.utc)), nhưng chính cột không nói điều đó, nên mỗi chỗ đọc
-- phải tự đoán (đã sinh ra _vn_date sai 14/24 khung giờ, file export hiện giờ UTC như giờ VN, và
-- frontend cắt 10 ký tự đầu của chuỗi giờ UTC rồi coi là ngày VN). Các bảng còn lại của hệ thống
-- (audit_logs, crawl_runs, job_applications, saved_jobs...) đã là TIMESTAMPTZ; migration này làm nốt
-- phần còn lại cho nhất quán.
--
-- Cách đổi: ALTER COLUMN ... TYPE timestamptz USING <cột> AT TIME ZONE 'UTC'. Giá trị cũ được hiểu là
-- UTC một cách TƯỜNG MINH, không phụ thuộc TimeZone của session chạy migration. Mọi thời điểm giữ
-- nguyên (chỉ đổi cách lưu), không dòng nào bị dịch giờ. DEFAULT now() và trigger trg_set_updated_at
-- giữ nguyên, không cần đổi.
--
-- View v_duplicate_job_candidates dùng job_postings.created_at nên Postgres không cho đổi kiểu khi view
-- còn tồn tại ("cannot alter type of a column used by a view or rule"). Vì vậy file này DROP view,
-- đổi cột, rồi tạo lại view với ĐÚNG định nghĩa của sql/0040 và sql/schema.sql, tất cả trong một
-- transaction: lỗi ở bước nào thì không có gì đổi. Quyền (GRANT) đặt riêng cho view, nếu có, phải cấp lại
-- (giống 0040).
--
-- An toàn:
--   * Nếu có view hay rule KHÁC phụ thuộc vào 9 cột (ví dụ view tạo tay trên Supabase), migration DỪNG
--     với thông báo nêu tên, không đổi gì. Gỡ hoặc ghi lại view đó rồi chạy lại.
--   * Khoá 5 bảng (ACCESS EXCLUSIVE) với lock_timeout 10 giây: nếu có giao dịch khác (ví dụ crawl đang
--     chạy) giữ khoá quá lâu thì migration báo lỗi và dừng, KHÔNG xếp hàng chờ vô hạn làm API đứng theo.
--     Gặp lỗi này thì chờ crawl xong rồi chạy lại.
--   * Đổi kiểu viết lại cả bảng và các index của nó; với vài nghìn dòng chỉ vài trăm mili giây, nhưng
--     vẫn nên chạy lúc không crawl và sau khi backup.
--   * Idempotent: cột nào đã là timestamptz thì bỏ qua; không còn cột nào cần đổi thì không đụng gì
--     (kể cả view).
--
-- Sau migration, JSON API trả giờ có hậu tố múi giờ (...Z), snapshot MERGE_JOB mới có "+00:00", psycopg2
-- trả datetime có tzinfo. Hai chỗ đọc của backend đã được sửa trước ở đợt 1 (db/dashboard.py:_vn_date,
-- api/services/export_query.py). Cách hiển thị ở frontend/Flask kiểm sau.
--
-- Gỡ ngược (nếu thật sự cần, đã thử trên bản sao: dữ liệu về đúng như trước). Khuyến nghị khôi phục từ
-- backup nếu đã có dữ liệu mới ghi sau migration. Chạy trong một transaction:
--   DROP VIEW v_duplicate_job_candidates;
--   ALTER TABLE app_users         ALTER COLUMN created_at TYPE TIMESTAMP USING created_at AT TIME ZONE 'UTC';
--   (tương tự created_at và updated_at cho companies, company_contacts, job_contact_links, job_postings)
--   CREATE VIEW v_duplicate_job_candidates ... (định nghĩa ở cuối file này);
--   DELETE FROM schema_migrations WHERE filename = '0042_timestamptz_core_tables.sql';

DO $$
DECLARE
    -- Danh sách 9 cột, theo thứ tự cố định (cũng là thứ tự khoá bảng, tránh deadlock giữa hai phiên).
    cols        CONSTANT TEXT[][] := ARRAY[
        ['app_users',         'created_at'],
        ['companies',         'created_at'],
        ['companies',         'updated_at'],
        ['company_contacts',  'created_at'],
        ['company_contacts',  'updated_at'],
        ['job_contact_links', 'created_at'],
        ['job_contact_links', 'updated_at'],
        ['job_postings',      'created_at'],
        ['job_postings',      'updated_at']
    ];
    i           INT;
    tbl         TEXT;
    col         TEXT;
    pending     INT := 0;
    other_views TEXT;
BEGIN
    -- 1. Còn cột nào đang là TIMESTAMP (không múi giờ) không? Không thì không có gì để làm.
    FOR i IN 1 .. array_length(cols, 1) LOOP
        IF EXISTS (SELECT 1
                     FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name   = cols[i][1]
                      AND column_name  = cols[i][2]
                      AND data_type    = 'timestamp without time zone') THEN
            pending := pending + 1;
        END IF;
    END LOOP;

    IF pending = 0 THEN
        RETURN;
    END IF;

    -- 2. Có view/rule nào KHÁC v_duplicate_job_candidates phụ thuộc vào 5 bảng không?
    SELECT string_agg(DISTINCT v.relname, ', ' ORDER BY v.relname)
      INTO other_views
      FROM pg_depend d
      JOIN pg_rewrite r ON r.oid = d.objid
      JOIN pg_class   v ON v.oid = r.ev_class
      JOIN pg_class   t ON t.oid = d.refobjid
     WHERE d.refclassid = 'pg_class'::regclass
       AND v.relname <> t.relname
       AND v.relname <> 'v_duplicate_job_candidates'
       AND t.relnamespace = 'public'::regnamespace
       AND t.relname IN ('app_users', 'companies', 'company_contacts',
                         'job_contact_links', 'job_postings');
    IF other_views IS NOT NULL THEN
        RAISE EXCEPTION
            'Không thể đổi sang TIMESTAMPTZ: view/rule sau còn phụ thuộc vào app_users, companies, '
            'company_contacts, job_contact_links hoặc job_postings: %. Gỡ (hoặc ghi lại sau khi đổi) '
            'chúng rồi chạy lại migrate. Không có gì bị thay đổi.', other_views;
    END IF;

    -- 3. Khoá 5 bảng, không chờ quá 10 giây (SET LOCAL hết hiệu lực khi transaction kết thúc).
    SET LOCAL lock_timeout = '10s';
    LOCK TABLE app_users, companies, company_contacts, job_contact_links, job_postings
        IN ACCESS EXCLUSIVE MODE;

    -- 4. Gỡ view, đổi cột, tạo lại view.
    DROP VIEW IF EXISTS v_duplicate_job_candidates;

    FOR i IN 1 .. array_length(cols, 1) LOOP
        tbl := cols[i][1];
        col := cols[i][2];
        IF EXISTS (SELECT 1
                     FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name   = tbl
                      AND column_name  = col
                      AND data_type    = 'timestamp without time zone') THEN
            EXECUTE format(
                'ALTER TABLE %I ALTER COLUMN %I TYPE TIMESTAMPTZ USING %I AT TIME ZONE ''UTC''',
                tbl, col, col);
        END IF;
    END LOOP;

    -- Định nghĩa y hệt sql/0040_dedup_key_duplicate_view.sql và sql/schema.sql.
    CREATE VIEW v_duplicate_job_candidates AS
    SELECT
        dedup_key,
        array_agg(job_id ORDER BY created_at) AS job_ids,
        array_agg(job_title ORDER BY created_at) AS job_titles,
        count(*) AS num_duplicates
    FROM job_postings
    GROUP BY dedup_key
    HAVING count(*) > 1;
END
$$;
