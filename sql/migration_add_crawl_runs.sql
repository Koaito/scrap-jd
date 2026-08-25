-- Thêm bảng crawl_runs — lưu bền lịch sử + trạng thái từng lượt crawl
-- (08/2026, xem lịch sử trao đổi "phương án 2: bảng crawl_runs riêng").
--
-- THAY THẾ HẲN _RUNS (dict trong RAM của api/crawl_runner.py) — không
-- chạy song song 2 nguồn dữ liệu. Giải quyết đồng thời 2 giới hạn cũ
-- đã ghi trong docstring crawl_runner.py:
--   - Mất lịch sử khi server restart (Render sleep dậy/deploy mới).
--   - Không đồng bộ nếu chạy nhiều worker uvicorn (mỗi worker RAM
--     riêng) — giờ mọi worker đọc/ghi chung 1 bảng Postgres.
--
-- ĐÃ CHỐT (xem lịch sử trao đổi):
--   - Giới hạn "1 lượt crawl/nguồn tại 1 thời điểm" enforce Ở TẦNG DB
--     bằng UNIQUE INDEX có điều kiện bên dưới (idx_crawl_runs_one_active_
--     per_source) — KHÔNG chỉ dựa vào disable nút ở frontend như trước.
--     TopCV và VietnamWorks (2 nguồn khác nhau) vẫn được chạy song song
--     bình thường.
--   - GET /crawl/{run_id} và GET /crawl (mới) đều yêu cầu tối thiểu
--     role 'ss_team' — xem api/routers/crawl.py (trước đây GET
--     /crawl/{run_id} không yêu cầu đăng nhập).
--
-- An toàn để chạy lại nhiều lần (IF NOT EXISTS ở mọi bước, trừ CREATE
-- TYPE dùng khối DO $$ bắt lỗi duplicate_object — cùng pattern
-- migration_add_audit_logs.sql).

DO $$ BEGIN
    CREATE TYPE crawl_status_enum AS ENUM ('queued', 'running', 'done', 'error');
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS crawl_runs (
    run_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Khớp _SOURCE_ADAPTERS / _CATEGORIES_BY_SOURCE trong
    -- api/crawl_runner.py, api/routers/crawl.py — không dùng ENUM vì
    -- danh sách nguồn/category còn có thể thêm (vd careerviet) mà
    -- không muốn phải ALTER TYPE mỗi lần, khác trường hợp status (cố
    -- định, ít khả năng đổi).
    source        VARCHAR(50)  NOT NULL,
    category      VARCHAR(100) NOT NULL,
    pages         INT NOT NULL,
    max_jobs      INT,

    status        crawl_status_enum NOT NULL DEFAULT 'queued',

    -- Điền khi status='done' — dict trả về từ pipeline.run_pipeline():
    -- fetched, inserted, skipped_duplicate, skipped_duplicate_repost,
    -- updated_existing, skipped_fetch_failed, errors,
    -- skipped_anonymous_employer.
    stats         JSONB,

    -- Điền khi status='error' (str(exception) bắt được trong execute()).
    error         TEXT,

    -- Admin nào bấm crawl — NULL đã CHỦ ĐÍCH dành sẵn cho crawl tự
    -- động theo lịch sau này (APScheduler/cron, xem "NÂNG CẤP SAU"
    -- trong crawl_runner.py cũ), không phải lỗi dữ liệu. KHÔNG dùng
    -- ON DELETE CASCADE/SET NULL tường minh — giữ mặc định RESTRICT
    -- như mọi FK khác trong schema.sql (admin có lượt crawl đã chạy
    -- thì không xoá cứng tài khoản được, phải soft-delete qua
    -- is_active nếu sau này thêm, giống app_users nói chung).
    triggered_by  UUID REFERENCES app_users(ss_user_id),

    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);

-- Tra nhanh "lượt nào đang chạy" — dùng cả cho check chặn trùng nguồn
-- (db/crawl_runs.py::create_run) lẫn filter ?status= ở GET /crawl.
CREATE INDEX IF NOT EXISTS idx_crawl_runs_status ON crawl_runs(status);

-- Trang lịch sử, sắp mới nhất trước — query chính của GET /crawl.
CREATE INDEX IF NOT EXISTS idx_crawl_runs_started_at ON crawl_runs(started_at DESC);

CREATE INDEX IF NOT EXISTS idx_crawl_runs_source       ON crawl_runs(source);
CREATE INDEX IF NOT EXISTS idx_crawl_runs_triggered_by ON crawl_runs(triggered_by);

-- ENFORCE Ở TẦNG DB: mỗi nguồn (source) chỉ được TỐI ĐA 1 dòng đang
-- 'queued' hoặc 'running' tại 1 thời điểm — UNIQUE INDEX có điều kiện
-- (partial index) trên riêng cột `source`, WHERE lọc đúng 2 trạng thái
-- "đang sống". Khi đã 'done'/'error', dòng đó không còn nằm trong index
-- này nữa nên nguồn đó lại được crawl tiếp bình thường.
--
-- db/crawl_runs.py::create_run() PHẢI tự SELECT kiểm tra trước và raise
-- ActiveCrawlExistsError để router trả 409 rõ ràng (giống pattern
-- NoteRequiredError ở audit_logs) — UNIQUE INDEX này chỉ là LỚP CHẶN
-- THỨ 2 phòng race condition (2 request POST /crawl cùng nguồn tới
-- gần như đồng thời, cả 2 đều pass qua SELECT check trước khi kịp
-- INSERT), không phải lớp chặn chính.
CREATE UNIQUE INDEX IF NOT EXISTS idx_crawl_runs_one_active_per_source
    ON crawl_runs(source)
    WHERE status IN ('queued', 'running');

-- ============================================================
-- HẾT FILE
-- ============================================================
