-- Thêm job_postings.closed_reason và closed_at (A2, nửa 1/2).
--
-- TẠI SAO CẦN: pipeline (Phần 3c) mở lại job CLOSED khi thấy tin được đăng lại, nhưng KHÔNG được
-- mở lại job do nhân viên chủ động đóng. Trước đây "ai đóng job" chỉ suy được từ audit_logs
-- (find_repost_candidate đọc bản ghi DELETE_JOB gần nhất). Cách đó yếu: check_expired_source_jobs
-- đóng job mà không ghi audit nên phải suy từ việc thiếu bản ghi, job đóng trước khi có audit_logs
-- không nhận ra được, và mỗi lần tra phải quét audit_logs. Ghi thẳng lý do lên dòng job thì tra
-- được ngay và không phụ thuộc audit.
--
-- GIÁ TRỊ closed_reason:
--   staff         nhân viên đóng (PATCH /jobs, import file).
--   expired_auto  check_expired_source_jobs tự đóng (quá hạn nộp hoặc nguồn trả 404/410).
--   merged        DỰ PHÒNG. Hiện chưa có nơi nào ghi: gộp job xoá thật job phụ nên không còn dòng
--                 nào để đánh dấu. Giữ trong CHECK để khỏi phải sửa ràng buộc khi cần dùng.
--   unknown       đã đóng nhưng không rõ do ai. Gồm job đóng từ trước khi có cột này mà audit_logs
--                 không đủ để kết luận, và dòng bị ghi mà quên truyền lý do (trigger điền).
--                 Pipeline KHÔNG tự mở lại job unknown (coi như do nhân viên đóng).
--   NULL          job đang OPEN.
-- closed_at: lúc đóng. NULL khi job đang OPEN, hoặc job đã đóng từ trước migration mà không có
-- bản ghi audit cho biết giờ (NULL = không biết, không phải "chưa đóng").
--
-- BẤT BIẾN (CHECK chk_job_postings_closed_state): job OPEN thì closed_reason và closed_at đều NULL;
-- job CLOSED thì closed_reason NOT NULL. Trigger set_job_closed_state giữ bất biến này cho mọi
-- đường ghi, kể cả câu SQL tay hay code quên truyền lý do: chuyển sang CLOSED mà không có lý do thì
-- ghi 'unknown' (hướng an toàn: không bị tự mở lại), chuyển sang OPEN thì xoá hai cột.
--
-- BACKFILL job đang CLOSED (chỉ dòng chưa có closed_reason), theo thứ tự:
--   1. Sự kiện gần nhất của job trong audit_logs là DELETE_JOB (nhân viên đóng) mà sau đó chưa có
--      UPDATE_JOB đổi job_status sang OPEN  -> 'staff', closed_at = giờ của bản ghi đó.
--   2. Không có bằng chứng ở bước 1 và job chưa từng có ai sửa (updated_by IS NULL)  ->
--      'expired_auto'. Chỉ check_expired_source_jobs đóng job mà không để lại dấu người sửa
--      (PATCH và import đều ghi updated_by). closed_at để NULL.
--   3. Còn lại (đã có người sửa nhưng không có bản ghi đóng) -> 'unknown'.
-- Bước 1 giữ đúng nghĩa luật cũ của find_repost_candidate; bước 3 chặt hơn luật cũ: job như vậy
-- trước đây được mở lại, nay thì không.
--
-- Backfill bật app.skip_updated_at nên KHÔNG đổi updated_at của dòng nào. Idempotent.

ALTER TABLE job_postings
    ADD COLUMN IF NOT EXISTS closed_reason VARCHAR(20);
ALTER TABLE job_postings
    ADD COLUMN IF NOT EXISTS closed_at TIMESTAMPTZ;

CREATE OR REPLACE FUNCTION trg_set_job_closed_state() RETURNS TRIGGER AS $$
BEGIN
    IF NEW.job_status = 'CLOSED' THEN
        IF TG_OP = 'INSERT' OR OLD.job_status <> 'CLOSED' THEN
            -- Vừa chuyển sang CLOSED.
            NEW.closed_reason := COALESCE(NEW.closed_reason, 'unknown');
            NEW.closed_at := COALESCE(NEW.closed_at, now());
        ELSE
            -- Đã CLOSED từ trước: giữ lý do và giờ cũ, không bịa giờ mới.
            NEW.closed_reason := COALESCE(NEW.closed_reason, OLD.closed_reason, 'unknown');
            NEW.closed_at := COALESCE(NEW.closed_at, OLD.closed_at);
        END IF;
    ELSE
        NEW.closed_reason := NULL;
        NEW.closed_at := NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS set_job_closed_state ON job_postings;
CREATE TRIGGER set_job_closed_state
BEFORE INSERT OR UPDATE ON job_postings
FOR EACH ROW EXECUTE FUNCTION trg_set_job_closed_state();

-- Backfill (không đổi updated_at).
SELECT set_config('app.skip_updated_at', 'on', true);

WITH last_events AS (
    SELECT DISTINCT ON (a.entity_id) a.entity_id AS job_id, a.action_type::text AS action_type,
           a.created_at
      FROM audit_logs a
     WHERE a.entity_type = 'JOB'
       AND (a.action_type = 'DELETE_JOB'
            OR (a.action_type = 'UPDATE_JOB'
                AND a.changes -> 'job_status' ->> 'new' = 'OPEN'))
     ORDER BY a.entity_id, a.created_at DESC
)
UPDATE job_postings j
   SET closed_reason = CASE
           WHEN x.action_type = 'DELETE_JOB' THEN 'staff'
           WHEN j.updated_by IS NULL THEN 'expired_auto'
           ELSE 'unknown'
       END,
       closed_at = CASE WHEN x.action_type = 'DELETE_JOB' THEN x.created_at END
  FROM (
        SELECT p.job_id, e.action_type, e.created_at
          FROM job_postings p
          LEFT JOIN last_events e ON e.job_id = p.job_id
         WHERE p.job_status = 'CLOSED' AND p.closed_reason IS NULL
       ) x
 WHERE j.job_id = x.job_id;

DO $$ BEGIN
    ALTER TABLE job_postings ADD CONSTRAINT chk_job_postings_closed_reason CHECK (
        closed_reason IS NULL OR closed_reason IN ('staff', 'expired_auto', 'merged', 'unknown')
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

-- CASE (không phải AND/OR): CHECK coi NULL là "đạt".
DO $$ BEGIN
    ALTER TABLE job_postings ADD CONSTRAINT chk_job_postings_closed_state CHECK (
        CASE
            WHEN job_status = 'CLOSED' THEN closed_reason IS NOT NULL
            ELSE closed_reason IS NULL AND closed_at IS NULL
        END
    );
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

COMMENT ON COLUMN job_postings.closed_reason IS
    'Vì sao job CLOSED: staff | expired_auto | merged (dự phòng) | unknown. NULL khi OPEN. Pipeline chỉ tự mở lại job expired_auto.';
COMMENT ON COLUMN job_postings.closed_at IS
    'Lúc job chuyển sang CLOSED. NULL khi OPEN, hoặc job đóng từ trước khi có cột mà không biết giờ.';
