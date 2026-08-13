# Job Crawler — Student Success

Crawler job từ **TopCV** và **VietnamWorks** (Data Analyst / Data Engineer /
Software Engineering, dễ mở rộng sang ngành khác), chuẩn hóa dữ liệu, crawl
sâu hồ sơ công ty (website, mã số thuế, quy mô, lĩnh vực, địa chỉ), lưu vào
PostgreSQL — expose ra ngoài qua 1 lớp **API FastAPI có auth**, **đã deploy
public trên Render**.

**Trạng thái hiện tại (08/2026):** pipeline crawl + API layer đã chạy ổn
định, bảo mật 3 lớp (API key dùng chung, đăng nhập JWT từng người, phân
quyền 3 cấp `user`/`ss_team`/`admin`), đăng ký công khai có xác thực
email qua Resend, CRUD liên hệ HR (`company_contacts`) có soft-delete,
kết nối Postgres qua connection pool, ghi audit trail (ai tạo/sửa job,
công ty, contact), đã lên production. Còn thiếu: frontend dashboard
(chưa làm), và vài field công ty còn thiếu dữ liệu (xem mục
[Tình trạng dữ liệu](#tình-trạng-dữ-liệu) bên dưới).

Ngoài pipeline crawl chính còn có 2 script độc lập, chạy khi cần:

- `get_company_fb_linkedin_link.py` — điền `fanpage_url`/`linkedin_url` bằng
  cách crawl website riêng của từng công ty.
- `enrich_company_web_info.py` — vá thêm `website`/`tax_id` cho công ty còn
  thiếu, bằng Tavily search + Gemini trích xuất.

## Kiến trúc

```
config.py                        <- ngành/category muốn crawl, delay, model AI...
models.py                        <- RawJobRecord: khuôn dữ liệu chung mọi adapter phải trả về
adapters/
  base.py                        <- interface chung (BaseAdapter)
  topcv.py                       <- adapter TopCV
  vietnamworks.py                <- adapter VietnamWorks
normalize.py                     <- dùng chung: parse lương, suy luận level, deadline, work_type
db.py                            <- dùng chung: mọi thao tác PostgreSQL
pipeline.py                      <- nối adapter -> normalize -> db
main.py                          <- CLI chạy crawl
get_company_fb_linkedin_link.py  <- script riêng: fanpage/LinkedIn
enrich_company_web_info.py       <- script riêng: website/tax_id qua Tavily + Gemini
api/                              <- lớp API FastAPI, bọc ngoài codebase crawler (xem mục riêng bên dưới)
  app.py                          <- entry point, đăng ký auth + CORS + router + lifespan (init/close connection pool)
  auth.py                         <- API key tĩnh, áp dụng cho toàn bộ endpoint
  security.py                     <- băm mật khẩu, ký/verify JWT access + refresh token
  email_service.py                <- gửi email xác thực đăng ký qua Resend
  deps.py                         <- get_db(): mượn/trả connection từ pool; get_current_user()/require_role()/require_admin(): xác thực JWT + phân quyền
  schemas.py                      <- Pydantic models (request/response JSON)
  crawl_runner.py                 <- chạy pipeline crawl ở nền, theo dõi qua run_id
  routers/                        <- jobs.py, companies.py, contacts.py, crawl.py, meta.py, auth.py
sql/schema.sql                   <- schema PostgreSQL đầy đủ (chạy 1 lần cho DB mới)
sql/migration_*.sql              <- vá DB cũ đã tạo trước khi có tính năng mới
tests/                           <- test parser + logic, không cần DB/internet
```

Muốn thêm nguồn crawl mới (ITviec...): viết `adapters/itviec.py` implement
`fetch_jobs()`, khai báo trong `SOURCES` ở `main.py` — không cần sửa
`normalize.py`, `db.py`, `pipeline.py`.

---

## Cài đặt

### 1. PostgreSQL

- **Windows**: https://www.postgresql.org/download/windows/
- **macOS**: `brew install postgresql@16 && brew services start postgresql@16`
- **Linux**: `sudo apt install postgresql postgresql-contrib`

Hoặc dùng managed Postgres cloud (vd Supabase) — không cần cài gì, chỉ cần
điền đúng thông tin kết nối vào `.env` ở bước 4.

### 2. Tạo database

```bash
psql -U postgres
```

Trong dấu nhắc `postgres=#` (copy-paste dòng dưới, đừng gõ tay — có dấu
em-dash `—`):

```sql
CREATE DATABASE "Student Success — Job Postings & Company Contacts";
```

Gõ `\q` để thoát.

### 3. Cài Python + thư viện

Cần Python 3.9+.

```bash
python3 -m venv venv
source venv/bin/activate        # macOS/Linux
venv\Scripts\activate           # Windows CMD

pip install -r requirements.txt
```

### 4. Cấu hình `.env`

```bash
cp .env.example .env      # macOS/Linux
copy .env.example .env    # Windows CMD
```

Sửa `PGPASSWORD` thành mật khẩu PostgreSQL thật. `PGDATABASE` đã điền sẵn
đúng tên database ở bước 2, không cần sửa.

Nếu định chạy `enrich_company_web_info.py`, điền thêm `TAVILY_API_KEY` và
`GEMINI_API_KEY` (xem [mục riêng](#enrich_company_web_infopy---vá-websitetax_id) bên dưới để biết cách lấy key).

Nếu định chạy **API layer** (`uvicorn api.app:app`), bắt buộc điền thêm
`API_KEY` và `ALLOWED_ORIGINS` — xem [mục API layer](#api-layer-fastapi)
bên dưới. Muốn dùng lớp đăng nhập JWT (`POST /jobs`, `PATCH /jobs/{id}`,
`POST /companies`, CRUD `/companies/{id}/contacts`, `POST /crawl` bắt
buộc đăng nhập/đúng role từ 08/2026) thì cần thêm `JWT_SECRET_KEY`. Muốn
bật luồng đăng ký công khai (`POST /auth/register`) gửi được email xác
thực thật thì cần thêm `RESEND_API_KEY`/`EMAIL_FROM`/`API_BASE_URL` (xem
[mục Đăng ký công khai](#đăng-ký-công-khai--xác-thực-email) bên dưới).
`DB_POOL_MIN`/`DB_POOL_MAX` có giá trị mặc định sẵn (2/20), chỉ cần sửa
nếu Postgres phía deploy giới hạn connection thấp hơn.

### 5. Tạo bảng

```bash
python main.py init-db
```

Kỳ vọng: `✅ Đã tạo/cập nhật schema trong database.`

> Nếu bạn có DB tạo từ bản rất cũ (trước khi có cột `tax_id` hoặc
> `work_type`/`deadline`), chạy thêm các file trong `sql/migration_*.sql`
> tương ứng — xem comment đầu mỗi file để biết chạy khi nào.
>
> **Quan trọng nếu định bật lớp ghi có JWT** (`POST`/`PATCH` bắt buộc đăng
> nhập, xem mục API layer): phải chạy đúng thứ tự sau (migration sau phụ
> thuộc bảng/cột migration trước):
>
> ```bash
> psql -U postgres -d "..." -f sql/migration_add_auth.sql
> psql -U postgres -d "..." -f sql/migration_add_audit_columns.sql
> psql -U postgres -d "..." -f sql/migration_add_role_hierarchy.sql
> psql -U postgres -d "..." -f sql/migration_add_email_verification.sql
> ```
>
> Thiếu 1 trong 4 file trên sẽ làm `POST`/`PATCH /jobs`,
> `POST /companies`, CRUD `/companies/{id}/contacts` hoặc
> `POST /auth/register` lỗi 500 (thiếu bảng/cột).

### 6. Chạy test (không cần DB/internet)

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

# Giới hạn theo SỐ LƯỢNG JD thay vì theo trang (tiện lấy mẫu nhỏ để test,
# không cần tính "mấy trang thì đủ N job"):
python main.py crawl --source topcv --category data-analyst --max-jobs 20
```

- `--source`: `topcv` (mặc định) hoặc `vietnamworks`.
- `--category`: ngành muốn crawl. Xem danh sách có sẵn trong `config.py`
  (`TOPCV_CATEGORIES` / `VIETNAMWORKS_CATEGORIES`). Hiện có: `data-analyst`,
  `data-engineer`, `software-engineering`.
- `--pages`: số trang tối đa. Bắt đầu với số nhỏ (2-3) để test. 1 trang
  TopCV ~20-25 job, 1 trang VietnamWorks ~50 job.
- `--max-jobs`: giới hạn TỔNG SỐ JD sẽ crawl, dừng ngay khi đủ — không cần
  đợi hết `--pages`. Dùng riêng `--max-jobs` (không kèm `--pages`) sẽ tự
  động crawl đủ số trang cần thiết để đạt số lượng đó. Dùng CÙNG lúc cả 2
  cờ -> dừng ở điều kiện nào tới trước. Cách dừng không tốn request thừa:
  `adapter.fetch_jobs()` sinh job theo từng trang (generator), dừng vòng
  lặp ở `pipeline.py` ngay khi đủ `--max-jobs` sẽ khiến adapter KHÔNG gọi
  thêm trang mới nữa.

Mỗi lần crawl, hệ thống tự động:

- Bỏ qua job đã crawl trước đó (theo link JD gốc), nhưng **vẫn vá thêm**
  `work_type`/`deadline`/nội dung JD cho job cũ nếu trước đó còn thiếu.
- Crawl sâu vào trang chi tiết job để lấy `work_type`, hạn ứng tuyển, mô tả
  công việc, yêu cầu, quyền lợi, kỹ năng cần có.
- Crawl sâu vào trang hồ sơ công ty (chỉ lần đầu gặp, hoặc khi còn thiếu
  field) để lấy website thật, mã số thuế, quy mô, lĩnh vực, địa chỉ.
- Match công ty **ưu tiên theo mã số thuế** — nếu 2 job cùng 1 công ty
  nhưng tên viết khác nhau, vẫn nhận ra là 1 công ty, không tạo trùng.

**⚠️ Chưa an toàn khi chạy song song 2 lượt crawl cùng lúc** (vd vừa chạy
CLI vừa gọi `POST /crawl`, hoặc bấm crawl 2 lần liên tiếp trước khi lượt
đầu kịp `commit()`) — có thể tạo ra 2 job trùng `source_url` do race
condition ở bước "check trùng rồi mới insert" trong `pipeline.py`. Đã gặp
thực tế 1 lần (2 job cùng link, timestamp cách nhau vài giây). Cách xử lý
tạm thời: chỉ chạy 1 lượt crawl tại 1 thời điểm; soát dọn bằng
`v_duplicate_job_candidates` (xem mục Xem kết quả) nếu nghi trùng. Nâng
cấp đúng cần thêm advisory lock theo `source_url` hoặc unique constraint ở
tầng DB — chưa làm, xem mục [Việc còn tồn đọng](#việc-còn-tồn-đọng).

## Xem kết quả

```bash
python main.py stats
```

Hoặc trực tiếp bằng `psql`:

```sql
-- 10 job mới nhất
SELECT job_title, company_id, salary_min, salary_max, salary_type
FROM job_postings ORDER BY created_at DESC LIMIT 10;

-- Thông tin công ty đã crawl sâu
SELECT company_name, tax_id, website, company_size, industry
FROM companies ORDER BY created_at DESC LIMIT 10;

-- Đếm job theo ngành
SELECT matching_industry, count(*) FROM job_postings GROUP BY matching_industry;

-- Soát job nghi trùng (khác link nguồn nhưng cùng nội dung)
SELECT * FROM v_duplicate_job_candidates;
```

---

## API layer (FastAPI)

Lớp API bọc ngoài codebase crawler hiện có — không sửa gì `main.py` (CLI
crawl cũ vẫn chạy y hệt), chỉ thêm nhóm hàm query mới cuối `db.py` (mục
"QUERY LAYER CHO API").

### Chạy local

```bash
uvicorn api.app:app --reload --port 8000
```

Mọi request cần header `X-API-Key: <giá trị API_KEY trong .env>`, kể cả
`/health`. Không có key hoặc sai key -> `401`.

Swagger UI (`/docs`) và ReDoc (`/redoc`) **mặc định TẮT** — 2 route này
không đi qua được lớp kiểm tra API key (giới hạn kỹ thuật của FastAPI, ai
cũng xem được cấu trúc API dù không lộ dữ liệu thật), nên tắt hẳn theo
nguyên tắc an toàn mặc định. Cần xem Swagger lúc dev local: set
`ENABLE_DOCS=true` trong `.env`. Không bật trên môi trường public trừ khi
đang debug tạm thời.

### Bảo mật — 3 lớp xếp chồng

1. **API key tĩnh** (`api/auth.py`) — 1 key dùng chung cho mọi request,
   gửi qua header `X-API-Key` (hoặc query `?api_key=` để tiện test,
   không khuyến khích dùng ở frontend thật). Áp dụng cho **toàn bộ**
   endpoint, kể cả các route chỉ đọc. Thiếu `API_KEY` trong `.env` ->
   server tự chặn hết (fail-closed), không âm thầm mở toang.
2. **Đăng nhập JWT từng người** (`api/security.py`, `api/routers/auth.py`,
   thêm 08/2026) — xác định **AI** đang gọi API, KHÁC lớp API key ở trên
   (chỉ xác nhận "client này là frontend của mình", không phân biệt
   người dùng). Luồng: `POST /auth/login` (email + password) trả
   `access_token` (JWT, sống 30 phút) + `refresh_token` (sống dài hơn,
   xoay vòng) -> gửi kèm `Authorization: Bearer <access_token>` cho các
   route cần đăng nhập -> `POST /auth/refresh` để lấy access token mới
   khi hết hạn.
3. **Phân quyền 3 cấp** (`ROLE_HIERARCHY`/`require_role()` trong
   `api/deps.py`, thêm 08/2026) — mỗi tài khoản có đúng 1 role, cấp cao
   thoả mọi route yêu cầu cấp thấp hơn:

   | Role | Cấp | Được làm |
   |---|---|---|
   | `user` | 0 (thấp nhất) | Chỉ xem/lọc job (`GET /jobs`) — mặc định khi tự đăng ký qua `POST /auth/register` |
   | `ss_team` | 1 | + tạo/sửa job/company, CRUD liên hệ HR (`/companies/{id}/contacts`), xem danh sách tài khoản |
   | `admin` | 2 (cao nhất) | + trigger crawl (`POST /crawl`), tạo tài khoản hộ (`POST /auth/users`), đổi role người khác |

   Có 2 cách tạo tài khoản: **tự đăng ký công khai** (`POST
   /auth/register`, luôn ra role `user`, phải xác thực email trước khi
   login được — xem mục [Đăng ký công khai](#đăng-ký-công-khai--xác-thực-email))
   hoặc **admin tạo hộ** (`POST /auth/users`, chọn được role bất kỳ,
   không cần xác thực email). Muốn nâng `user` lên `ss_team` phải nhờ
   admin gọi `PATCH /auth/users/{id}/role` — không tự nâng được.

Mọi thao tác ghi qua JWT được ghi lại vào cột `created_by`/`updated_by`
của `job_postings`/`companies`/`company_contacts` (audit trail, xem
`sql/migration_add_audit_columns.sql` và
`sql/migration_add_role_hierarchy.sql`) — job/công ty tạo qua crawl tự
động (không qua JWT) có `created_by = NULL`.

**CORS siết theo domain** — chỉ domain liệt kê trong `ALLOWED_ORIGINS`
(phân tách dấu phẩy) mới gọi được từ trình duyệt. Để trống -> không
domain nào gọi được (fail-closed).

### Đăng ký công khai + xác thực email

Thêm 08/2026 (`api/email_service.py`, `sql/migration_add_email_verification.sql`).
Ai cũng gọi được `POST /auth/register` (không cần JWT/API key theo
người), luôn tạo tài khoản role `user`, `email_verified=false`. Server
gửi email chứa link xác thực qua **Resend** (miễn phí, domain mặc định
`onboarding@resend.dev` — team hiện chưa có domain riêng verify với
Resend). `POST /auth/login` **chặn** nếu email chưa xác thực. Link hết
hạn sau 24h, xin gửi lại qua `POST /auth/resend-verification`.

Nếu Resend gửi lỗi (rate-limit, mạng chập chờn...) — tài khoản **vẫn đã
tạo thành công** trong DB, không mất dữ liệu, chỉ cần gọi lại
`resend-verification` sau, không cần đăng ký lại từ đầu.

Bắt buộc `RESEND_API_KEY` trong `.env`/biến môi trường Render để gửi
được email thật (thiếu -> log lỗi, tài khoản vẫn tạo nhưng email không
tới, xem docstring `send_verification_email()`). `API_BASE_URL` phải
trỏ đúng domain API thật đang chạy (vd
`https://scrap-jd-api.onrender.com`, KHÔNG có `/docs` hay dấu `/` cuối)
để link trong email đúng.

### Endpoints hiện có

| Method | Path                                                                            | Việc                                                                                                               |
| ------ | ------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| GET    | `/jobs?industry=&province=&level=&work_type=&status=&keyword=&limit=&offset=` | List job, filter + phân trang — chỉ cần API key                                                                    |
| GET    | `/jobs/{job_id}`                                                              | Chi tiết 1 job (kèm parsed_content) — chỉ cần API key                                                             |
| POST   | `/jobs`                                                                       | Tạo job thủ công (company phải có sẵn) — **role ss_team+**, ghi `created_by`                                       |
| PATCH  | `/jobs/{job_id}`                                                              | Sửa job (đổi trạng thái, lương, ghi chú...) — **role ss_team+**, ghi `updated_by`. Dùng `job_status:"CLOSED"` để "xoá mềm" |
| GET    | `/companies?keyword=&province=&has_social=&limit=&offset=`                    | List công ty, filter + phân trang — chỉ cần API key                                                               |
| GET    | `/companies/{company_id}`                                                     | Chi tiết công ty (kèm danh sách job) — chỉ cần API key                                                          |
| POST   | `/companies`                                                                  | Tạo công ty thủ công (tự dùng lại nếu trùng tax_id) — **role ss_team+**, ghi `created_by`/`updated_by`             |
| GET    | `/companies/{company_id}/contacts?include_inactive=`                          | List liên hệ HR của 1 công ty — **role ss_team+**                                                                |
| POST   | `/companies/{company_id}/contacts`                                            | Thêm liên hệ HR — **role ss_team+**                                                                              |
| PATCH  | `/companies/{company_id}/contacts/{contact_id}`                               | Sửa liên hệ HR — **role ss_team+**                                                                                |
| DELETE | `/companies/{company_id}/contacts/{contact_id}`                               | Xoá mềm liên hệ HR (`is_active=false`, giữ lịch sử) — **role ss_team+**                                          |
| POST   | `/crawl`                                                                      | Kích hoạt crawl nền — body`{"source": "topcv", "category": "data-analyst", "pages"?, "max_jobs"?}` — **role admin** |
| GET    | `/crawl/{run_id}`                                                             | Theo dõi tiến độ/kết quả 1 lượt crawl — chỉ cần API key                                                       |
| GET    | `/stats`                                                                      | Tổng job/công ty, tỷ lệ có social, phân bố ngành/nguồn — chỉ cần API key                                    |
| GET    | `/sources`                                                                    | Danh sách source/category có sẵn (đọc từ`config.py`) — frontend render dropdown                            |
| GET    | `/health`                                                                     | Health check đơn giản (vẫn cần API key)                                                                        |
| POST   | `/auth/register`                                                              | Tự đăng ký (luôn role `user`), gửi email xác thực — không cần đăng nhập trước                                    |
| GET    | `/auth/verify-email?token=`                                                   | Bấm từ link trong email, kích hoạt tài khoản vừa đăng ký                                                        |
| POST   | `/auth/resend-verification`                                                  | Xin gửi lại email xác thực nếu token cũ hết hạn/thất lạc                                                        |
| POST   | `/auth/login`                                                                 | Đăng nhập, trả `access_token` (30 phút) + `refresh_token`. Chặn nếu email chưa xác thực                        |
| POST   | `/auth/refresh`                                                               | Xoay vòng lấy access token mới                                                                                  |
| POST   | `/auth/logout`                                                                | Thu hồi refresh token hiện tại                                                                                  |
| GET    | `/auth/me`                                                                    | Thông tin tài khoản đang đăng nhập                                                                              |
| POST   | `/auth/change-password`                                                      | Tự đổi mật khẩu                                                                                                  |
| POST   | `/auth/users`                                                                 | Admin tạo hộ tài khoản mới (chọn được role) — **role admin**                                                    |
| GET    | `/auth/users`                                                                 | Danh sách toàn bộ tài khoản — **role ss_team+**                                                                  |
| PATCH  | `/auth/users/{id}/role`                                                      | Đổi role của 1 tài khoản khác (không tự đổi role chính mình) — **role admin**                                    |

Xem chi tiết body/response từng endpoint trong `API_README.md`.

### Giới hạn đã biết

- **Trạng thái crawl (`POST /crawl`) lưu trong RAM**, mất khi restart
  server, không đồng bộ nếu chạy nhiều worker (`--workers > 1`). Đủ dùng ở
  quy mô hiện tại. Nâng cấp sau: Celery + Redis hoặc RQ.
- **Không giới hạn số crawl chạy song song** — xem cảnh báo race condition
  ở mục [Crawl job](#crawl-job) bên trên. `require_admin` ở `POST /crawl`
  giảm rủi ro spam nhưng KHÔNG tự chặn 2 lượt chạy cùng lúc.
- **`GET /docs`/`/redoc`/`/openapi.json` không đi qua được API key** (giới
  hạn kỹ thuật FastAPI) — mặc định tắt hẳn, chỉ bật `ENABLE_DOCS=true`
  lúc dev local.
- **Auth API key vẫn là 1 khoá dùng chung** ở tầng "máy gọi máy" — muốn
  biết chính xác người nào gọi thì phải qua JWT (bắt buộc ở route ghi và
  route xem thông tin nhạy cảm, xem mục Bảo mật).
- **`RESEND_API_KEY` chưa cấu hình → email xác thực không gửi được**,
  nhưng tài khoản vẫn tạo thành công trong DB (không mất dữ liệu) — xem
  mục Đăng ký công khai.
- **Domain gửi email vẫn là domain test mặc định của Resend**
  (`onboarding@resend.dev`) — team chưa có domain riêng verify với
  Resend, đổi qua biến môi trường `EMAIL_FROM` khi có domain thật.

---

## Deploy production

Backend đã deploy thật, public trên internet:

- **Repo**: GitHub private (`Koaito/scrap-jd`) — `.env` không commit lên
  git (`.gitignore` đã chặn).
- **API server**: Render Web Service, build từ repo trên, đọc 10 biến môi
  trường (Postgres, `API_KEY`, `ALLOWED_ORIGINS`, Tavily/Gemini key) từ
  cấu hình Render — **không phải** từ file `.env` (file đó chỉ dùng local).
- **URL public**: `https://scrap-jd-api.onrender.com`

**Chưa làm — cần làm khi bắt đầu phần frontend:**

1. Deploy dashboard (frontend) lên Vercel, lấy domain thật.
2. Quay lại Render, cập nhật `ALLOWED_ORIGINS` cho khớp domain Vercel đó
   (hiện tại `ALLOWED_ORIGINS` trên Render đang trỏ vào domain
   placeholder/localhost, CHƯA có domain frontend thật).

---

## Tình trạng dữ liệu

Snapshot tại thời điểm viết (176 job / 122 công ty, crawl 2 ngành *Data
Analysis* + *Data Engineer* trên cả 2 nguồn):

| Field                                                          | Độ phủ |
| -------------------------------------------------------------- | --------- |
| `job_postings.work_type` / `deadline` / `parsed_content` | 97%       |
| `job_postings.salary_min` (không tính "Thoả thuận")      | 31%       |
| `companies.tax_id`                                           | 98%       |
| `companies.website`                                          | 86%       |
| `companies.industry`                                         | 81%       |
| `companies.fanpage_url`                                      | 57%       |
| `companies.company_size`                                     | 56%       |
| `companies.address`                                          | 44%       |
| `companies.linkedin_url`                                     | 31%       |

Không có job hay công ty nào trùng lặp thật (0 `content_hash` trùng, 0
`tax_id` trùng, 0 `source_url` trùng) tại thời điểm snapshot này.

### Bug đã sửa: sai đơn vị lương VietnamWorks (08/2026)

`normalize_salary()` trước đây luôn nhân số VNĐ với 1.000.000 (giả định
mọi số đều ở đơn vị "triệu"). VietnamWorks có 2 định dạng `prettySalary`
khác nhau cho cùng đơn vị VNĐ — `"15tr-30tr ₫/tháng"` (có hậu tố "tr", đúng
là triệu) và `"12,000-30,000 ₫/tháng"` (số đã ở đơn vị nghìn đồng, KHÔNG
có hậu tố) — nhân cứng 1 kiểu cho cả 2 khiến case thứ 2 bị lệch 1000 lần
(ra hàng chục tỷ thay vì hàng chục triệu). Phát hiện qua đối chiếu dữ liệu
thật đã crawl (1 outlier salary_max = 30 tỷ). Đã sửa bằng cách suy luận
hệ số nhân theo **độ lớn của chính con số** thay vì áp 1 hằng số cho cả
chuỗi — xem docstring `_vnd_multiplier()` trong `normalize.py`. Đã test
lại toàn bộ dữ liệu thật đã crawl: chỉ đúng 1 bản ghi thay đổi (case lỗi
trên), không ảnh hưởng bản ghi nào khác.

**Sửa code chỉ áp dụng cho job crawl MỚI SAU NÀY** — job đã insert từ
trước (kể cả bản ghi lỗi 30 tỷ đó) vẫn còn sai trong DB. Hiện **chưa có
script tự động vá lại dữ liệu cũ** — cần soát tay bằng SQL (vd tìm
`salary_max` bất thường lớn) hoặc xoá/crawl lại job liên quan nếu số
lượng ảnh hưởng nhỏ (tại thời điểm phát hiện: 1 bản ghi duy nhất).

### Việc còn tồn đọng

- **Chưa sửa lỗi trùng job do race condition** (xem cảnh báo ở mục Crawl
  job) — mới phát hiện, chưa vá.
- **9 job có `required_skills` bị lặp phần tử** trong `parsed_content` (vd
  cùng 1 kỹ năng xuất hiện 2-3 lần) — nghi do artifact khi parse DOM, chưa
  dedupe ở tầng `normalize`/`pipeline`.
- **Company_size (56%), address (44%), linkedin_url (31%) còn thiếu
  nhiều** — chạy thêm `get_company_fb_linkedin_link.py` /
  `enrich_company_web_info.py` để vá, hoặc chấp nhận vì nguồn gốc không
  luôn có sẵn field này (vd VietnamWorks không hiển thị mã số thuế công
  ty trên trang profile).
- **Dữ liệu hiện tại mới chỉ từ 1 lượt crawl, 2 ngành, 2 nguồn** — quy mô
  còn nhỏ so với mục tiêu dự án, cần crawl thêm định kỳ để có dữ liệu đủ
  lớn cho dashboard.
- **Frontend dashboard chưa làm** — backend đã sẵn sàng (API + JWT auth +
  phân quyền 3 cấp + connection pool + audit trail + đăng ký công khai +
  deploy), bước tiếp theo là xây frontend gọi vào các endpoint đã có
  (cần màn hình đăng nhập/đăng ký để dùng được các route ghi, xem mục
  API layer).
- **Chưa verify domain riêng với Resend** — email xác thực đang gửi từ
  domain test mặc định `onboarding@resend.dev`, cần domain thật của
  team để chuyên nghiệp hơn (xem mục Đăng ký công khai).

---

## `get_company_fb_linkedin_link.py` — điền fanpage/LinkedIn

```bash
python get_company_fb_linkedin_link.py --limit 10   # test thử ít công ty
python get_company_fb_linkedin_link.py               # chạy full
```

- Chỉ xử lý công ty **đã có `website`** (từ pipeline crawl chính) và **còn
  thiếu** `fanpage_url` hoặc `linkedin_url` — nên cần crawl job ít nhất 1
  lần trước để có `website` làm điểm bắt đầu.
- Vào thẳng website công ty, tìm link Facebook/LinkedIn thật trong trang
  (không đoán mò qua Google — tránh bắt nhầm trang công ty khác trùng tên).
- Công ty không có website, hoặc website không có link social nào → để
  trống, không cố tìm cách khác.
- Chạy lại nhiều lần được — chỉ xử lý công ty còn thiếu.
- Độc lập với pipeline crawl chính, lỗi/timeout ở 1 website không ảnh hưởng
  crawl job.

## `enrich_company_web_info.py` — vá website/tax_id

```bash
python enrich_company_web_info.py --limit 10   # test thử ít công ty
python enrich_company_web_info.py                # chạy full
```

Dùng cho công ty **còn thiếu `website` hoặc `tax_id`** sau khi crawl chính
(vd trang hồ sơ công ty trên nguồn crawl không có sẵn 2 field này). Cách
hoạt động:

1. Tavily search API — tìm kết quả web thật cho tên công ty.
2. Gemini — đọc kết quả Tavily, trích xuất `website`/`tax_id` ra JSON.
3. Chỉ lưu kết quả có độ tin cậy `high`/`medium`, `tax_id` đúng định dạng
   mã số doanh nghiệp VN, `website` không thuộc domain mạng xã hội/trang
   tuyển dụng/trang tra cứu MST. Không đủ tin cậy → để trống, không đoán mò.
4. Nếu domain tìm được không khớp token nào với tên công ty (dấu hiệu nhầm
   2 pháp nhân cùng thương hiệu, vd "AEON" và "AEONMALL"), log cảnh báo để
   tự kiểm tra tay — không tự động lưu sai, cũng không tự động xoá.
5. **Nếu `tax_id` tra được trùng với 1 công ty khác đã có trong DB** (vd
   cùng công ty được crawl từ TopCV lẫn VietnamWorks với tên ghi khác
   nhau, tạo thành 2 row riêng) — script **tự động gộp 2 công ty lại**:
   chuyển toàn bộ job và contact sang công ty đã có `tax_id` gốc, xoá công
   ty trùng. Không tự xoá job trùng nội dung sau khi gộp (dùng
   `v_duplicate_job_candidates` để soát tay).

Cần `TAVILY_API_KEY` (free tier tại https://tavily.com, không cần thẻ
thanh toán) và `GEMINI_API_KEY` (https://aistudio.google.com) trong `.env`.

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

Lấy URL category TopCV thật: vào https://www.topcv.vn/viec-lam → "Danh mục
Nghề" → chọn ngành → copy URL kết quả.

Với VietnamWorks chỉ cần `query` (chuỗi tìm kiếm), không cần URL category
riêng — xem ví dụ có sẵn trong `VIETNAMWORKS_CATEGORIES`.

## Debug khi nguồn crawl đổi giao diện

Cả 2 adapter bám theo **pattern URL** (link job/công ty) và **nhãn tiếng
Việt** (`"Mã số thuế"`, `"Quy mô"`...) thay vì tên class CSS — bền hơn khi
trang web redesign. Nếu 1 ngày crawl ra 0 kết quả hoặc thiếu field:

1. Mở URL category/job/công ty đó bằng trình duyệt → "View Page Source"
   (không phải Inspect Element — cần đúng HTML server trả về).
2. So khớp lại pattern URL hoặc nhãn tiếng Việt trong `adapters/topcv.py`
   hoặc `adapters/vietnamworks.py` với HTML thật vừa xem, sửa lại cho khớp.
3. Cập nhật fixture HTML mẫu tương ứng trong `tests/`, chạy lại
   `python tests/test_parse_and_normalize.py` để xác nhận trước khi crawl
   thật.
