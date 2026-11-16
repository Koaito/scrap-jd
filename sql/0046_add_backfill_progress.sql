-- Tiến độ của các script vá dữ liệu (scripts/backfill/*) lưu trong DB thay cho file `.backfill_*.done` cục bộ (E11).
--
-- Vấn đề của file cục bộ: chạy lại từ máy khác (hoặc sau khi xoá thư mục repo) là mất tiến độ, script tải lại
-- trang của mọi job đã vá xong, tốn request tới site nguồn và dễ bị chặn. DB là nơi mọi máy cùng nhìn thấy.
--
-- Mô hình: mỗi dòng nói "script X đã xử lý xong mục Y". Dùng chung cho mọi script nên không cần bảng mới cho
-- mỗi lần có script vá mới:
--   script_name  tên cố định của script (vd 'backfill_vnw_detail').
--   item_id      định danh mục đã xong, dạng chuỗi (hiện là job_id). TEXT, không phải UUID, để script sau này
--                đánh dấu được mục không phải job (công ty, URL...) mà khỏi sửa schema.
--   done_at      lúc đánh dấu.
-- Khoá chính (script_name, item_id): đánh dấu hai lần không sinh dòng trùng, và khoá này cũng là chỉ mục tra
-- theo script_name nên không cần chỉ mục riêng.
-- KHÔNG có khoá ngoại tới job_postings: dòng tiến độ không được chặn việc xoá job, và job đã xoá thì dòng đó
-- vô hại (script chỉ đọc danh sách "đã xong" để loại bớt, không join).
--
-- Migration CHỈ THÊM một bảng mới, chạy TRƯỚC khi push code dùng nó. Idempotent, không khoá bảng nào đang có.

SET LOCAL lock_timeout = '10s';

CREATE TABLE IF NOT EXISTS backfill_progress (
    script_name VARCHAR(60) NOT NULL,
    item_id     TEXT        NOT NULL,
    done_at     TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (script_name, item_id),
    CONSTRAINT ck_backfill_progress_not_blank
        CHECK (script_name <> '' AND item_id <> '')
);

COMMENT ON TABLE backfill_progress IS
    'Tiến độ script vá dữ liệu (scripts/backfill/*): mỗi dòng = script_name đã xử lý xong item_id (hiện là job_id). Thay cho file .backfill_*.done cục bộ.';
