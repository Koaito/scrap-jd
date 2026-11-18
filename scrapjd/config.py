"""
Cấu hình trung tâm cho crawler.

Muốn crawl thêm ngành / nguồn mới -> chỉ sửa file này, KHÔNG cần đụng vào
logic pipeline hay adapter.
"""

import os
from collections.abc import Mapping
from typing import Any

import psycopg2
from psycopg2.extensions import parse_dsn

# ------------------------------------------------------------------
# JOB_CATEGORIES là NGUỒN SỰ THẬT DUY NHẤT về category crawl: mỗi category khai báo một lần (label và
# matching_industry dùng chung mọi nguồn) cùng sub-dict "sources" liệt kê nguồn nào crawl được category này kèm
# field đặc thù của nguồn (url / query / keyword). Nguồn không có category thì KHÔNG xuất hiện trong "sources",
# nên nhìn một chỗ là thấy category nào phủ đủ 3 nguồn và thiếu ở đâu (thiếu có chủ đích, không phải sót).
#
# TOPCV_CATEGORIES / VIETNAMWORKS_CATEGORIES / CAREERVIET_CATEGORIES bên dưới là 3 view TỰ SINH từ đây
# (_categories_for_source), dạng {key: {label, url/query/keyword, matching_industry}}. Adapter, main.py và
# sources_registry.py chỉ import 3 tên đó. Thêm hoặc sửa category thì sửa ở JOB_CATEGORIES, đừng sửa view.
# ------------------------------------------------------------------
JOB_CATEGORIES: dict[str, dict[str, Any]] = {
    "data-analyst": {
        "label": "Data Analyst",
        "matching_industry": "Data Analysis",
        "sources": {
            "topcv": {"url": "https://www.topcv.vn/tim-viec-lam-data-analyst-cr257cb261cl145"},
            "vietnamworks": {"query": "data analyst"},
            "careerviet": {"keyword": "data-analyst"},
        },
    },
    "data-engineer": {
        "label": "Data Engineer",
        "matching_industry": "Data Engineer",
        "sources": {
            "topcv": {"url": "https://www.topcv.vn/tim-viec-lam-data-engineer-cr257cb261cl285"},
            "vietnamworks": {"query": "data engineer"},
            "careerviet": {"keyword": "data-engineer"},
        },
    },
    "data-scientist": {
        "label": "Data Scientist",
        "matching_industry": "Data Scientist",
        "sources": {
            "topcv": {"url": "https://www.topcv.vn/tim-viec-lam-data-scientist"},
            "vietnamworks": {"query": "data scientist"},
            "careerviet": {"keyword": "data-scientist"},
        },
    },
    "software-engineering": {
        "label": "Software Engineering",
        "matching_industry": "Code",
        "sources": {
            "topcv": {"url": "https://www.topcv.vn/tim-viec-lam-software-engineering-cr257cb258"},
            "vietnamworks": {"query": "software engineer"},
            "careerviet": {"keyword": "software-engineer"},
        },
    },
    "business-analyst": {
        "label": "Business Analysis",
        "matching_industry": "Business Analysis",
        "sources": {
            "topcv": {"url": "https://www.topcv.vn/tim-viec-lam-business-analyst"},
            "vietnamworks": {"query": "business analyst"},
            "careerviet": {"keyword": "business-analyst"},
        },
    },
    "ui-ux-design": {
        "label": "UI/UX Design",
        "matching_industry": "UI/UX Design",
        "sources": {
            "topcv": {"url": "https://www.topcv.vn/tim-viec-lam-ui-ux-design-cr826cb827cl317"},
            "vietnamworks": {"query": "ui ux design"},
            # Cố ý KHÔNG có "careerviet": keyword này không tồn tại trên CareerViet (trang
            # https://careerviet.vn/viec-lam/ui-ux-design-k-vi.html không có kết quả), thêm vào chỉ khiến fetch_jobs()
            # chạy vô ích. Nếu CareerViet có category UI/UX dưới slug khác, thêm lại kèm keyword đã kiểm.
        },
    },
}

# Ngành mặc định khi không truyền --category
DEFAULT_CATEGORY = "data-analyst"


def _categories_for_source(source_key: str) -> dict[str, dict[str, Any]]:
    """View {category_key: {label, matching_industry, <field nguồn>}} của JOB_CATEGORIES cho một nguồn, chỉ gồm
    category mà nguồn đó THỰC SỰ có trong "sources". Dùng để sinh TOPCV_CATEGORIES, VIETNAMWORKS_CATEGORIES,
    CAREERVIET_CATEGORIES bên dưới."""
    result = {}
    for cat_key, cat in JOB_CATEGORIES.items():
        source_fields = cat["sources"].get(source_key)
        if source_fields is None:
            continue
        result[cat_key] = {
            "label": cat["label"],
            **source_fields,
            "matching_industry": cat["matching_industry"],
        }
    return result


# 3 view phái sinh từ JOB_CATEGORIES (xem khối chú thích ở trên).
TOPCV_CATEGORIES = _categories_for_source("topcv")
VIETNAMWORKS_CATEGORIES = _categories_for_source("vietnamworks")
CAREERVIET_CATEGORIES = _categories_for_source("careerviet")

# API search job của VietnamWorks
VNW_SEARCH_URL = "https://ms.vietnamworks.com/job-search/v1.0/search"
VNW_HITS_PER_PAGE = 50

VNW_HEADERS = {
    "accept": "*/*",
    "accept-language": "vi",
    "content-type": "application/json",
    "dnt": "1",
    "origin": "https://www.vietnamworks.com",
    "referer": "https://www.vietnamworks.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "x-source": "Page-Container",
}

VNW_RETRIEVE_FIELDS = [
    "address", "benefits", "jobTitle", "salaryMax", "isSalaryVisible",
    "jobLevelVI", "isShowLogo", "salaryMin", "companyLogo", "userId",
    "jobLevel", "jobLevelId", "jobId", "jobUrl", "companyId", "approvedOn",
    "isAnonymous", "alias", "expiredOn", "industries", "industriesV3",
    "workingLocations", "services", "companyName", "salary", "onlineOn",
    "onlineOnText", "simpleServices", "visibilityDisplay",
    "isShowLogoInSearch", "priorityOrder", "skills",
    "profilePublishedSiteMask", "jobDescription", "jobRequirement",
    "prettySalary", "requiredCoverLetter", "languageSelectedVI",
    "languageSelected", "languageSelectedId", "typeWorkingId", "createdOn",
    "isAdrLiteJob", "applicantSignal",
]

# Số trang tối đa crawl / lần chạy
DEFAULT_MAX_PAGES = 3

# Độ trễ giữa các request (giây)
REQUEST_DELAY_SECONDS = 5.0

# TopCV trả 403 liên tục khi crawl chạy từ server Render (IP datacenter) dù cùng code chạy từ máy cá nhân (IP dân
# dụng) vẫn qua: nhiều khả năng bị chặn theo uy tín IP, không phải thiếu header hay TLS fingerprint (adapter đã xử
# lý, xem scrapjd/adapters/topcv.py). Cách dứt điểm là proxy IP dân dụng (chưa có). Tạm thời tăng delay và thêm
# jitter ngẫu nhiên RIÊNG cho TopCV; VietnamWorks và CareerViet chưa bị chặn nên không tăng chung, kẻo crawl chậm
# vô ích.
TOPCV_REQUEST_DELAY_SECONDS = float(os.getenv("TOPCV_REQUEST_DELAY_SECONDS", "12.0"))
# Biên độ +/- ngẫu nhiên cộng vào TOPCV_REQUEST_DELAY_SECONDS mỗi request
# (giây) — né việc khoảng cách giữa các request đều tăm tắp (dễ nhận diện
# bot hơn khoảng cách có dao động tự nhiên như người dùng thật).
TOPCV_REQUEST_JITTER_SECONDS = float(os.getenv("TOPCV_REQUEST_JITTER_SECONDS", "4.0"))

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# ------------------------------------------------------------------
# Database (đọc từ biến môi trường)
# ------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def build_db_config(env: Mapping[str, str]) -> dict[str, Any]:
    """Dựng tham số kết nối Postgres từ biến môi trường.

    `DATABASE_URL` (dạng `postgresql://user:mật_khẩu@host:port/dbname?sslmode=...`) được ưu tiên:
    đây là định dạng mà `pg_dump`, trang Connect của Supabase và hầu hết nền tảng cấp sẵn, nên khỏi phải
    dịch tay sang sáu biến. Không đặt (hoặc để trống) thì dùng nhóm `PG*` như trước. Không trộn hai
    nguồn: đã có `DATABASE_URL` thì `PGHOST`, `PGPORT`... bị bỏ qua hoàn toàn, để một biến cũ còn sót
    trong môi trường không âm thầm đổi nơi kết nối.

    Ký tự đặc biệt trong mật khẩu phải được mã hoá phần trăm (`@` thành `%40`); `parse_dsn` tự giải mã.
    URL không ghi `sslmode` thì mặc định `require` (cùng mặc định với nhóm `PG*`), nên bỏ sót tham số
    đó không làm kết nối rơi về không mã hoá. Muốn tắt SSL (DB cục bộ) thì ghi `?sslmode=disable`.
    """
    url = env.get("DATABASE_URL", "").strip()
    if url:
        try:
            cfg: dict[str, Any] = dict(parse_dsn(url))
        except psycopg2.ProgrammingError:
            # Không đưa URL vào thông báo lỗi: nó chứa mật khẩu.
            raise ValueError(
                "DATABASE_URL không hợp lệ (đúng dạng: postgresql://user:mật_khẩu@host:port/tên_db)"
            ) from None
        cfg.setdefault("sslmode", "require")
        return cfg
    return {
        "host": env.get("PGHOST", "localhost"),
        "port": env.get("PGPORT", "5432"),
        "dbname": env.get("PGDATABASE", "Student Success — Job Postings & Company Contacts"),
        "user": env.get("PGUSER", "postgres"),
        "password": env.get("PGPASSWORD", ""),
        "sslmode": env.get("PGSSLMODE", "require"),
    }


DB_CONFIG: dict[str, Any] = build_db_config(os.environ)

# ------------------------------------------------------------------
# Connection pool cho API layer (db.init_pool()): CHỈ app.py dùng lúc startup, CLI (main.py) vẫn mở/đóng
# connection trực tiếp bằng db.get_connection().
#
# DB_POOL_MAX = 20 giả định Transaction Pooler của Supabase (PGPORT=6543, xem .env.example): pooler dồn nhiều client
# xuống ít connection thật, nên mở nhiều connection ở tầng app là an toàn. Số nhỏ hơn (từng thử 8) cạn pool nội bộ
# (psycopg2.pool.PoolError "connection pool exhausted") khi nhiều tab và card bảo trì poll status/log mỗi 1-2 giây.
#
# CẢNH BÁO: đổi PGPORT về 5432 (Session Pooler hoặc Direct) thì PHẢI hạ DB_POOL_MAX xuống dưới giới hạn connection
# thật của Supabase/Render (Session Pooler giới hạn cứng 15), nếu không sẽ gặp lỗi EMAXCONNSESSION.
# ------------------------------------------------------------------
DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", "2"))
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "20"))

# Số giây TỐI ĐA get_pooled_connection() (scrapjd/db/connection.py) retry chờ pool có slot trống trước khi raise
# psycopg2.pool.PoolError. Đủ để hấp thụ burst polling thoáng qua (mỗi query thường giữ connection vài chục ms) mà
# request không treo lâu; nghẽn kéo dài thật vẫn raise lỗi rõ ràng sau đúng khoảng này, không che bằng cách chờ vô hạn.
DB_POOL_WAIT_TIMEOUT = float(os.getenv("DB_POOL_WAIT_TIMEOUT", "2.0"))

# ------------------------------------------------------------------
# Crawl watchdog (scrapjd/api/services/crawl_watchdog.py): phát hiện lượt crawl bị TREO (process bị kill, network
# timeout vô hạn...) rồi đánh dấu 'error' để giải phóng UNIQUE INDEX idx_crawl_runs_one_active_per_source, tránh một
# nguồn bị khoá crawl mãi. Hai ngưỡng (db.reconcile_stale_runs):
#
# CRAWL_STALE_NO_PROGRESS_MINUTES: lượt 'running' bị coi là treo nếu KHÔNG có tiến độ mới (progress.last_update, ghi
# sau MỖI job xong) trong ngần này phút. Khoảng lặng hợp lệ dài nhất là một job đơn lẻ: vài lần fetch (chi tiết, hồ
# sơ công ty), mỗi lần retry 3 lần backoff delay*2/4/8, TopCV (delay ~12s) khoảng 3 phút mỗi lần fetch, tức dưới 10
# phút cho cả job; 30 phút là dư. Không phụ thuộc max_jobs hay số trang nên crawl dài vẫn chạy được miễn còn tiến độ
# (đo tổng thời gian từ lúc tạo sẽ huỷ nhầm lượt hợp lệ như TopCV max_jobs=1000).
CRAWL_STALE_NO_PROGRESS_MINUTES = max(1, int(os.getenv("CRAWL_STALE_NO_PROGRESS_MINUTES", "30")))

# CRAWL_STALE_TIMEOUT_MINUTES: CHỈ áp cho lượt 'queued' (chưa chạy, không có heartbeat), tính từ lúc tạo dòng, cho
# trường hợp background task mất trước khi kịp chạy. Phải LỚN hơn thời gian một lượt có thể xếp hàng chờ slot trong
# GLOBAL_JOB_SEMAPHORE (scrapjd/api/concurrency.py), vì job đang giữ slot có thể chạy rất lâu.
CRAWL_STALE_TIMEOUT_MINUTES = int(os.getenv("CRAWL_STALE_TIMEOUT_MINUTES", "120"))

# Tần suất watchdog quét bảng crawl_runs: đây là lớp dự phòng, không cần nhanh; 10 phút giải phóng nguồn bị treo
# trong thời gian hợp lý mà không tốn tài nguyên quét liên tục.
CRAWL_WATCHDOG_INTERVAL_MINUTES = int(os.getenv("CRAWL_WATCHDOG_INTERVAL_MINUTES", "10"))

# CRAWL_BATCH_ORPHAN_GRACE_MINUTES: watchdog chỉ đóng batch crawl 'running' mà không còn run nào chạy khi hoạt động cuối
# (run con kết thúc gần nhất) đã cách hơn ngần này phút. Giữa lúc một run xong và lúc run kế tiếp được tạo batch cũng
# không có run nào chạy trong vài giây, nên phải chờ để không đóng nhầm batch đang tiến hành bình thường.
CRAWL_BATCH_ORPHAN_GRACE_MINUTES = max(1, int(os.getenv("CRAWL_BATCH_ORPHAN_GRACE_MINUTES", "10")))

# ------------------------------------------------------------------
# Ngắt mạch khi bị chặn GIỮA CHỪNG (scrapjd/adapters/base.py::BaseAdapter._note_fetch_failure). CrawlBlockedError chỉ
# raise ở TRANG ĐẦU; IP bị ban ở job thứ 10 thì mọi job còn lại vẫn chạy đủ 3 lần retry (TopCV: 12s x (2+4+8) ~ 168s
# mỗi lần fetch) rồi ghi skipped_fetch_failed và đi tiếp, phí hàng giờ và còn đập tiếp vào IP đã bị đánh dấu.
#
# CRAWL_BLOCK_CONSECUTIVE_FAILURES: số lần fetch thất bại LIÊN TIẾP (mỗi lần đã hết retry, chưa có request nào thành
# công xen giữa) thì dừng cả lượt. 3 lần TopCV ~ 8 phút không tiến độ, vẫn thấp hơn ngưỡng watchdog
# CRAWL_STALE_NO_PROGRESS_MINUTES (30). Đặt 0 để tắt ngắt mạch. 404/410 (tin đã gỡ) KHÔNG tính: đó là trang hết hạn
# thật, không phải bị chặn.
CRAWL_BLOCK_CONSECUTIVE_FAILURES = max(0, int(os.getenv("CRAWL_BLOCK_CONSECUTIVE_FAILURES", "3")))

# Chỉ để CẢNH BÁO: khi bắt đầu một lượt crawl, nếu cùng nguồn vừa có lượt bị chặn trong ngần này phút thì ghi WARNING
# vào log live. KHÔNG chặn bấm chạy (admin có thể vừa đổi proxy/IP và muốn thử lại ngay).
CRAWL_BLOCK_COOLDOWN_MINUTES = max(0, int(os.getenv("CRAWL_BLOCK_COOLDOWN_MINUTES", "60")))

# ------------------------------------------------------------------
# Đánh dấu lượt crawl "degraded" (scrapjd/field_stats.py::degraded_reasons): lượt vẫn status='done' nhưng dữ liệu
# nhiều khả năng sai vì selector hỏng. Chỉ xét các field BẮT BUỘC có nội dung (CRITICAL_FIELDS trong field_stats.py),
# nên ngưỡng cao mới an toàn.
DEGRADED_EMPTY_RATE = min(1.0, max(0.0, float(os.getenv("DEGRADED_EMPTY_RATE", "0.9"))))

# ------------------------------------------------------------------
# Job đã có trong DB nhưng còn thiếu work_type/deadline/parsed_content: sau khi đã fetch chi tiết, chỉ fetch lại sau
# ngần này NGÀY (scrapjd/db/jobs.py::job_needs_detail_enrichment). Nhiều tin trên nguồn không ghi hạn nộp; nếu cứ thiếu
# là fetch lại thì mọi lượt crawl đều tốn request cho chính những job đó mãi. Vẫn tự chữa được: sửa xong selector hỏng
# thì tối đa chừng này ngày sau job được vá. Đặt 0 để fetch lại ở mọi lượt crawl.
DETAIL_RECHECK_DAYS = max(0, int(os.getenv("DETAIL_RECHECK_DAYS", "7")))

# ------------------------------------------------------------------
# Snapshot HTML/JSON gốc để debug khi parser hỏng (scrapjd/snapshots.py). Lưu trong Postgres (gzip), giới hạn cứng để
# không ăn hết dung lượng gói free.
SNAPSHOT_MAX_PER_RUN = max(0, int(os.getenv("SNAPSHOT_MAX_PER_RUN", "6")))
# Cắt body dài hơn mức này (ký tự) trước khi nén — trang listing/chi tiết
# thật thường < 500k ký tự.
SNAPSHOT_MAX_CHARS = max(1000, int(os.getenv("SNAPSHOT_MAX_CHARS", "1500000")))
# Snapshot cũ hơn ngần này ngày bị xoá mỗi lần lưu snapshot mới.
SNAPSHOT_RETENTION_DAYS = max(1, int(os.getenv("SNAPSHOT_RETENTION_DAYS", "14")))

# Watchdog cho 5 job bảo trì dữ liệu (backfill / enrich / check_expired; scrapjd/api/services/maintenance_watchdog.py),
# đối xứng CRAWL_STALE_TIMEOUT_MINUTES và CRAWL_WATCHDOG_INTERVAL_MINUTES ở trên. Mặc định 180 phút (dài hơn crawl) vì
# enrich_company_web_info và get_company_fb_linkedin_link gọi Tavily + Gemini cho TỪNG công ty, chậm hơn hẳn crawl từng
# trang; chỉnh qua env nếu limit tối đa (5000, MaintenanceRunRequest.limit) khiến một lượt vượt số này.
MAINTENANCE_STALE_TIMEOUT_MINUTES = int(os.getenv("MAINTENANCE_STALE_TIMEOUT_MINUTES", "180"))
MAINTENANCE_WATCHDOG_INTERVAL_MINUTES = int(os.getenv("MAINTENANCE_WATCHDOG_INTERVAL_MINUTES", "10"))

# ------------------------------------------------------------------
# Enrich API Config
# ------------------------------------------------------------------
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_FREE_TIER_RPM = 15

ENRICH_REQUEST_DELAY_SECONDS = 60 / GEMINI_FREE_TIER_RPM + 1.5  # ~5.5s

# ------------------------------------------------------------------
# Supabase Storage — Lưu trữ file CV PDF (REST API)
# ------------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
SUPABASE_CV_BUCKET = os.getenv("SUPABASE_CV_BUCKET", "cv-files")

# ------------------------------------------------------------------
# Giới hạn dung lượng request body, chặn SỚM ở tầng middleware (scrapjd/api/app.py::reject_oversized_request).
# ------------------------------------------------------------------
# Vì sao có: nếu endpoint tự đọc HẾT body vào RAM (`cv_file.file.read()`, raw_bytes) rồi mới so kích thước, một
# request vài trăm MB vẫn bị đọc trọn trước khi bị từ chối, tốn RAM/CPU vô ích và có thể làm sập process trên tier
# free (RAM thấp). Middleware từ chối ngay khi biết kích thước, trước khi đọc.
#
# 15MB (không phải 5MB của CV) vì middleware chặn CHUNG cấp app: phải đủ rộng cho request "hợp lệ lớn nhất" (import
# CSV/XLSX tối đa 5000 dòng, chưa có giới hạn byte riêng, có thể vượt vài MB) cộng overhead multipart, mà vẫn đủ hẹp
# để chặn request bất thường. Đây là lớp chặn sớm, KHÔNG thay thế check nghiệp vụ từng endpoint (5MB cho CV trong
# routers/me.py, 5000 dòng cho import trong services/file_parser.py): giữ cả hai để báo lỗi đúng ngữ cảnh ("Dung lượng
# file CV tối đa là 5MB" rõ hơn lỗi 413 chung chung).
MAX_REQUEST_BODY_MB = int(os.getenv("MAX_REQUEST_BODY_MB", "15"))
MAX_REQUEST_BODY_BYTES = MAX_REQUEST_BODY_MB * 1024 * 1024
