"""
Cấu hình trung tâm cho crawler.

Muốn crawl thêm ngành / nguồn mới -> chỉ sửa file này, KHÔNG cần đụng vào
logic pipeline hay adapter.
"""

import os

# ------------------------------------------------------------------
# 08/2026 — ĐẢO CẤU TRÚC category-first (trước đây là 3 dict riêng
# TOPCV_CATEGORIES / VIETNAMWORKS_CATEGORIES / CAREERVIET_CATEGORIES,
# mỗi dict tự lặp lại "label" + "matching_industry" cho từng category).
#
# Lý do đảo: 3 dict cũ có CÙNG bộ key (data-analyst, data-engineer...)
# nhưng là 3 bản khai báo độc lập -> dễ lệch dữ liệu khi sửa 1 nơi quên
# nơi kia. Hậu quả thực tế đã xảy ra: TopCV/VietnamWorks có category
# "ui-ux-design", còn CareerViet thì không (CareerViet không có kết quả
# thật cho keyword này — xem xác nhận bên dưới) — với 3 dict riêng thì
# việc "CareerViet thiếu 1 category" trông giống thiếu sót/bug hơn là
# 1 lựa chọn có chủ đích, vì không có nơi nào so sánh chéo được cả 3
# nguồn cho cùng 1 category.
#
# JOB_CATEGORIES bên dưới là NGUỒN SỰ THẬT DUY NHẤT: mỗi category khai
# báo 1 lần (label + matching_industry dùng chung cho mọi nguồn), và 1
# sub-dict "sources" liệt kê nguồn nào crawl được category này kèm field
# đặc thù của nguồn đó (url/query/keyword). Nguồn nào KHÔNG có category
# này thì đơn giản là KHÔNG xuất hiện trong "sources" — nhìn 1 chỗ là
# thấy ngay category nào phủ đủ 3 nguồn, category nào thiếu ở đâu.
#
# TOPCV_CATEGORIES / VIETNAMWORKS_CATEGORIES / CAREERVIET_CATEGORIES bên
# dưới JOB_CATEGORIES là 3 "view" TỰ ĐỘNG SINH RA từ JOB_CATEGORIES (xem
# hàm _categories_for_source) — giữ NGUYÊN hình dạng {key: {label, url/
# query/keyword, matching_industry}} như trước, để adapters/topcv.py,
# adapters/vietnamworks.py, adapters/careerviet.py, main.py và
# sources_registry.py KHÔNG cần sửa gì (chúng chỉ import 3 tên biến này
# như cũ). Nói cách khác: sửa TẬN GỐC ở JOB_CATEGORIES, phần còn lại của
# codebase tự động thấy thay đổi.
# ------------------------------------------------------------------
JOB_CATEGORIES = {
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
            # CareerViet: KHÔNG có "careerviet" ở đây (có chủ đích, không
            # phải thiếu sót) — đã tự kiểm tra thật bằng
            # https://careerviet.vn/viec-lam/ui-ux-design-k-vi.html
            # (08/2026), keyword không tồn tại trên CareerViet (trang
            # không có kết quả, không phải ra job linh tinh) -> thêm vào
            # sẽ khiến fetch_jobs() chạy vô ích cho category này. Nếu sau
            # này CareerViet có category UI/UX dưới slug khác, thêm lại
            # tại đây kèm keyword đã xác nhận đúng.
        },
    },
}

# Ngành mặc định khi không truyền --category
DEFAULT_CATEGORY = "data-analyst"


def _categories_for_source(source_key):
    """Sinh view {category_key: {label, matching_industry, <field nguồn>}}
    từ JOB_CATEGORIES cho 1 nguồn cụ thể — chỉ gồm category mà nguồn đó
    THỰC SỰ có trong "sources" (xem docstring khối JOB_CATEGORIES ở
    trên). Dùng để tạo TOPCV_CATEGORIES/VIETNAMWORKS_CATEGORIES/
    CAREERVIET_CATEGORIES bên dưới mà không cần khai báo lại thủ công.
    """
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


# 3 view phái sinh — giữ NGUYÊN tên biến + hình dạng cũ, mọi nơi khác
# trong codebase (adapters/*, main.py, sources_registry.py) import các
# tên này y hệt trước khi đảo cấu trúc, không cần sửa gì thêm.
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

# 08/2026: TopCV bắt đầu trả 403 liên tục khi crawl chạy TỪ SERVER RENDER
# (IP datacenter), trong khi CÙNG code chạy từ máy cá nhân (IP dân dụng)
# vẫn qua bình thường -> khả năng cao là chặn theo UY TÍN IP (IP
# reputation), KHÔNG PHẢI thiếu header/TLS fingerprint (2 cái đó adapter
# đã xử lý đúng, xem adapters/topcv.py). Hướng dứt điểm là dùng proxy IP
# dân dụng (chưa có kinh phí, xem lịch sử trao đổi) — TẠM THỜI trong lúc
# chưa có proxy, tăng delay + thêm jitter ngẫu nhiên riêng cho TopCV để
# giảm khả năng bị đánh dấu "hành vi bot" lại, KHÔNG áp dụng cho
# VietnamWorks/CareerViet (2 nguồn đó chưa có dấu hiệu bị chặn tương tự,
# tăng delay chung sẽ làm chậm crawl không cần thiết).
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

DB_CONFIG = {
    "host": os.getenv("PGHOST", "localhost"),
    "port": os.getenv("PGPORT", "5432"),
    "dbname": os.getenv("PGDATABASE", "Student Success — Job Postings & Company Contacts"),
    "user": os.getenv("PGUSER", "postgres"),
    "password": os.getenv("PGPASSWORD", ""),
    "sslmode": os.getenv("PGSSLMODE", "require"),
}

# ------------------------------------------------------------------
# Connection pool cho API layer (db.init_pool(), xem db.py) — CHỈ dùng
# bởi api/app.py lúc startup, KHÔNG ảnh hưởng CLI (main.py vẫn dùng
# db.get_connection() mở/đóng trực tiếp như cũ).
#
# 08/2026 (2 lần chỉnh liên tiếp, xem lịch sử trao đổi):
#   (1) Hạ 20 -> 8 để fix lỗi "EMAXCONNSESSION ... pool_size: 15" khi
#       còn dùng Session Pooler (port 5432, giới hạn CỨNG 15 connection
#       thật phía Supabase).
#   (2) SAU KHI đã chuyển hẳn sang Transaction Pooler (PGPORT=6543, xem
#       .env.example) -> NÂNG LẠI 8 -> 20: 8 hoá ra QUÁ THẤP cho tải
#       polling thật (nhiều tab, nhiều job_type card tự poll status/
#       logs mỗi 1-2 giây cùng lúc) -> tự cạn NGAY TRONG pool nội bộ
#       của app (psycopg2.pool.PoolError: "connection pool exhausted"),
#       KHÔNG liên quan gì tới giới hạn phía Supabase nữa. Transaction
#       Pooler multiplex nhiều client xuống ít connection thật phía sau
#       (không phải 1-1 như session mode) nên app HOÀN TOÀN AN TOÀN để
#       mở nhiều connection hơn ở tầng này — 20 vẫn nằm trong khả năng
#       xử lý bình thường của pooler.
#
# maxconn NÊN thấp hơn giới hạn connection Postgres phía Render/Supabase
# cho phép — nếu LỠ đổi PGPORT về lại 5432 (Session Pooler/Direct) mà
# QUÊN hạ số này xuống, sẽ tái phát đúng lỗi EMAXCONNSESSION ở mục (1)
# phía trên.
# ------------------------------------------------------------------
DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", "2"))
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "20"))

# 08/2026 (xem lịch sử trao đổi "connection pool exhausted" + bounded-
# wait ở db/connection.py:get_pooled_connection()) — số giây TỐI ĐA sẽ
# retry chờ pool có slot trống trước khi raise psycopg2.pool.PoolError
# thật, thay vì raise NGAY khi gặp burst polling thoáng qua (nhiều tab/
# job_type card cùng xin connection trong tích tắc trong khi mỗi query
# thường chỉ giữ connection vài chục ms). 2 giây là đủ để hấp thụ burst
# bình thường mà KHÔNG làm request treo lâu tới mức người dùng cảm nhận
# được — nghẽn kéo dài THẬT SỰ (không phải burst thoáng qua) vẫn sẽ
# raise lỗi rõ ràng sau đúng khoảng thời gian này, không che giấu vấn
# đề bằng cách chờ vô thời hạn.
DB_POOL_WAIT_TIMEOUT = float(os.getenv("DB_POOL_WAIT_TIMEOUT", "2.0"))

# ------------------------------------------------------------------
# Crawl watchdog (08/2026, xem api/services/crawl_watchdog.py +
# sql/migration_add_crawl_runs.sql) — phát hiện lượt crawl bị TREO
# (process bị kill giữa chừng, network timeout vô hạn...) rồi tự đánh
# dấu 'error' để giải phóng UNIQUE INDEX idx_crawl_runs_one_active_per_source,
# tránh 1 nguồn bị "khoá" crawl mãi mãi.
#
# CRAWL_STALE_TIMEOUT_MINUTES: ước lượng THỜI GIAN TỐI ĐA 1 lượt crawl
# hợp lệ có thể chạy, cộng buffer an toàn — worst case max_jobs=1000
# (giới hạn ở CrawlRequest.max_jobs, xem api/schemas/crawl.py),
# REQUEST_DELAY_SECONDS=5s/job -> ~1000*5s ≈ 83 phút riêng phần
# fetch detail, CHƯA kể trang danh sách + enrich company. 120 phút mặc
# định đã dư buffer so với ước lượng đó — chỉnh qua env nếu sau này
# max_jobs tối đa đổi khác.
CRAWL_STALE_TIMEOUT_MINUTES = int(os.getenv("CRAWL_STALE_TIMEOUT_MINUTES", "120"))

# Tần suất watchdog quét bảng crawl_runs — không cần nhanh (đây là lớp
# dự phòng, không phải cơ chế chính), 10 phút đủ để giải phóng nguồn bị
# treo trong thời gian hợp lý mà không tốn tài nguyên quét liên tục.
CRAWL_WATCHDOG_INTERVAL_MINUTES = int(os.getenv("CRAWL_WATCHDOG_INTERVAL_MINUTES", "10"))

# 08/2026 (xem sql/migration_add_maintenance_runs.sql,
# api/services/maintenance_watchdog.py) — đối xứng CRAWL_STALE_TIMEOUT_MINUTES/
# CRAWL_WATCHDOG_INTERVAL_MINUTES ở trên nhưng cho 5 job bảo trì dữ liệu
# (backfill/enrich/check_expired). 180 phút mặc định (dài hơn crawl) vì
# enrich_company_web_info.py/get_company_fb_linkedin_link.py gọi
# Tavily+Gemini cho TỪNG company — chậm hơn hẳn crawl page-by-page, chỉnh
# qua env nếu limit tối đa cho phép (5000, xem MaintenanceRunRequest.limit)
# khiến 1 lượt chạy vượt quá con số này.
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
# Giới hạn dung lượng request body — chặn SỚM ở tầng middleware
# (08/2026, xem api/app.py::reject_oversized_request)
# ------------------------------------------------------------------
# BỐI CẢNH: trước đây 2 endpoint có nhận file (POST /me/applications —
# CV PDF, POST /import — CSV/XLSX) đều tự đọc HẾT body vào RAM
# (`cv_file.file.read()` / raw_bytes) RỒI MỚI so sánh kích thước
# (api/routers/me.py: > 5MB, api/services/file_parser.py: > 5000
# dòng). Nghĩa là 1 request vài trăm MB vẫn bị đọc trọn vào bộ nhớ
# trước khi bị từ chối — tốn RAM/CPU vô ích, và với tier free (RAM
# thấp) có thể khiến cả process sập (đúng triệu chứng "gửi file lỗi
# luôn web" thay vì chỉ đơn giản bị từ chối, xem việc_chưa_làm.txt).
#
# 15MB (không phải trùng 5MB của CV) — vì middleware này chặn CHUNG ở
# cấp app, phải đủ rộng cho request "hợp lệ lớn nhất" hiện có
# (import CSV/XLSX tối đa 5000 dòng chưa có giới hạn byte riêng, thực
# tế có thể vượt vài MB tuỳ số cột) cộng thêm overhead multipart, mà
# vẫn đủ hẹp để chặn được các request thật sự bất thường. Đây là lớp
# CHẶN SỚM (fail fast trước khi tốn RAM đọc file), KHÔNG thay thế cho
# check nghiệp vụ cụ thể từng endpoint (5MB cho CV, 5000 dòng cho
# import) — 2 lớp check đó vẫn giữ nguyên để báo lỗi đúng ngữ cảnh hơn
# ("Dung lượng file CV tối đa là 5MB" rõ ràng hơn cho học viên so với
# lỗi 413 chung chung).
MAX_REQUEST_BODY_MB = int(os.getenv("MAX_REQUEST_BODY_MB", "15"))
MAX_REQUEST_BODY_BYTES = MAX_REQUEST_BODY_MB * 1024 * 1024
