# Kiến trúc Backend Scrap JD

**Cập nhật:** 11/10/2026  
**Dành cho:** kỹ sư tiếp nhận vận hành hoặc phát triển backend.  
**Phạm vi:** repository này; frontend nằm ở repository độc lập.

## 1. Tổng quan

Scrap JD là backend quản lý dữ liệu việc làm cho Student Success. Hệ thống vừa crawl nhiều website tuyển dụng, vừa cung cấp API nghiệp vụ cho dashboard và học viên. PostgreSQL là nguồn dữ liệu trung tâm; client không truy cập database trực tiếp.

```text
Frontend/client -- X-API-Key + JWT --> FastAPI
                                     routers -> schemas -> services / db
                                                         |
                                                  PostgreSQL / Supabase
Crawler pipeline <- adapters <- TopCV / VietnamWorks / CareerViet
```

Tích hợp phụ: Resend (email), Supabase Storage (CV), Gemini/Tavily (company enrichment).

Hai entry point dùng chung DB:

- `uvicorn scrapjd.api.app:app`: API, DB pool, scheduler và background task.
- `python main.py ...`: CLI crawl, migration, report, merge duplicate và admin.

## 2. Quyết định kiến trúc

| Quyết định | Cách làm | Hệ quả |
| --- | --- | --- |
| Monolith theo domain | API, crawler và DB cùng repo/process deploy | Đơn giản ở quy mô hiện tại; chưa có queue/service tách riêng. |
| Adapter pattern | `BaseAdapter` + adapter từng nguồn, trả `RawJobRecord` | Thêm source không viết lại pipeline. |
| Pipeline dùng chung | `pipeline.py` normalize, enrich, dedup, ghi DB | Không đặt parser nguồn-specific trong router. |
| DB facade | SQL/logic dữ liệu ở `scrapjd/db/` | Router không tự viết SQL; giữ đúng transaction/connection contract. |
| API theo lớp | router -> Pydantic schema -> service/DB | HTTP contract ở `schemas`; logic dùng lại ở `services`/DB. |
| Defense in depth | API key + JWT + RBAC + DB constraints | API key không thay JWT. |
| Schema + migration | `schema.sql` cho DB mới, migrations cho DB cũ | Đổi schema phải cập nhật cả hai. |
| In-process background | BackgroundTasks, APScheduler, Semaphore | Chỉ đúng cho một process nếu chưa thay lock shared. |

## 3. Bản đồ repository

```text
main.py                         CLI
scrapjd/
  api/app.py                    composition root, middleware, lifespan
  api/routers/                  HTTP theo domain
  api/schemas/                  request/response contracts
  api/services/                 import/export, validation, conflict, watchdog
  api/deps.py                   DB dependency, current-user, RBAC
  api/crawl_runner.py           crawl background, batch, progress/log
  api/maintenance_runner.py     maintenance background
  adapters/                     crawler contract và parser nguồn
  pipeline.py                   ingest workflow dùng chung
  normalize.py                  chuẩn hoá title/salary/province/level
  models.py                     RawJobRecord contract
  sources_registry.py           registry nguồn crawl
  db/                           data access + transaction + domain logic
  maintenance/                  enrich/backfill/expiry scripts
  cli/                          report, merge duplicate, recompute level
sql/schema.sql                  schema hiện hành
sql/migration_*.sql, NNNN_*.sql nâng cấp DB
tests/                          unit, API contract, PostgreSQL integration
```

`config.py` đọc environment/category; `constants.py` giữ enum. `API_README.md` mô tả endpoint/payload; file này giải thích kiến trúc.

## 4. API và miền nghiệp vụ

`api/app.py` tạo app, quản lý DB lifecycle, middleware/scheduler và include router. Router nhận HTTP, kiểm tra quyền, gọi service/DB rồi trả schema; SQL tập trung trong `db/`.

| Miền | Chức năng |
| --- | --- |
| Auth & users | đăng ký, verify email, reset password, login/refresh/logout, quản trị user/role. |
| Jobs | search/filter, CRUD, data health, applicant/saver; đồng bộ trạng thái với listing nguồn. |
| Companies & contacts | profile, partnership/data-health, CRM contacts, assign và delete. |
| Student experience | saved jobs, applications/CV, chat qua `/me` và `messages`. |
| Dashboard/meta/audit | insight, stats, enum/source metadata, audit note. |
| Crawl & maintenance | run async, status/progress/live log; request trả `202`. |
| Import/export | preview/validate/resolve/confirm và download có filter. |
| Email templates | CRUD template/placeholder. |

Khi đổi request/response, cập nhật schema, router, `API_README.md`, frontend contract và test cùng nhau.

## 5. Crawl và ingest

### Registry và adapter

`sources_registry.py` là nguồn sự thật cho source name, adapter class, category. Hiện có TopCV, VietnamWorks, CareerViet. Adapter kế thừa `BaseAdapter`, implement `fetch_jobs()`, có thể override detail/profile, job-code matching hoặc dedup resolver.

Base adapter cung cấp throttle+jitter, retry/backoff, circuit breaker `CrawlBlockedError`, snapshot recorder, known-URL hook và HTTP handling chung. Adapter trả `RawJobRecord`, nên pipeline độc lập parser website.

### Pipeline

```text
listing -> kiểm tra record/URL hiện có -> fetch detail/profile
        -> normalize title/salary/province/work-type/deadline/level
        -> resolve company + level/province lookup
        -> dedup: job-code (nếu có) rồi repost/dedup key
        -> create/update job + source listing + stats/audit
```

`job_postings` là business job; `job_sources_log` giữ nhiều listing nguồn có thể thuộc cùng job. `listing_state.py`, `job_derivation.py`, `job_sync.py` suy ra trạng thái, deadline, source URL từ listing.

Phòng vệ dữ liệu: dedup key + PostgreSQL advisory lock; repost/reopen phù hợp; cờ `stats.degraded`; snapshot HTML/JSON nén; circuit breaker khi site block.

### Crawl qua API

`POST /crawl` và `POST /crawl/batch` tạo `crawl_runs`/`crawl_batches` queued, trả `202`; `BackgroundTasks` gọi runner:

```text
request -> queued run -> 202 + run_id
                       -> GLOBAL_JOB_SEMAPHORE
                       -> running -> pipeline -> done/error
                                      |-> progress, logs, snapshots
                                      `-> category tiếp theo nếu batch
```

Unique index chỉ cho một run active mỗi source. Batch chạy tuần tự; blocked dừng category còn lại. Progress write được throttle.

## 6. Data model và invariant

| Nhóm | Bảng chính |
| --- | --- |
| Lookup | `provinces`, `levels` |
| Identity | `app_users`, `auth_refresh_tokens` |
| Core | `companies`, `job_postings`, `company_contacts` |
| Lineage | `job_sources_log`, `job_contact_links`, `job_contact_interactions` |
| Engagement | `job_applications`, `saved_jobs` |
| Audit | `audit_logs` |
| Chat | `chat_relationships`, `messages` |
| Background | `crawl_*`, `maintenance_*` |
| Import | `import_previews` |

Invariant cần giữ:

- `companies.tax_id` unique khi có giá trị; FK dùng UUID.
- Job có `content_hash`/`dedup_key`; `v_duplicate_job_candidates` hỗ trợ rà soát.
- Trigger cập nhật `updated_at`, hash/dedup key và closed state. Chỉ dùng `app.skip_updated_at` trong flow thiết kế sẵn.
- Mỗi source/job type chỉ có một run queued/running.
- Merge duplicate dùng transaction theo nhóm, row lock, chuyển children, đối chiếu row count và audit snapshot trước khi xoá donor.
- Connection từ pool phải trả qua `release_connection()`, không `close()`; job dài/CLI dùng physical connection riêng mới close.

## 7. Security

1. **X-API-Key:** gateway client nội bộ, áp dụng router dữ liệu, login và health.
2. **JWT:** xác minh user ở route yêu cầu login.
3. **Refresh rotation + single session:** refresh token hash trong DB; `active_session_id` vô hiệu phiên cũ.
4. **RBAC:** `user < ss_team < admin`, dependency `require_role()`/`require_admin()`.

Register/verify/resend/forgot/reset là public có chủ đích để link email hoạt động; các route này rate-limit theo IP.

Hardening: CORS fail-closed theo `ALLOWED_ORIGINS`; docs/OpenAPI tắt mặc định; security headers; request-size guard qua Content-Length; slowapi với public routes/user key khi có JWT.

**Scale caveat:** rate limit, semaphore, scheduler đều per-process. Nhiều worker/instance cần Redis hoặc PostgreSQL advisory lock shared.

## 8. Background, scheduler và observability

Lifespan init DB pool, reconcile run mồ côi, start APScheduler rồi shutdown scheduler/pool. Scheduler dọn preview hết hạn, crawl watchdog, maintenance watchdog. Watchdog đánh error run stale để không khoá hệ thống mãi.

`GLOBAL_JOB_SEMAPHORE` chia sẻ crawl + maintenance (mặc định 2). Job dài giữ connection riêng cho execute/live log; trước khi tăng concurrency, tính lại tổng connection với `DB_POOL_MAX` và giới hạn Postgres/pooler.

Maintenance registry gồm: backfill source profile; enrich profile website; enrich Tavily/Gemini; tìm Facebook/LinkedIn; check expired listing/job. Status/log là dữ liệu DB và là điểm chẩn đoán đầu tiên.

## 9. Import/export

```text
CSV/XLSX upload -> pandas parse + field/cross-field validation
 -> DB/in-file conflict detection -> preview owner+TTL
 -> staff sửa/chọn resolution -> confirm: re-validate + re-check + transaction write
```

`EntitySpec` là khai báo tập trung cho `company`, `job`, `contact`: column, widget, option, cross-field rule. Thêm field import/export phải sửa spec, validation, parser, export cùng nhau; không hardcode riêng trong route.

## 10. Migrations

| Trường hợp | Lệnh |
| --- | --- |
| DB mới | `python main.py init-db` |
| Pre-deploy | `python main.py migrate --check` |
| DB hiện hữu | `python main.py migrate` |
| Schema đủ nhưng thiếu tracking | `python main.py migrate --baseline` |

Migration mới dùng `NNNN_mo_ta.sql`, số tiếp theo, idempotent; không thêm `migration_*.sql` cũ. Cập nhật đồng thời `sql/schema.sql`, không sửa baseline frozen, chạy `tests/test_pg_migrations.py` với `TEST_DATABASE_URL`.

## 11. Thay đổi thường gặp

### Thêm source crawl

1. Thêm category/config ở `config.py`.
2. Viết adapter kế thừa `BaseAdapter`, trả `RawJobRecord`, dùng HTTP helper có sẵn.
3. Đăng ký một entry ở `sources_registry.py`.
4. Thêm fixture đã bỏ PII và test parser/pipeline.
5. Cập nhật frontend nếu nó không lấy metadata từ `/sources`.

Không sửa pipeline chỉ vì selector riêng; chỉ mở rộng adapter contract khi là capability chung.

### Thêm endpoint/domain feature

1. Request/response ở `api/schemas/`.
2. Query/transaction ở `db/`; orchestration dùng lại ở `services/`.
3. Router mỏng, role tối thiểu, audit cho thay đổi đáng kể.
4. Test API và integration test SQL/constraint.
5. Cập nhật `API_README.md` và frontend contract.

### Thay đổi schema

Tạo migration, cập nhật `schema.sql`, test schema parity, chạy migration production **trước** khi deploy code dùng cột/bảng mới.

## 12. Runbook tối thiểu

```powershell
pip install -r requirements.txt
Copy-Item .env.example .env
python main.py init-db       # chỉ DB mới
uvicorn scrapjd.api.app:app --reload --port 8000

python -m pytest -q
mypy
python main.py migrate --check
```

PostgreSQL integration test cần `TEST_DATABASE_URL` riêng có tên chứa `test`; fixture có thể xoá schema của DB đó. Không commit `.env`.

Khi crawl lỗi: xem `GET /crawl/{run_id}` và logs, đọc `stats.blocked`/`stats.degraded`; nếu blocked không retry dồn dập; nếu degraded export snapshot, sửa adapter và thêm test; nếu stale kiểm scheduler/watchdog, run table và connection limit.

## 13. Rủi ro/nợ kỹ thuật

- Một process là giả định: semaphore, rate limit, scheduler không phân tán.
- `BackgroundTasks` không durable như queue; restart không resume chính xác.
- Dedup/repost là heuristic; dùng report/audit để rà soát.
- Crawler phụ thuộc selector/anti-bot bên ngoài; snapshot/fixture là tuyến debug quan trọng.
- Size guard chặn sớm khi có Content-Length; client chunked cần ASGI byte-counting nếu upload công khai hơn.
- Merge duplicate chưa có command undo; backup DB trước `merge-duplicates --apply`.

## 14. Tài liệu liên quan

- `README.md`: setup, crawler, maintenance và runbook.
- `API_README.md`: endpoint, payload, ví dụ HTTP.
- `sql/README_MIGRATIONS.md`: migration bắt buộc.
- `.env.example`: biến môi trường/default.
- `tests/`: executable specification cho API, pipeline và PostgreSQL.


---

# Phần II — Onboarding kỹ thuật cho kỹ sư mới

Phần này giải thích chi tiết cách hệ thống hoạt động và các nguyên tắc cần giữ khi thay đổi mã nguồn.

## 15. Thứ tự học codebase

Nên đọc code theo thứ tự sau:

1. README.md: local setup, environment, CLI và giới hạn vận hành.
2. scrapjd/api/app.py: mọi router, middleware, lifecycle và scheduler.
3. scrapjd/api/deps.py, security.py, auth.py: request được cấp DB connection, xác thực và phân quyền như thế nào.
4. Một vertical slice đơn giản, ví dụ schemas/companies.py -> routers/companies.py -> db/companies.py.
5. models.py, sources_registry.py, adapters/base.py, pipeline.py: crawler ingest.
6. sql/schema.sql và sql/README_MIGRATIONS.md: data model và migration.
7. Test tương ứng trong tests/: executable specification trước khi sửa code.

Router trả lời HTTP gì và quyền gì. Schema trả lời input/output nào. Service trả lời orchestration thế nào. DB module trả lời dữ liệu được đọc/ghi thế nào. Adapter/pipeline trả lời dữ liệu nguồn được tạo thế nào.

## 16. Lifecycle của một API request

Luồng chuẩn:

    HTTP request
      -> request-size middleware
      -> security-header middleware
      -> CORS / SlowAPI middleware
      -> X-API-Key dependency
      -> get_db, get_current_user, require_role dependency
      -> Pydantic parse và validate
      -> router -> service hoặc DB facade
      -> response model serialize
      -> finally: trả DB connection vào pool

### 16.1 Connection pool và transaction

API startup gọi init_pool; shutdown gọi close_pool. Dependency get_db mượn pooled connection và luôn release trong finally. Pool có bounded wait để hấp thụ burst request ngắn.

CLI và background runner có thể giữ connection nhiều phút, nên dùng physical connection riêng. Không đổi máy móc chúng sang pool: chỉ vài job dài có thể làm pool cạn và làm request thông thường lỗi.

Quy tắc bắt buộc:

- Connection mượn từ pool: release_connection, không gọi close.
- Physical connection: close sau khi hoàn thành.
- Đọc implementation của DB function trước khi ghép vào transaction lớn; một số hàm CRUD tự commit theo contract.
- Import, merge và các flow cần atomicity phải chủ động kiểm soát transaction.

### 16.2 Phân lớp feature

Khi thêm feature, không nhét tất cả vào router:

| Lớp | Trách nhiệm |
| --- | --- |
| schemas | type, field limit, request/response contract, validation bề mặt |
| routers | endpoint, status code, dependency, HTTP error |
| services | orchestration nhiều bước, nghiệp vụ tái sử dụng |
| db | SQL parameterized, locking, persistence, invariant gần DB |
| tests | chứng minh hành vi ở API và data layer |

Ví dụ thêm một field Job thường cần rà: schema, jobs router, DB list/detail/create/update query, export/import entity spec, migration và schema.sql, audit diff, API test và Postgres integration test.

## 17. Authentication, password và session

### 17.1 Hai lớp authentication

X-API-Key xác minh caller là frontend/client nội bộ và được gắn ở include_router trong app.py. Đa số route, kể cả health và login, cần lớp này.

JWT Bearer xác minh user thật. Route cần danh tính dùng get_current_user; route cần quyền cao dùng require_role hoặc require_admin.

Hai lớp có mục đích khác nhau: có API key không nghĩa là có quyền user; có JWT nhưng không có API key cũng bị chặn ở cổng client nội bộ.

### 17.2 Password hash thực tế

Password không dùng SHA-256. security.py khởi tạo argon2.PasswordHasher và hash bằng Argon2id.

- hash_password nhận plain password và trả PHC string để lưu app_users.password_hash.
- Chuỗi hash bao gồm algorithm version, tham số, salt ngẫu nhiên và digest.
- Salt tự sinh cho từng password; hai người cùng mật khẩu có hash khác nhau.
- verify_password dùng Argon2 verify và trả false cho sai password hoặc hash hỏng, không lộ nguyên nhân.
- needs_rehash được gọi sau login đúng để nâng hash cũ khi tham số Argon2 tăng.

Các luồng đều hash password trước khi ghi DB:

| Luồng | Cách xử lý |
| --- | --- |
| Register public | hash trước khi tạo account chờ verify email |
| Admin tạo user | server sinh temporary password 16 ký tự, chỉ trả rõ đúng một lần |
| create-admin CLI | password prompt rồi hash |
| Change password | verify old password, hash password mới, revoke refresh token |
| Reset password | hash password mới, xoá reset token, revoke toàn bộ refresh token |

### 17.3 Password policy và hardening kế tiếp

Hiện register/change/reset yêu cầu tối thiểu 8 ký tự. Argon2id là lựa chọn tốt và không phải rủi ro chính. Các điểm có thể cải thiện:

1. Nâng minimum lên 12 hoặc 14 sau khi cân nhắc UX.
2. Đặt maximum length, ví dụ 128 hoặc 256, tránh hash một request password cực dài.
3. Khai báo explicit Argon2 parameters và ghim argon2-cffi version, thay vì hoàn toàn phụ thuộc library defaults.
4. Benchmark trên Render trước khi tăng Argon2 cost: login hợp lệ phải không quá chậm, nhưng offline cracking phải đắt.
5. Cân nhắc password deny-list từ leaked/common passwords.
6. Không thay Argon2id bằng SHA-256 hoặc bcrypt cấu hình không rõ.

Không có pepper không phải lỗ hổng bắt buộc; pepper là defense-in-depth nhưng thêm secret lifecycle cần quản lý.

### 17.4 Login lockout và enumeration

- Sau 5 lần sai liên tiếp, account bị lock 15 phút.
- Login trả thông báo chung cho email không có và password sai.
- Public auth routes bị rate limit.
- User response schema không xuất password_hash.

Lockout theo account có thể bị abuse để lock một account nếu attacker biết email. Nếu vấn đề xảy ra, cân nhắc rate limit theo account cộng IP hoặc exponential backoff thay lock cứng.

### 17.5 JWT, refresh và one-time token

| Bí mật | Lưu trong DB | Lý do |
| --- | --- | --- |
| Password | Argon2id salted PHC hash | bí mật do con người chọn, cần password hash chậm |
| Refresh token | SHA-256 hash | token random 48 byte, entropy cao, cần deterministic lookup |
| Verify/reset email token | SHA-256 hash | token random, expiry ngắn, one-time use |
| Access token | không lưu raw trong DB | JWT ký HS256, hạn 30 phút |

Access token mang sub, role, email, session id, issued/expiry và type. Session id được đối chiếu với active_session_id để single-session: login mới, password change hoặc reset sẽ làm token phiên cũ không dùng được ở request sau.

Refresh token sống 30 ngày, có rotation. Grace window 10 giây chỉ cho race refresh hợp lệ; sau logout, login ở nơi khác hoặc đổi password, token đã revoke nên grace không bypass được.

## 18. Crawler: debug và mở rộng

### 18.1 Adapter contract

Mỗi adapter cần:

- fetch_jobs(category_key, max_pages) yield RawJobRecord.
- Có source_url, source_name, job_title và company_name đúng.
- fetch_job_full_detail trả None khi fetch lỗi thật; source không hỗ trợ detail thì trả dict rỗng an toàn.
- Dùng helper HTTP base thay vì request trực tiếp để giữ throttle, retry, snapshot và circuit breaker.
- Không chứa SQL.

Extension point: job_code_url_regex cho source có mã job stable; dedup_resolvers cho thứ tự dedup riêng; fetch_company_profile nếu source có trang company hữu ích.

### 18.2 Khi selector website đổi

Dấu hiệu: crawl done nhưng stats.degraded, zero listing, hoặc detail fields rỗng hàng loạt.

1. Dùng snapshot CLI lấy HTML/JSON run gần nhất.
2. Scrub email/phone/PII trước khi đưa vào fixture test.
3. Sửa parser ở đúng adapter; ưu tiên JSON nhúng hoặc nhãn business ổn định hơn CSS class.
4. Sửa/thêm test fixture trước khi crawl production.
5. Theo dõi status, blocked, degraded, fetched và inserted của run sau.

Không hạ degraded threshold hay bỏ validation chỉ để màn hình có trạng thái xanh.

### 18.3 Phân biệt dedup

| Khái niệm | Ý nghĩa |
| --- | --- |
| Cùng URL | record cũ đã biết; có thể skip hoặc recheck detail thiếu |
| Job code | cùng job khi URL/title nguồn thay đổi nhưng mã stable |
| Repost | URL mới cùng company/title/province, được link vào job hiện có |
| Duplicate report/merge | thao tác vận hành xử lý duplicate lịch sử hoặc heuristic chưa chắc |

Dedup key là quyết định nghiệp vụ liên quan pipeline, SQL trigger/view, reports, migration và dữ liệu lịch sử. Không thay key vì một case riêng mà chưa đánh giá toàn hệ thống.

## 19. Import/export: tại sao phải có preview

Import không ghi file vào DB trực tiếp vì dữ liệu thực thường có company tên lệch, duplicate, level/salary sai hoặc hai row trong cùng file trùng nhau.

- validation_engine kiểm field/type.
- entity_specs là một nguồn rule tập trung.
- conflict_detector tìm trùng DB và trong batch.
- preview_manager giữ preview theo owner và TTL.
- import_executor kiểm lại toàn bộ khi confirm.

Client validation chỉ phục vụ UX. Server bắt buộc re-validate và re-check conflict khi confirm vì preview có thể cũ, DB có thể đổi, hoặc request có thể bị sửa tay.

Khi đổi import flow, test: required/type/cross-field validation; conflict active/inactive/in-batch; company resolution; preview owner/expiry; race confirm; rollback khi một row lỗi.

## 20. Background jobs và failure modes

### 20.1 Lý do global concurrency limit

Request ngắn và polling dùng pool. Crawl/maintenance chạy lâu nên runner có physical connection riêng; live log writer cũng có connection riêng. Nếu giữ chúng trong pool, vài job dài làm request dashboard/list API thiếu slot.

GLOBAL_JOB_SEMAPHORE dùng chung crawl + maintenance, default 2. Trước khi tăng cần tính tổng: request pool + hai connection cho mỗi job dài + giới hạn Postgres/pooler.

### 20.2 Sự cố thường gặp

| Hiện tượng | Cơ chế hiện tại | Hành động operator |
| --- | --- | --- |
| Process restart giữa run | startup reconcile run mồ côi | xem run/log, chạy lại khi phù hợp |
| Task treo | watchdog mark error | tìm nguyên nhân site/DB, không retry mù |
| Source 403/429 liên tiếp | circuit breaker mark blocked | chờ, đổi IP, hoặc crawl môi trường khác |
| Polling nhiều tab | pool bounded wait | kiểm DB connection limit và frontend polling |
| Batch bị block | không chạy category sau | giải quyết block, tạo run/batch mới |

Nếu cần durable job, retry/backoff, resume checkpoint hoặc scale ngang, cần queue worker chuyên dụng. Chỉ thay BackgroundTasks bằng thread pool không giải quyết các yêu cầu đó.

## 21. Checklist code review backend

- Không log password, JWT, refresh token, API key hoặc service-role key.
- Route mới có đúng API-key, JWT và role dependency.
- Response schema không lộ password_hash, reset/verify token hay internal fields.
- SQL dùng parameters; không ghép user input vào query.
- Pooled connection release; physical connection close.
- Đổi DB có numbered migration, schema.sql và migration-parity test.
- Update quan trọng có audit log.
- Background work có status/log, không vượt global concurrency.
- Parser thay đổi có fixture đã scrub PII và test.
- Import flow re-validate/re-check conflict khi confirm.
- API docs/client contract cập nhật.

## 22. Glossary

| Thuật ngữ | Nghĩa |
| --- | --- |
| Job | business record trong job_postings, có thể có nhiều listing |
| Listing/source log | URL/phiên bản tin nguồn trong job_sources_log |
| Crawl run | một lượt source + category, có status/stats/progress/log |
| Crawl batch | chuỗi category chạy tuần tự trong một source |
| Maintenance run | một lượt enrich/backfill/check-expiry |
| Repost | listing URL mới được nhận là cùng job hiện có |
| Dedup key | business key phát hiện duplicate/repost candidate |
| Degraded | crawl xong kỹ thuật nhưng parser data không đáng tin |
| Physical connection | DB connection độc lập cho CLI/job dài |
| Pooled connection | slot mượn/trả cho request API nhanh |
