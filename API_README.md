# API Layer (FastAPI) — hướng dẫn chạy

Lớp API này **bọc ngoài** codebase crawler hiện có (`adapters/`, `normalize.py`,
`db.py`, `pipeline.py`) — không sửa gì các file đó ngoại trừ thêm 1 nhóm
hàm query mới cuối `db.py` (mục "QUERY LAYER CHO API"). `main.py` (CLI
crawl cũ) giữ nguyên 100%, chạy song song không xung đột với API.

## Cài đặt thêm

```bash
pip install -r requirements.txt   # đã thêm fastapi + uvicorn
```

Điền thêm trong `.env` (bắt buộc — xem `.env.example`):

- `API_KEY`, `ALLOWED_ORIGINS` — bắt buộc để chạy API ở mức tối thiểu.
- `JWT_SECRET_KEY` — bắt buộc nếu dùng lớp đăng nhập từng người (`/auth/*`
  và các route ghi `POST`/`PATCH` bên dưới).
- `DB_POOL_MIN`/`DB_POOL_MAX` — có giá trị mặc định (2/20), chỉ cần sửa
  nếu Postgres phía deploy giới hạn connection thấp hơn.

## Chạy

```bash
uvicorn api.app:app --reload --port 8000
```

Swagger UI (`/docs`) **mặc định TẮT** (xem mục "Bảo mật"). Set
`ENABLE_DOCS=true` trong `.env` để bật lúc dev local, rồi mở
`http://localhost:8000/docs` — nhớ bấm nút khoá (🔒) góc trên bên phải,
nhập `API_KEY`, hoặc thêm header thủ công khi thử "Try it out". Các route
ghi (POST/PATCH) cần thêm `Authorization: Bearer <access_token>` — lấy
token qua `POST /auth/login` trước.

## Bảo mật — 2 lớp xếp chồng

### Lớp 1 — API key tĩnh (mọi request)

Mọi endpoint (kể cả `/health`) yêu cầu header `X-API-Key` đúng giá trị
`API_KEY` trong `.env`. Fail-closed: quên cấu hình `API_KEY` → server tự
chặn hết, không âm thầm mở toang. Đây là khoá "máy gọi máy" dùng chung
cho cả team, xác nhận "client này là frontend của mình" — KHÔNG phân biệt
được người dùng cụ thể nào đang gọi. Chi tiết: `api/auth.py`.

### Lớp 2 — Đăng nhập JWT từng người (chỉ route ghi)

Thêm 08/2026, xác nhận **AI thật** đang gọi — dùng bảng `ss_team_members`
đã mở rộng qua `sql/migration_add_auth.sql` (cột `password_hash`, `role`)
và bảng `auth_refresh_tokens`. Chi tiết thiết kế: `api/security.py`,
`api/deps.py`.

Luồng:

1. `POST /auth/login` (email + password) → trả `access_token` (JWT, sống
   30 phút) + `refresh_token` (sống dài hơn, xoay vòng mỗi lần dùng).
2. Gửi kèm `Authorization: Bearer <access_token>` cho các route cần biết
   user thật (bảng dưới) hoặc route quản lý tài khoản (`/auth/me`, đổi
   mật khẩu...).
3. `POST /auth/refresh` khi access token hết hạn, lấy cặp token mới.
4. `POST /auth/users` — chỉ `role=admin` gọi được, tạo tài khoản mới cho
   thành viên team (không có luồng tự đăng ký công khai).

**Route nào bắt buộc lớp 2:**

| Route | Yêu cầu |
|---|---|
| `POST /jobs`, `PATCH /jobs/{id}` | Đăng nhập (`get_current_user`) |
| `POST /companies` | Đăng nhập (`get_current_user`) |
| `POST /crawl` | Đăng nhập **+ role admin** (`require_admin`) |
| Mọi route `GET` khác | Chỉ cần API key (lớp 1), KHÔNG bắt buộc đăng nhập |

Chọn `require_admin` riêng cho `POST /crawl` (chặt hơn `POST /jobs`) vì
kích hoạt crawl tốn tài nguyên server thật (network + CPU parse vài
phút) — hạn chế để tránh spam nhiều lượt crawl chạy song song.

### CORS

Chỉ domain liệt kê trong `ALLOWED_ORIGINS` (`.env`, phân tách bằng dấu
phẩy) mới gọi được từ trình duyệt. Để trống → không domain nào gọi được
(fail-closed, giống `API_KEY`).

### Swagger/ReDoc/openapi.json

3 route duy nhất KHÔNG đi qua được lớp `API_KEY` (giới hạn kỹ thuật của
FastAPI, route Starlette thuần, không phải path operation thường). Vì
vậy mặc định tắt hẳn (fail-closed) để không lộ cấu trúc API ra ngoài;
chỉ bật bằng `ENABLE_DOCS=true` lúc dev local, tránh bật trên môi trường
public. Xem chi tiết trong docstring `api/app.py`.

## Deploy production

Đã deploy thật lên **Render** (Web Service, kết nối GitHub private repo
`Koaito/scrap-jd`), khai báo đủ biến môi trường (Postgres, `API_KEY`,
`ALLOWED_ORIGINS`, `JWT_SECRET_KEY`, `DB_POOL_MIN`/`DB_POOL_MAX`,
Tavily/Gemini key). URL public: `https://scrap-jd-api.onrender.com`.

**Trước khi deploy bản có JWT audit trail này**, phải chạy trên Postgres
thật (đúng thứ tự, migration sau phụ thuộc bảng/cột migration trước):

```bash
psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_auth.sql
psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_audit_columns.sql
```

Deploy code trước khi chạy migration → mọi `POST`/`PATCH /jobs`,
`POST /companies` sẽ lỗi 500 (cột `created_by`/`updated_by` chưa tồn tại).

Khi làm frontend và deploy lên Vercel: quay lại Render, sửa
`ALLOWED_ORIGINS` cho khớp domain Vercel thật, KHÔNG quên bước này
(thiếu → frontend gọi API bị chặn bởi CORS dù key đúng).

## Cấu trúc

```
api/
  app.py              <- entry point FastAPI, đăng ký auth + CORS + router + lifespan (init/close connection pool)
  auth.py              <- kiểm tra X-API-Key, fail-closed nếu thiếu cấu hình
  security.py           <- băm mật khẩu (bcrypt), ký/verify JWT access token, sinh + băm refresh token
  deps.py               <- get_db(): mượn/trả connection từ pool; get_current_user()/require_admin(): xác thực JWT
  schemas.py             <- Pydantic models (request/response JSON)
  crawl_runner.py         <- chạy pipeline crawl ở nền, theo dõi qua run_id
  routers/
    jobs.py                <- GET/POST /jobs, GET/PATCH /jobs/{id}
    companies.py             <- GET/POST /companies, GET /companies/{id}
    crawl.py                  <- POST /crawl (admin), GET /crawl/{run_id}
    meta.py                    <- GET /stats, GET /sources, GET /health
    auth.py                     <- POST /auth/login, /refresh, /logout, GET /auth/me, POST /auth/change-password, POST /auth/users
```

## Endpoints hiện có

| Method | Path | Việc | Auth |
|---|---|---|---|
| GET | `/jobs?industry=&province=&level=&work_type=&status=&keyword=&limit=&offset=` | List job, filter + phân trang | API key |
| GET | `/jobs/{job_id}` | Chi tiết 1 job (kèm parsed_content) | API key |
| POST | `/jobs` | Tạo job thủ công (company phải có sẵn), idempotent | API key + JWT |
| PATCH | `/jobs/{job_id}` | Sửa job tự do (đổi trạng thái/lương/ghi chú...). Dùng `job_status:"CLOSED"` để "xoá mềm" | API key + JWT |
| GET | `/companies?keyword=&province=&has_social=&limit=&offset=` | List công ty, filter + phân trang | API key |
| GET | `/companies/{company_id}` | Chi tiết công ty (kèm danh sách job) | API key |
| POST | `/companies` | Tạo công ty thủ công (tự dùng lại công ty đã có nếu trùng tax_id) | API key + JWT |
| POST | `/crawl` | Kích hoạt crawl nền — body `{"source", "category", "pages"?, "max_jobs"?}`, trả `run_id` ngay | API key + JWT (admin) |
| GET | `/crawl/{run_id}` | Theo dõi tiến độ/kết quả 1 lượt crawl | API key |
| GET | `/stats` | Tổng job/công ty, tỷ lệ có social, phân bố ngành/nguồn | API key |
| GET | `/sources` | Danh sách source/category có sẵn (đọc từ `config.py`) — frontend render dropdown | API key |
| GET | `/health` | Health check | API key |
| POST | `/auth/login` | Đăng nhập, trả `access_token` + `refresh_token` | API key |
| POST | `/auth/refresh` | Xoay vòng lấy access token mới | API key |
| POST | `/auth/logout` | Thu hồi refresh token hiện tại | API key |
| GET | `/auth/me` | Thông tin tài khoản đang đăng nhập | API key + JWT |
| POST | `/auth/change-password` | Tự đổi mật khẩu | API key + JWT |
| POST | `/auth/users` | Tạo tài khoản mới cho thành viên team | API key + JWT (admin) |

"API key" = mọi request đều cần header `X-API-Key: <giá trị API_KEY>`.
"JWT" = cần thêm header `Authorization: Bearer <access_token>` (lấy từ
`POST /auth/login`).

### `POST /jobs` — tạo job thủ công

```json
{
  "job_title": "Data Analyst",
  "company_id": "<company_id lấy từ GET/POST /companies>",
  "matching_industry": "Data Analysis",
  "level_code": "Junior",
  "province_name": "Hà Nội",
  "work_type": "FULL_TIME",
  "currency": "VNĐ",
  "salary_min": 15000000,
  "salary_max": 25000000,
  "salary_type": "RANGE",
  "deadline": "2026-12-31"
}
```

`company_id` phải đã tồn tại (tạo trước bằng `POST /companies` nếu chưa
có) — route KHÔNG tự tạo company kèm job, tránh nhập nhằng giữa "chọn
đúng company có sẵn" với "gõ nhầm tên tạo company trùng". Gọi lại nhiều
lần với data y hệt (cùng company_id + job_title + level_code +
province_name) sẽ KHÔNG tạo job trùng — trả về job đã có.

### `PATCH /jobs/{job_id}` — sửa job

Chỉ gửi field muốn sửa, field không gửi giữ nguyên. Ví dụ "xoá mềm":

```json
{"job_status": "CLOSED"}
```

Không có endpoint DELETE thật — job xoá thật sẽ bị crawl lại tạo trùng ở
lượt crawl sau.

### `POST /companies` — tạo công ty thủ công

```json
{
  "company_name": "Công ty TNHH ABC",
  "tax_id": "0123456789",
  "website": "https://abc.vn",
  "industry": "Công nghệ thông tin",
  "company_size": "50-100",
  "address": "Hà Nội",
  "province_name": "Hà Nội",
  "fanpage_url": "https://facebook.com/abc",
  "linkedin_url": "https://linkedin.com/company/abc"
}
```

Nếu `tax_id` trùng với công ty đã có sẵn (vd đã crawl từ TopCV trước đó)
→ route tự động dùng lại company đã có, KHÔNG tạo bản ghi trùng. Luôn
dùng `company_id` trong response cho bước tạo job tiếp theo.

### `POST /crawl` — giới hạn theo trang hoặc theo số lượng JD

Khớp 1-1 với `--pages`/`--max-jobs` đã có ở CLI (`main.py`):

```json
// Giới hạn theo trang (như cũ)
{"source": "topcv", "category": "data-analyst", "pages": 3}

// Giới hạn theo TỔNG SỐ JD, không quan tâm bao nhiêu trang
{"source": "topcv", "category": "data-analyst", "max_jobs": 20}

// Kết hợp cả 2 -> dừng ở điều kiện nào tới trước
{"source": "topcv", "category": "data-analyst", "pages": 5, "max_jobs": 50}

// Không truyền cả 2 -> dùng DEFAULT_MAX_PAGES (config.py) như trước giờ
{"source": "topcv", "category": "data-analyst"}
```

`GET /crawl/{run_id}` trả thêm field `max_jobs` (null nếu không giới hạn
theo số lượng) bên cạnh `pages` đã có.

### `POST /auth/login`

```json
{"email": "member@example.com", "password": "..."}
```

Trả `{"access_token", "refresh_token", "token_type": "bearer",
"must_change_password"}`. `must_change_password=true` nghĩa là tài khoản
mới tạo/vừa bị admin reset — frontend nên ép chuyển sang màn đổi mật
khẩu (`POST /auth/change-password`) trước khi cho dùng tiếp.

## Audit trail — ai tạo/sửa job và công ty

Thêm 08/2026 cùng với việc bắt buộc JWT ở route ghi. `job_postings` và
`companies` có thêm 2 cột `created_by`/`updated_by` (UUID, tham chiếu
`ss_team_members.ss_user_id`), trả về trong response của
`GET`/`POST`/`PATCH` (`JobOut`/`CompanyOut`).

- Job/công ty tạo qua **crawl tự động** (`main.py`, không qua JWT) →
  `created_by = null`. Đây là giá trị bình thường, không phải lỗi.
- Job/công ty tạo/sửa qua **API có JWT** (`POST`/`PATCH` ở trên) →
  `created_by`/`updated_by` = `ss_user_id` của người vừa gọi.
- Migration `sql/migration_add_audit_columns.sql` PHẢI chạy trước khi
  deploy code này (xem mục Deploy production).

## Connection pool

Thêm 08/2026 — `api/deps.py:get_db()` trước đây mở 1 connection Postgres
MỚI mỗi request, giờ **mượn/trả** connection từ 1 pool đã mở sẵn
(`psycopg2.pool.ThreadedConnectionPool`, xem `db.py`), giảm round-trip
TCP/TLS khi nhiều người dùng dashboard cùng lúc. Pool khởi tạo 1 lần lúc
app khởi động (`api/app.py`, `lifespan`), đóng lại lúc app tắt.

Kích thước pool cấu hình qua `DB_POOL_MIN`/`DB_POOL_MAX` trong `.env`
(mặc định 2/20) — **nên đặt `DB_POOL_MAX` thấp hơn** giới hạn connection
Postgres phía Render/Supabase cho phép (managed Postgres tier free
thường giới hạn thấp), tránh pool "xin" nhiều hơn DB cho phép.

`main.py` (CLI) và các script độc lập (`enrich_company_web_info.py`,
`get_company_fb_linkedin_link.py`, `api/crawl_runner.py`) vẫn dùng
`db.get_connection()` mở/đóng connection trực tiếp như cũ — KHÔNG qua
pool, vì tần suất chạy thấp (1 lần/script), không cần thiết.

## Giới hạn đã biết

- **Trạng thái crawl lưu trong RAM** (dict `_RUNS`), mất khi restart server,
  không đồng bộ nếu chạy `uvicorn --workers > 1`. Đủ dùng cho quy mô hiện
  tại (dashboard nội bộ, ít người). Cần chạy nhiều worker/persistent thì
  nâng cấp sang Celery + Redis hoặc RQ sau.
- **`POST /crawl` không giới hạn số lượt chạy song song** — `require_admin`
  giảm rủi ro spam nhưng không tự chặn 2 lượt chạy cùng lúc. Nên tự giới
  hạn ở phía frontend (disable nút "Crawl" khi đang chạy).
- **Auth API key (lớp 1) vẫn là 1 khoá dùng chung** cho mọi client kiểu
  "máy gọi máy" — không phân biệt được nội bộ ai gọi ở TẦNG NÀY (muốn
  biết ai thì phải qua JWT, chỉ bắt buộc ở route ghi, xem mục Bảo mật).
- **Access token JWT không tự kiểm tra lại DB** (`get_current_user()` chỉ
  verify chữ ký, không query `is_active`) — thu hồi tức thời 1 tài khoản
  (vd nhân viên nghỉ việc) cần gọi `revoke_all_refresh_tokens_for_user()`
  để chặn cấp access token MỚI, access token cũ (tối đa 30 phút) vẫn còn
  hiệu lực tới khi tự hết hạn.

## Việc CHƯA làm (để team quyết định có cần không)

- Phân quyền chi tiết hơn 2 mức `admin`/`member` hiện có (vd quyền theo
  từng resource).
- Rate limiting.
- Frontend — chưa bắt đầu.
