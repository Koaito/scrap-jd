-- job_sources_log.listing_status: bỏ DEFAULT 'UNKNOWN'.
--
-- DEFAULT 'UNKNOWN' được đặt ở 0043 (C1) "cho dòng ghi bởi code chưa biết các cột này" để code cũ vẫn chạy trong
-- lúc chuyển đổi. Chuyển đổi đã xong: đường INSERT listing duy nhất (db.listing_state.insert_listing) luôn đặt
-- listing_status rõ ràng, và 0048 đã đóng hết UNKNOWN di sản. Giữ DEFAULT nghĩa là một đường ghi mới quên cột
-- này sẽ lặng lẽ sinh listing UNKNOWN, tức là tái tạo đúng lỗi 0048 vừa dọn (job OPEN mãi). Bỏ DEFAULT biến
-- "quên đặt trạng thái" thành lỗi NOT NULL ngay khi chạy, thay vì dữ liệu sai âm thầm.
--
-- Giá trị 'UNKNOWN' vẫn được CHECK cho phép và luật suy ra vẫn hiểu nó (không đổi hành vi, không đụng dòng nào);
-- chỉ là không còn gì tự sinh ra nó. Gỡ hẳn trạng thái UNKNOWN là việc khác, lớn hơn, chưa cần.
--
-- THỨ TỰ TRIỂN KHAI: chạy SAU 0048. Không cần code mới đi kèm (code hiện tại luôn đặt listing_status). Chỉ đổi
-- metadata cột (ACCESS EXCLUSIVE rất ngắn, không quét bảng). Idempotent: bỏ DEFAULT lần hai không làm gì.

SET LOCAL lock_timeout = '10s';

ALTER TABLE job_sources_log ALTER COLUMN listing_status DROP DEFAULT;

COMMENT ON COLUMN job_sources_log.listing_status IS
    'Trạng thái của listing (URL tin đăng) này: OPEN | CLOSED | UNKNOWN (chưa kết luận; không còn tự sinh, mọi lần ghi phải đặt rõ, xem 0049).';
