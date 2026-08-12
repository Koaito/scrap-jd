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
REQUEST_DELAY_SECONDS = 4.0 

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
# Enrich API Config
# ------------------------------------------------------------------
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_FREE_TIER_RPM = 15

ENRICH_REQUEST_DELAY_SECONDS = 60 / GEMINI_FREE_TIER_RPM + 1.5  # ~5.5s