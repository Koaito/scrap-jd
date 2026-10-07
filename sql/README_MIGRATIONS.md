# Migration DB

Schema được quản lý bằng hai thứ đi cùng nhau:

| Thành phần | Dùng để | Lệnh |
| --- | --- | --- |
| `sql/schema.sql` | Dựng DB **mới** từ đầu. Luôn phản ánh schema mới nhất. | `python main.py init-db` |
| `sql/migration_*.sql` (cũ) và `sql/NNNN_*.sql` (mới) + bảng `schema_migrations` | Nâng cấp DB **đã có dữ liệu** khi schema thay đổi. | `python main.py migrate` |
| `sql/baseline/0036_schema.sql` | Bản sao đóng băng của `schema.sql` tại baseline. Chỉ test dùng, không chạy khi vận hành. | không có |

Bảng `schema_migrations` ghi tên từng file migration đã áp dụng cho DB đó.

## Hai kiểu tên file migration

| Kiểu | Số lượng | Thứ tự chạy |
| --- | --- | --- |
| `migration_<mô_tả>.sql` | 36 file **cũ**, coi là baseline, **đóng băng** (không thêm, không đổi tên, không xoá) | theo tên file, luôn chạy **trước** |
| `NNNN_<mô_tả>.sql` | file **mới**, NNNN gồm 4 chữ số, bắt đầu từ `0037`, liên tục và không trùng | theo **số**, luôn chạy **sau** file cũ |

Phần mô tả dùng chữ thường, chữ số và dấu gạch dưới, ví dụ `0037_add_closed_reason.sql`. File sai tên
(`37_x.sql`, `0037-x.sql`, `0037_X.sql`...) sẽ không bao giờ được chạy, nên `tests/test_migrations.py`
báo đỏ nếu thư mục `sql/` có file `.sql` không thuộc `schema.sql`, kiểu cũ hay kiểu mới. Hai file cùng
số làm `migrate` dừng ngay với lỗi nêu tên hai file (thường do hai nhánh cùng lấy số kế tiếp; đổi số
một file trước khi merge).

## Dựng DB mới

```bash
python main.py init-db
```

Lệnh này chạy `schema.sql` (chạy lại nhiều lần an toàn), rồi ghi nhận mọi
`migration_*.sql` hiện có là đã áp dụng, vì `schema.sql` đã chứa kết quả
của chúng. Sau đó `python main.py migrate --check` báo không còn gì cần chạy.

## Nâng cấp DB đã có

Kiểm tra còn migration nào chưa chạy (không chạy gì, exit code 1 nếu còn
thiếu, dùng được trong CI/CD trước khi deploy):

```bash
python main.py migrate --check
```

Áp dụng các migration còn thiếu, theo thứ tự tên file, mỗi file một
transaction. Lỗi ở file nào thì dừng tại đó; các file trước đó đã chạy xong
vẫn được giữ:

```bash
python main.py migrate
```

## DB đã ở trạng thái mới nhất nhưng `schema_migrations` còn thiếu: `--baseline`

Trường hợp thường gặp: DB được dựng hoặc cập nhật tay từ trước khi có bảng
`schema_migrations`, nên `migrate` tưởng mọi migration cũ chưa chạy. **Không
chạy `migrate` thẳng** trên DB kiểu này. Các migration cũ chạy theo thứ tự
tên file, không theo thứ tự phụ thuộc thật (ví dụ `migration_add_crawl_batches.sql`
cần kiểu enum do `migration_add_crawl_runs.sql` tạo; vài file còn viết theo tên
bảng `ss_team_members` đã đổi thành `app_users`), nên sẽ lỗi giữa chừng.

Dùng `--baseline` để chỉ **ghi nhận** các migration đó là đã áp dụng, không
chạy SQL của chúng:

```bash
python main.py migrate --baseline
```

Lệnh in danh sách file sẽ được ghi nhận và hỏi xác nhận (gõ `yes`; thêm
`--yes` để bỏ qua bước hỏi). Trước khi ghi nhận, lệnh kiểm tra DB đã có các
bảng chính (`app_users`, `job_postings`, `companies`, `crawl_runs`,
`crawl_batches`, `crawl_run_logs`, `maintenance_runs`, `import_previews`) và
từ chối nếu thiếu.

Nếu DB còn thiếu đúng một số migration (ví dụ chưa có bảng `crawl_snapshots`),
giữ các file đó lại bằng `--except` (lặp lại được), rồi chạy `migrate` để áp
dụng thật:

```bash
python main.py migrate --baseline --except migration_add_crawl_snapshots.sql
python main.py migrate
```

`--baseline` tin vào trạng thái hiện tại của DB: chỉ dùng khi bạn chắc DB
đã có kết quả của các migration cũ.

## Thêm một thay đổi schema mới

1. Tạo `sql/NNNN_<mô_tả_ngắn>.sql` với NNNN là số kế tiếp (xem file lớn nhất trong `sql/`), viết
   **idempotent** (`IF NOT EXISTS`, `ON CONFLICT DO NOTHING`...) và chỉ phụ thuộc trạng thái schema
   hiện tại. **Không** tạo `migration_*.sql` mới.
2. Cập nhật `sql/schema.sql` cho khớp, để DB mới dựng bằng `init-db` có đúng schema mới nhất.
   **Không** sửa `sql/baseline/0036_schema.sql`.
3. Chạy test trên Postgres thật (xem mục dưới). `tests/test_pg_migrations.py` dựng hai DB tạm, một từ
   `schema.sql`, một từ baseline cộng mọi migration đánh số, rồi so schema (cột, ràng buộc, index, enum,
   hàm, trigger, view, sequence, extension, comment). Quên một trong hai nơi thì test đỏ và in ra đối
   tượng nào chỉ có ở bên nào.
4. Chạy `python main.py migrate --check` rồi `python main.py migrate` trên từng môi trường (dev, staging,
   prod). Render tự deploy khi push `main`, nên migration chỉ thêm (cột, bảng, index) phải chạy **trước**
   khi push code dùng nó.

### Chạy test so schema

```bash
TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/scrapjd_test pytest tests/test_pg_migrations.py
```

Database trong URL phải có chữ `test` trong tên. Test tự tạo và xoá các database tạm `<tên>_mig_<mã>`
(cần quyền `CREATEDB`, PostgreSQL 13 trở lên); CI đã có sẵn Postgres 16 (`.github/workflows/test.yml`).
Không đặt `TEST_DATABASE_URL` thì test bị bỏ qua.

Giới hạn: phép so chỉ xét **cấu trúc** schema, không so **dữ liệu** (bảng tham chiếu như `levels`,
`provinces`). Migration chỉ sửa dữ liệu thì test này không bắt được lệch với `schema.sql`.

## Lưu ý

`migration_drop_products_services.sql` đã ngưng hiệu lực (chỉ chứa `SELECT 1;`).
Cột `companies.products_services` đang được pipeline crawl và script enrich
ghi vào, không được xoá.

### Thêm giá trị vào enum (`ALTER TYPE ... ADD VALUE`)

Dùng `ADD VALUE IF NOT EXISTS` (PostgreSQL >= 12 mới chạy được trong transaction; mỗi migration
chạy trong một transaction). Giá trị mới **không dùng được ngay trong cùng transaction** với câu thêm
nó, nên file migration chỉ chứa câu `ADD VALUE`, không kèm câu `INSERT/UPDATE` dùng giá trị đó. Ví dụ
gần nhất: `migration_add_merge_job_audit_action.sql` (action `MERGE_JOB` của audit_logs, dùng bởi
`python main.py merge-duplicates --apply`); `schema.sql` có dòng `ADD VALUE` tương ứng ở cuối file.
