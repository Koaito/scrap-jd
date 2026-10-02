-- Snapshot HTML/JSON gốc của các lượt crawl để debug khi parser hỏng (đợt 3,
-- 10/2026, xem snapshots.py + db/crawl_snapshots.py).
--
-- Vì sao cần: site đổi giao diện thì adapter thường KHÔNG báo lỗi mà chỉ trả 0
-- record hoặc field rỗng hàng loạt; không có HTML gốc của đúng thời điểm đó
-- thì không biết selector hỏng ở đâu, và fixture trong tests/ vẫn là dữ liệu
-- dựng tay. Mỗi lượt chỉ giữ vài mẫu (xem SNAPSHOT_MAX_PER_RUN trong
-- config.py): trang listing đầu, trang chi tiết đầu, và các trang bất thường
-- (listing parse ra 0 job, chi tiết không có nội dung).
--
-- content_gz là gzip của body text (UTF-8): HTML/JSON nén được ~5-10 lần nên
-- 1 mẫu thường chỉ vài chục KB. Bản ghi cũ hơn SNAPSHOT_RETENTION_DAYS bị xoá
-- cơ hội mỗi lần lưu snapshot mới (db/crawl_snapshots.py), không cần cron.
--
-- ON DELETE CASCADE: xoá lượt crawl thì snapshot đi theo (cùng lý do
-- crawl_run_logs). An toàn để chạy lại nhiều lần (IF NOT EXISTS mọi bước).

CREATE TABLE IF NOT EXISTS crawl_snapshots (
    id          BIGSERIAL PRIMARY KEY,
    run_id      UUID NOT NULL REFERENCES crawl_runs(run_id) ON DELETE CASCADE,
    -- Chép từ crawl_runs để tra "mẫu mới nhất của nguồn X" không cần JOIN.
    source      VARCHAR(50) NOT NULL,
    -- 'listing' | 'detail' | ...
    kind        VARCHAR(30) NOT NULL,
    -- 'sample' hoặc mã bất thường: 'listing_empty', 'detail_blank',
    -- 'detail_unparsable'.
    reason      VARCHAR(50) NOT NULL DEFAULT 'sample',
    url         TEXT NOT NULL DEFAULT '',
    content_gz  BYTEA NOT NULL,
    -- Kích thước text TRƯỚC khi nén (byte), để biết trang lớn cỡ nào.
    raw_bytes   INT NOT NULL,
    -- TRUE nếu body bị cắt theo SNAPSHOT_MAX_CHARS.
    truncated   BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_crawl_snapshots_run_id ON crawl_snapshots(run_id);
-- Dọn bản ghi cũ + tra "mẫu mới nhất của nguồn X".
CREATE INDEX IF NOT EXISTS idx_crawl_snapshots_source_created
    ON crawl_snapshots(source, created_at DESC);

-- ============================================================
-- HẾT FILE
-- ============================================================
