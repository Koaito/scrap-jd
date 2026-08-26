"""
Cấu hình trung tâm cho crawler.

Muốn crawl thêm ngành / nguồn mới -> chỉ sửa file này, KHÔNG cần đụng vào
logic pipeline hay adapter.
"""

import os

# ------------------------------------------------------------------
# Danh sách "category" TopCV có thể crawl.
# Đã tinh chỉnh: Loại bỏ các category trùng lặp/bao hàm (software-engineer, fullstack-developer)
# ------------------------------------------------------------------
TOPCV_CATEGORIES = {
    "data-analyst": {
        "label": "Data Analyst",
        "url": "https://www.topcv.vn/tim-viec-lam-data-analyst-cr257cb261cl145",
        "matching_industry": "Data Analysis",
    },
    "data-engineer": {
        "label": "Data Engineer",
        "url": "https://www.topcv.vn/tim-viec-lam-data-engineer-cr257cb261cl285",
        "matching_industry": "Data Engineer",
    },
    "data-scientist": {
        "label": "Data Scientist",
        "url": "https://www.topcv.vn/tim-viec-lam-data-scientist",
        "matching_industry": "Data Scientist",
    },
    "software-engineering": {
        "label": "Software Engineering",
        "url": "https://www.topcv.vn/tim-viec-lam-software-engineering-cr257cb258",
        "matching_industry": "Code",
    },
    "business-analyst": {
        "label": "Business Analysis",
        "url": "https://www.topcv.vn/tim-viec-lam-business-analyst",
        "matching_industry": "Business Analysis",
    },
    "ui-ux-design": {
        "label": "UI/UX Design",
        "url": "https://www.topcv.vn/tim-viec-lam-ui-ux-design-cr826cb827cl317",
        "matching_industry": "UI/UX Design",
    },
}

# Ngành mặc định khi không truyền --category
DEFAULT_CATEGORY = "data-analyst"

# ------------------------------------------------------------------
# VietnamWorks — danh sách category khớp key 1:1 với TOPCV_CATEGORIES
# ------------------------------------------------------------------
VIETNAMWORKS_CATEGORIES = {
    "data-analyst": {
        "label": "Data Analyst",
        "query": "data analyst",
        "matching_industry": "Data Analysis",
    },
    "data-engineer": {
        "label": "Data Engineer",
        "query": "data engineer",
        "matching_industry": "Data Engineer",
    },
    "data-scientist": {
        "label": "Data Scientist",
        "query": "data scientist",
        "matching_industry": "Data Scientist",
    },
    "software-engineering": {
        "label": "Software Engineering",
        "query": "software engineer",
        "matching_industry": "Code",
    },
    "business-analyst": {
        "label": "Business Analysis",
        "query": "business analyst",
        "matching_industry": "Business Analysis",
    },
    "ui-ux-design": {
        "label": "UI/UX Design",
        "query": "ui ux design",
        "matching_industry": "UI/UX Design",
    },
}

# ------------------------------------------------------------------
# CareerViet.vn — danh sách category khớp key 1:1 với TOPCV_CATEGORIES /
# VIETNAMWORKS_CATEGORIES. "keyword" dùng để build URL search dạng
# https://careerviet.vn/viec-lam/<keyword>-k-vi.html.
# ĐÃ xác nhận hoạt động thật, ra đúng job theo ngành — TOÀN BỘ 5/5
# category còn lại: "data-engineer", "business-analyst",
# "data-analyst", "data-scientist", "software-engineer" (+ "vinfast"
# test công ty, không phải category thật).
# ĐÃ BỎ "ui-ux-design": tự kiểm tra thật bằng
# https://careerviet.vn/viec-lam/ui-ux-design-k-vi.html -> keyword
# không tồn tại trên CareerViet, xem comment ngay phía dưới.

# ------------------------------------------------------------------
CAREERVIET_CATEGORIES = {
    "data-analyst": {
        "label": "Data Analyst",
        "keyword": "data-analyst",
        "matching_industry": "Data Analysis",
    },
    "data-engineer": {
        "label": "Data Engineer",
        "keyword": "data-engineer",
        "matching_industry": "Data Engineer",
    },
    "data-scientist": {
        "label": "Data Scientist",
        "keyword": "data-scientist",
        "matching_industry": "Data Scientist",
    },
    "software-engineering": {
        "label": "Software Engineering",
        "keyword": "software-engineer",
        "matching_industry": "Code",
    },
    "business-analyst": {
        "label": "Business Analysis",
        "keyword": "business-analyst",
        "matching_industry": "Business Analysis",
    },
    # "ui-ux-design" ĐÃ BỎ (08/2026) — đã tự kiểm tra thật bằng
    # https://careerviet.vn/viec-lam/ui-ux-design-k-vi.html, keyword
    # không tồn tại trên CareerViet (không phải chỉ ra job linh tinh,
    # mà trang không có kết quả) -> để nguyên trong dict sẽ khiến
    # fetch_jobs() chạy vô ích cho category này. Nếu sau này CareerViet
    # có thêm category UI/UX (dưới slug khác), thêm lại tại đây kèm
    # keyword đã xác nhận đúng.
}

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
# maxconn NÊN thấp hơn giới hạn connection Postgres phía Render/Supabase
# cho phép (managed Postgres tier free thường giới hạn thấp) — mặc định
# 20 là ước lượng an toàn cho quy mô team nhỏ, chỉnh qua env nếu cần.
# ------------------------------------------------------------------
DB_POOL_MIN = int(os.getenv("DB_POOL_MIN", "2"))
DB_POOL_MAX = int(os.getenv("DB_POOL_MAX", "20"))

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
