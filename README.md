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

**Tính lại level job** sau khi đổi quy tắc suy level (`normalize.LEVEL_RULE_VERSION`
tăng) hoặc để xử lý job chưa đóng dấu `level_source`. Chạy hoàn toàn trong DB, không
tải trang. Cần đã `migrate` (có `level_source`, `level_signals` và cờ
`app.skip_updated_at`):

```bash
python main.py recompute-levels            # chạy thử: chỉ in báo cáo, không ghi
python main.py recompute-levels --apply    # ghi thật; updated_at giữ nguyên
```

Chỉ chọn job chưa đóng dấu hoặc dấu cũ hơn phiên bản hiện tại; không bao giờ đụng dòng
`manual`; bỏ qua job từng có người sửa (`updated_by`) mà level chưa đóng dấu; job cũ
không có `level_signals` chỉ đổi level khi tiêu đề nêu rõ cấp. Báo cáo liệt kê job rời/nhập
nhóm cùng `content_hash` (khoá cũ, còn gồm level; nhóm trùng theo `dedup_key` không đổi khi đổi
level; lệnh không tự gộp). Chi tiết xem docstring
`recompute_levels.py`.

**Báo cáo job nghi trùng** (Phần 3a, chỉ đọc: không ghi DB, không cần migration mới):

```bash
python main.py report-duplicates                  # in tổng hợp + vài nhóm mẫu cho mỗi mức độ chắc
python main.py report-duplicates --show 20        # in 20 nhóm chi tiết cho mỗi mức độ chắc
python main.py report-duplicates --csv trung.csv  # xuất toàn bộ nhóm (mỗi job một dòng) để duyệt tay
```

Gom các job cùng công ty và cùng tiêu đề chuẩn hoá (cùng công thức `dedup_key`). Khoá chống
trùng `dedup_key` (công ty + tiêu đề + tỉnh, không level) là khoá mà crawler, `POST /jobs` và view
`v_duplicate_job_candidates` cùng dùng; báo cáo này gom rộng hơn một bậc nên thấy thêm cả cặp
khác tỉnh (tầng "khác tỉnh") mà view không thấy. Mỗi nhóm được phân tầng (cùng tỉnh+level / khác level / khác tỉnh), chấm độ chắc là trùng
thật (cao / cần xem / thấp), liệt kê dữ liệu cần bảo vệ khi gộp (người sửa, ghi chú, đơn ứng
tuyển, lượt lưu, liên hệ) và **đề xuất** job giữ theo luật v0 (chưa phải luật đã chốt). Lệnh
không gộp, không xoá gì. Chi tiết xem docstring `duplicate_report.py`.

**Đo tỷ lệ gộp nhầm** (A5, chỉ đọc: không ghi DB, không cần migration mới):

```bash
python main.py report-reposts                       # tổng hợp, phân bố, vài job nghi gộp nhầm nhất
python main.py report-reposts --show 30             # in chi tiết 30 job
python main.py report-reposts --csv gop.csv         # xuất mọi cặp tin so được để duyệt tay
python main.py report-reposts --threshold 0.3       # chỉnh ngưỡng "nghi gộp nhầm" (mặc định 0.2)
```

Khoá chống trùng không gồm level nên hai vị trí cùng tên, công ty, tỉnh nhưng khác cấp có thể bị coi là
một job. Lệnh này so nội dung JD (`job_sources_log.raw_jd_content`) giữa các tin của cùng một job: tin đăng
lại thật thì JD gần như giống, tin có JD khác hẳn là nghi gộp nhầm. Chỉ so phần "Mô tả công việc" và "Yêu cầu
ứng viên" (bỏ "Quyền lợi" vì hay dùng chung giữa các JD của cùng công ty), độ giống là Jaccard trên cụm 3 từ,
mỗi job lấy cặp tin khác nhau nhất. Số liệu được tách theo cặp cùng trang / khác trang và theo việc có tin do
`merge-duplicates` chuyển sang hay không (đọc từ `audit_logs` `MERGE_JOB`). **Các ngưỡng (0.80 / 0.50 / 0.20)
là ước lượng ban đầu, chưa hiệu chuẩn**: xem phân bố và `--csv`, đối chiếu bằng mắt rồi chỉnh `--threshold`.
Chỉ bắt được gộp nhầm khi nội dung khác; hai vị trí khác cấp dùng chung một JD thì không bị bắt. Chi tiết xem
docstring `repost_report.py`.

**Gộp job trùng** (Phần 3b, phương án A): job phụ bị **xoá thật** sau khi chụp nguyên dòng vào
`audit_logs`; dữ liệu con chuyển sang job giữ. Mặc định chỉ **chạy thử** (in kế hoạch, không ghi gì),
`--apply` mới gộp thật:

```bash
python main.py merge-duplicates                          # kế hoạch cho nhóm độ chắc "cao" (không ghi gì)
python main.py merge-duplicates --csv ke_hoach.csv       # xuất kế hoạch từng nhóm ra file duyệt
python main.py merge-duplicates --only duyet.csv         # chỉ nhóm đã duyệt tay (CSV từ report-duplicates)
python main.py merge-duplicates --only danh_sach_id.txt  # hoặc file mỗi dòng một job_id
python main.py merge-duplicates --apply --limit 1        # gộp thật 1 nhóm đầu để thử (hỏi xác nhận)
python main.py merge-duplicates --apply                  # gộp thật toàn bộ kế hoạch (hỏi xác nhận)
python main.py merge-duplicates --apply --yes            # như trên, bỏ qua hỏi (chạy tự động)
```

Mặc định chỉ xét nhóm độ chắc "cao", không khác tỉnh, và không có từ 2 job trở lên cùng chứa dữ
liệu cần bảo vệ. Nhóm "cần xem", "thấp", khác tỉnh hoặc cần chọn tay chỉ được gộp khi bạn duyệt
qua `--only` (CSV từ `report-duplicates --csv`: xoá dòng của nhóm không duyệt, cột `de_xuat_giu`
= `x` là job giữ; nhóm khác tỉnh bắt buộc tự đánh dấu `x`). Kế hoạch cho mỗi nhóm: job giữ theo luật v0,
hợp nhất lương / level / ghi chú lên job giữ mà không ghi đè trường job giữ đã có (bản lệch được ghi nhận là
xung đột), và chuyển `job_sources_log`, `saved_jobs`, `job_applications`, `job_contact_links` sang job giữ (vướng
UNIQUE thì giữ bản của job giữ; hai liên kết cùng một liên hệ thì dồn lịch sử trao đổi vào liên kết của job giữ).
**Trạng thái, hạn nộp, `source_url` của job giữ theo luật suy ra từ các listing sau gộp** (C3c; cùng luật với
`check-listing-derivation`): OPEN nếu còn listing OPEN hoặc UNKNOWN (job giữ đang CLOSED mà job phụ còn listing sống thì
"hồi sinh"), hạn = hạn muộn nhất trong listing OPEN, `source_url` = URL listing OPEN mới nhất. Lúc gộp thật, kết quả
ghi vào job được đối chiếu với kế hoạch; lệch thì rollback nhóm đó. **Ngoại lệ job giữ nhập tay** (mọi listing là
`manual://`): luật suy ra không đè trạng thái, hạn, `source_url` của nó; hạn có sẵn được ghi vào mọi listing OPEN để giữ
lâu dài, job giữ đang CLOSED thì listing còn sống của job phụ đóng theo job, hạn trống thì điền hạn suy ra. Riêng
`source_url` của job nhập tay không giữ được lâu dài (không có cột đánh dấu): lần đồng bộ kế tiếp suy ra lại.
Chi tiết xem docstring `merge_duplicates.py`.

**Trước khi chạy `--apply`:**

1. **Backup DB** (`pg_dump` hoặc snapshot). Chưa có lệnh khôi phục tự động.
2. `python main.py migrate` (cần `migration_add_merge_job_audit_action.sql` và
   `migration_add_skip_updated_at_flag.sql`; thiếu thì `--apply` từ chối). Cần PostgreSQL >= 12.
3. Không crawl và không chạy bảo trì trong lúc gộp. Có crawl/bảo trì `queued`/`running` thì
   `--apply` từ chối. `--force` chỉ bỏ qua kiểm tra này, **không dừng** crawl nào (repo không có cơ chế
   huỷ crawl). Dòng `running` mà tiến trình đã chết vẫn chặn tới khi API dọn (~30 phút).
4. Chạy thử trước, rồi `--apply --limit 1`, kiểm tra kết quả, rồi mới gộp phần còn lại.

**Cách gộp:** mỗi nhóm một transaction riêng; khoá dòng job, đọc lại và so với kế hoạch (dữ liệu
đã đổi từ lúc lập kế hoạch thì bỏ qua nhóm đó, báo "stale"); chụp snapshot; cập nhật job giữ;
chuyển/bỏ dữ liệu con (kiểm số dòng); kiểm tra không còn dòng nào trỏ vào job phụ rồi mới xoá; ghi
`audit_logs`. Lỗi ở bất kỳ bước nào thì rollback cả nhóm. Nhóm stale/lỗi được bỏ qua, các nhóm sau vẫn
chạy, cuối lệnh liệt kê; chạy lại cùng lệnh sẽ lập kế hoạch mới cho các nhóm đó. Exit code: `0` xong
hết, `1` từ chối/chưa sẵn sàng/huỷ, `2` có nhóm stale hoặc lỗi, `130` bị ngắt (nhóm đã gộp xong vẫn giữ).
`updated_at` của job giữ không nhảy (cờ `app.skip_updated_at`). URL nguồn của job phụ được chuyển sang
job giữ nên lần crawl sau vẫn nhận ra, không sinh lại bản trùng.

**Audit:** action `MERGE_JOB` (log tự động, `actor_id` NULL, không bắt buộc note). Mỗi job phụ một dòng
với `entity_id` = job đã xoá, `changes = {merged_into, snapshot}`; job giữ thêm một dòng nếu có trường
đổi hoặc xung đột, `changes = {<cột>: {old, new}, merged_from, conflicts, link_status_conflicts, notes}`.
`snapshot` gồm `job` (nguyên dòng `job_postings` của job phụ), và với mỗi bảng con `moved` (id đã chuyển)
cùng `dropped` (nguyên dòng đã bỏ vì trùng khoá; riêng `job_sources_log` bỏ cột `raw_jd_content`, chỉ giữ
`raw_jd_content_chars`). Đơn ứng tuyển trùng bị bỏ **không** xoá file CV trong storage; đường dẫn nằm ở
`snapshot.job_applications.dropped[].cv_url`. `interaction_status` của hai liên kết liên hệ bị dồn: giữ
của job giữ, trống thì lấy của bên kia, bản lệch ghi ở `link_status_conflicts`.

**Khôi phục thủ công** một job phụ từ snapshot (chưa có lệnh `unmerge`; ghi chú để làm sau). Ví dụ với
`<ID>` là id job phụ, chạy trong một transaction, bật cờ để `updated_at` không nhảy:

```sql
BEGIN;
SELECT set_config('app.skip_updated_at', 'on', true);
CREATE TEMP TABLE snap ON COMMIT DROP AS
  SELECT changes->'snapshot' AS s FROM audit_logs WHERE action_type = 'MERGE_JOB' AND entity_id = '<ID>';
INSERT INTO job_postings SELECT (jsonb_populate_record(NULL::job_postings, s->'job')).* FROM snap;
-- trả dòng con đã chuyển về job phụ (làm tương tự với job_sources_log, job_applications, job_contact_links)
UPDATE saved_jobs SET job_id = '<ID>' WHERE saved_job_id IN
  (SELECT jsonb_array_elements_text(s->'saved_jobs'->'moved')::uuid FROM snap);
-- chèn lại dòng đã bỏ vì trùng khoá (saved_jobs, job_applications; job_sources_log thì chọn từng cột
-- vì snapshot không có raw_jd_content; liên kết liên hệ: s->'job_contact_links'->'merged'->'link')
INSERT INTO saved_jobs SELECT (jsonb_populate_record(NULL::saved_jobs, e)).*
  FROM snap, jsonb_array_elements(s->'saved_jobs'->'dropped') e;
COMMIT;
```

Muốn trả cả trường đã đổi ở job giữ thì lấy giá trị `old` trong dòng `MERGE_JOB` của job giữ. Nhớ: khôi phục
xong nếu không muốn job bị gộp lại lần sau thì xử lý nguyên nhân trùng (level/tỉnh) trước.

**Chặn nguồn sinh job trùng (Phần 3c).** Gộp xong mà crawler vẫn sinh trùng thì trùng sẽ mọc lại. Nguyên nhân
(đo trên 230 job trùng thật): bước 3c của `pipeline.py` tra tin đăng lại bằng `find_manual_job_duplicate`, hàm này bỏ
qua job đã CLOSED (khoảng 88% job trùng) và dùng cả level làm khoá (khoảng 34%). Nay pipeline dùng
`db.find_repost_candidate` (`scrapjd/db/job_recrawl.py`): khoá là công ty + tiêu đề (chuẩn hoá giống `generate_job_hash`:
không phân biệt hoa/thường, gộp khoảng trắng) + tỉnh, **không xét level**, **xét cả job CLOSED**; nhiều job khớp thì
chọn OPEN, rồi cùng level, rồi tạo gần nhất. `find_manual_job_duplicate` (dùng cho `POST /jobs` nhập tay) giữ nguyên.
Xử lý theo trạng thái job cũ (`pipeline._import_repost`), mọi trường hợp đều ghi URL mới làm nguồn phụ:

- Job OPEN: hạn nộp theo listing mới (C4 phần 2/3): hạn job là hạn muộn nhất trong các listing OPEN nên hạn mới muộn hơn
  thì dời hạn ra sau, hạn sớm hơn thì giữ nguyên, kể cả job nhân viên đã sửa tay. Không còn hàm dời hạn riêng; số liệu
  `repost_deadline_extended` đếm theo kết quả đồng bộ (`RepostLink.deadline_extended`).
- Job CLOSED vì `expired_auto` (do `check_expired_source_jobs` tự đóng): **mở lại** (C4 phần 1/3). `db.link_repost_source`
  ghi listing của URL mới ở trạng thái OPEN kèm hạn của tin mới (luật 1 ở `scrapjd/db/listing_state.py`), job suy ra OPEN từ
  listing đó nên `deadline` và `source_url` theo listing (để URL cũ đã chết thì `check_expired_source_jobs` đóng lại
  ngay), và hàm ghi audit `REOPEN_JOB` cùng transaction. Tin mới không có hạn thì hạn job là NULL.
- **Không mở lại** (chỉ ghi nguồn phụ, giữ CLOSED) khi: `closed_reason` là `staff`, `unknown` hoặc `merged` (listing mới
  sinh ra đã CLOSED với đúng lý do đó), hoặc hạn của tin mới đã qua (listing sinh ra CLOSED `expired_auto`), hoặc job
  vừa bị luồng khác mở trước.

Thống kê lượt crawl có thêm hai khoá, chỉ xuất hiện khi > 0 và đã nằm trong `skipped_duplicate_repost`:
`repost_reopened` (số job được mở lại) và `repost_kept_closed` (số tin đăng lại khớp job CLOSED nhưng không mở lại).
Cái giá của việc bỏ level khỏi khoá: hai vị trí cùng tên, cùng công ty, cùng tỉnh nhưng khác cấp bị coi là một (tin gốc
vẫn nằm trong `job_sources_log`). Thứ tự khuyến nghị khi áp dụng: commit code, backup DB, `merge-duplicates --apply`
để dọn trùng cũ, rồi mới crawl lại.

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
python -m scrapjd.maintenance.backfill_company_profiles
python -m scrapjd.maintenance.enrich_company_profile_from_website
python -m scrapjd.maintenance.enrich_company_web_info
python -m scrapjd.maintenance.get_company_fb_linkedin_link

# 3. Dọn job hết hạn: chạy định kỳ (cron hằng ngày là hợp lý)
python -m scrapjd.maintenance.check_expired_source_jobs --dry-run   # xem thử
python -m scrapjd.maintenance.check_expired_source_jobs             # chạy thật
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
scrapjd/                   <- package code lõi (đang dời dần từ thư mục gốc vào đây, xem kế hoạch B4b)
  config.py                <- ngành/category (JOB_CATEGORIES), độ trễ request, ngưỡng ngắt mạch,
                              snapshot, model AI... (đọc từ biến môi trường, xem .env.example)
  constants.py             <- hằng số dùng chung (enum, tỉnh...)
  models.py                <- RawJobRecord: khuôn dữ liệu chung mọi adapter phải trả về
  normalize.py             <- dùng chung: parse lương, suy luận level, deadline, work_type
  province_alias.py        <- quy đổi tên tỉnh cũ/mới
  sources_registry.py      <- nguồn sự thật DUY NHẤT để đăng ký nguồn crawl (SOURCES);
                              main.py và toàn bộ api/ import từ đây
  adapters/                <- base.py (BaseAdapter: session curl_cffi, _throttle(), _fetch_html()
                              với retry/backoff, ngắt mạch, ghi snapshot) + topcv.py,
                              vietnamworks.py, careerviet.py (mỗi adapter chỉ chứa logic parse riêng)
  db/                      <- mọi thao tác PostgreSQL, tách theo domain
    connection.py          <- connection, connection pool, apply_schema, migration tracking
    jobs.py, companies.py  <- GHI job / công ty (insert, update, gộp, probe cho pipeline)
    job_queries.py, company_queries.py     <- ĐỌC danh sách / chi tiết cho API (chỉ SELECT)
    job_health.py, company_analytics.py    <- thống kê "tình trạng dữ liệu", tín hiệu hợp tác
    company_enrichment.py  <- chọn công ty cần bổ sung thông tin cho các script enrich_*
    contacts.py, auth.py, audit_logs.py, applications.py, messages.py,
    email_templates.py, dashboard.py, stats.py, lookups.py <- theo domain
    crawl_runs.py, crawl_batches.py, crawl_snapshots.py, maintenance_runs.py
    job_levels.py, job_recrawl.py, job_level_recompute.py <- luật đóng dấu level, tái crawl theo mã job, SQL của `recompute-levels`
    job_duplicates.py      <- SQL (chỉ đọc) của `report-duplicates`: job nằm trong nhóm nghi trùng + dữ liệu con
    job_merge.py           <- SQL của `merge-duplicates`: đọc chi tiết job + dữ liệu con, gộp một nhóm trong transaction (merge_job_group), kiểm tra enum MERGE_JOB / crawl đang chạy
    __init__.py            <- re-export toàn bộ tên, dùng qua `from scrapjd import db`
  pipeline_db.py           <- PipelineDB (Protocol): những hàm db mà pipeline.py gọi, kèm phân loại đọc/ghi (B2)
  pipeline.py              <- nối adapter -> normalize -> db; xử lý từng job theo các bước nhỏ
                              (_process_job -> _import_new_job -> _import_repost / _insert_new_job)
  pipeline_stats.py        <- PipelineStats: bộ đếm của một lượt crawl (dataclass, gõ sai tên báo lỗi
                              ngay); run_pipeline() vẫn trả dict qua to_dict()
  field_stats.py           <- đếm tỷ lệ field rỗng, quyết định lượt crawl có "degraded" không
  snapshots.py             <- SnapshotRecorder: giữ mẫu HTML/JSON gốc của mỗi lượt crawl
  cli/                     <- logic của các lệnh con `main.py` (main.py chỉ phân lệnh và gọi vào đây)
    recompute_levels.py    <- logic lệnh `recompute-levels`: tính lại level từ tiêu đề + level_signals (chạy thử / --apply)
    duplicate_report.py    <- logic lệnh `report-duplicates`: phân loại nhóm job nghi trùng + đề xuất job giữ (chỉ đọc)
    merge_duplicates.py    <- lệnh `merge-duplicates` (3b): chọn nhóm, job giữ, hợp nhất trường, kế hoạch chuyển dữ liệu con (thuần) + điều phối --apply (xác nhận, kiểm tra crawl, gộp từng nhóm, báo cáo)
    repost_report.py       <- logic lệnh `report-reposts`: ước lượng tỷ lệ gộp nhóm job (chỉ đọc)
    check_listing_derivation.py  <- logic lệnh `check-listing-derivation`: so giá trị job suy ra từ listing với giá trị đang lưu (chỉ đọc)
  maintenance/             <- vá/dọn dữ liệu: API gọi qua api/maintenance_runner.py; chạy tay từ GỐC repo: python -m scrapjd.maintenance.<tên>
    backfill_company_profiles.py            <- vá profile công ty qua source_profile_url đã lưu
    enrich_company_profile_from_website.py  <- vá industry/products_services qua website + Gemini
    enrich_company_web_info.py              <- vá website/tax_id qua Tavily + Gemini
    get_company_fb_linkedin_link.py         <- vá fanpage/LinkedIn qua website
    check_expired_source_jobs.py            <- re-check job OPEN còn sống ở nguồn không
main.py                    <- CLI: init-db, migrate, crawl, stats, snapshots, snapshot-export, create-admin, recompute-levels, report-duplicates, merge-duplicates

scripts/                                 <- script chạy tay, KHÔNG do API gọi. Chạy từ GỐC repo: python -m scripts.<nhóm>.<tên>
  backfill/backfill_vnw_detail.py        <- vá JD đầy đủ + level cho job VietnamWorks đã lưu (python -m scripts.backfill.backfill_vnw_detail --limit 20)
  backfill/backfill_topcv_level.py       <- vá level Senior -> Lead cho job TopCV nhãn "Trên 5 năm" đã lưu (python -m scripts.backfill.backfill_topcv_level --limit 20)
  oneoff/fix_company_size_format.py      <- chạy MỘT LẦN: bỏ hậu tố "nhân viên" của company_size cũ (mặc định dry-run, thêm --apply để ghi)

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
sql/migration_*.sql        <- 36 migration cũ, đã đóng băng (baseline)
sql/NNNN_*.sql             <- migration MỚI, đánh số từ 0037 (xem sql/README_MIGRATIONS.md)
sql/baseline/0036_schema.sql <- schema.sql tại baseline, đóng băng; test dùng để so schema.sql với migration
tests/                     <- test parser, logic, CLI; một số test chạy trên Postgres thật
```

### Quy ước khi sửa code

Các quy ước dưới đây được test canh giữ; vi phạm thì `pytest` báo đỏ:

- **Một job = một transaction.** Các hàm `db.*` không tự commit hay rollback;
  `pipeline.py` quyết định. Mỗi nhánh có ghi DB commit đúng một lần ở cuối
  nhánh thành công, mọi lỗi rollback phần chưa commit của job đó. Thêm nhánh
  mới có ghi DB thì phải tự commit ở cuối nhánh (`tests/test_pipeline_transactions.py`).
- **`api/routers/` không chứa SQL thô.** SQL nằm ở `scrapjd/db/` (`tests/test_layering.py`).
- **Chỉ dùng một thư viện HTTP: `curl_cffi`.** Không import `requests`
  (`tests/test_http_library.py`).
- **Migration tạo bảng mới thì `schema.sql` cũng phải có bảng đó**
  (`tests/test_migrations.py`).
- **Thay đổi schema mới = file `sql/NNNN_<mô_tả>.sql` (số tiếp theo, từ 0037) kèm sửa `sql/schema.sql`.**
  Không thêm file `migration_*.sql` nữa. `tests/test_pg_migrations.py` dựng DB từ `schema.sql` và
  DB từ baseline cộng migration, rồi so hai schema trên Postgres thật; lệch thì đỏ.

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
  chưa có `ui-ux-design`). Danh sách đầy đủ theo nguồn nằm trong `scrapjd/config.py`.
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
  `company_id` + `job_title` (chuẩn hoá) + `province_id` với job đã có, **kể cả job
  đã CLOSED và không xét level**, thì **không tạo job mới**. URL mới được ghi làm
  nguồn phụ của job cũ nên lượt sau không fetch lại; job OPEN thì deadline được dời
  ra sau nếu hạn mới muộn hơn (không bao giờ rút ngắn), job CLOSED thì được mở lại
  (trừ job nhân viên đã chủ động đóng, xem mục "Chặn nguồn sinh job trùng" ở trên).
  Nội dung (mô tả, `work_type`) không vá từ bản đăng lại. Số lượng nằm ở
  `skipped_duplicate_repost`, `repost_deadline_extended`, `repost_reopened`,
  `repost_kept_closed` trong thống kê lượt chạy (xem
  [Giới hạn đã biết](#giới-hạn-đã-biết)).
- Một `source_url` chỉ thuộc **một** job: `job_sources_log` có `UNIQUE (source_url)` (migration
  `0041`, D2), đồng thời là index cho các câu tra "URL này đã crawl chưa". Ghi nguồn phụ trùng URL
  của job khác thì `link_repost_source` không ghi đè, trả `False` và cảnh báo; `insert_job` raise
  `UniqueViolation` và pipeline rollback job đó. Migration dừng nếu DB đang có URL nằm ở nhiều dòng.
- **Trạng thái từng listing** (C1, migration `0043` và `scrapjd/db/listing_state.py`): mỗi dòng `job_sources_log`
  (một URL tin đăng) có `listing_status` (`OPEN` | `CLOSED` | `UNKNOWN`), `deadline`, `first_seen_at`,
  `last_seen_at`, `closed_reason`, `closed_at`. Hiện CHỈ ĐƯỢC GHI, chưa chỗ đọc nào dùng (C2 và C3 sẽ chuyển
  job thành giá trị tổng hợp từ listing). Mọi SQL ghi trạng thái nằm ở `scrapjd/db/listing_state.py`. Luật ghi:
  listing mới là `OPEN` (cả job nhập tay), trừ khi job đang `CLOSED` thì listing sinh ra đã `CLOSED` với đúng
  `closed_reason` của job; job chuyển `CLOSED` thì mọi listing chưa đóng đóng theo cùng lý do (listing đã đóng
  giữ lý do cũ); nhân viên mở lại job thì listing đóng vì `staff` và listing hiện hành (URL trùng
  `job_postings.source_url`) về `OPEN`; pipeline mở lại job vì tin đăng lại thì listing của URL mới về `OPEN`
  kèm hạn mới. Fetch chi tiết thành công ghi `last_seen_at` và hạn đọc được; `check_expired_source_jobs` ghi
  `last_seen_at` cho URL trả HTTP 2xx. Không có migration mới ở bước này, chạy được ngay sau khi push.
- **So job với listing** (C2 nửa 1/2): `python main.py check-listing-derivation` (chỉ đọc) suy ra trạng thái,
  hạn và `source_url` của từng job từ các listing của nó (luật ở `scrapjd/db/job_derivation.py`: OPEN nếu có listing
  OPEN hoặc UNKNOWN; hạn muộn nhất trong các listing OPEN; URL của listing OPEN mới nhất) rồi so với giá trị
  đang lưu trong `job_postings`, in số liệu lệch theo từng trường và loại lệch. `--csv FILE` xuất từng trường
  lệch, `--show N` đổi số ví dụ, `--strict` thoát mã 2 nếu còn lệch.
- **Job theo kịp listing** (C2 nửa 2/2, `scrapjd/db/job_sync.py`): sau mỗi lần ghi listing, `sync_job_from_listings` ghi lại
  `job_status`, `closed_reason`, `deadline`, `source_url` của job cho bằng giá trị suy ra (chỉ cột lệch; không làm
  nhảy `updated_at`). Gọi từ `link_repost_source`, `update_job` (đóng, mở lại, sửa hạn),
  `mark_source_detail_checked`, `mark_listing_seen`. Hệ quả cần biết: job OPEN nhận tin đăng lại có `source_url` là
  URL của listing OPEN mới nhất (nên `check_expired_source_jobs` kiểm tra URL đó); nhân viên sửa hoặc xoá hạn thì hạn
  được ghi vào mọi listing OPEN của job, nên hạn job đúng bằng những gì nhân viên gõ. Pipeline không còn
  ghi thẳng trạng thái, hạn hay URL của job (C4 xong): tin đăng lại của job đóng `expired_auto` sinh listing OPEN và
  job tự mở lại nhờ đồng bộ (phần 1/3); tin đăng lại mang hạn của chính nó ở listing mới và job theo listing (phần
  2/3); `get_open_jobs_with_source_url` đã xoá, `check_expired_source_jobs` đọc qua `list_checkable_listings`. Migration `0044` gỡ `uq_job_source`; chạy SAU khi code này
  Live. `merge-duplicates` đồng bộ job giữ theo listing từ C3c (xem mục `merge-duplicates` ở trên).
- **VietnamWorks: nhận ra tin bị sửa tiêu đề theo mã job.** Nhà tuyển dụng sửa
  tiêu đề thì URL đổi (phần chữ) còn mã số cuối URL (`...-<mã>-jv`) giữ
  nguyên. Gặp URL chưa có trong DB, pipeline tìm job VietnamWorks còn `OPEN`
  cùng mã (dùng JD đã tải, không thêm request):
  - tiêu đề còn gần giống (trùng từ ≥ 0,5, `normalize.titles_similar`): **cập
    nhật job cũ**, không tạo job mới. Ghi tiêu đề, level, mô tả/yêu cầu, lương,
    hình thức làm việc, và ghi URL mới làm nguồn phụ (hạn nộp của job theo
    listing mới, hạn muộn nhất thắng, xem mục tin đăng lại). Không đụng công ty, tỉnh, ngành. Lương chỉ ghi khi
    nguồn có chuỗi lương; JD bị cắt ("...") thì giữ JD và level cũ;
  - job cũ đã có người sửa tay (`updated_by` khác rỗng): chỉ ghi URL mới làm
    nguồn phụ (nội dung giữ nguyên; hạn nộp vẫn theo listing mới như mọi job OPEN);
  - tiêu đề khác hẳn (nhà tuyển dụng đổi sang vị trí khác) hoặc job cũ đã
    `CLOSED`: không đụng job cũ, tạo job mới như trước. Trường hợp tiêu đề khác
    hẳn có log WARNING kèm cả hai tiêu đề để xem tay.

  Số lượng nằm ở `updated_by_job_code`, `linked_by_job_code_only` và
  `job_code_title_mismatch` trong thống kê lượt chạy (chỉ xuất hiện khi > 0).
  Cặp trùng mã đã có sẵn trong DB không được tự gộp.
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

Bốn script độc lập, không nằm trong pipeline crawl chính, chạy khi cần. Chạy từ GỐC repo bằng
`python -m scrapjd.maintenance.<tên>` (chạy `python x.py` thì import hỏng).
So sánh nhanh ở bảng trong [Quy trình đầu-cuối](#quy-trình-đầu-cuối).

### `backfill_company_profiles.py`

```bash
python -m scrapjd.maintenance.backfill_company_profiles --limit 10   # thử ít công ty
python -m scrapjd.maintenance.backfill_company_profiles              # chạy đầy đủ
```

Vá `industry`, `company_size`, `address`, `website` (nhặt kèm
`products_services`) cho công ty **đã có** `source_profile_url` nhưng còn
thiếu ít nhất một trong bốn field đầu, bằng cách gọi lại
`fetch_company_profile()` trên đúng URL đã lưu. Miễn phí, không tốn
Tavily/Gemini; nên dùng trước các script còn lại vì chính xác hơn (đọc
thẳng trang gốc, không qua search và LLM suy luận).

### `enrich_company_profile_from_website.py`

```bash
python -m scrapjd.maintenance.enrich_company_profile_from_website --limit 50
python -m scrapjd.maintenance.enrich_company_profile_from_website
```

Vá `industry` và `products_services` cho công ty **đã có `website`** nhưng
còn thiếu một trong hai, bằng cách đọc trang chủ/giới thiệu của chính
website đó rồi nhờ Gemini phân loại. Không cần Tavily nên rẻ hơn
`enrich_company_web_info.py`. Đặc biệt cần cho công ty nguồn CareerViet
(trang công ty CareerViet không hiển thị `industry`). Điều kiện chọn công ty
là OR: thiếu `industry` HOẶC thiếu `products_services` đều được chọn.

### `enrich_company_web_info.py`

```bash
python -m scrapjd.maintenance.enrich_company_web_info --limit 10
python -m scrapjd.maintenance.enrich_company_web_info
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
python -m scrapjd.maintenance.get_company_fb_linkedin_link --limit 10
python -m scrapjd.maintenance.get_company_fb_linkedin_link
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

**`check_expired_source_jobs.py`** (`scrapjd/maintenance/`, chạy từ GỐC repo bằng `-m` như các lệnh dưới): nên chạy sau mỗi đợt crawl hoặc định kỳ
(cron hằng ngày). JD trên nguồn bị nhà tuyển dụng xoá sau một thời gian
nhưng DB không tự biết, nên job vẫn hiện `OPEN` dù link nguồn đã chết.

```bash
python -m scrapjd.maintenance.check_expired_source_jobs --dry-run          # xem thử, KHÔNG ghi DB
python -m scrapjd.maintenance.check_expired_source_jobs                    # chạy thật
python -m scrapjd.maintenance.check_expired_source_jobs --check-deadline   # chỉ check deadline, không fetch mạng, nhanh hơn
python -m scrapjd.maintenance.check_expired_source_jobs --limit 20         # giới hạn số job xử lý, để thử
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

`scrapjd/config.py` khai báo category theo kiểu **category-first**: dict
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

Việc đăng ký nguồn nằm trong **một module duy nhất**: `scrapjd/sources_registry.py`.

1. Thêm bộ category cho nguồn mới vào `scrapjd/config.py` (theo cấu trúc `sources`
   ở trên, hoặc một dict `{key: {..., "matching_industry": ...}}` riêng nếu
   nguồn mới không dùng chung bộ category).
2. Viết `scrapjd/adapters/itviec.py`, kế thừa `BaseAdapter` (`scrapjd/adapters/base.py`).
   Session `curl_cffi`, `_throttle()`, `_fetch_html()` (retry/backoff),
   ngắt mạch và ghi snapshot đã có sẵn ở lớp cha; chỉ cần implement
   `fetch_jobs()` với logic parse riêng. Nếu nguồn có mã job ổn định trong URL
   (URL đổi theo tiêu đề nhưng mã giữ nguyên, như VietnamWorks) thì override
   thêm `job_code_url_regex()` để pipeline cập nhật job cũ thay vì tạo job trùng.
3. Thêm đúng một entry vào `SOURCES` trong `scrapjd/sources_registry.py`:

   ```python
   "itviec": {"adapter_cls": ITViecAdapter, "categories": ITVIEC_CATEGORIES},
   ```

Xong: `main.py` và toàn bộ `api/` (crawl runner, router `/crawl`, router
`/sources`) tự thấy nguồn mới; không cần sửa `scrapjd/normalize.py`, `scrapjd/db/`,
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
2. Đối chiếu pattern URL hoặc nhãn tiếng Việt trong `scrapjd/adapters/topcv.py`,
   `scrapjd/adapters/vietnamworks.py`, `scrapjd/adapters/careerviet.py` với HTML thật, sửa
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
  ty ở cùng tỉnh và cùng tên vị trí bị coi là một job (kể cả khác cấp bậc, vì
  level không còn nằm trong khoá), nên có thể gộp nhầm. Dữ liệu gốc của tin bị gộp vẫn được giữ trong nguồn phụ để soát
  lại. Theo dõi `skipped_duplicate_repost` trên trang `/crawl` trước khi
  quyết định siết khoá. Bước kiểm tra này nằm sau bước fetch chi tiết và hồ
  sơ công ty, nên một tin đăng lại tốn 1-2 request ở lần đầu gặp.
- **Nhận diện tin VietnamWorks bị sửa tiêu đề dựa trên so khớp từ.** Hai tiêu đề
  được coi là cùng vị trí khi trùng từ ≥ 0,5 (tính theo tiêu đề ngắn hơn, bỏ
  dấu), nên một tin sửa tiêu đề nhẹ nhưng đổi nghĩa vẫn có thể bị cập nhật nhầm,
  và một tin viết lại tiêu đề quá nhiều sẽ thành job mới (không mất tin, chỉ
  có thể để lại job cũ). Theo dõi `job_code_title_mismatch`. Các cặp job trùng
  mã đã có sẵn trong DB cần script gộp riêng.
- **URL fetch chi tiết thất bại bị thử lại ở mọi lượt** (ví dụ job đã gỡ
  khỏi nguồn, trả 404), vì lỗi tạm thời cần thử lại ngay nên không ghi dấu.
- **Một nguồn một lượt tại một thời điểm.** Ràng buộc này chỉ áp dụng cho
  lượt có ghi `crawl_runs`. Lượt chạy `--no-track` hoặc gọi thẳng
  `run_pipeline()` không có khoá này; không chạy chồng hai lượt cùng nguồn
  (race condition có thể sinh job trùng, soát bằng `v_duplicate_job_candidates`).
  Khác nguồn thì chạy song song được (TopCV, VietnamWorks, CareerViet, trên web lẫn máy cá nhân):
  từ A4 pipeline giành khoá advisory theo `dedup_key` (`db.lock_job_dedup_key`) trước bước tra
  đăng lại, nên cùng một tin xuất hiện ở hai nguồn cùng lúc chỉ sinh một job. Khoá cấp transaction
  nên không rò qua pooler; chờ quá 10 giây thì job đó bị bỏ qua và đếm vào lỗi, lần crawl sau xử lý lại.
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
