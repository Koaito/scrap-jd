-- Thêm bảng maintenance_runs — chạy nền + theo dõi 5 script bảo trì dữ
-- liệu (backfill_company_profiles.py, enrich_company_profile_from_website.py,
-- enrich_company_web_info.py, get_company_fb_linkedin_link.py,
-- check_expired_source_jobs.py) từ trang web dưới quyền admin, KHÔNG cần
-- chạy tay CLI từ máy local nữa (08/2026, xem lịch sử trao đổi "phương án
-- B — 1 bảng generic dùng chung", đối xứng crawl_runs/crawl_run_logs
-- nhưng generic hoá qua cột job_type + params thay vì source/category
-- riêng cho crawl).
--
-- KHÁC crawl_runs ở 2 điểm CHỦ Ý:
--   1. Khoá đồng thời theo job_type (không phải source) — 5 job này
--      không có khái niệm "nguồn", mỗi LOẠI job tối đa 1 lượt
--      queued/running tại 1 thời điểm, nhưng 2 job_type khác nhau (vd
--      đang backfill + đang check_expired_jobs) vẫn chạy song song
--      được, không đụng dữ liệu nhau theo cách gây race condition rõ
--      ràng (khác 2 lượt CÙNG job_type dễ chọn trùng batch job/company
--      để xử lý).
--   2. params (JSONB) thay cho các cột riêng — mỗi job_type có tập
--      tham số khác nhau (limit; hoặc limit+dry_run+check_deadline_only
--      ở check_expired_jobs) — xem api/schemas/maintenance.py để biết
--      đúng shape từng job_type. Không ép kiểu ở tầng DB (khác cột
--      thường), validate ở tầng Pydantic trước khi tới đây.
--
-- An toàn để chạy lại nhiều lần (IF NOT EXISTS mọi bước, trừ CREATE TYPE
-- dùng khối DO $$ bắt lỗi duplicate_object — cùng pattern
-- migration_add_crawl_runs.sql).

DO $$ BEGIN
    CREATE TYPE maintenance_job_type_enum AS ENUM (
        'backfill_company_profiles',
        'enrich_profile_from_website',
        'enrich_web_info',
        'get_fb_linkedin',
        'check_expired_jobs'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS maintenance_runs (
    run_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    job_type      maintenance_job_type_enum NOT NULL,

    -- Tham số truyền vào run() của script tương ứng — vd
    -- {"limit": 50} hoặc {"limit": 100, "dry_run": true,
    -- "check_deadline_only": false}. '{}' nghĩa là chạy KHÔNG giới hạn
    -- (limit=None), giống chạy CLI không kèm --limit.
    params        JSONB NOT NULL DEFAULT '{}',

    -- Dùng LẠI crawl_status_enum đã có (queued/running/done/error) —
    -- cùng ý nghĩa, không cần tạo enum trạng thái riêng.
    status        crawl_status_enum NOT NULL DEFAULT 'queued',

    -- Điền khi status='done' — dict `stats` trả về từ run() của từng
    -- script (shape khác nhau tuỳ job_type, xem docstring run() từng
    -- file — vd checked/updated/unchanged/errors ở backfill, hoặc
    -- checked/expired_by_deadline/... ở check_expired_jobs).
    stats         JSONB,

    -- Điền khi status='error' (str(exception) bắt được trong execute()).
    error         TEXT,

    -- Admin nào bấm — NULL dành sẵn cho trường hợp chạy tự động theo
    -- lịch sau này (APScheduler/cron), không phải lỗi dữ liệu, cùng quy
    -- ước triggered_by ở crawl_runs.
    triggered_by  UUID REFERENCES app_users(ss_user_id),

    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_maintenance_runs_status       ON maintenance_runs(status);
CREATE INDEX IF NOT EXISTS idx_maintenance_runs_job_type     ON maintenance_runs(job_type);
CREATE INDEX IF NOT EXISTS idx_maintenance_runs_started_at   ON maintenance_runs(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_maintenance_runs_triggered_by ON maintenance_runs(triggered_by);

-- ENFORCE Ở TẦNG DB: mỗi job_type chỉ được TỐI ĐA 1 dòng đang
-- 'queued' hoặc 'running' tại 1 thời điểm — UNIQUE INDEX có điều kiện,
-- đối xứng idx_crawl_runs_one_active_per_source nhưng khoá theo
-- job_type thay vì source. db/maintenance_runs.py::create_run() PHẢI
-- tự SELECT kiểm tra trước và raise ActiveMaintenanceRunExistsError để
-- router trả 409 rõ ràng — UNIQUE INDEX này là LỚP CHẶN THỨ 2 phòng
-- race condition, không phải lớp chặn chính (cùng lý do đã ghi ở
-- migration_add_crawl_runs.sql).
CREATE UNIQUE INDEX IF NOT EXISTS idx_maintenance_runs_one_active_per_job_type
    ON maintenance_runs(job_type)
    WHERE status IN ('queued', 'running');

CREATE TABLE IF NOT EXISTS maintenance_run_logs (
    id         BIGSERIAL PRIMARY KEY,
    run_id     UUID NOT NULL REFERENCES maintenance_runs(run_id) ON DELETE CASCADE,
    -- Khớp logging.LEVELNAME chuẩn của Python — dùng để tô màu dòng log
    -- khác nhau ở frontend, giống crawl_run_logs.level.
    level      VARCHAR(10) NOT NULL DEFAULT 'INFO',
    message    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Trang web poll GET /maintenance/{run_id}/logs?after_id=N để chỉ lấy
-- dòng MỚI — cùng lý do cần (run_id, id) như idx_crawl_run_logs_run_id_id.
CREATE INDEX IF NOT EXISTS idx_maintenance_run_logs_run_id_id ON maintenance_run_logs(run_id, id);

-- ============================================================
-- HẾT FILE
-- ============================================================
