# Job Crawler — Student Success

Crawler job từ **TopCV** và **VietnamWorks** (Data Analyst / Data Engineer /
Software Engineering, dễ mở rộng sang ngành khác), chuẩn hóa dữ liệu, crawl
sâu hồ sơ công ty (website, mã số thuế, quy mô, lĩnh vực, địa chỉ), lưu vào
PostgreSQL.

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

### 5. Tạo bảng

```bash
python main.py init-db
```

Kỳ vọng: `✅ Đã tạo/cập nhật schema trong database.`

> Nếu bạn có DB tạo từ bản rất cũ (trước khi có cột `tax_id` hoặc
> `work_type`/`deadline`), chạy thêm các file trong `sql/migration_*.sql`
> tương ứng — xem comment đầu mỗi file để biết chạy khi nào.

### 6. Chạy test (không cần DB/internet)

```bash
python tests/test_parse_and_normalize.py
```

Kỳ vọng `✅ PASS`.

---

## Crawl job

```bash
python main.py crawl --source topcv --category data-analyst --pages 3
python main.py crawl --source vietnamworks --category data-engineer --pages 3
```

- `--source`: `topcv` (mặc định) hoặc `vietnamworks`.
- `--category`: ngành muốn crawl. Xem danh sách có sẵn trong `config.py`
  (`TOPCV_CATEGORIES` / `VIETNAMWORKS_CATEGORIES`). Hiện có: `data-analyst`,
  `data-engineer`, `software-engineering`.
- `--pages`: số trang tối đa. Bắt đầu với số nhỏ (2-3) để test.

Mỗi lần crawl, hệ thống tự động:

- Bỏ qua job đã crawl trước đó (theo link JD gốc), nhưng **vẫn vá thêm**
  `work_type`/`deadline`/nội dung JD cho job cũ nếu trước đó còn thiếu.
- Crawl sâu vào trang chi tiết job để lấy `work_type`, hạn ứng tuyển, mô tả
  công việc, yêu cầu, quyền lợi, kỹ năng cần có.
- Crawl sâu vào trang hồ sơ công ty (chỉ lần đầu gặp, hoặc khi còn thiếu
  field) để lấy website thật, mã số thuế, quy mô, lĩnh vực, địa chỉ.
- Match công ty **ưu tiên theo mã số thuế** — nếu 2 job cùng 1 công ty
  nhưng tên viết khác nhau, vẫn nhận ra là 1 công ty, không tạo trùng.

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

## `get_company_fb_linkedin_link.py` — điền fanpage/LinkedIn

```bash
python get_company_fb_linkedin_link.py --limit 10   # test thử ít công ty
	                # chạy full
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
