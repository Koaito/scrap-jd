# Migration DB — quy ước (thêm 08/2026)

Trước đây thư mục này chỉ có 29 file `migration_*.sql` rời rạc, chạy tay
qua `psql -f`, không có cách nào biết môi trường nào (dev/staging/prod)
đã chạy file nào. Từ giờ có 2 cơ chế đi CÙNG NHAU, KHÔNG thay thế nhau:

## `schema.sql` — bản snapshot đầy đủ, dùng cho DB MỚI

`python main.py init-db` chạy `sql/schema.sql` — file này LUÔN được cập
nhật để phản ánh đúng trạng thái CUỐI CÙNG của toàn bộ schema (đã fold
mọi migration vào). Dùng khi setup DB từ đầu (máy dev mới, môi trường
test). Idempotent — chạy lại nhiều lần an toàn.

## `migration_*.sql` + bảng `schema_migrations` — cập nhật DB ĐÃ CÓ SẴN

`python main.py migrate` chạy MỌI file `migration_*.sql` trong thư mục
này mà DB đang kết nối **chưa** có ghi log trong bảng `schema_migrations`
(tự tạo bảng này nếu chưa có), rồi ghi log lại. Dùng khi deploy lên
staging/prod đã có dữ liệu — không cần (và không nên) chạy lại
`schema.sql` full trên DB đã có data.

An toàn để chạy `python main.py migrate` bất kỳ lúc nào, kể cả trên DB
đã chạy tay 1 số migration này từ trước KHÔNG qua lệnh này: mọi file
migration ở đây đều viết idempotent (`ADD COLUMN IF NOT EXISTS`,
`CREATE ... IF NOT EXISTS`, `ON CONFLICT DO NOTHING`, `EXCEPTION WHEN
duplicate_object`...) — chạy lại 1 migration đã áp dụng vẫn là no-op an
toàn. Lệnh `migrate` sẽ tự "bắt kịp" (catch up) đúng trạng thái thật
của DB đó ngay lần chạy đầu tiên.

Kiểm tra còn migration nào chưa chạy mà KHÔNG chạy gì (an toàn dùng
trong CI/CD trước khi deploy, exit code 1 nếu còn thiếu):

```bash
python main.py migrate --check
```

## Quy trình thêm 1 thay đổi schema mới

1. Tạo file `sql/migration_<mô_tả_ngắn>.sql` — viết idempotent (dùng
   `IF NOT EXISTS`/`ON CONFLICT`/tương đương, xem file bất kỳ trong
   thư mục này làm mẫu).
2. Cập nhật `sql/schema.sql` cho khớp (để DB mới tạo từ `init-db` có
   đúng schema mới nhất ngay từ đầu, không cần chạy thêm migration).
3. Chạy `python main.py migrate` trên DB đang phát triển/staging/prod
   để áp dụng thay đổi — KHÔNG cần chạy tay `psql -f` nữa.
