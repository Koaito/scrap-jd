-- Thêm bảng audit_logs — lịch sử thao tác của ss_team/admin trên JD,
-- company, HR contact (08/2026, xem lịch sử trao đổi "log tự động +
-- log thủ công").
--
-- THIẾT KẾ: 1 BẢNG DUY NHẤT cho cả 2 "loại log" mà team nhìn thấy ở
-- UI — KHÔNG tách 2 bảng riêng. "Log tự động" và "log thủ công" chỉ là
-- 2 CÁCH LỌC khác nhau trên cùng 1 bảng này (xem
-- api/routers/audit_logs.py::list_audit_logs, query param `view`):
--   - view=auto   -> trả TẤT CẢ dòng, không hiển thị cột note.
--   - view=manual -> chỉ trả dòng có is_manual_log=true, kèm cột note.
-- Lý do gộp 1 bảng: log thủ công vốn là tập con của log tự động (mọi
-- thao tác sửa/xoá JD, sửa/xoá company, mọi thao tác HR contact ĐỀU
-- nằm trong log tự động luôn) — tách 2 bảng sẽ phải ghi 2 lần mỗi thao
-- tác, dễ lệch dữ liệu giữa 2 bảng.
--
-- is_manual_log / note_required: tính SẴN lúc INSERT theo action_type
-- (xem db.py::ACTION_LOG_RULES), KHÔNG derive lại mỗi lần query — nếu
-- sau này đổi luật phân loại (vd thêm action bắt buộc note), log CŨ
-- vẫn giữ nguyên đúng luật lúc nó được tạo ra, không bị đổi ngược lại.
--
-- note: CHỈ 1 CỘT duy nhất dùng chung cho mọi action — "bắt buộc hay
-- không" là logic ở tầng API (xem api/routers/*.py), KHÔNG phải 2 cột
-- riêng. CHECK constraint bên dưới là lớp chặn THỨ 2 ở tầng DB, phòng
-- trường hợp code/script nào đó lỡ insert thẳng mà quên validate.
--
-- An toàn để chạy lại nhiều lần (IF NOT EXISTS ở mọi bước).

DO $$ BEGIN
    CREATE TYPE audit_action_enum AS ENUM (
        'CREATE_JOB', 'UPDATE_JOB', 'DELETE_JOB',
        'CREATE_COMPANY', 'UPDATE_COMPANY', 'DELETE_COMPANY',
        'CREATE_CONTACT', 'UPDATE_CONTACT', 'DELETE_CONTACT', 'ASSIGN_CONTACT'
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

CREATE TABLE IF NOT EXISTS audit_logs (
    log_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Người thực hiện — NULL nghĩa là "crawl/hệ thống tự động" (giống
    -- quy ước created_by=NULL ở companies/job_postings), KHÔNG phải lỗi.
    actor_id        UUID REFERENCES app_users(ss_user_id),

    action_type     audit_action_enum NOT NULL,

    -- 'JOB' | 'COMPANY' | 'CONTACT' — dùng text ngắn thay vì enum riêng
    -- vì chỉ 3 giá trị cố định, không cần validate ở tầng DB chặt hơn
    -- audit_action_enum đã tự nói lên entity_type rồi (UPDATE_JOB chỉ
    -- có thể đi với entity_type='JOB'), cột này chủ yếu để filter/JOIN
    -- nhanh không cần CASE theo action_type.
    entity_type     VARCHAR(20) NOT NULL,
    entity_id       UUID NOT NULL,

    -- Snapshot tên JD/company/contact TẠI THỜI ĐIỂM log — để hiển thị
    -- được dù entity sau này bị xoá/đổi tên (không phải JOIN lại lúc
    -- hiển thị, cũng không bị "vỡ" nếu entity đã xoá mềm/xoá cứng).
    entity_label    VARCHAR(255),

    -- company_id: LUÔN điền dù entity_type là JOB/CONTACT (không chỉ
    -- COMPANY) — để lọc "mọi hoạt động liên quan đến công ty X" trong
    -- 1 query, không cần JOIN ngược qua job_postings/company_contacts.
    company_id      UUID REFERENCES companies(company_id),

    -- Diff các field thay đổi cho action UPDATE_*, dạng
    -- {"field_name": {"old": ..., "new": ...}}. NULL cho CREATE/DELETE/
    -- ASSIGN (xem docstring db.py::log_action để biết action nào có
    -- changes).
    changes         JSONB,

    -- Có nằm trong view "log thủ công" không — tính theo action_type
    -- lúc insert (xem db.py::ACTION_LOG_RULES).
    is_manual_log   BOOLEAN NOT NULL,

    -- Note có BẮT BUỘC lúc thao tác không — tính theo action_type lúc
    -- insert. true => note KHÔNG được NULL (ép ở CHECK bên dưới, và ở
    -- tầng API chặn TRƯỚC KHI thao tác chính chạy — xem docstring
    -- db.py::log_action).
    note_required   BOOLEAN NOT NULL DEFAULT false,

    note            TEXT,

    -- Ai sửa note GẦN NHẤT — CHỈ actor_id gốc được sửa (ép ở tầng API,
    -- xem api/routers/audit_logs.py::update_audit_log_note), 2 cột này
    -- vẫn lưu lại phòng khi sau này đổi luật cho phép người khác sửa.
    note_updated_by UUID REFERENCES app_users(ss_user_id),
    note_updated_at TIMESTAMPTZ,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT chk_audit_logs_note_required
        CHECK (NOT note_required OR note IS NOT NULL)
);

-- Tra theo entity cụ thể (vd xem lịch sử sửa của 1 job) — dùng ở trang
-- detail JD/company/contact sau này nếu cần.
CREATE INDEX IF NOT EXISTS idx_audit_logs_entity ON audit_logs(entity_type, entity_id);

-- Tra "mọi hoạt động liên quan công ty X" (JD + contact của công ty đó).
CREATE INDEX IF NOT EXISTS idx_audit_logs_company ON audit_logs(company_id);

-- Tra theo người thực hiện (vd "admin xem tất cả log của nhân viên Y").
CREATE INDEX IF NOT EXISTS idx_audit_logs_actor ON audit_logs(actor_id);

-- Tab "log thủ công", sắp mới nhất trước — query chính của UI.
CREATE INDEX IF NOT EXISTS idx_audit_logs_manual
    ON audit_logs(is_manual_log, created_at DESC);

-- Query "còn log nào đang chờ note" — dùng cho badge nhắc nhở ở UI.
-- Partial index, chỉ có ý nghĩa (và chỉ tốn dung lượng) cho các dòng
-- thật sự cần note mà chưa điền, không index toàn bộ bảng.
CREATE INDEX IF NOT EXISTS idx_audit_logs_pending_note
    ON audit_logs(is_manual_log)
    WHERE note_required = true AND note IS NULL;

-- ============================================================
-- HẾT FILE
-- ============================================================
