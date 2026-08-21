"""
Script RIÊNG (không nằm trong pipeline crawl chính) — re-check job đang
OPEN trong DB xem còn tồn tại thật ở nguồn (TopCV/VietnamWorks) hay
<<<<<<< HEAD
không, tự động chuyển job_status='EXPIRED' cho job KHÔNG còn tồn tại.
=======
không, tự động chuyển job_status='CLOSED' cho job KHÔNG còn tồn tại.
>>>>>>> 30bf9a43af4e25374ed7eade1dce9557ac563b8a

BỐI CẢNH: JD trên TopCV/VietnamWorks bị nhà tuyển dụng xoá sau 1 thời
gian (hết nhu cầu tuyển, đủ hồ sơ...) — DB của mình vẫn giữ job đó ở
OPEN mãi vì không có gì tự động phát hiện link nguồn đã chết. Học viên
bấm "Xem JD gốc" sẽ ra trang lỗi/404, và job vẫn hiện ra như đang tuyển
dù thực ra không còn nữa.

<<<<<<< HEAD
TẠI SAO DÙNG job_status='EXPIRED' MÀ KHÔNG PHẢI 'CLOSED': 2 status này
khác nghĩa trong hệ thống (xem api/schemas.py) — CLOSED dành cho quyết
định CHỦ ĐỘNG của team SS (nút "Xoá" job ở frontend), EXPIRED dành cho
job tự nhiên hết hiệu lực không do ai quyết định. Job nguồn bị xoá đúng
là trường hợp EXPIRED, không phải CLOSED — dùng đúng status để sau này
lọc/báo cáo phân biệt được lý do đóng job (query WHERE job_status =
'EXPIRED' sẽ ra đúng nhóm job "chết tự nhiên", không lẫn job team SS
chủ động đóng).

NGUYÊN TẮC "THÀ THIẾU CÒN HƠN SAI" (xuyên suốt project, xem
get_company_fb_linkedin_link.py) — áp dụng NGHIÊM NGẶT ở đây vì hậu quả
sai lớn hơn nhiều so với việc thiếu social link: EXPIRED nhầm 1 job vẫn
đang tuyển thật sẽ chặn học viên ứng tuyển (xem api/routers/me.py — chỉ
ứng tuyển được job OPEN). Vì vậy CHỈ tự động EXPIRED khi tín hiệu KHÔNG
MƠ HỒ:

  - HTTP 404 hoặc 410 (Gone) từ chính source_url -> EXPIRED. Đây là tín
=======
08/2026: bỏ status 'EXPIRED' khỏi job_status_enum (xem
sql/migration_remove_expired_job_status.sql) — trước đây script này
dùng EXPIRED để phân biệt "job chết tự nhiên" với CLOSED ("SS chủ động
đóng"), giờ đổi ý gộp chung, không phân biệt lý do đóng job ở tầng
job_status nữa. Script vẫn giữ nguyên logic PHÁT HIỆN job chết (deadline
qua hạn / nguồn trả 404-410), chỉ đổi status ghi vào DB từ EXPIRED sang
CLOSED.

NGUYÊN TẮC "THÀ THIẾU CÒN HƠN SAI" (xuyên suốt project, xem
get_company_fb_linkedin_link.py) — áp dụng NGHIÊM NGẶT ở đây vì hậu quả
sai lớn hơn nhiều so với việc thiếu social link: đóng nhầm 1 job vẫn
đang tuyển thật sẽ chặn học viên ứng tuyển (xem api/routers/me.py — chỉ
ứng tuyển được job OPEN). Vì vậy CHỈ tự động đóng job khi tín hiệu KHÔNG
MƠ HỒ:

  - HTTP 404 hoặc 410 (Gone) từ chính source_url -> CLOSED. Đây là tín
>>>>>>> 30bf9a43af4e25374ed7eade1dce9557ac563b8a
    hiệu rõ ràng nhất: server nguồn xác nhận URL không còn tồn tại.

  MỌI trường hợp khác (200 nhưng redirect sang trang khác/trang chủ,
  timeout, lỗi mạng, 403 bị chặn bot, 5xx server nguồn tạm lỗi...) —
  KHÔNG kết luận, KHÔNG đụng vào job_status, chỉ ghi vào
  stats["cần_kiểm_tra_tay"] để người xem log tự vào tay kiểm tra nếu
  muốn. Lý do: TopCV/VietnamWorks có thể trả 200 kèm redirect về trang
  chủ/trang tìm kiếm khi job hết hạn (chưa xác nhận được bằng thực
  nghiệm mẫu HTML/redirect thật của từng trang lúc viết script này) —
  tự ý đoán dấu hiệu "trang đã đổi nội dung" dễ bắt nhầm job THẬT (vd
  site tạm bảo trì, đổi giao diện, chặn bot bằng challenge page) thành
<<<<<<< HEAD
  EXPIRED, an toàn hơn nhiều nếu chỉ tin 404/410 rồi để phần còn lại
=======
  đã đóng, an toàn hơn nhiều nếu chỉ tin 404/410 rồi để phần còn lại
>>>>>>> 30bf9a43af4e25374ed7eade1dce9557ac563b8a
  cho người kiểm tra tay. Có thể bổ sung tín hiệu khác sau khi đã xem
  qua vài chục job ở "cần_kiểm_tra_tay" để biết dấu hiệu thật của từng
  site trông như thế nào.

  Case KHÔNG cần fetch mạng, hoàn toàn an toàn, được gộp CHUNG script
<<<<<<< HEAD
  này qua cờ --check-deadline: deadline đã qua ngày hôm nay -> EXPIRED
  luôn (không phụ thuộc source_url có sống hay không, đây là quyết định
  đã có sẵn ý nghĩa rõ ràng trong schema, tách biệt hoàn toàn với phần
  check link nguồn ở trên).
=======
  này qua cờ --check-deadline: deadline đã qua ngày hôm nay -> CLOSED
  luôn (không phụ thuộc source_url có sống hay không).
>>>>>>> 30bf9a43af4e25374ed7eade1dce9557ac563b8a

CHẠY:
    python check_expired_source_jobs.py                # check tất cả job OPEN có source_url
    python check_expired_source_jobs.py --limit 20      # test thử trước khi chạy full
    python check_expired_source_jobs.py --check-deadline  # CHỈ check deadline quá hạn, không fetch mạng
<<<<<<< HEAD
    python check_expired_source_jobs.py --dry-run       # chỉ in ra job sẽ bị EXPIRED, KHÔNG ghi DB
=======
    python check_expired_source_jobs.py --dry-run       # chỉ in ra job sẽ bị đóng, KHÔNG ghi DB
>>>>>>> 30bf9a43af4e25374ed7eade1dce9557ac563b8a

NÊN CHẠY ĐỊNH KỲ (chưa có cron tự động — xem README mục "Việc còn tồn
đọng"), gợi ý: 1 tuần/lần bằng tay hoặc lên lịch sau khi có hạ tầng
scheduler.
"""

import argparse
import logging
import time
from datetime import date
from typing import Optional

from curl_cffi import requests

import db
from config import DEFAULT_HEADERS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                     datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

REQUEST_DELAY_SECONDS = 3.0   # giãn cách giữa các lần fetch, tránh bị site nguồn chặn bot
REQUEST_TIMEOUT_SECONDS = 10


class _Throttled404Checker:
    """Chỉ hỏi source_url còn sống hay không (HEAD trước, fallback GET
    nếu server không hỗ trợ HEAD đúng) — không cần đọc/parse nội dung
    trang như get_company_fb_linkedin_link.py, nên KHÔNG cần BeautifulSoup
    ở đây, chỉ cần status_code."""

    def __init__(self):
        self.session = requests.Session(impersonate="chrome124")
        self.session.headers.update(DEFAULT_HEADERS)
        self._last_request_time: Optional[float] = None

    def check(self, url: str) -> Optional[int]:
        """Trả HTTP status_code, hoặc None nếu fetch lỗi hoàn toàn (mất
        mạng/timeout/site chặn ở tầng TLS...) — None KHÔNG được coi là
<<<<<<< HEAD
        tín hiệu EXPIRED (xem nguyên tắc ở docstring đầu file)."""
=======
        tín hiệu job đã đóng (xem nguyên tắc ở docstring đầu file)."""
>>>>>>> 30bf9a43af4e25374ed7eade1dce9557ac563b8a
        self._throttle()
        try:
            resp = self.session.head(url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=True)
            self._last_request_time = time.monotonic()
            # Vài site không hỗ trợ HEAD đúng chuẩn (trả 405 dù URL vẫn
            # sống) -> fallback GET trước khi kết luận gì từ mã lỗi này.
            if resp.status_code == 405:
                return self._get_fallback(url)
            return resp.status_code
        except requests.exceptions.RequestException as exc:
            self._last_request_time = time.monotonic()
            logger.warning("Lỗi fetch %s: %s -> bỏ qua, không kết luận", url, exc)
            return None

    def _get_fallback(self, url: str) -> Optional[int]:
        self._throttle()
        try:
            resp = self.session.get(url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=True)
            self._last_request_time = time.monotonic()
            return resp.status_code
        except requests.exceptions.RequestException as exc:
            self._last_request_time = time.monotonic()
            logger.warning("Lỗi fetch (fallback GET) %s: %s -> bỏ qua", url, exc)
            return None

    def _throttle(self):
        if self._last_request_time is None:
            return
        elapsed = time.monotonic() - self._last_request_time
        remaining = REQUEST_DELAY_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)


def run(limit: Optional[int] = None, check_deadline_only: bool = False,
        dry_run: bool = False) -> dict:
    stats = {
<<<<<<< HEAD
        "checked": 0, "expired_by_source_dead": 0, "expired_by_deadline": 0,
=======
        "checked": 0, "closed_by_source_dead": 0, "closed_by_deadline": 0,
>>>>>>> 30bf9a43af4e25374ed7eade1dce9557ac563b8a
        "still_alive": 0, "cần_kiểm_tra_tay": 0,
    }

    conn = db.get_connection()
    checker = _Throttled404Checker()
    today = date.today()
    try:
        jobs = db.get_open_jobs_with_source_url(conn)
        if limit:
            jobs = jobs[:limit]

        logger.info("Tìm thấy %d job đang OPEN có source_url để kiểm tra", len(jobs))

        for job_id, job_title, source_url, deadline in jobs:
            stats["checked"] += 1
            logger.info("[%d/%d] %s (%s)", stats["checked"], len(jobs), job_title, job_id)

            # Nhánh deadline — không tốn request mạng, luôn chạy trước
            # (kể cả khi --check-deadline không bật, vì đây là tín hiệu
            # miễn phí, không có lý do bỏ qua).
            if deadline is not None and deadline < today:
<<<<<<< HEAD
                stats["expired_by_deadline"] += 1
                logger.info("  -> deadline %s đã qua -> EXPIRED", deadline)
                if not dry_run:
                    db.update_job(conn, job_id, job_status="EXPIRED")
=======
                stats["closed_by_deadline"] += 1
                logger.info("  -> deadline %s đã qua -> CLOSED", deadline)
                if not dry_run:
                    db.update_job(conn, job_id, job_status="CLOSED")
>>>>>>> 30bf9a43af4e25374ed7eade1dce9557ac563b8a
                    conn.commit()
                continue

            if check_deadline_only:
                continue

            status_code = checker.check(source_url)
            if status_code in (404, 410):
<<<<<<< HEAD
                stats["expired_by_source_dead"] += 1
                logger.info("  -> nguồn trả HTTP %d -> EXPIRED", status_code)
                if not dry_run:
                    db.update_job(conn, job_id, job_status="EXPIRED")
=======
                stats["closed_by_source_dead"] += 1
                logger.info("  -> nguồn trả HTTP %d -> CLOSED", status_code)
                if not dry_run:
                    db.update_job(conn, job_id, job_status="CLOSED")
>>>>>>> 30bf9a43af4e25374ed7eade1dce9557ac563b8a
                    conn.commit()
            elif status_code is not None and 200 <= status_code < 300:
                stats["still_alive"] += 1
            else:
                # None (lỗi fetch) hoặc mã khác (3xx lạ, 403, 5xx...) —
                # KHÔNG mơ hồ đủ để tự kết luận, xem docstring đầu file.
                stats["cần_kiểm_tra_tay"] += 1
                logger.info("  -> HTTP %s, không đủ rõ để tự kết luận -> cần kiểm tra tay: %s",
                            status_code, source_url)
    finally:
        conn.close()

    return stats


def main():
    parser = argparse.ArgumentParser(
        description="Re-check job OPEN xem còn tồn tại ở nguồn (TopCV/VietnamWorks) không, "
<<<<<<< HEAD
                     "tự động chuyển EXPIRED nếu nguồn xác nhận đã xoá (404/410) hoặc deadline đã qua."
=======
                     "tự động chuyển CLOSED nếu nguồn xác nhận đã xoá (404/410) hoặc deadline đã qua."
>>>>>>> 30bf9a43af4e25374ed7eade1dce9557ac563b8a
    )
    parser.add_argument("--limit", type=int, default=None,
                         help="Giới hạn số job xử lý (dùng để test thử trước khi chạy full)")
    parser.add_argument("--check-deadline", action="store_true",
                         help="CHỈ check deadline quá hạn, KHÔNG fetch mạng tới source_url "
                              "(nhanh hơn nhiều, chạy được thường xuyên hơn)")
    parser.add_argument("--dry-run", action="store_true",
<<<<<<< HEAD
                         help="Chỉ in ra job SẼ bị EXPIRED, không ghi gì vào DB — dùng để xem "
=======
                         help="Chỉ in ra job SẼ bị đóng, không ghi gì vào DB — dùng để xem "
>>>>>>> 30bf9a43af4e25374ed7eade1dce9557ac563b8a
                              "trước kết quả trước khi chạy thật")
    args = parser.parse_args()

    stats = run(limit=args.limit, check_deadline_only=args.check_deadline, dry_run=args.dry_run)

    print("\n===== KẾT QUẢ =====" + (" (DRY RUN — chưa ghi gì vào DB)" if args.dry_run else ""))
    print(f"Đã kiểm tra                      : {stats['checked']}")
<<<<<<< HEAD
    print(f"EXPIRED do deadline đã qua        : {stats['expired_by_deadline']}")
    print(f"EXPIRED do nguồn trả 404/410      : {stats['expired_by_source_dead']}")
=======
    print(f"CLOSED do deadline đã qua         : {stats['closed_by_deadline']}")
    print(f"CLOSED do nguồn trả 404/410       : {stats['closed_by_source_dead']}")
>>>>>>> 30bf9a43af4e25374ed7eade1dce9557ac563b8a
    print(f"Vẫn còn sống (200 OK)             : {stats['still_alive']}")
    print(f"⚠️  Cần kiểm tra tay (không rõ)    : {stats['cần_kiểm_tra_tay']}")


if __name__ == "__main__":
    main()
