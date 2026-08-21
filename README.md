# Job Crawler — Student Success

Crawler job từ **TopCV**, **VietnamWorks**, **CareerViet** (6 ngành: Data
Analyst, Data Engineer, Data Scientist, Software Engineering, Business
Analysis, UI/UX Design), chuẩn hoá dữ liệu, crawl sâu hồ sơ công ty
(website, mã số thuế, quy mô, lĩnh vực, địa chỉ), lưu vào PostgreSQL —
expose ra ngoài qua 1 lớp **API FastAPI có auth**, có **frontend
dashboard** riêng gọi vào API này.

- **Backend** (repo này) — pipeline crawl + API, deploy trên Render:
  `https://scrap-jd-api.onrender.com`
- **Frontend** (repo `mindx-jobs`) — deploy trên Vercel, gọi API qua JWT.

Chi tiết API (endpoint, body, auth) xem `API_README.md`. File này tập
trung vào pipeline crawl + các script bổ trợ + quy trình vận hành.

## Mục lục

- [Quy trình đầu-cuối](#quy-trình-đầu-cuối)
- [Kiến trúc](#kiến-trúc)
- [Cài đặt](#cài-đặt)
- [1. Crawl job](#1-crawl-job)
- [2. Vá hồ sơ công ty](#2-vá-hồ-sơ-công-ty)
- [3. Dọn job hết hạn](#3-dọn-job-hết-hạn)
- [Xem kết quả](#xem-kết-quả)
- [Thêm ngành / nguồn crawl mới](#thêm-ngành--nguồn-crawl-mới)
- [Debug khi nguồn crawl đổi giao diện](#debug-khi-nguồn-crawl-đổi-giao-diện)
- [Deploy production](#deploy-production)
- [Tình trạng dữ liệu &amp; giới hạn đã biết](#tình-trạng-dữ-liệu--giới-hạn-đã-biết)
- [Lịch sử bug đã sửa](#lịch-sử-bug-đã-sửa)

---

## Quy trình đầu-cuối

Thứ tự chạy thực tế, từ DB trống tới dữ liệu đầy đủ sẵn sàng cho dashboard:

```bash
# 0. Chỉ 1 lần lúc khởi tạo
python main.py init-db
python main.py create-admin --email admin@congty.vn --name "Nguyễn Văn A"

# 1. Crawl job — chạy cho từng nguồn/ngành cần, lặp lại định kỳ
python main.py crawl --source topcv --category data-analyst --pages 3
python main.py crawl --source vietnamworks --category data-engineer --pages 5
python main.py crawl --source careerviet --category business-analyst --pages 3

# 2. Vá hồ sơ công ty còn thiếu field — chạy SAU crawl, theo đúng thứ tự dưới
#    (script sau chỉ xử lý công ty script trước không vá được)
python backfill_company_profiles.py
python enrich_company_profile_from_website.py
python enrich_company_web_info.py
python get_company_fb_linkedin_link.py

# 3. Dọn job hết hạn — chạy định kỳ (cron hằng ngày là hợp lý)
python check_expired_source_jobs.py --dry-run   # xem thử trước
python check_expired_source_jobs.py              # chạy thật
```

Bước 1 và bước 2 độc lập với nhau về mặt kỹ thuật (không script nào chặn
script nào chạy trước), nhưng **chạy bước 2 sau bước 1** thì hiệu quả hơn
— công ty vừa crawl xong luôn có `source_profile_url`, giúp
`backfill_company_profiles.py` (rẻ nhất, đọc lại đúng trang gốc) xử lý
được nhiều nhất trước khi phải tới các script tốn tài nguyên hơn.

Lý do có 4 script riêng ở bước 2 thay vì gộp làm 1 — mỗi script nhắm 1
nguồn dữ liệu khác nhau, ưu tiên **rẻ + chính xác trước**:

| Thứ tự | Script                                     | Vá field                                                                                     | Đọc từ đâu                                                                    | Chi phí                                                                    |
| -------- | ------------------------------------------ | --------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| 1        | `backfill_company_profiles.py`           | `industry`, `company_size`, `address`, `website` (+ `products_services` nhặt kèm) | `source_profile_url` đã lưu (đúng trang TopCV/VietnamWorks/CareerViet gốc) | Miễn phí — chỉ tốn thời gian chờ                                     |
| 2        | `enrich_company_profile_from_website.py` | `industry`, `products_services`                                                           | `companies.website` + Gemini phân loại                                         | Rẻ (1 lần gọi Gemini/công ty, không Tavily)                            |
| 3        | `enrich_company_web_info.py`             | `website`, `tax_id`                                                                       | Tavily search (2 query/công ty) + Gemini trích xuất                             | Tốn nhất — chỉ nên chạy cho công ty không có`source_profile_url` |
| 4        | `get_company_fb_linkedin_link.py`        | `fanpage_url`, `linkedin_url`                                                             | `companies.website` (crawl HTML thô)                                            | Miễn phí, giới hạn với site SPA/React                                  |

Mỗi script chỉ chọn công ty **còn thiếu đúng field nó vá được** — chạy
lại nhiều lần an toàn, không tốn thêm gì cho công ty đã đủ dữ liệu.

---

## Kiến trúc

```
config.py                        <- ngành/category muốn crawl, delay, model AI...
models.py                        <- RawJobRecord: khuôn dữ liệu chung mọi adapter phải trả về
adapters/                        <- topcv.py, vietnamworks.py, careerviet.py (implement BaseAdapter)
normalize.py                     <- dùng chung: parse lương, suy luận level, deadline, work_type
db.py                            <- dùng chung: mọi thao tác PostgreSQL
pipeline.py                      <- nối adapter -> normalize -> db
main.py                          <- CLI chạy crawl

backfill_company_profiles.py             <- script riêng: vá profile công ty qua source_profile_url đã lưu
enrich_company_profile_from_website.py   <- script riêng: vá industry/products_services qua website + Gemini
enrich_company_web_info.py               <- script riêng: vá website/tax_id qua Tavily + Gemini
get_company_fb_linkedin_link.py          <- script riêng: vá fanpage/LinkedIn qua website
check_expired_source_jobs.py             <- script riêng: re-check job OPEN còn sống ở nguồn không

api/                              <- lớp API FastAPI (chi tiết xem API_README.md)
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
tests/                            <- test parser + logic, không cần DB/internet
```

Muốn thêm nguồn crawl mới (ITviec...): viết `adapters/itviec.py` implement
`fetch_jobs()`, khai báo trong `SOURCES` ở `main.py` — không cần sửa
`normalize.py`, `db.py`, `pipeline.py`.

---

## Cài đặt

1. **PostgreSQL** — cài local (`brew install postgresql@16` / `apt install postgresql` / [Windows installer](https://www.postgresql.org/download/windows/))
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

   - Chạy `enrich_company_profile_from_website.py` hoặc
     `enrich_company_web_info.py`: cần `GEMINI_API_KEY`. Riêng
     `enrich_company_web_info.py` cần thêm `TAVILY_API_KEY`.
   - Chạy **API layer**: bắt buộc `API_KEY`, `ALLOWED_ORIGINS`,
     `JWT_SECRET_KEY` (xem `API_README.md`). Bật đăng ký công khai + quên
     mật khẩu qua email thật: thêm `RESEND_API_KEY`/`EMAIL_FROM`/`API_BASE_URL`.
4. **Tạo bảng**:

   ```bash
   python main.py init-db
   ```

   Kỳ vọng: `✅ Đã tạo/cập nhật schema trong database.`

   > DB tạo từ bản cũ (trước khi có lớp auth/audit/role/đăng ký/quên mật
   > khẩu/đổi tên bảng/ứng tuyển/lưu job/products_services) cần chạy thêm
   > `sql/migration_*.sql` — xem comment đầu mỗi file để biết chi tiết.
   > Chạy **đúng thứ tự sau** (migration sau phụ thuộc bảng/cột migration
   > trước tạo ra):
   >
   > ```bash
   > psql -U postgres -d "..." -f sql/migration_add_auth.sql
   > psql -U postgres -d "..." -f sql/migration_add_audit_columns.sql
   > psql -U postgres -d "..." -f sql/migration_add_role_hierarchy.sql
   > psql -U postgres -d "..." -f sql/migration_add_email_verification.sql
   > psql -U postgres -d "..." -f sql/migration_rename_ss_team_members.sql
   > psql -U postgres -d "..." -f sql/migration_add_applications_saved_jobs.sql
   > psql -U postgres -d "..." -f sql/migration_add_phone_track.sql
   > psql -U postgres -d "..." -f sql/migration_add_password_reset.sql
   > psql -U postgres -d "..." -f sql/migration_add_tax_id.sql
   > psql -U postgres -d "..." -f sql/migration_add_work_type_deadline.sql
   > psql -U postgres -d "..." -f sql/migration_update_provinces_2025.sql
   > psql -U postgres -d "..." -f sql/migration_add_salary_period.sql
   > psql -U postgres -d "..." -f sql/migration_add_products_services.sql
   > ```
   >
   > Thiếu bất kỳ file nào ở trên có thể làm `POST`/`PATCH /jobs`,
   > `POST /companies`, CRUD `/companies/{id}/contacts`,
   > `POST /auth/register`, đăng nhập, quên mật khẩu, ứng tuyển, lưu job,
   > hoặc crawl/enrich lỗi 500 — tuỳ file nào bị thiếu. Riêng thiếu
   > `migration_add_salary_period.sql` hoặc `migration_add_products_services.sql`:
   > **crawl lỗi 500 ngay lập tức** (`insert_job()`/`update_company_profile()`
   > luôn ghi 2 cột này không điều kiện, không có nhánh fallback). DB tạo
   > mới hoàn toàn từ `sql/schema.sql` (bước 4 ở trên) đã có sẵn đầy đủ,
   > **không cần** chạy lại các migration này.
   >
   > ⚠️ **KHÔNG chạy `sql/migration_drop_products_services.sql`** — file
   > này còn trên đĩa như lịch sử, nhưng chạy nó sẽ `DROP COLUMN`
   > đúng cột `products_services` mà pipeline/enrich đang chủ động ghi
   > vào, gây lỗi 500 ngay khi crawl.
   >
5. **Test** (không cần DB/internet):

   ```bash
   python tests/test_parse_and_normalize.py
   python tests/test_merge_companies.py
   ```

   Kỳ vọng `✅ PASS` cho cả 2.

---

## 1. Crawl job

```bash
python main.py crawl --source topcv --category data-analyst --pages 3
python main.py crawl --source vietnamworks --category data-engineer --pages 3
python main.py crawl --source careerviet --category business-analyst --pages 3

# Giới hạn theo SỐ LƯỢNG JD thay vì theo trang (tiện lấy mẫu nhỏ để test):
python main.py crawl --source topcv --category data-analyst --max-jobs 20
```

- `--source`: `topcv` (mặc định), `vietnamworks`, hoặc `careerviet`.
- `--category`: xem danh sách trong `config.py` (`TOPCV_CATEGORIES` /
  `VIETNAMWORKS_CATEGORIES` / `CAREERVIET_CATEGORIES`). Hiện có:
  `data-analyst`, `data-engineer`, `data-scientist`,
  `software-engineering`, `business-analyst`, `ui-ux-design`.
- `--pages`: số trang tối đa. 1 trang TopCV ~20-25 job, VietnamWorks
  ~50 job.
- `--max-jobs`: giới hạn TỔNG SỐ JD, dừng ngay khi đủ — không cần đợi hết
  `--pages`. Dùng riêng thì tự nới `--pages` đủ lớn; dùng cùng lúc cả 2 cờ
  thì dừng ở điều kiện nào tới trước.

Mỗi lần crawl, hệ thống tự động:

- Bỏ qua job đã crawl trước đó (theo link JD gốc), nhưng **vẫn vá thêm**
  `work_type`/`deadline`/nội dung JD nếu trước đó còn thiếu.
- Bỏ qua job của nhà tuyển dụng ẩn danh (vd "Vietnamworks' Client").
- Crawl sâu trang chi tiết job (work_type, hạn ứng tuyển, mô tả, yêu cầu,
  quyền lợi, kỹ năng) và trang hồ sơ công ty (website, mã số thuế, quy
  mô, lĩnh vực, địa chỉ, mô tả sản phẩm/dịch vụ) — công ty chỉ crawl sâu
  lần đầu gặp hoặc khi còn thiếu field, và luôn ghi lại
  `source_profile_url` để các script vá ở bước 2 dùng lại sau này.
- Match công ty **ưu tiên theo mã số thuế** — 2 job cùng công ty nhưng
  tên viết khác nhau vẫn nhận ra là 1, không tạo trùng.
- Phát hiện job "đăng lại" (repost) dưới `source_url` khác — nếu job mới
  crawl trùng `company_id` + `job_title` + `level_id` + `province_id` với
  job đã có, **bỏ qua, không insert job mới**.
- Chuẩn hoá đúng chu kỳ trả lương (tháng/năm) từ text gốc.

**⚠️ Chưa an toàn khi chạy song song 2 lượt crawl CÙNG LÚC** (race
condition thuần tuý — 2 request đọc DB "chưa có job này" cùng lúc, trước
khi bên nào kịp ghi). Tạm thời: chỉ chạy 1 lượt crawl/lúc, soát bằng
`v_duplicate_job_candidates` nếu nghi trùng.

---

## 2. Vá hồ sơ công ty

4 script độc lập, không nằm trong pipeline crawl chính, chạy khi cần —
xem bảng so sánh ở [Quy trình đầu-cuối](#quy-trình-đầu-cuối).

### `backfill_company_profiles.py`

```bash
python backfill_company_profiles.py --limit 10   # test thử ít công ty
python backfill_company_profiles.py               # chạy full
```

Vá `industry`/`company_size`/`address`/`website` (+ `products_services`
nhặt kèm) cho công ty **đã có** `source_profile_url` nhưng còn thiếu ít
nhất 1 trong 4 field đầu, bằng cách gọi lại `fetch_company_profile()`
trên đúng URL đã lưu. Miễn phí, không tốn Tavily/Gemini — ưu tiên dùng
trước các script còn lại vì chính xác hơn hẳn (đọc thẳng trang gốc, không
qua search + LLM suy luận).

### `enrich_company_profile_from_website.py`

```bash
python enrich_company_profile_from_website.py --limit 50   # test thử ít công ty
python enrich_company_profile_from_website.py               # chạy full
```

Vá `industry`/`products_services` cho công ty **đã có `website`** nhưng
còn thiếu 1 trong 2 field, bằng cách đọc thẳng trang chủ/giới thiệu của
chính website đó rồi nhờ Gemini phân loại — không cần Tavily, rẻ hơn
`enrich_company_web_info.py`. Đặc biệt cần cho công ty nguồn CareerViet
(trang công ty CareerViet không hiển thị `industry`, nên
`backfill_company_profiles.py` không vá được field này cho nhóm công ty
đó). Điều kiện chọn công ty là OR: thiếu `industry` HOẶC thiếu
`products_services` đều được chọn lại.

### `enrich_company_web_info.py`

```bash
python enrich_company_web_info.py --limit 10   # test thử ít công ty
python enrich_company_web_info.py               # chạy full
```

Vá `website`/`tax_id` cho công ty còn thiếu, bằng Tavily search (2 query
riêng biệt/công ty) + Gemini trích xuất ra JSON, confidence tách riêng
từng field. Chỉ lưu kết quả tin cậy `high`/`medium`, `tax_id` đúng định
dạng mã số doanh nghiệp VN, `website` không thuộc mạng xã hội/trang
tuyển dụng/trang tra MST. `tax_id` trùng công ty khác đã có trong DB →
tự động gộp (chuyển job/contact sang công ty gốc, xoá công ty trùng).

Tốn nhất trong 4 script (Tavily credit + Gemini quota) — chỉ nên chạy
cho công ty **không có** `source_profile_url` nào (tạo tay qua
`POST /companies`, hoặc crawl từ nguồn không hỗ trợ
`fetch_company_profile`). Cần `TAVILY_API_KEY` (free tier tại
https://tavily.com) và `GEMINI_API_KEY` (https://aistudio.google.com)
trong `.env`.

### `get_company_fb_linkedin_link.py`

```bash
python get_company_fb_linkedin_link.py --limit 10   # test thử ít công ty
python get_company_fb_linkedin_link.py               # chạy full
```

Vá `fanpage_url`/`linkedin_url` cho công ty **đã có `website`**, bằng
cách vào thẳng website tìm link Facebook/LinkedIn thật (không đoán mò
qua Google — tránh bắt nhầm trang công ty khác trùng tên). Không có
website hoặc không có link social → để trống, không cố tìm cách khác.

**Giới hạn đã biết:** fetch HTML thô, không chạy JavaScript — website
dạng SPA/CSR (React/Next.js/Vue...) render link social bằng JS sau khi
tải trang sẽ không tìm thấy gì dù link thật sự tồn tại khi mở bằng trình
duyệt.

---

## 3. Dọn job hết hạn

**`check_expired_source_jobs.py`** — nên chạy sau mỗi đợt crawl (hoặc
định kỳ, vd cron hằng ngày). JD trên nguồn bị nhà tuyển dụng xoá sau 1
thời gian, nhưng DB không tự phát hiện — job vẫn hiện `OPEN` mãi dù link
nguồn đã chết.

```bash
python check_expired_source_jobs.py --dry-run        # xem thử, KHÔNG ghi DB
python check_expired_source_jobs.py                    # chạy thật
python check_expired_source_jobs.py --check-deadline  # chỉ check deadline, không fetch mạng — nhanh hơn
python check_expired_source_jobs.py --limit 20         # giới hạn số job xử lý, test trước
```

<<<<<<< HEAD
**Nguyên tắc "thà thiếu còn hơn sai"** — chỉ tự động chuyển `EXPIRED` khi
tín hiệu không mơ hồ:

- `source_url` trả về HTTP 404/410 (Gone) → `EXPIRED`.
- Deadline job đã qua (mặc định hoặc `--check-deadline`) → `EXPIRED`.

Mọi trường hợp khác (200 kèm redirect, timeout, 403 bị chặn bot, 5xx tạm
lỗi...) — **không** kết luận, đếm vào `cần_kiểm_tra_tay` để soát thủ
công. Dùng `EXPIRED` (job tự nhiên hết hiệu lực) chứ không phải `CLOSED`
(team SS chủ động đóng qua frontend) — 2 status khác nghĩa, để sau này
lọc/báo cáo phân biệt được lý do đóng job.
=======
**Nguyên tắc "thà thiếu còn hơn sai"** — chỉ tự động chuyển `CLOSED` khi
tín hiệu không mơ hồ:

- `source_url` trả về HTTP 404/410 (Gone) → `CLOSED`.
- Deadline job đã qua (mặc định hoặc `--check-deadline`) → `CLOSED`.

Mọi trường hợp khác (200 kèm redirect, timeout, 403 bị chặn bot, 5xx tạm
lỗi...) — **không** kết luận, đếm vào `cần_kiểm_tra_tay` để soát thủ
công.

> 08/2026: `job_status_enum` chỉ còn `OPEN`/`CLOSED` — đã bỏ `EXPIRED`
> (xem `sql/migration_remove_expired_job_status.sql`). Trước đây job
> "tự nhiên hết hiệu lực" (script này phát hiện) và job "SS chủ động
> đóng qua frontend" dùng 2 status khác nhau để phân biệt lý do đóng
> job; giờ gộp chung vào `CLOSED`, không còn phân biệt ở tầng
> `job_status` nữa.
>>>>>>> 30bf9a43af4e25374ed7eade1dce9557ac563b8a

---

## Xem kết quả

```bash
python main.py stats
```

Hoặc trực tiếp bằng `psql`:

```sql
SELECT job_title, company_id, salary_min, salary_max, salary_type, salary_period
FROM job_postings ORDER BY created_at DESC LIMIT 10;

SELECT company_name, tax_id, website, company_size, industry
FROM companies ORDER BY created_at DESC LIMIT 10;

SELECT matching_industry, count(*) FROM job_postings GROUP BY matching_industry;

-- Soát job nghi trùng (khác link nguồn nhưng cùng nội dung)
SELECT * FROM v_duplicate_job_candidates;
```

---

## Thêm ngành / nguồn crawl mới

Mở `config.py`, thêm vào `TOPCV_CATEGORIES` (hoặc
`VIETNAMWORKS_CATEGORIES`/`CAREERVIET_CATEGORIES`):

```python
"business-analyst": {
    "label": "Business Analyst",
    "url": "https://www.topcv.vn/tim-viec-lam-business-analyst-<mã-category-thật>",
    "matching_industry": "Business Analysis",
},
```

Lấy URL category TopCV thật: vào https://www.topcv.vn/viec-lam → "Danh
mục Nghề" → chọn ngành → copy URL kết quả. Với VietnamWorks chỉ cần
`query` (chuỗi tìm kiếm); với CareerViet chỉ cần `keyword` — xem ví dụ có
sẵn trong `config.py`.

Thêm hẳn 1 **nguồn** crawl mới (ITviec...): viết `adapters/itviec.py`
implement `fetch_jobs()` (dựa theo `adapters/base.py`), khai báo trong
`SOURCES` ở `main.py` — không cần sửa `normalize.py`, `db.py`,
`pipeline.py`.

## Debug khi nguồn crawl đổi giao diện

Cả 3 adapter bám theo **pattern URL** và **nhãn tiếng Việt** (`"Mã số thuế"`, `"Quy mô"`...) thay vì tên class CSS — bền hơn khi trang web
redesign. Nếu 1 ngày crawl ra 0 kết quả hoặc thiếu field:

1. Mở URL category/job/công ty bằng trình duyệt → "View Page Source"
   (không phải Inspect Element — cần đúng HTML server trả về).
2. So khớp lại pattern URL hoặc nhãn tiếng Việt trong
   `adapters/topcv.py`/`adapters/vietnamworks.py`/`adapters/careerviet.py`
   với HTML thật, sửa cho khớp.
3. Cập nhật fixture HTML mẫu trong `tests/`, chạy lại
   `python tests/test_parse_and_normalize.py` để xác nhận trước khi
   crawl thật.

---

## Deploy production

- **Backend**: Render Web Service, build từ repo này (`Koaito/scrap-jd`,
  GitHub private). Biến môi trường (Postgres, `API_KEY`,
  `ALLOWED_ORIGINS`, `JWT_SECRET_KEY`, Resend/Tavily/Gemini key...) cấu
  hình trực tiếp trên Render — **không phải** qua `.env` (file đó chỉ
  dùng local, `.gitignore` đã chặn commit). URL public:
  `https://scrap-jd-api.onrender.com`.
- **Frontend**: repo `mindx-jobs` (Flask), deploy trên Vercel, gọi API
  qua `Authorization: Bearer` (JWT). `ALLOWED_ORIGINS` trên Render cần
  trỏ đúng domain Vercel thật (không cập nhật → frontend bị chặn bởi
  CORS dù key đúng).

Chi tiết đầy đủ (thứ tự migration bắt buộc trước khi deploy bản có
JWT/phân quyền, danh sách biến môi trường) xem `API_README.md`.

---

## Tình trạng dữ liệu & giới hạn đã biết

Snapshot tại thời điểm viết (183 job / 134 công ty, crawl **6 ngành**
trên **TopCV + VietnamWorks**, CareerViet mới thêm sau nên chưa nằm
trong snapshot này):

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

**Giới hạn đang tồn tại:**

- **Chưa sửa race condition thật** (2 lượt crawl chạy chồng lên nhau
  cùng lúc) — xem cảnh báo ở [1. Crawl job](#1-crawl-job). Tạm thời chỉ
  chạy 1 lượt/lúc.
- **`company_size` (61%), `address` (46%), `linkedin_url` (28%) còn
  thiếu nhiều** — chạy `get_company_fb_linkedin_link.py` /
  `enrich_company_web_info.py` để vá thêm, hoặc chấp nhận vì nguồn crawl
  không phải lúc nào cũng có sẵn field này.
- 1 cặp job "Fullstack Developer" trùng nội dung (case repost cũ, sinh ra
  trước khi bug repost được vá — xem [Lịch sử bug đã
  sửa](#lịch-sử-bug-đã-sửa)) **chưa được dọn tay** — soát bằng
  `v_duplicate_job_candidates`.
- 2 record lương sai đã biết (iOS Dev, Vendor Development — do bug
  "/năm" đã sửa) và 4 job TopCV Brand Pro từng bị null nội dung (do bug
  selector đã sửa) **chưa được backfill/re-crawl lại** — code đã vá,
  data cũ trong DB vẫn cần dọn tay hoặc đợi lượt crawl định kỳ tiếp theo
  tự vá qua nhánh "vá job cũ còn thiếu field".

---

## Lịch sử bug đã sửa

Các mục dưới đây là **changelog**, không phải hướng dẫn vận hành — giữ
lại để biết dữ liệu cũ trong DB có thể còn sai sót gì cần soát tay.

<details>
<summary><strong>Sai đơn vị lương VietnamWorks (08/2026)</strong></summary>

`normalize_salary()` trước đây luôn nhân số VNĐ với 1.000.000 (giả định
mọi số ở đơn vị "triệu"). VietnamWorks có 2 định dạng `prettySalary`
khác nhau cho cùng đơn vị VNĐ — `"15tr-30tr ₫/tháng"` (có hậu tố "tr") và
`"12,000-30,000 ₫/tháng"` (đã ở đơn vị nghìn đồng, không hậu tố) — nhân
cứng 1 kiểu khiến case thứ 2 lệch 1000 lần. Đã sửa bằng cách suy luận hệ
số nhân theo độ lớn của chính con số — xem docstring `_vnd_multiplier()`
trong `normalize.py`. Job cũ bị lệch đơn vị trong DB đã được vá lại,
không còn tồn đọng.

</details>

<details>
<summary><strong><code>required_skills</code> bị lặp phần tử (08/2026)</strong></summary>

4 job có danh sách kỹ năng bị lặp phần tử trong `parsed_content`, nghi do
artifact khi parse DOM (TopCV) hoặc dữ liệu API trả kèm trùng
(VietnamWorks). Đã sửa bằng cách dedupe tại
`pipeline._build_parsed_content_and_raw()` — dùng chung cho mọi adapter.
Chỉ áp dụng cho job crawl mới sau này — job cũ trong DB (nếu còn) cần
soát tay bằng SQL nếu cần.

</details>

<details>
<summary><strong>Lương "/năm" bị hiểu nhầm thành lương/tháng (08/2026)</strong></summary>

`normalize_salary()` trước đây chỉ trích số ra khỏi text lương rồi suy
luận đơn vị tiền tệ (triệu/nghìn đồng) theo độ lớn con số, hoàn toàn
không đọc chu kỳ trả lương ("/tháng" hay "/năm") trong text gốc — mọi
mức lương crawl được mặc định coi là lương/tháng. 2 job thật ("iOS
Developer", "Vendor Development") có raw text `"200tr-500tr ₫/năm"` bị
lưu `salary_min`/`salary_max` y hệt như lương/tháng (sai lệch 12 lần).

Đã sửa bằng cách thêm cột `salary_period` (`MONTH`/`YEAR`) và detect tín
hiệu "/năm"/"annual"/"per year"/"yearly" trong text gốc — xem
`_YEARLY_SALARY_MARKER` trong `normalize.py`. `salary_min`/`salary_max`
GIỮ NGUYÊN con số gốc theo đúng chu kỳ đã detect, KHÔNG tự chia 12 để
quy đổi ra "tháng tương đương". 2 record sai đã biết (iOS Dev, Vendor
Development) chưa được backfill lại — vẫn cần soát tay hoặc đọc lại
`raw_jd_content` đã lưu sẵn trong `job_sources_log`.

</details>

<details>
<summary><strong>4 job TopCV "Brand Pro" bị null toàn bộ nội dung (08/2026)</strong></summary>

4 job có URL dạng `topcv.vn/brand/<company>/tuyen-dung/...` (trang "Brand
Pro" — gói trả phí cho nhà tuyển dụng) bị lưu `job_description`/
`requirements`/`perks`/`required_skills` rỗng hoàn toàn, dù HTML fetch
thành công. Nguyên nhân: `fetch_job_full_detail()` trong
`adapters/topcv.py` chỉ khớp selector class CSS của trang thường — trang
Brand Pro dùng template hoàn toàn khác.

Đã sửa bằng fallback: khi selector class CSS không tìm được nội dung,
thử lại bằng cách quét theo text heading (heading Brand Pro có khác biệt
nhỏ so với trang thường, vd "Quyền lợi được hưởng" thay vì "Quyền lợi
ứng viên"). Khi cả 2 cách đều không ra nội dung, log cảnh báo riêng
"template mismatch" thay vì âm thầm lưu NULL. 4 job cũ chưa được
re-crawl lại.

</details>

<details>
<summary><strong>VietnamWorks <code>typeWorkingId</code> lạ bị hiểu sai (08/2026)</strong></summary>

`typeWorkingId` (field số nguyên VietnamWorks trả về) chỉ được xác nhận
chắc chắn 2 giá trị: `1` = Toàn thời gian, `3` = Thực tập — các giá trị
khác trước đây bị map sai/để trống. Đối chiếu thực tế: các job có
`typeWorkingId` không phải `1`/`3`/`0` đều hiện đúng "Hình thức làm việc:
Khác" trên trang thật. Đã sửa: mọi `typeWorkingId` không khớp 2 giá trị
đã xác nhận → fallback về `"Khác"` (map sang `OTHER`) — xem
`_work_type_text_from_id()` trong `adapters/vietnamworks.py`.

</details>

<details>
<summary><strong>Job trùng nội dung do đăng lại — repost (08/2026)</strong></summary>

Cơ chế chống trùng của pipeline trước đây chỉ so khớp theo `source_url`
chính xác từng ký tự. TopCV/VietnamWorks gán `source_url`/job ID mới mỗi
khi nhà tuyển dụng "làm mới" tin đăng để đẩy lên top tìm kiếm — dù nội
dung JD giống hệt job cũ, hệ thống coi là 2 job riêng biệt và insert cả
hai (case thật: 2 job "Fullstack Developer" cùng công ty, cùng nội dung,
khác `source_url`, đăng cách nhau ~1 phút).

Đã sửa: sau khi resolve được `company_id`/`level_id`/`province_id` của
job mới, kiểm tra xem đã có job nào cùng bộ khoá này chưa (khớp đúng
công thức `generate_job_hash()`) — nếu có, bỏ qua hoàn toàn, không
insert job mới. Quyết định thiết kế: chỉ CHẶN insert trùng, chưa tự động
"vá" job cũ bằng nội dung/deadline mới từ lượt phát hiện repost này — vì
lượt đăng lại có thể đi kèm nội dung/deadline mới hơn thật sự, nhưng gộp
2 luồng "vá theo repost" và "vá theo thiếu field" cùng lúc phức tạp hơn
cần thiết cho lần sửa này.

</details>

<details>
<summary><strong>Thiếu cột <code>companies.products_services</code> trong schema (08/2026)</strong></summary>

`sql/schema.sql` bị bỏ sót cột `products_services` dù `pipeline.py` và
`enrich_company_profile_from_website.py` đã chủ động ghi vào cột này từ
trước — DB nào tạo mới hoàn toàn từ schema (trước bản vá) sẽ thiếu cột,
khiến crawl/enrich lỗi 500. Tệ hơn: từng có
`sql/migration_drop_products_services.sql` và README từng hướng dẫn
chạy nó khi setup DB mới — chạy đúng theo hướng dẫn cũ sẽ **xoá luôn**
cột mà code đang cần.

Đã sửa: thêm `products_services TEXT` vào `sql/schema.sql`, tạo
`sql/migration_add_products_services.sql` cho DB cũ, xoá dòng hướng dẫn
chạy migration DROP khỏi README/API_README.

</details>
