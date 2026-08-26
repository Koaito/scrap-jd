-- Thêm "crawl nhiều category liên tục" (08/2026, xem lịch sử trao đổi
-- "Hướng A — batch job ở tầng backend").
--
-- Ý TƯỞNG: 1 request POST /crawl/batch (source + list category) tạo ra:
--   - 1 dòng crawl_batches (metadata chung: source, danh sách category
--     theo ĐÚNG THỨ TỰ sẽ crawl, pages/max_jobs áp dụng chung).
--   - N dòng crawl_runs con (MỖI category 1 run RIÊNG, y hệt crawl đơn
--     lẻ hiện có — không đổi gì ở crawl_runs/pipeline.py cho từng run),
--     nối vào batch qua batch_id + batch_position (thứ tự 0..N-1).
--
-- CHẠY TUẦN TỰ, KHÔNG SONG SONG: api/crawl_runner.py::execute() tự tạo
-- + chạy run kế tiếp trong batch NGAY SAU KHI run hiện tại xong (done
-- HOẶC error đều tính là "xong", batch vẫn tiếp tục category kế) —
-- đúng hành vi vòng for tuần tự người dùng đang gõ tay, và tự động giữ
-- đúng UNIQUE INDEX idx_crawl_runs_one_active_per_source đã có sẵn (chỉ
-- tạo run tiếp theo sau khi run trước đã đổi khỏi 'queued'/'running').
--
-- KHÔNG cần bảng/queue riêng: chỉ cần bảng crawl_batches (metadata) +
-- 2 cột nối trên crawl_runs — mọi cơ chế heartbeat/log live/watchdog
-- đã có sẵn cho crawl_runs áp dụng nguyên vẹn cho từng run con của
-- batch, không cần sửa gì thêm ở đó.
--
-- An toàn để chạy lại nhiều lần (IF NOT EXISTS mọi bước).

CREATE TABLE IF NOT EXISTS crawl_batches (
    batch_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    source        VARCHAR(50) NOT NULL,

    -- Danh sách category ĐÚNG THỨ TỰ sẽ crawl, vd
    -- ["data-analyst","data-engineer","business-analyst"] — batch_position
    -- ở crawl_runs chính là index trong mảng này (0-based).
    categories    JSONB NOT NULL,

    pages         INT NOT NULL,
    max_jobs      INT,

    -- Dùng LẠI crawl_status_enum đã có (queued/running/done/error) —
    -- batch chỉ thật sự dùng 'running' (đang crawl dở category nào đó)
    -- và 'done' (hết category); 'error' dành cho trường hợp HIẾM tự
    -- advance sang category kế thất bại giữa chừng (vd ActiveCrawlExistsError
    -- do có ai đó crawl tay đúng lúc source này rảnh — xem
    -- api/crawl_runner.py::execute()), KHÁC với 1 category lỗi bình
    -- thường (category đó vẫn tính "xong", batch vẫn tiếp tục category
    -- sau, không rơi vào 'error' ở đây).
    status        crawl_status_enum NOT NULL DEFAULT 'running',
    error         TEXT,

    triggered_by  UUID REFERENCES app_users(ss_user_id),

    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_crawl_batches_status       ON crawl_batches(status);
CREATE INDEX IF NOT EXISTS idx_crawl_batches_created_at   ON crawl_batches(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_crawl_batches_triggered_by ON crawl_batches(triggered_by);

-- Nối crawl_runs -> crawl_batches. NULL ở cả 2 cột = run đơn lẻ như
-- trước giờ (không đổi hành vi gì cho crawl không qua batch).
ALTER TABLE crawl_runs ADD COLUMN IF NOT EXISTS batch_id       UUID REFERENCES crawl_batches(batch_id) ON DELETE CASCADE;
ALTER TABLE crawl_runs ADD COLUMN IF NOT EXISTS batch_position INT;

-- Tra "các run của batch X, đúng thứ tự" — dùng cho GET /crawl/batch/{batch_id}.
CREATE INDEX IF NOT EXISTS idx_crawl_runs_batch_id ON crawl_runs(batch_id, batch_position);

-- ============================================================
-- HẾT FILE
-- ============================================================
