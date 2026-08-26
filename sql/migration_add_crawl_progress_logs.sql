-- Thêm heartbeat/tiến độ real-time + log live cho crawl_runs (08/2026,
-- xem lịch sử trao đổi "phương án Heartbeat / tiến độ theo thời gian
-- thực"). 2 phần TÁCH RIÊNG có chủ đích:
--
--   1. crawl_runs.progress (JSONB) — snapshot GỌN {page, fetched,
--      inserted, last_update} do execute() ghi đè liên tục trong lúc
--      chạy pipeline (không phải lịch sử, chỉ trạng thái mới nhất) —
--      dùng cho khu "Hiện tại" ở trang /crawl (số trang đang crawl,
--      đã lấy bao nhiêu job) VÀ cho watchdog (crawl_watchdog.py) so
--      sánh last_update để phát hiện "treo quá lâu không cập nhật".
--
--   2. crawl_run_logs (bảng riêng, KHÔNG gộp vào crawl_runs) — TỪNG
--      DÒNG log (giống log thấy trong cmd khi crawl chạy ở máy local),
--      phục vụ khu "Xem log live" ở trang /crawl. Tách bảng riêng
--      (không phải 1 cột TEXT nối chuỗi trong crawl_runs) vì:
--      - Ghi liên tục nhiều dòng/giây lúc crawl chạy -> UPDATE nối
--        chuỗi ngày càng dài trên CHÍNH dòng crawl_runs sẽ khoá
--        (row lock) cả dòng đó, đụng độ với các UPDATE status/progress
--        khác đang cần ghi đồng thời lên cùng 1 dòng.
--      - INSERT có thể set primary key tăng dần (id) -> tự nhiên đã có
--        thứ tự để ORDER BY, không cần thêm cột "line_number" riêng.
--      - Dọn dẹp dễ hơn (xem cron dọn log cũ ở cuối file) khi tách
--        riêng khỏi bảng chính, không đụng tới lịch sử crawl_runs.
--
-- An toàn để chạy lại nhiều lần (IF NOT EXISTS mọi bước).

ALTER TABLE crawl_runs ADD COLUMN IF NOT EXISTS progress JSONB;

CREATE TABLE IF NOT EXISTS crawl_run_logs (
    id         BIGSERIAL PRIMARY KEY,
    run_id     UUID NOT NULL REFERENCES crawl_runs(run_id) ON DELETE CASCADE,
    -- Khớp logging.LEVELNAME chuẩn của Python (INFO/WARNING/ERROR) —
    -- dùng để tô màu dòng log khác nhau ở frontend (giống terminal thật
    -- có màu vàng/đỏ cho warning/error), xem crawl.html.
    level      VARCHAR(10) NOT NULL DEFAULT 'INFO',
    message    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Trang /crawl poll GET /crawl/{run_id}/logs?after_id=N để chỉ lấy dòng
-- MỚI kể từ lần poll trước (không tải lại toàn bộ log mỗi 2-3 giây) —
-- cần cả (run_id, id) để query "id > after_id AND run_id = X" nhanh.
CREATE INDEX IF NOT EXISTS idx_crawl_run_logs_run_id_id ON crawl_run_logs(run_id, id);

-- ============================================================
-- HẾT FILE
-- ============================================================
