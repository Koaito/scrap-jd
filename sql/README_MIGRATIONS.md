# Migration DB

Schema được quản lý bằng hai thứ đi cùng nhau:

| Thành phần | Dùng để | Lệnh |
| --- | --- | --- |
| `sql/schema.sql` | Dựng DB **mới** từ đầu. Luôn phản ánh schema mới nhất. | `python main.py init-db` |
| `sql/migration_*.sql` + bảng `schema_migrations` | Nâng cấp DB **đã có dữ liệu** khi schema thay đổi. | `python main.py migrate` |

Bảng `schema_migrations` ghi tên từng file migration đã áp dụng cho DB đó.

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

1. Tạo `sql/migration_<mô_tả_ngắn>.sql`, viết **idempotent** (`IF NOT EXISTS`,
   `ON CONFLICT DO NOTHING`...) và chỉ phụ thuộc trạng thái schema hiện tại.
2. Cập nhật `sql/schema.sql` cho khớp, để DB mới dựng bằng `init-db` có
   đúng schema mới nhất. Test `tests/test_migrations.py` kiểm tra mọi bảng
   mà migration tạo ra đều có trong `schema.sql`.
3. Chạy `python main.py migrate` trên từng môi trường (dev, staging, prod).

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
