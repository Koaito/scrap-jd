# Job Crawler — Student Success

Crawler job từ **TopCV**, **VietnamWorks**, **CareerViet** (6 ngành: Data
Analyst, Data Engineer, Data Scientist, Software Engineering, Business
Analysis, UI/UX Design), chuẩn hoá dữ liệu, crawl sâu hồ sơ công ty
(website, mã số thuế, quy mô, lĩnh vực, địa chỉ), lưu vào PostgreSQL và
expose ra ngoài qua một lớp **API FastAPI có xác thực**. Một **frontend
dashboard** riêng gọi vào API này.

- **Backend** (repo này): pipeline crawl + API, deploy trên Render tại
  `https://scrap-jd-api.onrender.com`.
- **Frontend** (repo `mindx-jobs`): deploy trên Vercel, gọi API bằng JWT.

Chi tiết API (endpoint, body, xác thực) xem `API_README.md`. File này tập
trung vào pipeline crawl, các script bổ trợ và quy trình vận hành.

## Mục lục

- [Cài đặt](#cài-đặt)
- [Quy trình đầu-cuối](#quy-trình-đầu-cuối)
- [Kiến trúc](#kiến-trúc)
- [1. Crawl job](#1-crawl-job)
- [2. Vá hồ sơ công ty](#2-vá-hồ-sơ-công-ty)
- [3. Dọn job hết hạn](#3-dọn-job-hết-hạn)
- [Theo dõi chất lượng crawl](#theo-dõi-chất-lượng-crawl)
- [Xem kết quả](#xem-kết-quả)
- [Thêm ngành / nguồn crawl mới](#thêm-ngành--nguồn-crawl-mới)
- [Debug khi nguồn crawl đổi giao diện](#debug-khi-nguồn-crawl-đổi-giao-diện)
- [Deploy production](#deploy-production)
- [Giới hạn đã biết](#giới-hạn-đã-biết)

---

## Cài đặt

### 1. Môi trường Python

Cần Python 3.10 trở lên (CI dùng 3.12).

```bash
python -m venv venv
source venv/bin/activate          # Git Bash trên Windows: source venv/Scripts/activate
pip install -r requirements.txt
```

### 2. Cấu hình `.env`

```bash
cp .env.example .env
```

File `.env.example` chia thành các nhóm và ghi chú từng biến. Bạn chỉ cần
điền nhóm đang dùng:

| Bạn muốn làm gì | Biến cần điền |
| --- | --- |
| Crawl, `migrate`, `stats`, mọi lệnh `python main.py ...` | Nhóm PostgreSQL: `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, `PGSSLMODE` |
| Chạy API local (`uvicorn api.app:app`) | Thêm `API_KEY`, `JWT_SECRET_KEY`, `ALLOWED_ORIGINS` |
| Gửi email xác thực / quên mật khẩu, upload CV | `RESEND_API_KEY`, `EMAIL_FROM`, `API_BASE_URL`, `FRONTEND_BASE_URL`; `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` |
| Chạy script `enrich_*` | `GEMINI_API_KEY`; riêng `enrich_company_web_info.py` thêm `TAVILY_API_KEY` |

**Lấy giá trị cho nhóm PostgreSQL.** Backend trên Render đang dùng chính
database này, nên cách chắc chắn nhất là mở Render, vào service backend,
mục *Environment*, rồi chép 5 biến `PG*` sang `.env`. Hoặc lấy từ Supabase
Dashboard, mục *Connect*, chọn *Transaction pooler*: `PGHOST` có dạng
`aws-0-<vùng>.pooler.supabase.com`, `PGPORT=6543`, `PGUSER` có dạng
`postgres.<mã-project>`. Postgres cài trên máy thì dùng `PGHOST=localhost`,
`PGPORT=5432` và `PGSSLMODE=disable`.

**Tạo `API_KEY` và `JWT_SECRET_KEY`** (hai khoá phải khác nhau):

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Kiểm tra kết nối DB:

```bash
python main.py stats
```

Kỳ vọng: in ra `Tổng job trong DB: <số>`.

### 3. Chuẩn bị database

**DB mới (trống):**

```bash
python main.py init-db
python main.py create-admin --email admin@congty.vn --name "Nguyễn Văn A"
```

`init-db` dựng toàn bộ schema từ `sql/schema.sql` và ghi nhận mọi migration
hiện có là đã áp dụng. `create-admin` tạo tài khoản admin đầu tiên (hỏi mật
khẩu, tối thiểu 8 ký tự).

**DB đã có dữ liệu:** kiểm tra và áp dụng migration còn thiếu.

```bash
python main.py migrate --check    # chỉ liệt kê, không chạy gì
python main.py migrate            # áp dụng
```

Nếu DB đã ở trạng thái mới nhất nhưng chưa từng có log migration (dựng hoặc
sửa tay từ trước), **đừng chạy `migrate` thẳng**; dùng `--baseline` để ghi
nhận trạng thái hiện tại. Ví dụ DB đã đủ mọi bảng trừ `crawl_snapshots`:

```bash
python main.py migrate --baseline --except migration_add_crawl_snapshots.sql
python main.py migrate
```

Chi tiết và lý do xem `sql/README_MIGRATIONS.md`.

### 4. Chạy test

```bash
python -m pytest -q
```

Test không cần internet và không chạm DB thật, nhưng cần biến `JWT_SECRET_KEY`
(module `api/security.py` báo lỗi ngay khi import nếu thiếu). Nếu `.env` đã
có biến này thì không cần làm gì thêm; nếu chưa, đặt một giá trị tạm:

```bash
JWT_SECRET_KEY=gia_tri_tam python -m pytest -q
```

Các test chạm SQL thật tự bỏ qua nếu không đặt `TEST_DATABASE_URL`. Để chạy
chúng, trỏ tới một database **trống, riêng cho test** (tên phải chứa chữ
`test`, vì fixture sẽ `DROP SCHEMA public CASCADE`). Không bao giờ trỏ vào
DB thật:

```bash
TEST_DATABASE_URL=postgresql://postgres:mật_khẩu@localhost:5432/scrapjd_test python -m pytest -q
```

---

## Quy trình đầu-cuối

Thứ tự chạy thực tế, từ DB sẵn sàng tới dữ liệu đầy đủ cho dashboard:

```bash
# 1. Crawl job: chạy cho từng nguồn/ngành cần, lặp lại định kỳ
python main.py crawl --source topcv --category data-analyst --pages 3
python main.py crawl --source vietnamworks --category data-engineer --pages 5
python main.py crawl --source careerviet --category business-analyst --pages 3

# 2. Vá hồ sơ công ty còn thiếu field: chạy SAU crawl, đúng thứ tự dưới
#    (script sau chỉ xử lý công ty mà script trước không vá được)
python backfill_company_profiles.py
python enrich_company_profile_from_website.py
python enrich_company_web_info.py
python get_company_fb_linkedin_link.py

# 3. Dọn job hết hạn: chạy định kỳ (cron hằng ngày là hợp lý)
python check_expired_source_jobs.py --dry-run   # xem thử
python check_expired_source_jobs.py             # chạy thật
```

Bước 1 và 2 độc lập về mặt kỹ thuật, nhưng chạy bước 2 sau bước 1 hiệu quả
hơn: công ty vừa crawl luôn có `source_profile_url`, giúp
`backfill_company_profiles.py` (rẻ nhất, đọc lại đúng trang gốc) xử lý được
nhiều nhất trước khi phải dùng các script tốn tài nguyên hơn.

Bước 2 gồm 4 script riêng, mỗi script nhắm một nguồn dữ liệu khác nhau và
ưu tiên **rẻ và chính xác trước**:

| Thứ tự | Script | Vá field | Đọc từ đâu | Chi phí |
| --- | --- | --- | --- | --- |
| 1 | `backfill_company_profiles.py` | `industry`, `company_size`, `address`, `website` (nhặt kèm `products_services`) | `source_profile_url` đã lưu (trang TopCV/VietnamWorks/CareerViet gốc) | Miễn phí, chỉ tốn thời gian chờ |
| 2 | `enrich_company_profile_from_website.py` | `industry`, `products_services` | `companies.website` + Gemini phân loại | Rẻ (1 lần gọi Gemini mỗi công ty, không dùng Tavily) |
| 3 | `enrich_company_web_info.py` | `website`, `tax_id` | Tavily search (2 query mỗi công ty) + Gemini trích xuất | Tốn nhất; chỉ nên chạy cho công ty không có `source_profile_url` |
| 4 | `get_company_fb_linkedin_link.py` | `fanpage_url`, `linkedin_url` | `companies.website` (HTML thô) | Miễn phí, hạn chế với website SPA/React |

Mỗi script chỉ chọn công ty **còn thiếu đúng field nó vá được**, nên chạy
lại nhiều lần an toàn và không tốn thêm gì cho công ty đã đủ dữ liệu.

---

## Kiến trúc

```
config.py                  <- ngành/category (JOB_CATEGORIES), độ trễ request, ngưỡng ngắt mạch,
                              snapshot, model AI... (đọc từ biến môi trường, xem .env.example)
sources_registry.py        <- nguồn sự thật DUY NHẤT để đăng ký nguồn crawl (SOURCES);
                              main.py và toàn bộ api/ import từ đây
models.py                  <- RawJobRecord: khuôn dữ liệu chung mọi adapter phải trả về
adapters/                  <- base.py (BaseAdapter: session curl_cffi, _throttle(), _fetch_html()
                              với retry/backoff, ngắt mạch, ghi snapshot) + topcv.py,
                              vietnamworks.py, careerviet.py (mỗi adapter chỉ chứa logic parse riêng)
normalize.py               <- dùng chung: parse lương, suy luận level, deadline, work_type
pipeline.py                <- nối adapter -> normalize -> db; xử lý từng job theo các bước nhỏ
                              (_process_job -> _import_new_job -> _import_repost / _insert_new_job)
pipeline_stats.py          <- PipelineStats: bộ đếm của một lượt crawl (dataclass, gõ sai tên báo lỗi
                              ngay); run_pipeline() vẫn trả dict qua to_dict()
field_stats.py             <- đếm tỷ lệ field rỗng, quyết định lượt crawl có "degraded" không
snapshots.py               <- SnapshotRecorder: giữ mẫu HTML/JSON gốc của mỗi lượt crawl
main.py                    <- CLI: init-db, migrate, crawl, stats, snapshots, snapshot-export, create-admin

db/                        <- mọi thao tác PostgreSQL, tách theo domain
  connection.py            <- connection, connection pool, apply_schema, migration tracking
  jobs.py, companies.py    <- GHI job / công ty (insert, update, gộp, probe cho pipeline)
  job_queries.py, company_queries.py       <- ĐỌC danh sách / chi tiết cho API (chỉ SELECT)
  job_health.py, company_analytics.py      <- thống kê "tình trạng dữ liệu", tín hiệu hợp tác
  company_enrichment.py    <- chọn công ty cần bổ sung thông tin cho các script enrich_*
  contacts.py, auth.py, audit_logs.py, applications.py, messages.py,
  email_templates.py, dashboard.py, stats.py, lookups.py   <- theo domain
  crawl_runs.py, crawl_batches.py, crawl_snapshots.py, maintenance_runs.py
  __init__.py              <- re-export toàn bộ tên, dùng qua `import db`

backfill_company_profiles.py             <- vá profile công ty qua source_profile_url đã lưu
enrich_company_profile_from_website.py   <- vá industry/products_services qua website + Gemini
enrich_company_web_info.py               <- vá website/tax_id qua Tavily + Gemini
get_company_fb_linkedin_link.py          <- vá fanpage/LinkedIn qua website
check_expired_source_jobs.py             <- re-check job OPEN còn sống ở nguồn không

api/                       <- lớp API FastAPI (chi tiết xem API_README.md)
  app.py                   <- entry point: xác thực, CORS, router, lifespan
  auth.py, security.py     <- API key tĩnh; băm mật khẩu, ký/verify JWT
  email_service.py         <- gửi email xác thực và quên mật khẩu qua Resend
  deps.py                  <- get_db(), get_current_user(), require_role()
  schemas/                 <- Pydantic models (request/response), tách theo domain
  crawl_runner.py          <- chạy pipeline crawl, theo dõi qua run_id (dùng chung cho nút Crawl
                              trên web và `python main.py crawl`)
  run_log.py               <- ghi log live của từng lượt chạy xuống DB, tách riêng theo lượt
  routers/                 <- jobs, companies, contacts, crawl, meta, auth, me
  services/                <- entity_specs, validation_engine, preview_manager, import_executor,
                              conflict_detector, company_resolver (luồng import CSV/XLSX), watchdog

sql/schema.sql             <- schema PostgreSQL đầy đủ, mới nhất (dựng DB mới)
sql/migration_*.sql        <- nâng cấp DB đã có (xem sql/README_MIGRATIONS.md)
tests/                     <- test parser, logic, CLI; một số test chạy trên Postgres thật
```

### Quy ước khi sửa code

Các quy ước dưới đây được test canh giữ; vi phạm thì `pytest` báo đỏ:

- **Một job = một transaction.** Các hàm `db.*` không tự commit hay rollback;
  `pipeline.py` quyết định. Mỗi nhánh có ghi DB commit đúng một lần ở cuối
  nhánh thành công, mọi lỗi rollback phần chưa commit của job đó. Thêm nhánh
  mới có ghi DB thì phải tự commit ở cuối nhánh (`tests/test_pipeline_transactions.py`).
- **`api/routers/` không chứa SQL thô.** SQL nằm ở `db/` (`tests/test_layering.py`).
- **Chỉ dùng một thư viện HTTP: `curl_cffi`.** Không import `requests`
  (`tests/test_http_library.py`).
- **Migration tạo bảng mới thì `schema.sql` cũng phải có bảng đó**
  (`tests/test_migrations.py`).

---

## 1. Crawl job

```bash
python main.py crawl --source topcv --category data-analyst --pages 3
python main.py crawl --source vietnamworks --category data-engineer --pages 3
python main.py crawl --source careerviet --category business-analyst --pages 3

# Giới hạn theo SỐ LƯỢNG JD thay vì số trang (tiện lấy mẫu nhỏ để thử):
python main.py crawl --source topcv --category data-analyst --max-jobs 20
```

- `--source`: `topcv` (mặc định), `vietnamworks` hoặc `careerviet`.
- `--category`: `data-analyst`, `data-engineer`, `data-scientist`,
  `software-engineering`, `business-analyst`, `ui-ux-design` (CareerViet
  chưa có `ui-ux-design`). Danh sách đầy đủ theo nguồn nằm trong `config.py`.
- `--pages`: số trang tối đa. Một trang TopCV khoảng 20-25 job, VietnamWorks
  khoảng 50 job.
- `--max-jobs`: giới hạn TỔNG số JD, dừng ngay khi đủ. Dùng riêng thì tự
  nới `--pages`; dùng cùng `--pages` thì dừng ở điều kiện nào tới trước.
- `--no-track`: chạy không ghi lịch sử (xem bên dưới), tiện khi thử nhanh.

### Mỗi lượt crawl làm gì

- Bỏ qua job đã crawl (theo link JD gốc) nhưng **vẫn vá thêm** `work_type`,
  `deadline`, nội dung JD nếu trước đó còn thiếu. Job thiếu field chỉ được
  fetch lại trang chi tiết nếu chưa từng ghi nhận lần fetch thành công hoặc
  lần gần nhất đã cách đây từ `DETAIL_RECHECK_DAYS` ngày (mặc định 7; đặt 0
  để fetch lại ở mọi lượt). Nhờ vậy tin mà nguồn vốn không ghi hạn nộp không
  bị fetch lại mãi, nhưng sửa xong selector hỏng thì tối đa chừng đó ngày
  sau job được vá. Lần fetch thất bại không được ghi nhận nên lượt sau thử
  lại ngay.
- Bỏ qua job của nhà tuyển dụng ẩn danh (ví dụ "Vietnamworks' Client").
- Crawl sâu trang chi tiết job (work_type, hạn ứng tuyển, mô tả, yêu cầu,
  quyền lợi, kỹ năng) và trang hồ sơ công ty (website, mã số thuế, quy mô,
  lĩnh vực, địa chỉ, mô tả sản phẩm/dịch vụ). Công ty chỉ crawl sâu lần
  đầu gặp hoặc khi còn thiếu field; luôn lưu `source_profile_url` để các
  script vá ở bước 2 dùng lại.
- Gộp công ty **ưu tiên theo mã số thuế**: hai job cùng công ty nhưng tên
  viết khác nhau vẫn nhận ra là một.
- Phát hiện job "đăng lại" (repost) dưới `source_url` khác: nếu trùng
  `company_id` + `job_title` + `level_id` + `province_id` với job đã có thì
  **không tạo job mới**. URL mới được ghi làm nguồn phụ của job cũ nên lượt
  sau không fetch lại; nếu bản đăng lại có hạn nộp muộn hơn thì deadline
  của job cũ được dời ra sau (không bao giờ rút ngắn), job đã quá hạn được
  đăng lại sẽ sống lại. Nội dung (mô tả, `work_type`) không vá từ bản đăng
  lại. Số lượng nằm ở `skipped_duplicate_repost` và `repost_deadline_extended`
  trong thống kê lượt chạy (xem [Giới hạn đã biết](#giới-hạn-đã-biết)).
- Chuẩn hoá chu kỳ trả lương (tháng/năm) từ text gốc.

### Lịch sử lượt chạy

`python main.py crawl` chạy qua cùng đường với nút *Crawl* trên web
(`api/crawl_runner.py`), nên mỗi lượt chạy trên máy cũng có một dòng trong
bảng `crawl_runs` (hiện ở trang `/crawl` của dashboard, cột người chạy để
trống), kèm log, snapshot và các cờ cảnh báo bên dưới. Mỗi nguồn chỉ chạy
**một lượt tại một thời điểm**, tính cả lượt bấm trên web. Bấm `Ctrl+C` thì
lượt được ghi nhận là lỗi ngay.

Nếu bảng `crawl_runs` chưa có, lệnh in cảnh báo, chạy không ghi lịch sử và
gợi ý chạy `python main.py migrate`.

Exit code của `python main.py crawl`:

| Code | Ý nghĩa |
| --- | --- |
| 0 | Chạy xong |
| 1 | Lỗi (hoặc nguồn đang có lượt khác chạy) |
| 2 | Bị chặn giữa chừng (xem ngắt mạch) |
| 130 | Dừng thủ công bằng `Ctrl+C` |

### Ngắt mạch khi bị chặn

Mỗi lần fetch thất bại (sau khi đã hết retry) tăng một bộ đếm; một request
thành công đặt lại bộ đếm về 0. Đủ `CRAWL_BLOCK_CONSECUTIVE_FAILURES` lần
liên tiếp (mặc định 3, đặt 0 để tắt) thì crawler dừng cả lượt và ném
`CrawlBlockedError`. Mục đích: khi IP đã bị chặn thì không tiếp tục đập vào
site và không tốn hàng giờ retry vô ích.

- Lượt dừng có `status = 'error'` và `stats.blocked = true`; số job đã lưu
  trước đó được giữ lại (`stats.fetched`, `stats.inserted`).
- Lượt thuộc một batch (crawl nhiều category) thì dừng cả batch, vì cùng
  nguồn là cùng IP.
- Trang 404/410 (tin đã gỡ) không retry và không tính là bị chặn.
- Khi bắt đầu lượt mới, nếu cùng nguồn vừa bị chặn trong
  `CRAWL_BLOCK_COOLDOWN_MINUTES` phút gần nhất (mặc định 60) thì log live
  ghi cảnh báo. Đây chỉ là cảnh báo, không chặn chạy, vì bạn có thể vừa đổi
  IP.

### Cảnh báo dữ liệu sai (degraded)

Khi site đổi giao diện, parser thường không báo lỗi mà trả field rỗng hàng
loạt. Lượt crawl được đánh dấu `stats.degraded` (kèm `reasons`) nếu
`job_title`, `company_name` hoặc `job_description` rỗng từ
`DEGRADED_EMPTY_RATE` (mặc định 0.9) trở lên với ít nhất 10 mẫu, hoặc trang
listing đầu parse ra 0 job. Lượt vẫn có `status = 'done'`, nên phải nhìn cờ
này (xem [Theo dõi chất lượng crawl](#theo-dõi-chất-lượng-crawl)) chứ không
chỉ nhìn trạng thái. Tên công ty placeholder `Chưa xác định` của TopCV được
tính là rỗng.

### Snapshot HTML gốc

Mỗi lượt lưu tối đa `SNAPSHOT_MAX_PER_RUN` (mặc định 6) mẫu HTML/JSON gốc
vào bảng `crawl_snapshots` (nén gzip, tự xoá sau `SNAPSHOT_RETENTION_DAYS`
= 14 ngày): trang listing đầu, trang chi tiết đầu, và các trang bất thường
(listing parse ra 0 job, chi tiết không có nội dung). Đặt
`SNAPSHOT_MAX_PER_RUN=0` để tắt. Lỗi khi ghi snapshot không bao giờ làm hỏng
lượt crawl.

```bash
python main.py snapshots --source careerviet            # liệt kê mẫu đã lưu
python main.py snapshots --run-id <run_id>              # mẫu của một lượt
python main.py snapshot-export 12 --out tests/fixture_careerviet_listing.html
```

Snapshot có thể chứa email, số điện thoại của nhà tuyển dụng: kiểm tra và
ẩn trước khi commit làm fixture.

---

## 2. Vá hồ sơ công ty

Bốn script độc lập, không nằm trong pipeline crawl chính, chạy khi cần.
So sánh nhanh ở bảng trong [Quy trình đầu-cuối](#quy-trình-đầu-cuối).

### `backfill_company_profiles.py`

```bash
python backfill_company_profiles.py --limit 10   # thử ít công ty
python backfill_company_profiles.py              # chạy đầy đủ
```

Vá `industry`, `company_size`, `address`, `website` (nhặt kèm
`products_services`) cho công ty **đã có** `source_profile_url` nhưng còn
thiếu ít nhất một trong bốn field đầu, bằng cách gọi lại
`fetch_company_profile()` trên đúng URL đã lưu. Miễn phí, không tốn
Tavily/Gemini; nên dùng trước các script còn lại vì chính xác hơn (đọc
thẳng trang gốc, không qua search và LLM suy luận).

### `enrich_company_profile_from_website.py`

```bash
python enrich_company_profile_from_website.py --limit 50
python enrich_company_profile_from_website.py
```

Vá `industry` và `products_services` cho công ty **đã có `website`** nhưng
còn thiếu một trong hai, bằng cách đọc trang chủ/giới thiệu của chính
website đó rồi nhờ Gemini phân loại. Không cần Tavily nên rẻ hơn
`enrich_company_web_info.py`. Đặc biệt cần cho công ty nguồn CareerViet
(trang công ty CareerViet không hiển thị `industry`). Điều kiện chọn công ty
là OR: thiếu `industry` HOẶC thiếu `products_services` đều được chọn.

### `enrich_company_web_info.py`

```bash
python enrich_company_web_info.py --limit 10
python enrich_company_web_info.py
```

Vá `website` và `tax_id` cho công ty còn thiếu, bằng Tavily search (2 query
riêng mỗi công ty) và Gemini trích xuất ra JSON, confidence tách riêng từng
field. Chỉ lưu kết quả tin cậy `high`/`medium`, `tax_id` đúng định dạng mã
số doanh nghiệp VN, `website` không thuộc mạng xã hội, trang tuyển dụng hay
trang tra mã số thuế. `tax_id` trùng công ty khác đã có trong DB thì tự gộp
(chuyển job/contact sang công ty gốc, xoá công ty trùng).

Tốn nhất trong 4 script (Tavily credit + Gemini quota): chỉ nên chạy cho
công ty **không có** `source_profile_url` (tạo tay qua `POST /companies`,
hoặc crawl từ nguồn không hỗ trợ `fetch_company_profile`). Cần
`TAVILY_API_KEY` và `GEMINI_API_KEY` trong `.env`.

### `get_company_fb_linkedin_link.py`

```bash
python get_company_fb_linkedin_link.py --limit 10
python get_company_fb_linkedin_link.py
```

Vá `fanpage_url` và `linkedin_url` cho công ty **đã có `website`**, bằng
cách vào thẳng website tìm link Facebook/LinkedIn thật (không đoán qua
Google để tránh bắt nhầm trang công ty khác trùng tên). Không có website
hoặc không có link social thì để trống.

**Giới hạn:** fetch HTML thô, không chạy JavaScript. Website dạng SPA/CSR
(React, Next.js, Vue...) render link social bằng JS sẽ không tìm thấy gì dù
link có thật khi mở bằng trình duyệt.

---

## 3. Dọn job hết hạn

**`check_expired_source_jobs.py`**: nên chạy sau mỗi đợt crawl hoặc định kỳ
(cron hằng ngày). JD trên nguồn bị nhà tuyển dụng xoá sau một thời gian
nhưng DB không tự biết, nên job vẫn hiện `OPEN` dù link nguồn đã chết.

```bash
python check_expired_source_jobs.py --dry-run          # xem thử, KHÔNG ghi DB
python check_expired_source_jobs.py                    # chạy thật
python check_expired_source_jobs.py --check-deadline   # chỉ check deadline, không fetch mạng, nhanh hơn
python check_expired_source_jobs.py --limit 20         # giới hạn số job xử lý, để thử
```

**Nguyên tắc "thà thiếu còn hơn sai"**: chỉ tự chuyển `EXPIRED` khi tín hiệu
không mơ hồ:

- `source_url` trả HTTP 404/410 thì `EXPIRED`.
- Deadline job đã qua (mặc định hoặc với `--check-deadline`) thì `EXPIRED`.

Mọi trường hợp khác (200 kèm redirect, timeout, 403 bị chặn bot, 5xx tạm
lỗi...) không kết luận, đếm vào `needs_manual_check` để soát tay. Dùng
`EXPIRED` (hết hiệu lực tự nhiên) chứ không phải `CLOSED` (team SS chủ động
đóng qua frontend) để sau này lọc và báo cáo phân biệt được lý do đóng job.

---

## Theo dõi chất lượng crawl

Sau vài lượt crawl, xem các lượt bị chặn hoặc nghi dữ liệu sai (chạy trong
Supabase SQL Editor hoặc `psql`):

```sql
SELECT started_at::date AS ngay, source, category, status,
       (stats ? 'blocked')                 AS bi_chan,
       stats->'degraded'->'reasons'        AS ly_do_degraded,
       stats->>'fetched'                   AS fetched,
       stats->>'inserted'                  AS inserted,
       error
FROM crawl_runs
WHERE stats ? 'blocked' OR stats ? 'degraded'
ORDER BY started_at DESC
LIMIT 20;
```

Cách đọc:

- **`bi_chan = true`**: IP đang bị nguồn chặn. Đổi mạng/IP, chờ một lúc rồi
  chạy lại. Nếu TopCV bị chặn thường xuyên khi chạy từ Render mà chạy từ
  máy cá nhân vẫn tốt, hãy crawl từ máy cá nhân.
- **`ly_do_degraded` có giá trị**: selector có thể đã hỏng. Xem
  [Debug khi nguồn crawl đổi giao diện](#debug-khi-nguồn-crawl-đổi-giao-diện).
  Nếu cảnh báo nhầm hoặc bỏ sót, chỉnh `DEGRADED_EMPTY_RATE`.

---

## Xem kết quả

```bash
python main.py stats
```

Hoặc bằng SQL:

```sql
SELECT job_title, company_id, salary_min, salary_max, salary_type, salary_period
FROM job_postings ORDER BY created_at DESC LIMIT 10;

SELECT company_name, tax_id, website, company_size, industry
FROM companies ORDER BY created_at DESC LIMIT 10;

SELECT matching_industry, count(*) FROM job_postings GROUP BY matching_industry;

-- Soát job nghi trùng (khác link nguồn nhưng cùng nội dung)
SELECT * FROM v_duplicate_job_candidates;
```

Số liệu độ phủ dữ liệu theo thời gian thực (theo nguồn crawl, job hết hạn,
nghi trùng) lấy qua `GET /jobs/data-health` và `GET /companies/data-health`
(xem `API_README.md`); đây là hai endpoint dùng cho tab "Tình trạng dữ
liệu" của frontend.

---

## Thêm ngành / nguồn crawl mới

### Thêm ngành (category) cho nguồn đã có

`config.py` khai báo category theo kiểu **category-first**: dict
`JOB_CATEGORIES` khai báo mỗi category **một lần**, kèm sub-dict `sources`
liệt kê nguồn nào crawl được category đó:

```python
"business-analyst": {
    "label": "Business Analysis",
    "matching_industry": "Business Analysis",
    "sources": {
        "topcv": {"url": "https://www.topcv.vn/tim-viec-lam-business-analyst-<mã-category-thật>"},
        "vietnamworks": {"query": "business analyst"},
        "careerviet": {"keyword": "business-analyst"},
    },
},
```

- URL category TopCV: vào https://www.topcv.vn/viec-lam, mục "Danh mục
  Nghề", chọn ngành, copy URL kết quả.
- VietnamWorks chỉ cần `query` (chuỗi tìm kiếm). CareerViet chỉ cần
  `keyword` (URL dạng `careerviet.vn/viec-lam/<keyword>-k-vi.html`; tự kiểm
  tra URL đó có ra kết quả thật trước khi thêm).
- Nguồn nào **không** crawl được category này thì bỏ qua key đó trong
  `sources` (ví dụ `ui-ux-design` không có `careerviet`), không thêm dict
  rỗng hay giá trị giả.

`TOPCV_CATEGORIES`, `VIETNAMWORKS_CATEGORIES`, `CAREERVIET_CATEGORIES` là
ba "view" tự sinh từ `JOB_CATEGORIES` (hàm `_categories_for_source()`),
nên adapter và `main.py` không cần sửa khi thêm category.

### Thêm hẳn một nguồn crawl mới (ví dụ ITviec)

Việc đăng ký nguồn nằm trong **một module duy nhất**: `sources_registry.py`.

1. Thêm bộ category cho nguồn mới vào `config.py` (theo cấu trúc `sources`
   ở trên, hoặc một dict `{key: {..., "matching_industry": ...}}` riêng nếu
   nguồn mới không dùng chung bộ category).
2. Viết `adapters/itviec.py`, kế thừa `BaseAdapter` (`adapters/base.py`).
   Session `curl_cffi`, `_throttle()`, `_fetch_html()` (retry/backoff),
   ngắt mạch và ghi snapshot đã có sẵn ở lớp cha; chỉ cần implement
   `fetch_jobs()` với logic parse riêng.
3. Thêm đúng một entry vào `SOURCES` trong `sources_registry.py`:

   ```python
   "itviec": {"adapter_cls": ITViecAdapter, "categories": ITVIEC_CATEGORIES},
   ```

Xong: `main.py` và toàn bộ `api/` (crawl runner, router `/crawl`, router
`/sources`) tự thấy nguồn mới; không cần sửa `normalize.py`, `db/`,
`pipeline.py`. Ngoại lệ duy nhất (vì là hai repo tách biệt): frontend
`mindx-jobs` cần tự thêm nhãn hiển thị ở `blueprints/crawl.py::_SOURCE_LABELS`.

---

## Debug khi nguồn crawl đổi giao diện

Cả 3 adapter bám theo **pattern URL** và **nhãn tiếng Việt** (`"Mã số
thuế"`, `"Quy mô"`...) thay vì tên class CSS, nên bền hơn khi trang
redesign. Khi một lượt crawl ra 0 kết quả hoặc thiếu field hàng loạt (lượt
có cờ `degraded`):

1. Lấy HTML thật của đúng thời điểm đó từ snapshot:

   ```bash
   python main.py snapshots --source <nguồn>
   python main.py snapshot-export <id> --out tests/fixture_<tên>.html
   ```

   Các lượt crawl chạy từ máy cũng có snapshot; nếu thiếu, mở URL trong
   trình duyệt, chọn "View Page Source" (không phải Inspect Element, vì cần
   đúng HTML server trả về).
2. Đối chiếu pattern URL hoặc nhãn tiếng Việt trong `adapters/topcv.py`,
   `adapters/vietnamworks.py`, `adapters/careerviet.py` với HTML thật, sửa
   cho khớp. Nếu trang có JSON nhúng trong HTML, ưu tiên đọc từ JSON thay
   vì từ DOM.
3. Ẩn email/số điện thoại trong file fixture, đặt vào `tests/`, rồi chạy
   `python -m pytest -q` trước khi crawl thật.

---

## Deploy production

- **Backend**: Render Web Service, build từ repo này (`Koaito/scrap-jd`,
  GitHub private). Biến môi trường cấu hình trực tiếp trên Render, **không**
  qua `.env` (file đó chỉ dùng local, `.gitignore` đã chặn commit). URL
  public: `https://scrap-jd-api.onrender.com`.
- **Frontend**: repo `mindx-jobs` (Flask), deploy trên Vercel, gọi API qua
  `Authorization: Bearer` (JWT). `ALLOWED_ORIGINS` trên Render phải trỏ
  đúng domain Vercel thật, nếu không trình duyệt bị chặn bởi CORS dù key
  đúng.
- **Trước mỗi lần deploy code có đổi schema**: chạy
  `python main.py migrate --check`, nếu còn thiếu thì `python main.py migrate`
  với biến `PG*` trỏ vào DB production.
- **TopCV và IP của Render**: TopCV có thể trả 403 cho IP của Render nhưng
  vẫn chạy được từ máy cá nhân. Nếu bị chặn thường xuyên, crawl từ máy cá
  nhân (lượt chạy vẫn được ghi vào `crawl_runs` và hiện trên dashboard).

Chi tiết danh sách biến môi trường và endpoint xem `API_README.md`.

---

## Giới hạn đã biết

- **Khoá nhận diện tin đăng lại khá thô.** Hai tin khác nhau của cùng công
  ở cùng tỉnh, cùng cấp bậc và cùng tên vị trí bị coi là một job, nên có thể
  gộp nhầm. Dữ liệu gốc của tin bị gộp vẫn được giữ trong nguồn phụ để soát
  lại. Theo dõi `skipped_duplicate_repost` trên trang `/crawl` trước khi
  quyết định siết khoá. Bước kiểm tra này nằm sau bước fetch chi tiết và hồ
  sơ công ty, nên một tin đăng lại tốn 1-2 request ở lần đầu gặp.
- **URL fetch chi tiết thất bại bị thử lại ở mọi lượt** (ví dụ job đã gỡ
  khỏi nguồn, trả 404), vì lỗi tạm thời cần thử lại ngay nên không ghi dấu.
- **Một nguồn một lượt tại một thời điểm.** Ràng buộc này chỉ áp dụng cho
  lượt có ghi `crawl_runs`. Lượt chạy `--no-track` hoặc gọi thẳng
  `run_pipeline()` không có khoá này; không chạy chồng hai lượt cùng nguồn
  (race condition có thể sinh job trùng, soát bằng `v_duplicate_job_candidates`).
- **Log live chỉ gắn đúng lượt chạy với code chạy cùng luồng.** Code chạy
  trong thread con do chính job tạo ra không thừa kế dấu này nên log của
  nó không vào log của lượt (hiện crawl và bảo trì không làm vậy; xem
  `api/run_log.py`).
- **Fixture VietnamWorks và CareerViet trong `tests/` là dữ liệu tổng hợp**,
  nên test parser chưa chứng minh selector còn đúng với site thật. Thay
  bằng HTML thật qua `snapshot-export`.
- **Ngưỡng `degraded` (90%) chưa được hiệu chỉnh bằng dữ liệu thật dài
  hạn.** Chỉnh bằng `DEGRADED_EMPTY_RATE` nếu cảnh báo nhầm hoặc bỏ sót.
- **Tốc độ TopCV bị giới hạn có chủ đích** (khoảng 12 giây cộng ngẫu nhiên
  mỗi request, mỗi job mới tốn 1-2 request, tức khoảng 130-250 job mỗi giờ)
  để tránh bị chặn. Đổi ngôn ngữ hoặc dùng async không làm số này nhanh hơn.
- **`company_size`, `address`, `linkedin_url` thường còn thiếu nhiều**, vì
  nguồn crawl không phải lúc nào cũng có sẵn. Chạy các script ở bước 2 để
  vá thêm, hoặc chấp nhận để trống.
- **Trạng thái "blocked" và "degraded" nằm trong `crawl_runs.stats`**, không
  phải một giá trị riêng của `crawl_status_enum`. Frontend muốn hiện huy
  hiệu riêng cần đọc `stats.blocked` và `stats.degraded`.
