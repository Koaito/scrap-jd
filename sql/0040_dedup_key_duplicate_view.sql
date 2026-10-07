-- Chuyển view v_duplicate_job_candidates sang gom theo dedup_key (A3, nửa 2/2).
--
-- Trước: gom theo content_hash = công ty + tiêu đề + LEVEL + tỉnh. Sau: gom theo dedup_key = công ty +
-- tiêu đề + tỉnh (xem sql/0039_add_job_dedup_key.sql), cùng khoá mà crawler, POST /jobs và báo cáo
-- report-duplicates dùng. Level không còn nằm trong định nghĩa "trùng", nên view không còn tách một
-- nhóm chỉ vì level khác nhau. Số liệu thật lúc chuyển: 141 nhóm (khoá cũ) lên 213 nhóm (khoá mới).
--
-- Cột đầu đổi tên content_hash thành dedup_key. CREATE OR REPLACE VIEW không đổi được tên cột nên phải
-- DROP rồi tạo lại; chưa có view hay hàm nào khác phụ thuộc vào view này. Trong code chỉ đếm số dòng
-- (SELECT count(*)) hoặc đọc bằng SELECT * tay. Quyền (GRANT) đặt riêng cho view, nếu có, phải cấp lại.
--
-- KHÔNG đổi: content_hash vẫn được trigger set_job_hash tính như cũ, vẫn còn cột và index.
-- Idempotent. Không đụng dữ liệu.

DROP VIEW IF EXISTS v_duplicate_job_candidates;
CREATE VIEW v_duplicate_job_candidates AS
SELECT
    dedup_key,
    array_agg(job_id ORDER BY created_at) AS job_ids,
    array_agg(job_title ORDER BY created_at) AS job_titles,
    count(*) AS num_duplicates
FROM job_postings
GROUP BY dedup_key
HAVING count(*) > 1;
