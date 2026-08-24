# API Layer (FastAPI) — hướng dẫn chạy

Lớp API này **bọc ngoài** codebase crawler hiện có (`adapters/`, `normalize.py`,
`db/`, `pipeline.py`) — không sửa gì các file đó ngoại trừ thêm 1 nhóm
hàm query mới riêng cho API (nằm rải theo domain trong `db/`, ví dụ
`db/jobs.py`, `db/companies.py`... — trước 08/2026 gộp chung 1 file
`db.py` duy nhất, đã tách theo domain, xem "Kiến trúc" trong
`README.md`). `main.py` (CLI crawl cũ) giữ nguyên 100%, chạy song song
không xung đột với API. Quy trình crawl/vá dữ liệu xem `README.md`; file
này chỉ nói về lớp API.

## Mục lục

- [Cài đặt thêm](#cài-đặt-thêm)
- [Chạy](#chạy)
- [Bảo mật — 3 lớp xếp chồng](#bảo-mật--3-lớp-xếp-chồng)
- [Endpoints hiện có](#endpoints-hiện-có)
- [Chi tiết 1 số endpoint](#chi-tiết-1-số-endpoint)
- [Phân quyền — role hierarchy](#phân-quyền--role-hierarchy)
- [Audit trail](#audit-trail--ai-tạosửa-job-công-ty-liên-hệ)
- [Connection pool](#connection-pool)
- [Deploy production](#deploy-production)
- [Giới hạn đã biết](#giới-hạn-đã-biết)
- [Việc chưa làm](#việc-chưa-làm-để-team-quyết-định-có-cần-không)

---

## Cài đặt thêm

```bash
pip install -r requirements.txt   # đã có fastapi + uvicorn
```

Điền thêm trong `.env` (bắt buộc — xem `.env.example`):

- `API_KEY`, `ALLOWED_ORIGINS` — bắt buộc để chạy API ở mức tối thiểu.
- `JWT_SECRET_KEY` — bắt buộc nếu dùng lớp đăng nhập từng người (`/auth/*`
  và các route ghi `POST`/`PATCH` bên dưới).
- `RESEND_API_KEY`, `EMAIL_FROM`, `API_BASE_URL` — bắt buộc nếu muốn
  `POST /auth/register` gửi được email xác thực thật.
- `DB_POOL_MIN`/`DB_POOL_MAX` — có giá trị mặc định (2/20), chỉ cần sửa
  nếu Postgres phía deploy giới hạn connection thấp hơn.

## Chạy

```bash
uvicorn api.app:app --reload --port 8000
```

Swagger UI (`/docs`) **mặc định TẮT** (xem mục Bảo mật). Set
`ENABLE_DOCS=true` trong `.env` để bật lúc dev local, rồi mở
`http://localhost:8000/docs` — nhớ bấm nút khoá (🔒) góc trên bên phải,
nhập `API_KEY`. Các route ghi (POST/PATCH) cần thêm
`Authorization: Bearer <access_token>` — lấy token qua `POST /auth/login`
trước.

## Bảo mật — 3 lớp xếp chồng

### Lớp 1 — API key tĩnh (mọi request)

Mọi endpoint (kể cả `/health`) yêu cầu header `X-API-Key` đúng giá trị
`API_KEY` trong `.env`. Fail-closed: quên cấu hình `API_KEY` → server tự
chặn hết, không âm thầm mở toang. Khoá "máy gọi máy" dùng chung cho cả
team, xác nhận "client này là frontend của mình" — KHÔNG phân biệt được
người dùng cụ thể nào đang gọi. Chi tiết: `api/auth.py`.

### Lớp 2 — Đăng nhập JWT từng người

Xác nhận AI thật đang gọi — dùng bảng `app_users` (`password_hash`,
`role`) và bảng `auth_refresh_tokens`. Chi tiết: `api/security.py`,
`api/deps.py`.

Luồng:

1. Có tài khoản qua **tự đăng ký** (`POST /auth/register` — luôn role
   `user`, phải xác thực email trước khi login được) hoặc **admin tạo
   hộ** (`POST /auth/users` — chọn được role, không cần xác thực email).
2. `POST /auth/login` (email + password) → trả `access_token` (JWT, sống
   30 phút) + `refresh_token` (sống dài hơn, xoay vòng mỗi lần dùng).
3. Gửi kèm `Authorization: Bearer <access_token>` cho route cần biết
   user thật hoặc route quản lý tài khoản (`/auth/me`, đổi mật khẩu...).
4. `POST /auth/refresh` khi access token hết hạn, lấy cặp token mới.

### Lớp 3 — Phân quyền 3 cấp

`ROLE_HIERARCHY`/`require_role()` trong `api/deps.py`. Mỗi tài khoản có
đúng 1 role, cấp cao thoả mọi route yêu cầu cấp thấp hơn (so sánh theo
bậc, không so khớp đúng 1 chuỗi):

| Role | Cấp | Được làm |
|---|---|---|
| `user` | 0 | Chỉ route `GET` đọc dữ liệu + `/me/*` (ứng tuyển/lưu job của chính mình) — mặc định khi tự đăng ký |
| `ss_team` | 1 | + `POST`/`PATCH /jobs`, `POST /companies`, CRUD `/companies/{id}/contacts`, `GET /auth/users` |
| `admin` | 2 | + `POST /crawl`, `POST /auth/users`, `PATCH /auth/users/{id}/role`, `PATCH /auth/users/{id}/active-status` |

Nâng `user` lên `ss_team` phải nhờ admin gọi `PATCH
/auth/users/{id}/role` — không có cách tự nâng. Admin **không tự đổi
được role chính mình** (chặn cứng ở route) — cần admin khác thực hiện,
hoặc sửa thẳng DB nếu chỉ có 1 admin duy nhất.

Chọn `role admin` riêng cho `POST /crawl` (chặt hơn `POST /jobs`) vì
kích hoạt crawl tốn tài nguyên server thật (network + CPU parse vài
phút) — hạn chế để tránh spam nhiều lượt crawl chạy song song.

### CORS

Chỉ domain liệt kê trong `ALLOWED_ORIGINS` (`.env`, phân tách bằng dấu
phẩy) mới gọi được từ trình duyệt. Để trống → không domain nào gọi được
(fail-closed, giống `API_KEY`).

### Swagger/ReDoc/openapi.json

3 route duy nhất KHÔNG đi qua được lớp `API_KEY` (giới hạn kỹ thuật của
FastAPI, route Starlette thuần). Vì vậy mặc định tắt hẳn để không lộ cấu
trúc API ra ngoài; chỉ bật bằng `ENABLE_DOCS=true` lúc dev local. Chi
tiết: docstring `api/app.py`.

### Khoá tạm tài khoản do đăng nhập sai nhiều lần

`POST /auth/login` tự đếm số lần sai mật khẩu liên tiếp
(`failed_login_count`) — sai đủ 5 lần thì khoá tạm tài khoản đó 15 phút
(`locked_until`, xem `FAILED_LOGIN_LOCK_THRESHOLD`/
`FAILED_LOGIN_LOCK_MINUTES` trong `api/security.py`), trả `403`. Đăng
nhập đúng mật khẩu bất kỳ lúc nào tự reset bộ đếm về 0. Khác
`is_active=false` (vô hiệu hoá vĩnh viễn do admin) — khoá tạm tự hết hạn
sau 15 phút, không cần admin can thiệp.

### Đăng ký công khai + xác thực email

`POST /auth/register`, `GET /auth/verify-email`, `POST
/auth/resend-verification` là **3 route DUY NHẤT trong toàn bộ API
không cần `X-API-Key`** (tách riêng thành `auth.public_router`). Lý do:
`GET /auth/verify-email` được bấm thẳng từ email, trình duyệt không có
cách nào tự gắn header `X-API-Key` vào request đó.

Luôn tạo role `user`, `email_verified=false`. Server gửi email xác thực
qua Resend. `POST /auth/login` chặn nếu email chưa xác thực. Link hết
hạn 24h, gửi lại qua `POST /auth/resend-verification`. Resend lỗi (rate
limit, mạng chập chờn) → tài khoản **vẫn tạo thành công**, gọi lại
`resend-verification` sau là được.

### Quên / đặt lại mật khẩu

`POST /auth/forgot-password` (email) → luôn trả cùng 1 message chung
chung dù email có tồn tại hay không (chống dò email hàng loạt) → nếu
email tồn tại, gửi link đặt lại qua Resend, token sống 1 giờ, dùng đúng
1 lần.

`POST /auth/reset-password` (token + mật khẩu mới) → đổi mật khẩu, sau
đó thu hồi toàn bộ refresh token cũ của user (phiên đăng nhập cũ bị đá
ra ngay, không đợi access token 30 phút tự hết hạn).

---

## Endpoints hiện có

| Method | Path | Việc | Auth |
|---|---|---|---|
| GET | `/jobs?industry=&province=&level=&work_type=&status=&keyword=&limit=&offset=` | List job, filter + phân trang | API key |
| GET | `/jobs/{job_id}` | Chi tiết 1 job (kèm parsed_content) | API key |
| POST | `/jobs` | Tạo job thủ công (company phải có sẵn), idempotent | API key + JWT (ss_team+) |
| PATCH | `/jobs/{job_id}` | Sửa job tự do (đổi trạng thái/lương/ghi chú...). Dùng `job_status:"CLOSED"` để "xoá mềm" | API key + JWT (ss_team+) |
| GET | `/jobs/{job_id}/applications` | Ai đã ứng tuyển job này (full_name/email/phone) | API key + JWT (ss_team+) |
| GET | `/companies?keyword=&province=&has_social=&limit=&offset=` | List công ty, filter + phân trang | API key |
| GET | `/companies/{company_id}` | Chi tiết công ty (kèm danh sách job) | API key |
| POST | `/companies` | Tạo công ty thủ công (tự dùng lại công ty đã có nếu trùng tax_id) | API key + JWT (ss_team+) |
| GET | `/companies/{company_id}/contacts?include_inactive=` | List liên hệ HR của 1 công ty | API key + JWT (ss_team+) |
| POST | `/companies/{company_id}/contacts` | Thêm liên hệ HR | API key + JWT (ss_team+) |
| PATCH | `/companies/{company_id}/contacts/{contact_id}` | Sửa liên hệ HR (chỉ field gửi lên bị ghi đè) | API key + JWT (ss_team+) |
| DELETE | `/companies/{company_id}/contacts/{contact_id}` | Xoá mềm (`is_active=false`, giữ lịch sử) | API key + JWT (ss_team+) |
| DELETE | `/companies/{company_id}/contacts/{contact_id}/hard` | Xoá THẬT, không thể khôi phục — `409` nếu contact còn `job_contact_links` | API key + JWT (ss_team+) |
| POST | `/me/applications` | Học viên ứng tuyển 1 job — `409` nếu đã ứng tuyển, `400` nếu job không `OPEN` | API key + JWT |
| GET | `/me/applications` | Danh sách job mình đã ứng tuyển | API key + JWT |
| DELETE | `/me/applications/{job_id}` | Huỷ ứng tuyển | API key + JWT |
| POST | `/me/saved-jobs` | Lưu 1 job — `409` nếu đã lưu | API key + JWT |
| GET | `/me/saved-jobs` | Danh sách job đã lưu | API key + JWT |
| DELETE | `/me/saved-jobs/{job_id}` | Bỏ lưu job | API key + JWT |
| POST | `/crawl` | Kích hoạt crawl nền — body `{"source", "category", "pages"?, "max_jobs"?}`, trả `run_id` ngay | API key + JWT (admin) |
| GET | `/crawl/{run_id}` | Theo dõi tiến độ/kết quả 1 lượt crawl | API key |
| GET | `/stats` | Tổng job/công ty/đơn ứng tuyển (`total_applications`), tỷ lệ có social, phân bố ngành/nguồn | API key |
| GET | `/stats/engagement` | Thêm 08/2026 — mọi job đang OPEN kèm `application_count`/`saved_count` (lọc "JD ế" phía client), + `monthly` (ứng tuyển/lưu job tháng này vs tháng trước, để tính % chênh lệch) | API key |
| GET | `/sources` | Danh sách source/category có sẵn (đọc từ `config.py`) — frontend render dropdown | API key |
| GET | `/health` | Health check | API key |
| POST | `/auth/register` | Tự đăng ký (phone/track cho học viên, luôn role `user`), gửi email xác thực | **KHÔNG cần API key** |
| GET | `/auth/verify-email?token=` | Kích hoạt tài khoản — bấm từ link trong email, trả HTML | **KHÔNG cần API key** |
| POST | `/auth/resend-verification` | Xin gửi lại email xác thực | **KHÔNG cần API key** |
| POST | `/auth/forgot-password` | Xin link đặt lại mật khẩu qua email — luôn trả message chung chung | API key |
| POST | `/auth/reset-password` | Đặt mật khẩu mới bằng token từ email, thu hồi toàn bộ refresh token cũ | API key |
| POST | `/auth/login` | Đăng nhập, trả `access_token` + `refresh_token`. Chặn nếu email chưa xác thực hoặc tài khoản bị khoá | API key |
| POST | `/auth/refresh` | Xoay vòng lấy access token mới | API key |
| POST | `/auth/logout` | Thu hồi refresh token hiện tại | API key |
| GET | `/auth/me` | Thông tin tài khoản đang đăng nhập | API key + JWT |
| POST | `/auth/change-password` | Tự đổi mật khẩu | API key + JWT |
| POST | `/auth/users` | Admin tạo hộ tài khoản mới, chọn được role | API key + JWT (admin) |
| GET | `/auth/users` | Danh sách toàn bộ tài khoản | API key + JWT (ss_team+) |
| PATCH | `/auth/users/{id}/role` | Đổi role tài khoản khác (không tự đổi role chính mình) | API key + JWT (admin) |
| PATCH | `/auth/users/{id}/active-status` | Khoá/mở khoá vĩnh viễn tài khoản khác (không tự khoá chính mình) | API key + JWT (admin) |

Tài khoản `ss_team`/`admin` **luôn ẩn** `phone`/`track` trong mọi
response — 2 field này chỉ có ý nghĩa với học viên (`user`).

---

## Chi tiết 1 số endpoint

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
  "salary_period": "MONTH",
  "deadline": "2026-12-31"
}
```

`company_id` phải đã tồn tại (tạo trước bằng `POST /companies` nếu chưa
có) — route KHÔNG tự tạo company kèm job. Gọi lại nhiều lần với data y
hệt (cùng company_id + job_title + level_code + province_name) sẽ KHÔNG
tạo job trùng — trả về job đã có.

`salary_period`: `"MONTH"` (mặc định) | `"YEAR"`. **Job nhập tay KHÔNG
tự suy luận được field này** như job crawl — nếu nhập lương NĂM, **phải
tự truyền `"salary_period": "YEAR"`**, nếu không hệ thống mặc định hiểu
là lương/tháng.

### `PATCH /jobs/{job_id}` — sửa job

Chỉ gửi field muốn sửa, field không gửi giữ nguyên.

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
→ route tự động dùng lại company đã có, KHÔNG tạo bản ghi trùng.

### `POST /companies/{company_id}/contacts` — thêm liên hệ HR

```json
{
  "contact_name": "Nguyễn Văn A",
  "job_title": "HR Manager",
  "work_email": "a.nguyen@abc.vn",
  "social_link": "https://linkedin.com/in/nguyenvana",
  "phone_number": "0901234567",
  "found_source": "LinkedIn"
}
```

Chỉ `contact_name` bắt buộc. `contact_status` mặc định `UNCONTACTED` lúc
tạo (đổi qua `PATCH`, phải là 1 trong `UNCONTACTED` | `EMAIL_SENT` |
`RESPONDED` | `IN_PARTNERSHIP`).

### `DELETE /companies/{company_id}/contacts/{contact_id}` — xoá mềm

Không xoá thật — chỉ đặt `is_active=false`, giữ nguyên lịch sử liên hệ.
`GET` mặc định không trả contact đã ẩn; thêm `?include_inactive=true` để
xem lại.

### `POST /crawl` — giới hạn theo trang hoặc theo số lượng JD

Khớp 1-1 với `--pages`/`--max-jobs` ở CLI (`main.py`):

```json
{"source": "topcv", "category": "data-analyst", "pages": 3}
{"source": "topcv", "category": "data-analyst", "max_jobs": 20}
{"source": "topcv", "category": "data-analyst", "pages": 5, "max_jobs": 50}
```

`GET /crawl/{run_id}` trả thêm field `max_jobs` (null nếu không giới hạn
theo số lượng) bên cạnh `pages`.

### `POST /auth/register` — tự đăng ký

```json
{
  "full_name": "Nguyễn Văn B",
  "email": "b.nguyen@example.com",
  "password": "mat-khau-toi-thieu-8-ky-tu"
}
```

Luôn tạo tài khoản role `user`. Trả `{"ss_user_id", "email", "message"}`
— KHÔNG trả token, phải xác thực email trước mới login được.

### `POST /auth/login`

```json
{"email": "member@example.com", "password": "..."}
```

Trả `{"access_token", "refresh_token", "token_type": "bearer",
"must_change_password"}`. `must_change_password=true` nghĩa là tài khoản
mới tạo qua `POST /auth/users`/vừa bị admin reset — nên ép chuyển sang
màn đổi mật khẩu trước khi cho dùng tiếp.

### `PATCH /auth/users/{id}/role` / `PATCH /auth/users/{id}/active-status`

```json
{"role": "ss_team"}
{"is_active": false}
```

Cả 2 trả `400` nếu `{id}` trùng chính admin đang gọi request. Vô hiệu
hoá tài khoản **không** revoke JWT access token đang có hiệu lực — token
cũ (tối đa 30 phút) vẫn dùng được tới khi hết hạn tự nhiên.

---

## Phân quyền — role hierarchy

`api/deps.py` định nghĩa `ROLE_HIERARCHY = {"user": 0, "ss_team": 1,
"admin": 2}` — mỗi route khai báo `Depends(require_role("ss_team"))`
(hoặc `require_admin`, alias của `require_role("admin")`) sẽ chấp nhận
role của người gọi nếu cấp số của role đó >= cấp yêu cầu, không so khớp
đúng 1 chuỗi.

Token JWT phát hành **trước** khi role hierarchy tồn tại vẫn mang giá
trị role cũ trong payload — bị `require_role()` từ chối (coi như cấp
thấp nhất, an toàn theo hướng từ chối) cho tới khi access token tự hết
hạn (30 phút) hoặc người dùng đăng nhập lại.

## Audit trail — ai tạo/sửa job, công ty, liên hệ

`job_postings`, `companies`, `company_contacts` có 2 cột
`created_by`/`updated_by` (UUID, tham chiếu `app_users.ss_user_id`),
trả về trong response của `GET`/`POST`/`PATCH` tương ứng.

- Job/công ty tạo qua **crawl tự động** (`main.py`, không qua JWT) →
  `created_by = null`. Bình thường, không phải lỗi.
- Job/công ty/liên hệ HR tạo/sửa qua **API có JWT** →
  `created_by`/`updated_by` = `ss_user_id` của người vừa gọi.

## Connection pool

`api/deps.py:get_db()` mượn/trả connection từ 1 pool đã mở sẵn
(`psycopg2.pool.ThreadedConnectionPool`, xem `db/connection.py`) thay vì
mở connection mới mỗi request. Pool khởi tạo 1 lần lúc app khởi động
(`api/app.py`, `lifespan`), đóng lại lúc app tắt.

Kích thước pool cấu hình qua `DB_POOL_MIN`/`DB_POOL_MAX` trong `.env`
(mặc định 2/20) — nên đặt `DB_POOL_MAX` thấp hơn giới hạn connection
Postgres phía Render/Supabase cho phép.

`main.py` (CLI) và các script độc lập (`backfill_company_profiles.py`,
`enrich_company_profile_from_website.py`, `enrich_company_web_info.py`,
`get_company_fb_linkedin_link.py`, `check_expired_source_jobs.py`,
`api/crawl_runner.py`) vẫn dùng `db.get_connection()` mở/đóng connection
trực tiếp như cũ — KHÔNG qua pool, vì tần suất chạy thấp (1 lần/script).

---

## Deploy production

Đã deploy thật lên **Render** (Web Service, kết nối GitHub private repo
`Koaito/scrap-jd`), khai báo đủ biến môi trường (Postgres, `API_KEY`,
`ALLOWED_ORIGINS`, `JWT_SECRET_KEY`, `RESEND_API_KEY`, `EMAIL_FROM`,
`API_BASE_URL`, `DB_POOL_MIN`/`DB_POOL_MAX`, Tavily/Gemini key). URL
public: `https://scrap-jd-api.onrender.com`.

**Trước khi deploy bản có JWT/phân quyền/đăng ký/ứng tuyển/lưu job
này**, phải chạy trên Postgres thật **đúng thứ tự sau** (migration sau
phụ thuộc bảng/cột migration trước tạo ra) — repo hiện có **13 file**
migration cần chạy tay cho DB cũ, không chỉ 4:

```bash
psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_auth.sql
psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_audit_columns.sql
psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_role_hierarchy.sql
psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_email_verification.sql
psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_rename_ss_team_members.sql
psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_applications_saved_jobs.sql
psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_phone_track.sql
psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_password_reset.sql
psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_tax_id.sql
psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_work_type_deadline.sql
psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_update_provinces_2025.sql
psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_salary_period.sql
psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_products_services.sql
```

Deploy code trước khi chạy đủ 13 migration → `POST`/`PATCH /jobs`,
`POST /companies`, CRUD `/companies/{id}/contacts`, `POST /auth/register`,
đăng nhập, quên mật khẩu, ứng tuyển, lưu job, hoặc crawl sẽ lỗi 500 (bảng/
cột liên quan chưa tồn tại). DB tạo mới hoàn toàn từ `sql/schema.sql` đã
có sẵn đầy đủ, **không cần** chạy lại các migration này.

⚠️ **KHÔNG chạy `sql/migration_drop_products_services.sql`** — file này
`DROP COLUMN companies.products_services`, cột mà pipeline crawl và
`enrich_company_profile_from_website.py` đang chủ động ghi vào. Chạy nó
sẽ khiến crawl lỗi 500 ngay lập tức.

Khi làm frontend và deploy lên Vercel: quay lại Render, sửa
`ALLOWED_ORIGINS` cho khớp domain Vercel thật, KHÔNG quên bước này.

---

## Giới hạn đã biết

- **Trạng thái crawl lưu trong RAM** (dict `_RUNS`), mất khi restart
  server, không đồng bộ nếu chạy `uvicorn --workers > 1`. Đủ dùng cho quy
  mô hiện tại. Cần chạy nhiều worker/persistent thì nâng cấp sang Celery
  + Redis hoặc RQ sau.
- **`POST /crawl` không giới hạn số lượt chạy song song** —
  `require_admin` giảm rủi ro spam nhưng không tự chặn 2 lượt chạy cùng
  lúc. Nên tự giới hạn ở phía frontend (disable nút "Crawl" khi đang
  chạy).
- **Auth API key (lớp 1) vẫn là 1 khoá dùng chung** — không phân biệt
  được nội bộ ai gọi ở tầng này (muốn biết ai thì phải qua JWT).
- **Access token JWT không tự kiểm tra lại DB** (`get_current_user()`
  chỉ verify chữ ký, không query `is_active`) — thu hồi tức thời 1 tài
  khoản (vd nhân viên nghỉ việc) cần gọi
  `revoke_all_refresh_tokens_for_user()` để chặn cấp access token MỚI,
  access token cũ (tối đa 30 phút) vẫn còn hiệu lực tới khi tự hết hạn.
- **Thiếu `RESEND_API_KEY` không làm sập `POST /auth/register`** — tài
  khoản vẫn tạo thành công, chỉ email không gửi được.

## Việc CHƯA làm (để team quyết định có cần không)

- Rate limiting (đặc biệt `POST /auth/register`, `POST
  /auth/resend-verification` — hiện ai cũng gọi được không giới hạn số
  lần).
- Trang xác nhận `GET /auth/verify-email` hiện trả HTML tĩnh đơn giản —
  khi có frontend thật, nên đổi sang redirect về 1 URL frontend cụ thể.
- `PATCH /jobs/{job_id}` không tự "vá" `source_url`/nội dung job cũ khi
  pipeline crawl phát hiện đây là bản "đăng lại" (repost) của job đã có
  — hiện chỉ chặn insert job mới, giữ nguyên `source_url` của job cũ.
