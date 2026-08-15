# Job Crawler — Student Success

Crawler job từ **TopCV** và **VietnamWorks** (6 ngành: Data Analyst, Data
Engineer, Data Scientist, Software Engineering, Business Analysis, UI/UX
Design), chuẩn hóa dữ liệu, crawl sâu hồ sơ công ty (website, mã số thuế,
quy mô, lĩnh vực, địa chỉ), lưu vào PostgreSQL — expose ra ngoài qua 1 lớp
**API FastAPI có auth**, có **frontend dashboard** riêng gọi vào API này.

**Trạng thái (08/2026):** cả backend lẫn frontend đã lên production.

- **Backend** (repo này) — pipeline crawl + API, deploy trên Render:
  `https://scrap-jd-api.onrender.com`
- **Frontend** (repo `mindx-jobs`) — deploy trên Vercel, gọi API qua JWT.
  `ALLOWED_ORIGINS` trên Render đã trỏ đúng domain Vercel thật.

Tính năng chính: bảo mật 3 lớp (API key dùng chung + JWT từng người +
phân quyền `user`/`ss_team`/`admin`), đăng ký công khai có xác thực email,
quên/đặt lại mật khẩu, học viên ứng tuyển/lưu job, staff xem ai đã ứng
tuyển + số điện thoại liên hệ, CRUD liên hệ HR có soft-delete, audit trail
(ai tạo/sửa job, công ty, contact), connection pool Postgres.

Xem tình trạng dữ liệu thật và các hạn chế đang biết ở cuối file.

Ngoài pipeline crawl chính còn có 2 script độc lập, chạy khi cần:

- `get_company_fb_linkedin_link.py` — điền `fanpage_url`/`linkedin_url`
  bằng cách crawl website riêng của từng công ty.
- `enrich_company_web_info.py` — vá thêm `website`/`tax_id` cho công ty
  còn thiếu, bằng Tavily search + Gemini trích xuất.

## Kiến trúc

```
config.py                        <- ngành/category muốn crawl, delay, model AI...
models.py                        <- RawJobRecord: khuôn dữ liệu chung mọi adapter phải trả về
adapters/                        <- topcv.py, vietnamworks.py (implement BaseAdapter)
normalize.py                     <- dùng chung: parse lương, suy luận level, deadline, work_type
db.py                            <- dùng chung: mọi thao tác PostgreSQL
pipeline.py                      <- nối adapter -> normalize -> db
main.py                          <- CLI chạy crawl
get_company_fb_linkedin_link.py  <- script riêng: fanpage/LinkedIn
enrich_company_web_info.py       <- script riêng: website/tax_id qua Tavily + Gemini
api/                              <- lớp API FastAPI (xem mục API layer bên dưới)
  app.py                          <- entry point, đăng ký auth + CORS + router + lifespan
  auth.py                         <- API key tĩnh, áp dụng cho toàn bộ endpoint
  security.py                     <- băm mật khẩu, ký/verify JWT access + refresh token
  email_service.py                <- gửi email xác thực + quên mật khẩu qua Resend
  deps.py                         <- get_db(), get_current_user(), require_role()
  schemas.py                      <- Pydantic models (request/response JSON)
  crawl_runner.py                 <- chạy pipeline crawl ở nền, theo dõi qua run_id
  routers/                        <- jobs.py, companies.py, contacts.py, crawl.py, meta.py, auth.py, me.py
sql/schema.sql                   <- schema PostgreSQL đầy đủ (chạy 1 lần cho DB mới)
sql/migration_*.sql              <- vá DB cũ đã tạo trước khi có tính năng mới
tests/                           <- test parser + logic, không cần DB/internet
```

Muốn thêm nguồn crawl mới (ITviec...): viết `adapters/itviec.py` implement
`fetch_jobs()`, khai báo trong `SOURCES` ở `main.py` — không cần sửa
`normalize.py`, `db.py`, `pipeline.py`.

---

## Cài đặt

1. **PostgreSQL** — cài local (`brew install postgresql@16` / `apt install
   postgresql` / [Windows installer](https://www.postgresql.org/download/windows/))
   hoặc dùng managed Postgres cloud (Supabase...), rồi tạo database:
   ```bash
   psql -U postgres -c 'CREATE DATABASE "Student Success — Job Postings & Company Contacts";'
   ```
2. **Python 3.9+** + thư viện:
   ```bash
   python3 -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. **Cấu hình `.env`**: `cp .env.example .env`, sửa `PGPASSWORD`.
   - Chạy `enrich_company_web_info.py`: thêm `TAVILY_API_KEY`,
     `GEMINI_API_KEY` (xem [mục riêng](#enrich_company_web_infopy---vá-websitetax_id)).
   - Chạy **API layer**: bắt buộc `API_KEY`, `ALLOWED_ORIGINS`,
     `JWT_SECRET_KEY` (xem [mục API layer](#api-layer-fastapi)). Bật đăng
     ký công khai + quên mật khẩu qua email thật: thêm
     `RESEND_API_KEY`/`EMAIL_FROM`/`API_BASE_URL`.
4. **Tạo bảng**:
   ```bash
   python main.py init-db
   ```
   Kỳ vọng: `✅ Đã tạo/cập nhật schema trong database.`

   > DB tạo từ bản cũ (trước khi có lớp auth/audit/role/đăng ký/quên mật
   > khẩu/đổi tên bảng/ứng tuyển/lưu job) cần chạy thêm `sql/migration_*.sql`
   > — xem comment đầu mỗi file để biết chi tiết. Repo hiện có **12 file**
   > migration; chạy **đúng thứ tự sau** (migration sau phụ thuộc bảng/cột
   > migration trước tạo ra):
   > ```bash
   > psql -U postgres -d "..." -f sql/migration_add_auth.sql
   > psql -U postgres -d "..." -f sql/migration_add_audit_columns.sql
   > psql -U postgres -d "..." -f sql/migration_add_role_hierarchy.sql
   > psql -U postgres -d "..." -f sql/migration_add_email_verification.sql
   > psql -U postgres -d "..." -f sql/migration_rename_ss_team_members.sql
   > psql -U postgres -d "..." -f sql/migration_add_applications_saved_jobs.sql
   > psql -U postgres -d "..." -f sql/migration_add_phone_track.sql
   > psql -U postgres -d "..." -f sql/migration_add_password_reset.sql
   > psql -U postgres -d "..." -f sql/migration_drop_products_services.sql
   > psql -U postgres -d "..." -f sql/migration_add_tax_id.sql
   > psql -U postgres -d "..." -f sql/migration_add_work_type_deadline.sql
   > psql -U postgres -d "..." -f sql/migration_update_provinces_2025.sql
   > ```
   > Thiếu bất kỳ file nào ở trên có thể làm `POST`/`PATCH /jobs`,
   > `POST /companies`, CRUD `/companies/{id}/contacts`,
   > `POST /auth/register`, đăng nhập, quên mật khẩu, ứng tuyển, hoặc lưu
   > job lỗi 500 — tuỳ file nào bị thiếu. DB tạo mới hoàn toàn từ
   > `sql/schema.sql` (bước 4 ở trên) đã có sẵn đầy đủ, **không cần** chạy
   > lại các migration này.

5. **Test** (không cần DB/internet):
   ```bash
   python tests/test_parse_and_normalize.py
   python tests/test_merge_companies.py
   ```
   Kỳ vọng `✅ PASS` cho cả 2.

---

## Crawl job

```bash
python main.py crawl --source topcv --category data-analyst --pages 3
python main.py crawl --source vietnamworks --category data-engineer --pages 3

# Giới hạn theo SỐ LƯỢNG JD thay vì theo trang (tiện lấy mẫu nhỏ để test):
python main.py crawl --source topcv --category data-analyst --max-jobs 20
```

- `--source`: `topcv` (mặc định) hoặc `vietnamworks`.
- `--category`: xem danh sách trong `config.py` (`TOPCV_CATEGORIES` /
  `VIETNAMWORKS_CATEGORIES`). Hiện có: `data-analyst`, `data-engineer`,
  `data-scientist`, `software-engineering`, `business-analyst`,
  `ui-ux-design`.
- `--pages`: số trang tối đa. 1 trang TopCV ~20-25 job, VietnamWorks ~50 job.
- `--max-jobs`: giới hạn TỔNG SỐ JD, dừng ngay khi đủ — không cần đợi hết
  `--pages`. Dùng riêng thì tự nới `--pages` đủ lớn; dùng cùng lúc cả 2 cờ
  thì dừng ở điều kiện nào tới trước.

Mỗi lần crawl, hệ thống tự động:

- Bỏ qua job đã crawl trước đó (theo link JD gốc), nhưng **vẫn vá thêm**
  `work_type`/`deadline`/nội dung JD nếu trước đó còn thiếu.
- Crawl sâu trang chi tiết job (work_type, hạn ứng tuyển, mô tả, yêu cầu,
  quyền lợi, kỹ năng) và trang hồ sơ công ty (website, mã số thuế, quy
  mô, lĩnh vực, địa chỉ) — công ty chỉ crawl sâu lần đầu gặp hoặc khi còn
  thiếu field.
- Match công ty **ưu tiên theo mã số thuế** — 2 job cùng công ty nhưng
  tên viết khác nhau vẫn nhận ra là 1, không tạo trùng.

**⚠️ Chưa an toàn khi chạy song song 2 lượt crawl cùng lúc** (vd vừa CLI
vừa `POST /crawl`, hoặc bấm crawl 2 lần liên tiếp trước khi lượt đầu kịp
`commit()`) — có thể tạo job trùng do race condition ở bước "check trùng
rồi mới insert" trong `pipeline.py`. Đã gặp thực tế 2 kiểu: (1) 2 job
cùng `source_url`; (2) 2 job khác `source_url` nhưng cùng `content_hash`
(case "Fullstack Developer" x2, cách nhau ~11 giây — xem [Tình trạng dữ
liệu](#tình-trạng-dữ-liệu)). Tạm thời: chỉ chạy 1 lượt crawl/lúc, soát
bằng `v_duplicate_job_candidates` nếu nghi trùng. Fix đúng cần advisory
lock theo `source_url` hoặc unique constraint DB — chưa làm.

## Xem kết quả

```bash
python main.py stats
```

Hoặc trực tiếp bằng `psql`:

```sql
SELECT job_title, company_id, salary_min, salary_max, salary_type
FROM job_postings ORDER BY created_at DESC LIMIT 10;

SELECT company_name, tax_id, website, company_size, industry
FROM companies ORDER BY created_at DESC LIMIT 10;

SELECT matching_industry, count(*) FROM job_postings GROUP BY matching_industry;

-- Soát job nghi trùng (khác link nguồn nhưng cùng nội dung)
SELECT * FROM v_duplicate_job_candidates;
```

---

## API layer (FastAPI)

Lớp API bọc ngoài codebase crawler — không sửa gì `main.py` (CLI crawl cũ
vẫn chạy y hệt), chỉ thêm nhóm hàm query mới cuối `db.py`.

### Chạy local

```bash
uvicorn api.app:app --reload --port 8000
```

Mọi request cần header `X-API-Key: <API_KEY trong .env>`, kể cả `/health`
(sai/thiếu key → `401`). Swagger (`/docs`)/ReDoc (`/redoc`) **mặc định
tắt** (không đi qua được lớp kiểm tra key) — bật lúc dev bằng
`ENABLE_DOCS=true` trong `.env`, không bật trên môi trường public.

### Bảo mật — 3 lớp xếp chồng

1. **API key tĩnh** (`api/auth.py`) — 1 key dùng chung, gửi qua header
   `X-API-Key` (hoặc `?api_key=` để test). Áp dụng toàn bộ endpoint kể cả
   route chỉ đọc. Thiếu `API_KEY` trong `.env` → server tự chặn hết
   (fail-closed).
2. **Đăng nhập JWT từng người** (`api/security.py`,
   `api/routers/auth.py`) — xác định AI đang gọi, khác lớp API key (chỉ
   xác nhận "đúng client của mình"). `POST /auth/login` → `access_token`
   (30 phút) + `refresh_token` (xoay vòng) → gửi
   `Authorization: Bearer <access_token>` cho route cần đăng nhập →
   `POST /auth/refresh` khi hết hạn.
3. **Phân quyền 3 cấp** (`ROLE_HIERARCHY`/`require_role()` trong
   `api/deps.py`) — cấp cao thoả mọi route yêu cầu cấp thấp hơn:

   | Role | Được làm |
   |---|---|
   | `user` | Xem/lọc job, ứng tuyển/lưu job của chính mình (`/me/*`) — mặc định khi tự đăng ký |
   | `ss_team` | + tạo/sửa job/company, CRUD liên hệ HR, xem ai đã ứng tuyển 1 job, xem danh sách tài khoản |
   | `admin` | + trigger crawl, tạo tài khoản hộ, đổi role người khác |

   Tạo tài khoản: **tự đăng ký** (`POST /auth/register`, luôn role
   `user`, phải xác thực email) hoặc **admin tạo hộ** (`POST
   /auth/users`, chọn role bất kỳ). Nâng role phải nhờ admin gọi `PATCH
   /auth/users/{id}/role`.

Mọi thao tác ghi qua JWT ghi vào `created_by`/`updated_by` của
`job_postings`/`companies`/`company_contacts` (audit trail) — job/công
ty tạo qua crawl tự động có `created_by = NULL`.

**CORS siết theo domain** — chỉ domain trong `ALLOWED_ORIGINS` (phân
tách dấu phẩy) gọi được từ trình duyệt. Để trống → không domain nào gọi
được (fail-closed).

### Đăng ký công khai + xác thực email

Ai cũng gọi được `POST /auth/register` — route này cùng `verify-email`
và `resend-verification` **KHÔNG cần** `X-API-Key` (khác mọi route khác,
vì link xác thực được bấm thẳng từ trình duyệt, không tự gắn header
được). Luôn tạo role `user`, `email_verified=false`. Server gửi email
xác thực qua **Resend** từ domain riêng `no-reply@scrapjd.xyz`. `POST
/auth/login` chặn nếu email chưa xác thực. Link hết hạn 24h, gửi lại qua
`POST /auth/resend-verification`.

Resend lỗi (rate-limit, mạng chập chờn) → tài khoản **vẫn tạo thành
công**, không mất dữ liệu, gọi lại `resend-verification` sau là được.

Bắt buộc `RESEND_API_KEY` để gửi email thật (thiếu → log lỗi, tài khoản
vẫn tạo nhưng email không tới). `API_BASE_URL` phải trỏ đúng domain API
thật (vd `https://scrap-jd-api.onrender.com`, không có `/docs` hay `/`
cuối) để link trong email đúng.

### Quên / đặt lại mật khẩu

`POST /auth/forgot-password` (email) → **luôn** trả cùng 1 message chung
chung dù email có tồn tại hay không (chống dò email hàng loạt, giống cơ
chế `resend-verification`) → nếu email tồn tại, gửi link đặt lại qua
Resend, token sống **1 giờ**, dùng đúng 1 lần. Không chặn nếu tài khoản
chưa xác thực email — quên mật khẩu và chưa-verify là 2 vấn đề độc lập.

`POST /auth/reset-password` (token + mật khẩu mới) → đổi mật khẩu, sau
đó **thu hồi toàn bộ refresh token cũ** của user (nếu lý do quên mật
khẩu là bị lộ mật khẩu, phiên đăng nhập cũ bị đá ra ngay, không đợi
access token 30 phút tự hết hạn). Token sai/hết hạn → `400`.

### Endpoints hiện có

| Method | Path                                                                            | Việc                                                                                                               |
| ------ | ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| GET    | `/jobs?industry=&province=&level=&work_type=&status=&keyword=&limit=&offset=` | List job, filter + phân trang — chỉ cần API key                                                                    |
| GET    | `/jobs/{job_id}`                                                              | Chi tiết 1 job (kèm parsed_content) — chỉ cần API key                                                             |
| POST   | `/jobs`                                                                       | Tạo job thủ công (company phải có sẵn) — **role ss_team+**, ghi `created_by`                                       |
| PATCH  | `/jobs/{job_id}`                                                              | Sửa job (trạng thái, lương, ghi chú...) — **role ss_team+**. Dùng `job_status:"CLOSED"` để "xoá mềm"               |
| GET    | `/jobs/{job_id}/applications`                                                 | Ai đã ứng tuyển job này (full_name/email/phone) — **role ss_team+**                                               |
| GET    | `/companies?keyword=&province=&has_social=&limit=&offset=`                    | List công ty, filter + phân trang — chỉ cần API key                                                               |
| GET    | `/companies/{company_id}`                                                     | Chi tiết công ty (kèm danh sách job) — chỉ cần API key                                                          |
| POST   | `/companies`                                                                  | Tạo công ty thủ công (tự dùng lại nếu trùng tax_id) — **role ss_team+**                                            |
| PATCH  | `/companies/{company_id}`                                                     | Sửa tự do field công ty đã có (chỉ field gửi lên bị ghi đè) — **role ss_team+**                                    |
| GET    | `/companies/{company_id}/contacts?include_inactive=`                          | List liên hệ HR của 1 công ty — **role ss_team+**                                                                |
| POST   | `/companies/{company_id}/contacts`                                            | Thêm liên hệ HR — **role ss_team+**                                                                              |
| PATCH  | `/companies/{company_id}/contacts/{contact_id}`                               | Sửa liên hệ HR — **role ss_team+**                                                                                |
| DELETE | `/companies/{company_id}/contacts/{contact_id}`                               | Xoá mềm liên hệ HR (`is_active=false`, giữ lịch sử) — **role ss_team+**                                          |
| POST   | `/me/applications`                                                            | Học viên ứng tuyển 1 job — 409 nếu đã ứng tuyển, 400 nếu job không `OPEN`                                          |
| GET    | `/me/applications`                                                            | Danh sách job mình đã ứng tuyển                                                                                    |
| DELETE | `/me/applications/{job_id}`                                                   | Huỷ ứng tuyển                                                                                                      |
| POST   | `/me/saved-jobs`                                                              | Lưu 1 job — 409 nếu đã lưu                                                                                          |
| GET    | `/me/saved-jobs`                                                              | Danh sách job đã lưu                                                                                                |
| DELETE | `/me/saved-jobs/{job_id}`                                                     | Bỏ lưu job                                                                                                           |
| POST   | `/crawl`                                                                      | Kích hoạt crawl nền — body `{"source", "category", "pages"?, "max_jobs"?}` — **role admin**                        |
| GET    | `/crawl/{run_id}`                                                             | Theo dõi tiến độ/kết quả 1 lượt crawl — chỉ cần API key                                                       |
| GET    | `/stats`                                                                      | Tổng job/công ty/đơn ứng tuyển (`total_applications`), tỷ lệ có social, phân bố ngành/nguồn — chỉ cần API key |
| GET    | `/sources`                                                                    | Danh sách source/category có sẵn — frontend render dropdown                                                    |
| GET    | `/health`                                                                     | Health check (vẫn cần API key)                                                                                  |
| POST   | `/auth/register`                                                              | Tự đăng ký (phone/track cho học viên, luôn role `user`), gửi email xác thực — không cần đăng nhập trước           |
| GET    | `/auth/verify-email?token=`                                                   | Bấm từ link trong email, kích hoạt tài khoản vừa đăng ký                                                        |
| POST   | `/auth/resend-verification`                                                  | Xin gửi lại email xác thực nếu token cũ hết hạn/thất lạc                                                        |
| POST   | `/auth/forgot-password`                                                      | Xin link đặt lại mật khẩu qua email — luôn trả message chung chung                                                |
| POST   | `/auth/reset-password`                                                       | Đặt mật khẩu mới bằng token từ email, thu hồi toàn bộ refresh token cũ                                            |
| POST   | `/auth/login`                                                                 | Đăng nhập, trả `access_token` (30 phút) + `refresh_token`. Chặn nếu email chưa xác thực                        |
| POST   | `/auth/refresh`                                                               | Xoay vòng lấy access token mới                                                                                  |
| POST   | `/auth/logout`                                                                | Thu hồi refresh token hiện tại                                                                                  |
| GET    | `/auth/me`                                                                    | Thông tin tài khoản đang đăng nhập                                                                              |
| POST   | `/auth/change-password`                                                      | Tự đổi mật khẩu                                                                                                  |
| POST   | `/auth/users`                                                                 | Admin tạo hộ tài khoản mới (chọn được role) — **role admin**                                                    |
| GET    | `/auth/users`                                                                 | Danh sách toàn bộ tài khoản — **role ss_team+**                                                                  |
| PATCH  | `/auth/users/{id}/role`                                                      | Đổi role tài khoản khác (không tự đổi role chính mình) — **role admin**                                          |

Xem chi tiết body/response từng endpoint trong `API_README.md`. Tài
khoản `ss_team`/`admin` **luôn ẩn** `phone`/`track` trong mọi response —
2 field này chỉ có ý nghĩa với học viên (`user`).

### Giới hạn đã biết

- **Trạng thái crawl (`POST /crawl`) lưu trong RAM**, mất khi restart
  server, không đồng bộ nếu chạy nhiều worker. Đủ dùng ở quy mô hiện
  tại. Nâng cấp sau: Celery + Redis hoặc RQ.
- **Không giới hạn crawl chạy song song** — xem cảnh báo race condition
  ở [Crawl job](#crawl-job). `require_admin` giảm rủi ro spam nhưng
  KHÔNG tự chặn 2 lượt chạy cùng lúc.
- **`GET /docs`/`/redoc`/`/openapi.json` không đi qua được API key**
  (giới hạn kỹ thuật FastAPI) — mặc định tắt, chỉ bật `ENABLE_DOCS=true`
  lúc dev local.
- **Auth API key vẫn là 1 khoá dùng chung** ở tầng "máy gọi máy" — muốn
  biết chính xác người nào gọi phải qua JWT (bắt buộc ở route ghi/route
  xem thông tin nhạy cảm).
- **`RESEND_API_KEY` chưa cấu hình → email (xác thực lẫn quên mật khẩu)
  không gửi được**, nhưng thao tác vẫn thành công trong DB (không mất
  dữ liệu).

---

## Deploy production

- **Backend**: Render Web Service, build từ repo này (`Koaito/scrap-jd`,
  GitHub private). 10 biến môi trường (Postgres, `API_KEY`,
  `ALLOWED_ORIGINS`, `JWT_SECRET_KEY`, Resend/Tavily/Gemini key...) cấu
  hình trực tiếp trên Render — **không phải** qua `.env` (file đó chỉ
  dùng local, `.gitignore` đã chặn commit). URL public:
  `https://scrap-jd-api.onrender.com`.
- **Frontend**: repo `mindx-jobs` (Flask), deploy trên Vercel, gọi API
  qua `Authorization: Bearer` (JWT). `ALLOWED_ORIGINS` trên Render đã
  cập nhật đúng domain Vercel — không còn bị CORS chặn.

---

## Tình trạng dữ liệu

Snapshot tại thời điểm viết (183 job / 134 công ty, crawl **6 ngành**
trên cả 2 nguồn):

| Field                                                          | Độ phủ |
| -------------------------------------------------------------- | --------- |
| `job_postings.work_type` / `deadline` / `parsed_content` | ~97%      |
| `job_postings.salary_min` (không tính "Thoả thuận")      | 29%       |
| `companies.tax_id`                                           | 96%       |
| `companies.website`                                          | 73%       |
| `companies.industry`                                         | 80%       |
| `companies.company_size`                                     | 61%       |
| `companies.fanpage_url`                                      | 41%       |
| `companies.address`                                          | 46%       |
| `companies.linkedin_url`                                     | 28%       |

Phân bố job theo ngành: Code 38, UI/UX Design 39, Data Engineer 35,
Business Analysis 31, Data Scientist 20, Data Analysis 20.

**Đã phát hiện 1 cặp job trùng nội dung thật** (0 `tax_id` trùng, 0
`source_url` trùng, nhưng 1 cặp `content_hash` trùng — 2 job "Fullstack
Developer" cùng nội dung, tạo cách nhau ~11 giây) — đúng cảnh báo race
condition ở [Crawl job](#crawl-job). Soát bằng `v_duplicate_job_candidates`,
chưa dọn tay.

### Bug đã sửa: sai đơn vị lương VietnamWorks (08/2026)

`normalize_salary()` trước đây luôn nhân số VNĐ với 1.000.000 (giả định
mọi số ở đơn vị "triệu"). VietnamWorks có 2 định dạng `prettySalary` khác
nhau cho cùng đơn vị VNĐ — `"15tr-30tr ₫/tháng"` (có hậu tố "tr") và
`"12,000-30,000 ₫/tháng"` (đã ở đơn vị nghìn đồng, không hậu tố) — nhân
cứng 1 kiểu khiến case thứ 2 lệch 1000 lần. Đã sửa bằng cách suy luận hệ
số nhân theo **độ lớn của chính con số** — xem docstring
`_vnd_multiplier()` trong `normalize.py`. **Chỉ áp dụng cho job crawl mới
sau này** — job cũ trong DB (kể cả bản ghi lỗi) vẫn chưa được vá lại,
chưa có script tự động, cần soát tay bằng SQL nếu cần.

### Việc còn tồn đọng

- **Chưa sửa lỗi trùng job do race condition** khi crawl song song (xem
  [Crawl job](#crawl-job)).
- **4 job có `required_skills` bị lặp phần tử** trong `parsed_content`
  (nghi do artifact khi parse DOM), chưa dedupe ở tầng
  `normalize`/`pipeline`.
- **`company_size` (61%), `address` (46%), `linkedin_url` (28%) còn
  thiếu nhiều** — chạy thêm `get_company_fb_linkedin_link.py` /
  `enrich_company_web_info.py` để vá, hoặc chấp nhận vì nguồn crawl
  không phải lúc nào cũng có sẵn field này.
- **Dữ liệu mới từ 1 lượt crawl, 6 ngành, 2 nguồn** — cần crawl thêm
  định kỳ để có dữ liệu đủ lớn cho dashboard.
- **Job listing/company listing ở frontend chưa lưu lại link JD gốc khi
  học viên bấm vào**, và giao diện quản lý staff còn vài chỗ cần dọn —
  xem `mindx-jobs` repo để biết chi tiết việc còn lại phía frontend.

---

## `get_company_fb_linkedin_link.py` — điền fanpage/LinkedIn

```bash
python get_company_fb_linkedin_link.py --limit 10   # test thử ít công ty
python get_company_fb_linkedin_link.py               # chạy full
```

- Chỉ xử lý công ty **đã có `website`** và **còn thiếu**
  `fanpage_url`/`linkedin_url`.
- Vào thẳng website công ty tìm link Facebook/LinkedIn thật (không đoán
  mò qua Google — tránh bắt nhầm trang công ty khác trùng tên).
- Không có website hoặc không có link social → để trống, không cố tìm
  cách khác. Chạy lại nhiều lần được, chỉ xử lý công ty còn thiếu, độc
  lập với pipeline crawl chính.

## `enrich_company_web_info.py` — vá website/tax_id

```bash
python enrich_company_web_info.py --limit 10   # test thử ít công ty
python enrich_company_web_info.py                # chạy full
```

Dùng cho công ty còn thiếu `website` hoặc `tax_id` sau khi crawl chính:

1. Tavily search — tìm kết quả web thật cho tên công ty.
2. Gemini — đọc kết quả Tavily, trích xuất `website`/`tax_id` ra JSON.
3. Chỉ lưu kết quả tin cậy `high`/`medium`, `tax_id` đúng định dạng mã số
   doanh nghiệp VN, `website` không thuộc mạng xã hội/trang tuyển
   dụng/trang tra MST. Không đủ tin cậy → để trống.
4. Domain tìm được không khớp token nào với tên công ty (nghi nhầm 2
   pháp nhân cùng thương hiệu, vd "AEON" vs "AEONMALL") → log cảnh báo,
   không tự lưu sai.
5. **`tax_id` trùng công ty khác đã có trong DB** (vd cùng công ty crawl
   từ cả 2 nguồn, tên ghi khác nhau) → **tự động gộp**: chuyển job/contact
   sang công ty gốc, xoá công ty trùng. Không tự xoá job trùng nội dung
   sau khi gộp (soát tay bằng `v_duplicate_job_candidates`).

Cần `TAVILY_API_KEY` (free tier tại https://tavily.com) và
`GEMINI_API_KEY` (https://aistudio.google.com) trong `.env`.

---

## Thêm ngành mới để crawl

Mở `config.py`, thêm vào `TOPCV_CATEGORIES` (hoặc `VIETNAMWORKS_CATEGORIES`):

```python
"business-analyst": {
    "label": "Business Analyst",
    "url": "https://www.topcv.vn/tim-viec-lam-business-analyst-<mã-category-thật>",
    "matching_industry": "Business Analysis",
},
```

Lấy URL category TopCV thật: vào https://www.topcv.vn/viec-lam → "Danh
mục Nghề" → chọn ngành → copy URL kết quả. Với VietnamWorks chỉ cần
`query` (chuỗi tìm kiếm), xem ví dụ có sẵn trong `VIETNAMWORKS_CATEGORIES`.

## Debug khi nguồn crawl đổi giao diện

Cả 2 adapter bám theo **pattern URL** và **nhãn tiếng Việt** (`"Mã số
thuế"`, `"Quy mô"`...) thay vì tên class CSS — bền hơn khi trang web
redesign. Nếu 1 ngày crawl ra 0 kết quả hoặc thiếu field:

1. Mở URL category/job/công ty bằng trình duyệt → "View Page Source"
   (không phải Inspect Element — cần đúng HTML server trả về).
2. So khớp lại pattern URL hoặc nhãn tiếng Việt trong
   `adapters/topcv.py`/`adapters/vietnamworks.py` với HTML thật, sửa
   cho khớp.
3. Cập nhật fixture HTML mẫu trong `tests/`, chạy lại
   `python tests/test_parse_and_normalize.py` để xác nhận trước khi
   crawl thật.
