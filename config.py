"""
Cấu hình trung tâm cho crawler.

Muốn crawl thêm ngành / nguồn mới -> chỉ sửa file này, KHÔNG cần đụng vào
logic pipeline hay adapter.
"""

# ------------------------------------------------------------------
# Danh sách "category" TopCV có thể crawl.
# Mỗi category = 1 URL trang danh mục nghề (đã xác nhận là SSR, lấy được
# bằng requests + BeautifulSoup, không cần Playwright).
#
# Cách tìm thêm category mới:
#   1. Vào https://www.topcv.vn/viec-lam
#   2. Chọn "Danh mục Nghề" -> chọn ngành muốn crawl
#   3. Copy URL kết quả (dạng .../tim-viec-lam-<ten>-cr...cb...cl...)
#   4. Dán vào dict bên dưới với key ngắn gọn tuỳ ý
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
    "software-engineering": {
        "label": "Software Engineering",
        "url": "https://www.topcv.vn/tim-viec-lam-software-engineering-cr257cb258",
        "matching_industry": "Code",
    },
}

# Ngành mặc định khi không truyền --category
DEFAULT_CATEGORY = "data-analyst"

# ------------------------------------------------------------------
# VietnamWorks — nguồn thứ 2 (thêm 08/2026).
#
# Khác TopCV: category không phải URL trang danh mục, mà là 1 chuỗi
# "query" gửi vào API search (trang listing là CSR, không có URL SSR
# theo từng ngành để crawl trực tiếp như TopCV).
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
    "software-engineering": {
        "label": "Software Engineering",
        "query": "software engineer",
        "matching_industry": "Code",
    },
}

# API search job của VietnamWorks — xác nhận bằng cURL thật user copy từ
# DevTools (08/2026). Method POST, body JSON (xem adapters/vietnamworks.py).
VNW_SEARCH_URL = "https://ms.vietnamworks.com/job-search/v1.0/search"

VNW_HITS_PER_PAGE = 50

# Header khớp đúng cURL thật user gửi — 'origin'/'referer' bắt buộc phải
# đúng domain vietnamworks.com (API check CORS same-site), thiếu 2 header
# này nhiều khả năng bị chặn.
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

# CHÚ Ý: danh sách này COPY TRỰC TIẾP từ cURL thật user gửi nhưng BỊ CẮT
# NGANG ở "numOfApplicat..." (giới hạn hiển thị lúc copy-paste) — có thể
# response API bỏ qua field lạ nên KHÔNG BẮT BUỘC phải đủ 100% để API
# chạy được, nhưng nếu thiếu field nào đó cần dùng (vd field slug/alias
# của CÔNG TY để build company_url — xem cảnh báo #2 trong
# adapters/vietnamworks.py) thì phải bổ sung lại đúng list đầy đủ bằng
# cách copy lại "Copy as cURL" 1 lần nữa từ DevTools.
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

# Số trang tối đa crawl / lần chạy (mỗi trang TopCV ~ 20-25 job).
# Để nhỏ lúc test, tăng lên khi crawl thật.
DEFAULT_MAX_PAGES = 3

# Độ trễ giữa các request (giây) — lịch sự với server, tránh bị chặn.
REQUEST_DELAY_SECONDS = 4.0 

# Header giả lập trình duyệt thật — bổ sung đầy đủ hơn (Accept,
# Accept-Encoding, Sec-Fetch-*, Upgrade-Insecure-Requests...) vì WAF của
# TopCV có thể chặn 403 những request "thiếu" các header trình duyệt
# thật luôn gửi kèm, chỉ có User-Agent + Accept-Language KHÔNG đủ giống
# request thật.
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
# Database (đọc từ biến môi trường, xem .env.example)
# ------------------------------------------------------------------
import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # đọc file .env nếu có, không lỗi nếu không có
except ImportError:
    pass

DB_CONFIG = {
    "host": os.getenv("PGHOST", "localhost"),
    "port": os.getenv("PGPORT", "5432"),
    "dbname": os.getenv("PGDATABASE", "Student Success — Job Postings & Company Contacts"),
    "user": os.getenv("PGUSER", "postgres"),
    "password": os.getenv("PGPASSWORD", ""),
    # Supabase (và hầu hết managed Postgres cloud khác) bắt buộc SSL —
    # nếu thiếu, psycopg2 sẽ báo lỗi kết nối. Mặc định "require" vì giờ
    # project chỉ dùng DB cloud; nếu sau này quay lại chạy DB local
    # không có SSL, đặt PGSSLMODE=disable trong .env.
    "sslmode": os.getenv("PGSSLMODE", "require"),
}

# ------------------------------------------------------------------
# Enrich website/tax_id công ty qua tìm kiếm web — dùng cho
# enrich_company_web_info.py (script RIÊNG, không nằm trong pipeline
# crawl chính, xem docstring đầu file đó để biết lý do tách riêng).
#
# Tavily: search API thiết kế cho AI agent — free tier 1.000
# credit/tháng, KHÔNG cần thẻ thanh toán (xác nhận 08/2026), tự reset
# hàng tháng. Lấy key tại https://tavily.com (dashboard, không cần
# billing).
# Gemini: dùng bản text THƯỜNG (KHÔNG bật tool "grounding"/google_search
# — tính năng đó bắt buộc phải bật billing mới dùng được). Ở đây Gemini
# chỉ đóng vai trò "đọc kết quả Tavily trả về, trích xuất website/tax_id
# ra JSON có cấu trúc" — việc này KHÔNG cần grounding, chạy được trên
# free tier bình thường (free tier Gemini vẫn giới hạn theo request/
# phút, không giới hạn theo tháng như Tavily).
# ------------------------------------------------------------------
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# gemini-2.5-flash ĐÃ NGỪNG cấp cho API key mới tạo (Google trả 404 "no
# longer available to new users" kể từ ~08/2026, model sẽ tắt hẳn
# 16/10/2026) — dù key cũ vẫn gọi được, key mới generate sau đợt siết
# này sẽ luôn bị 404 với model này.
#
# gemini-3.1-flash-lite: model hiện hành Google khuyến nghị cho free
# tier (xác nhận 08/2026), phù hợp cho việc trích xuất JSON đơn giản
# này (không cần reasoning phức tạp). Free tier RPM = 15/phút — CAO HƠN
# gemini-3-flash (10/phút), quan trọng vì script này gọi hàng loạt công
# ty liên tiếp. Nếu sau này Google lại đổi/rút model, xem danh sách
# model + RPM hiện hành tại https://ai.google.dev/gemini-api/docs/rate-limits
GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_FREE_TIER_RPM = 15  # dùng để tính delay an toàn giữa các lần gọi

# ------------------------------------------------------------------
# Delay riêng cho enrich_company_web_info.py — TÁCH KHỎI
# REQUEST_DELAY_SECONDS (delay của crawler TopCV/VietnamWorks, dùng cho
# mục đích khác hẳn: lịch sự với WAF, không phải rate limit API trả phí
# theo phút). Dùng chung 1 hằng số cho 2 việc không liên quan sẽ gây rối
# khi cần tinh chỉnh riêng (vd tăng delay TopCV để né 403 sẽ vô tình làm
# chậm oan script enrich, hoặc ngược lại).
#
# Tính từ GEMINI_FREE_TIER_RPM + biên an toàn (không sát ngưỡng, vì mỗi
# lần chạy còn có thêm 1 lệnh gọi Tavily trước đó, và AFC — cơ chế retry
# tự động của SDK — có thể âm thầm tốn thêm quota nếu response bị lỗi
# tạm thời).
ENRICH_REQUEST_DELAY_SECONDS = 60 / GEMINI_FREE_TIER_RPM + 1.5  # ~5.5s
