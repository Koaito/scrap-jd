"""
Script RIÊNG (không nằm trong pipeline crawl chính) — re-check job đang
OPEN trong DB xem còn tồn tại thật ở nguồn (TopCV/VietnamWorks) hay
không, tự động chuyển job_status='CLOSED' cho job KHÔNG còn tồn tại.

BỐI CẢNH: JD trên TopCV/VietnamWorks bị nhà tuyển dụng xoá sau 1 thời
gian (hết nhu cầu tuyển, đủ hồ sơ...) — DB của mình vẫn giữ job đó ở
OPEN mãi vì không có gì tự động phát hiện link nguồn đã chết. Học viên
bấm "Xem JD gốc" sẽ ra trang lỗi/404, và job vẫn hiện ra như đang tuyển
dù thực ra không còn nữa.

CẬP NHẬT 08/2026 — ĐÃ ĐỔI TỪ 'EXPIRED' SANG 'CLOSED': trước đây script
này dùng riêng job_status='EXPIRED' để phân biệt "job chết tự nhiên"
(script này set) với "SS chủ động đóng" (CLOSED, set tay qua frontend).
Quyết định đã đổi ý — gộp 2 trường hợp lại chung CLOSED cho đơn giản,
và enum job_status_enum trong DB ĐÃ BỎ HẲN giá trị EXPIRED (xem
sql/migration_remove_expired_job_status.sql). Gọi
db.update_job(..., job_status="EXPIRED") sau migration đó sẽ lỗi ngay ở
tầng Postgres (invalid input value for enum job_status_enum) — script
PHẢI dùng "CLOSED", không được quay lại "EXPIRED" trừ khi migration đó
bị revert.

NGUYÊN TẮC "THÀ THIẾU CÒN HƠN SAI" (xuyên suốt project, xem
scrapjd/maintenance/get_company_fb_linkedin_link.py) — áp dụng NGHIÊM NGẶT ở đây vì hậu quả
sai lớn hơn nhiều so với việc thiếu social link: đóng nhầm 1 job vẫn
đang tuyển thật sẽ chặn học viên ứng tuyển (xem scrapjd/api/routers/me.py — chỉ
ứng tuyển được job OPEN). Vì vậy CHỈ tự động đóng job khi tín hiệu KHÔNG
MƠ HỒ:

  - HTTP 404 hoặc 410 (Gone) từ URL của một listing -> listing đó CLOSED
    (từ C3b kiểm MỌI listing OPEN/UNKNOWN của job OPEN, không chỉ URL hiện
    hành của job; job CLOSED khi không còn listing sống nào; HTTP 2xx chỉ
    ghi last_seen_at, KHÔNG biến listing UNKNOWN thành OPEN; listing
    manual:// không có URL để hỏi nên bị bỏ qua). Đây là tín
    hiệu rõ ràng nhất: server nguồn xác nhận URL không còn tồn tại.

  MỌI trường hợp khác (200 nhưng redirect sang trang khác/trang chủ,
  timeout, lỗi mạng, 403 bị chặn bot, 5xx server nguồn tạm lỗi...) —
  KHÔNG kết luận, KHÔNG đụng vào job_status, chỉ ghi vào
  stats["needs_manual_check"] để người xem log tự vào tay kiểm tra nếu
  muốn. Lý do: TopCV/VietnamWorks có thể trả 200 kèm redirect về trang
  chủ/trang tìm kiếm khi job hết hạn (chưa xác nhận được bằng thực
  nghiệm mẫu HTML/redirect thật của từng trang lúc viết script này) —
  tự ý đoán dấu hiệu "trang đã đổi nội dung" dễ bắt nhầm job THẬT (vd
  site tạm bảo trì, đổi giao diện, chặn bot bằng challenge page) thành
  đã đóng, an toàn hơn nhiều nếu chỉ tin 404/410 rồi để phần còn lại
  cho người kiểm tra tay. Có thể bổ sung tín hiệu khác sau khi đã xem
  qua vài chục job ở "needs_manual_check" để biết dấu hiệu thật của từng
  site trông như thế nào.

  Case KHÔNG cần fetch mạng, hoàn toàn an toàn, được gộp CHUNG script
  này qua cờ --check-deadline: hạn của LISTING (một URL tin đăng, bảng
  job_sources_log) đã qua ngày hôm nay -> listing đó CLOSED 'expired_auto'
  (không phụ thuộc URL có sống hay không). Từ C3a nhánh này chạy trên
  listing chứ không trên job: job chỉ CLOSED khi không còn listing sống
  nào (job tự suy ra, xem scrapjd/db/job_derivation.py), nên job có một tin hết hạn
  nhưng còn tin khác chưa hết hạn vẫn OPEN. Nhánh check link nguồn ở trên
  Từ C3b cũng chạy trên listing (xem dưới).

CHẠY:
    python -m scrapjd.maintenance.check_expired_source_jobs                # check tất cả job OPEN có source_url
    python -m scrapjd.maintenance.check_expired_source_jobs --limit 20      # test thử trước khi chạy full
    python -m scrapjd.maintenance.check_expired_source_jobs --check-deadline  # CHỈ check deadline quá hạn, không fetch mạng
    python -m scrapjd.maintenance.check_expired_source_jobs --dry-run       # chỉ in ra job sẽ bị đóng, KHÔNG ghi DB
    python -m scrapjd.maintenance.check_expired_source_jobs --skip-cv-cleanup  # bỏ qua bước dọn CV (xem mục DỌN CV bên dưới)

NÊN CHẠY ĐỊNH KỲ — hiện CHƯA CÓ cron tự động (hạ tầng máy chủ hiện tại
chưa đáp ứng được lịch chạy tự động, xem thảo luận 08/2026), tạm thời
CHẠY THỦ CÔNG qua nút bấm trên web (trang bảo trì dữ liệu, job_type
'check_expired_jobs' — xem scrapjd/api/maintenance_runner.py) hoặc CLI, gợi ý
1 tuần/lần.

DỌN CV (thêm 08/2026 — xem việc_chưa_làm.txt mục "chưa có hệ thống tự
động dọn CV của học viên khi đã ứng tuyển những Job hết hạn"): SAU khi
đóng job (cả 2 nhánh deadline lẫn 404/410 ở trên), script quét TOÀN BỘ
job_applications của MỌI job đang CLOSED (không riêng job vừa đóng
trong lượt chạy này — bao gồm cả backlog job đã đóng từ trước, kể cả
do SS chủ động đóng tay qua frontend) mà vẫn còn cv_url, xoá file PDF
trên Supabase Storage rồi gỡ cv_url trong DB (KHÔNG xoá dòng
job_applications — application vẫn còn giá trị lịch sử cho staff xem
"ai đã ứng tuyển job này", xem db.list_applications_for_job(); chỉ file
PDF không còn giá trị sau khi job đóng mới bị dọn). Vì đây là bước
XOÁ FILE THẬT (không đảo ngược được), mặc định BẬT SẴN (chạy cùng lượt
check-expired luôn, kể cả khi bấm từ web) nhưng vẫn tôn trọng --dry-run
(chỉ đếm, không xoá gì thật) — dùng --skip-cv-cleanup nếu chỉ muốn đóng
job, chưa muốn đụng vào CV trong lượt chạy đó.
"""

import argparse
import logging
import time
from datetime import date
from typing import Optional

from curl_cffi import requests

from scrapjd import db
from scrapjd.api import storage as cv_storage
from scrapjd.config import DEFAULT_HEADERS
from scrapjd.db.pg_types import Conn

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                     datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)

REQUEST_DELAY_SECONDS = 3.0   # giãn cách giữa các lần fetch, tránh bị site nguồn chặn bot
REQUEST_TIMEOUT_SECONDS = 10


class _Throttled404Checker:
    """Chỉ hỏi source_url còn sống hay không (HEAD trước, fallback GET
    nếu server không hỗ trợ HEAD đúng) — không cần đọc/parse nội dung
    trang như scrapjd/maintenance/get_company_fb_linkedin_link.py, nên KHÔNG cần BeautifulSoup
    ở đây, chỉ cần status_code."""

    def __init__(self) -> None:
        self.session: requests.Session = requests.Session(impersonate="chrome124")
        self.session.headers.update(DEFAULT_HEADERS)
        self._last_request_time: Optional[float] = None

    def check(self, url: str) -> Optional[int]:
        """Trả HTTP status_code, hoặc None nếu fetch lỗi hoàn toàn (mất
        mạng/timeout/site chặn ở tầng TLS...) — None KHÔNG được coi là
        tín hiệu để đóng job (xem nguyên tắc ở docstring đầu file)."""
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

    def _throttle(self) -> None:
        if self._last_request_time is None:
            return
        elapsed = time.monotonic() - self._last_request_time
        remaining = REQUEST_DELAY_SECONDS - elapsed
        if remaining > 0:
            time.sleep(remaining)


def cleanup_cvs_of_closed_jobs(conn: Conn, dry_run: bool = False) -> dict:
    """Dọn CV (file PDF trên Supabase Storage + cv_url trong DB) của
    MỌI application thuộc job đang CLOSED — xem docstring đầu file mục
    "DỌN CV". Trả {"cv_cleaned": int, "cv_cleanup_errors": int}.

    cv_storage.delete_cv() TỰ NUỐT exception + chỉ log warning (xem
    scrapjd/api/storage.py — thiết kế sẵn từ chỗ dùng lúc học viên rút đơn),
    KHÔNG raise và KHÔNG trả về biết thành công hay không — nên hàm này
    LUÔN clear_application_cv() sau khi gọi delete_cv(), coi lượt gọi
    storage là "đã cố gắng dọn" bất kể thành công thật hay không. Đánh
    đổi CÓ Ý: nếu Supabase lỗi tạm thời, file có thể còn sót lại trên
    storage (chỉ tốn thêm dung lượng, vô hại) NHƯNG cv_url đã NULL nên
    không lặp lại việc gọi xoá cho application đó ở lượt chạy sau —
    tránh vòng lặp gọi lại storage mãi mãi cho 1 file lỗi vĩnh viễn
    (vd bucket/object_path hỏng). Đúng tinh thần "thà thiếu còn hơn
    sai" nhưng áp theo hướng ngược lại nhánh check nguồn ở run(): ở đây
    hậu quả tối đa của việc "lỡ tay" là tốn thêm ít dung lượng storage,
    KHÔNG phải đóng nhầm job đang tuyển thật — rủi ro thấp hơn hẳn nên
    không cần giữ lại để người kiểm tra tay như nhánh 404/410.
    """
    stats = {"cv_cleaned": 0, "cv_cleanup_errors": 0}
    applications = db.list_closed_job_applications_with_cv(conn)
    if not applications:
        return stats

    logger.info("Tìm thấy %d CV thuộc job đã CLOSED cần dọn", len(applications))
    for app_row in applications:
        application_id = app_row["application_id"]
        cv_url = app_row["cv_url"]
        try:
            if not dry_run:
                cv_storage.delete_cv(cv_url)
                db.clear_application_cv(conn, application_id)
                conn.commit()
            stats["cv_cleaned"] += 1
            logger.info("  -> đã dọn CV của application %s (%s)", application_id, cv_url)
        except Exception as exc:  # noqa: BLE001 - lỗi 1 application không được chặn cả lượt dọn
            conn.rollback()
            stats["cv_cleanup_errors"] += 1
            logger.warning("  -> lỗi khi dọn CV của application %s: %s", application_id, exc)

    return stats


def _group_listings_by_job(listings: list) -> dict:
    """{job_id: [(job_title, source_url, deadline), ...]} giữ thứ tự job của list_checkable_listings."""
    by_job: dict = {}
    for job_id, job_title, source_url, deadline in listings:
        by_job.setdefault(job_id, []).append((job_title, source_url, deadline))
    return by_job


def _expire_by_deadline(conn: Conn, by_job: dict, today: date, dry_run: bool, stats: dict) -> set:
    """Nhánh HẠN (C3a), chạy trên LISTING, không đụng mạng: mỗi listing OPEN/UNKNOWN có hạn đã qua thì đóng
    'expired_auto' (db.close_expired_listings), rồi đồng bộ job theo listing (db.sync_job_from_listings): job
    chỉ CLOSED khi không còn listing sống. Một job còn listing khác chưa hết hạn (hoặc không có hạn) vẫn OPEN.

    Cập nhật stats: expired_by_deadline = số JOB bị đóng vì hạn (cùng ý nghĩa trước C3a),
    listings_expired_by_deadline = số LISTING bị đóng vì hạn. Commit từng job (lỗi một job không kéo theo job
    khác). Trả tập job_id đã (hoặc, khi dry_run, SẼ) bị đóng.
    """
    closed_jobs: set = set()
    for n, (job_id, listings) in enumerate(by_job.items(), 1):
        expired = [(url, dl) for _, url, dl in listings if dl is not None and dl < today]
        if not expired:
            continue
        job_title = listings[0][0]
        whole_job = len(expired) == len(listings)       # mọi listing sống của job đều hết hạn
        for url, dl in expired:
            logger.info("[%d/%d] %s (%s): hạn %s của listing %s đã qua -> listing CLOSED",
                        n, len(by_job), job_title, job_id, dl, url)
        if dry_run:
            stats["listings_expired_by_deadline"] += len(expired)
            if whole_job:
                stats["expired_by_deadline"] += 1
                closed_jobs.add(job_id)
                logger.info("  -> mọi listing sống đều hết hạn -> job sẽ CLOSED")
            continue
        try:
            closed = db.close_expired_listings(conn, job_id, today)
            changes = db.sync_job_from_listings(conn, job_id)
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - lỗi một job không được chặn cả lượt kiểm tra
            conn.rollback()
            logger.warning("  -> lỗi khi đóng listing hết hạn của job %s: %s", job_id, exc)
            continue
        stats["listings_expired_by_deadline"] += closed
        if "job_status" in changes and changes["job_status"][1] == "CLOSED":
            stats["expired_by_deadline"] += 1
            closed_jobs.add(job_id)
            logger.info("  -> mọi listing sống đều hết hạn -> job CLOSED")
    return closed_jobs


def _check_source_urls(conn: Conn, checker: _Throttled404Checker, by_job: dict, today: date, dry_run: bool, stats: dict) -> None:
    """Nhánh MẠNG (C3b), chạy trên LISTING: hỏi từng URL của MỌI listing OPEN/UNKNOWN của job OPEN (không chỉ
    URL hiện hành của job).

      - HTTP 404/410: đóng ĐÚNG listing đó 'expired_auto' (db.close_listing_dead), rồi đồng bộ job
        (db.sync_job_from_listings): job chỉ CLOSED khi hết listing sống. Job còn listing khác vẫn OPEN.
      - HTTP 2xx: ghi last_seen_at (db.mark_listing_seen). KHÔNG đổi listing UNKNOWN thành OPEN (bạn chốt
        09/10: 2xx chưa đủ để kết luận tin còn sống, vd redirect về trang chủ).
      - Mọi trường hợp khác: không kết luận, đếm needs_manual_check (xem docstring đầu file).

    Bỏ qua listing manual:// (job nhập tay, không có URL để hỏi) và listing đã hết hạn theo hạn (nhánh hạn
    đã đóng, hoặc sẽ đóng khi dry-run). Đọc lại danh sách SAU nhánh hạn. `by_job` (đã áp --limit) chỉ dùng để
    giữ cùng tập job khi có --limit.

    Số liệu: expired_by_source_dead = số JOB bị đóng vì nguồn chết; listings_expired_by_source_dead = số
    LISTING; still_alive và needs_manual_check đếm theo LISTING (mỗi URL một lần hỏi). Commit từng listing."""
    rows = [r for r in db.list_checkable_listings(conn) if not by_job or r[0] in by_job]
    live = _group_listings_by_job(
        [r for r in rows if not (r[3] is not None and r[3] < today)])   # chưa hết hạn: còn sống sau nhánh hạn
    todo = [(job_id, title, url) for job_id, listings in live.items()
            for title, url, _deadline in listings if not url.startswith("manual://")]
    logger.info("Tìm thấy %d listing (của %d job OPEN) có URL để kiểm tra nguồn", len(todo), len(live))

    dead_by_job: dict = {}
    closed_jobs: set = set()
    for i, (job_id, job_title, url) in enumerate(todo, 1):
        logger.info("[%d/%d] %s (%s)", i, len(todo), job_title, job_id)
        status_code = checker.check(url)
        if status_code in (404, 410):
            logger.info("  -> nguồn trả HTTP %d -> listing CLOSED: %s", status_code, url)
            if dry_run:
                dead_by_job.setdefault(job_id, set()).add(url)
                stats["listings_expired_by_source_dead"] += 1
                if len(dead_by_job[job_id]) == len(live[job_id]) and job_id not in closed_jobs:
                    closed_jobs.add(job_id)
                    stats["expired_by_source_dead"] += 1
                    logger.info("  -> mọi listing sống đều chết -> job sẽ CLOSED")
                continue
            try:
                closed = db.close_listing_dead(conn, job_id, url)
                changes = db.sync_job_from_listings(conn, job_id)
                conn.commit()
            except Exception as exc:  # noqa: BLE001 - lỗi một listing không được chặn cả lượt kiểm tra
                conn.rollback()
                logger.warning("  -> lỗi khi đóng listing %s của job %s: %s", url, job_id, exc)
                continue
            stats["listings_expired_by_source_dead"] += int(closed)
            if "job_status" in changes and changes["job_status"][1] == "CLOSED":
                stats["expired_by_source_dead"] += 1
                logger.info("  -> mọi listing sống đều chết -> job CLOSED")
        elif status_code is not None and 200 <= status_code < 300:
            stats["still_alive"] += 1
            if not dry_run:
                # Bằng chứng còn sống (C1): chỉ ghi last_seen_at, không đổi trạng thái listing.
                db.mark_listing_seen(conn, url)
                conn.commit()
        else:
            # None (lỗi fetch) hoặc mã khác (3xx lạ, 403, 5xx...) — KHÔNG mơ hồ đủ để tự kết luận.
            stats["needs_manual_check"] += 1
            logger.info("  -> HTTP %s, không đủ rõ để tự kết luận -> cần kiểm tra tay: %s", status_code, url)


def run(limit: Optional[int] = None, check_deadline_only: bool = False,
        dry_run: bool = False, skip_cv_cleanup: bool = False) -> dict:
    stats = {
        "checked": 0, "expired_by_source_dead": 0, "expired_by_deadline": 0,
        # C3a: số listing đóng vì hạn (expired_by_deadline vẫn đếm JOB bị đóng, nên hai số này chỉ khác
        # nhau khi job có nhiều listing sống mà chỉ một phần hết hạn).
        "listings_expired_by_deadline": 0,
        # C3b: số listing đóng vì URL trả 404/410 (expired_by_source_dead vẫn đếm JOB bị đóng).
        "listings_expired_by_source_dead": 0,
        # BUG FIX (migrate Next.js, Phần 5 mục 13 của plan): đổi key
        # "cần_kiểm_tra_tay" (tiếng Việt có dấu) sang "needs_manual_check"
        # (snake_case tiếng Anh) cho nhất quán với MỌI key khác trong toàn
        # bộ API — key cũ không gây lỗi chức năng gì (JSON chấp nhận key
        # Unicode), nhưng dễ vấp khi viết type TypeScript (khó gõ, khó
        # autocomplete, khó tìm trong editor). stats dict này được
        # json.dumps() nguyên vẹn rồi lưu thẳng vào cột JSONB
        # maintenance_runs.stats (scrapjd/db/maintenance_runs.py::mark_done()),
        # trả ra API y hệt cấu trúc — đổi key ở ĐÚNG 1 nơi duy nhất
        # (nguồn) là đủ, không cần transform gì thêm ở tầng DB/router.
        "still_alive": 0, "needs_manual_check": 0,
        "cv_cleaned": 0, "cv_cleanup_errors": 0,
    }

    conn = db.get_connection()
    checker = _Throttled404Checker()
    today = date.today()
    try:
        # --- Nhánh hạn (C3a): chạy trên listing, không tốn request mạng, luôn chạy trước (kể cả khi
        # --check-deadline không bật, vì đây là tín hiệu miễn phí, không có lý do bỏ qua).
        by_job = _group_listings_by_job(db.list_checkable_listings(conn))
        if limit:
            by_job = dict(list(by_job.items())[:limit])
        stats["checked"] = len(by_job)
        logger.info("Tìm thấy %d job đang OPEN (%d listing còn sống) để kiểm tra hạn",
                    len(by_job), sum(len(v) for v in by_job.values()))
        _expire_by_deadline(conn, by_job, today, dry_run, stats)

        if not check_deadline_only:
            _check_source_urls(conn, checker, by_job if limit else {}, today, dry_run, stats)

        # Dọn CV — chạy SAU khi vòng đóng job ở trên đã commit xong (để
        # nhánh dọn CV thấy được cả job VỪA đóng trong lượt này lẫn job
        # đã CLOSED từ trước, xem docstring cleanup_cvs_of_closed_jobs()).
        # Đặt trong cùng try/finally để chắc chắn conn.close() vẫn chạy
        # dù nhánh dọn CV lỗi giữa chừng.
        if not skip_cv_cleanup:
            cv_stats = cleanup_cvs_of_closed_jobs(conn, dry_run=dry_run)
            stats["cv_cleaned"] = cv_stats["cv_cleaned"]
            stats["cv_cleanup_errors"] = cv_stats["cv_cleanup_errors"]
    finally:
        conn.close()

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Re-check job OPEN xem còn tồn tại ở nguồn (TopCV/VietnamWorks) không, "
                     "tự động chuyển CLOSED nếu nguồn xác nhận đã xoá (404/410) hoặc deadline đã qua."
    )
    parser.add_argument("--limit", type=int, default=None,
                         help="Giới hạn số job xử lý (dùng để test thử trước khi chạy full)")
    parser.add_argument("--check-deadline", action="store_true",
                         help="CHỈ check deadline quá hạn, KHÔNG fetch mạng tới source_url "
                              "(nhanh hơn nhiều, chạy được thường xuyên hơn)")
    parser.add_argument("--dry-run", action="store_true",
                         help="Chỉ in ra job SẼ bị đóng (CLOSED) và CV SẼ bị dọn, không ghi/xoá gì "
                              "thật — dùng để xem trước kết quả trước khi chạy thật")
    parser.add_argument("--skip-cv-cleanup", action="store_true",
                         help="Chỉ đóng job hết hạn, KHÔNG dọn CV của job đã CLOSED trong lượt "
                              "chạy này (xem mục DỌN CV ở docstring đầu file)")
    args = parser.parse_args()

    stats = run(limit=args.limit, check_deadline_only=args.check_deadline, dry_run=args.dry_run,
                skip_cv_cleanup=args.skip_cv_cleanup)

    print("\n===== KẾT QUẢ =====" + (" (DRY RUN — chưa ghi/xoá gì thật)" if args.dry_run else ""))
    print(f"Đã kiểm tra                      : {stats['checked']}")
    print(f"Đóng do deadline đã qua            : {stats['expired_by_deadline']} job "
          f"({stats['listings_expired_by_deadline']} listing)")
    print(f"Đóng do nguồn trả 404/410          : {stats['expired_by_source_dead']} job "
          f"({stats['listings_expired_by_source_dead']} listing)")
    print(f"Listing vẫn còn sống (2xx)         : {stats['still_alive']}")
    print(f"⚠️  Listing cần kiểm tra tay       : {stats['needs_manual_check']}")
    if not args.skip_cv_cleanup:
        print(f"CV đã dọn (job CLOSED)            : {stats['cv_cleaned']}")
        if stats["cv_cleanup_errors"]:
            print(f"⚠️  Lỗi khi dọn CV                : {stats['cv_cleanup_errors']}")


if __name__ == "__main__":
    main()
