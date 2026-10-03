-- Thêm job_sources_log.detail_checked_at — lần gần nhất pipeline fetch THÀNH
-- CÔNG trang chi tiết của 1 source_url.
--
-- TẠI SAO CẦN (10/2026): pipeline coi job đã có là "cần vá" khi còn thiếu
-- work_type / deadline / parsed_content, rồi fetch lại trang chi tiết. Nhưng
-- nhiều tin trên nguồn KHÔNG ghi hạn nộp, nên deadline mãi NULL và job đó bị
-- fetch lại ở MỌI lượt crawl, mãi mãi. Chưa có chỗ nào ghi "đã thử rồi" để
-- phân biệt "chưa fetch" với "fetch rồi mà nguồn không có".
--
-- Có cột này, job còn thiếu field chỉ được fetch lại sau DETAIL_RECHECK_DAYS
-- ngày (mặc định 7, xem config.py), nên vẫn tự chữa được khi sửa selector.
--
-- Đặt ở job_sources_log (theo từng URL) chứ không phải job_postings: UPDATE
-- job_postings kích hoạt trigger updated_at, mỗi lần kiểm tra lại sẽ làm
-- "cập nhật lần cuối" của job nhảy dù dữ liệu không đổi.
--
-- Dòng cũ để NULL: lượt crawl kế tiếp fetch lại MỘT lần (đúng như hành vi
-- trước đây) rồi ghi dấu. Idempotent.
ALTER TABLE job_sources_log
    ADD COLUMN IF NOT EXISTS detail_checked_at TIMESTAMPTZ;
