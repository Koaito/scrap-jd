# `tools/` — công cụ nội bộ, không phải phần backend chính

Thư mục này chứa các tool phụ trợ dùng nội bộ (không phải API layer,
không deploy) — hiện có 1 file:

## `hr_contact_tool.html` — HR Contact Finder (Mức 1)

Prototype HTML thuần (không framework), chạy local qua
`python3 -m http.server 3000` (cổng 3000 khớp `ALLOWED_ORIGINS` trên
Render). **Không nằm trong git** (dùng API key hardcode trong file,
`.gitignore` đã loại trừ) — file này chỉ tồn tại trên máy người dùng.

Mục đích: bản chứng minh cách 1 frontend thật nên gọi API để làm tính
năng "tìm & lưu contact HR" — frontend thật (React/Vue...) nên implement
lại đúng luồng bên dưới, không cần đọc HTML/CSS bên trong file (phần đó
sẽ viết lại theo framework riêng).

### Bối cảnh — vì sao làm tính năng này

LinkedIn/group Facebook không tự động hoá an toàn được (rủi ro khoá tài
khoản, cần đăng nhập thật). Giải pháp: bán tự động — sinh sẵn link
search Facebook đúng tab "Bài viết"
(`facebook.com/search/posts/?q=<tên công ty>`) cho từng công ty chưa có
contact, con người tự tìm & đọc bài JD, rồi nhập tay qua 1 form ngắn để
lưu vào `company_contacts`. Đã test tay 20 công ty mẫu: ~50% ra kết quả
(công ty lớn ra nhiều hơn hẳn công ty nhỏ/vừa — ngược dự đoán ban đầu).

### Luồng gọi API (frontend thật nên làm y hệt)

1. `POST /auth/login` (email/password) → lưu `access_token`/
   `refresh_token`, tự gọi `POST /auth/refresh` khi access token hết
   hạn (401) — xem hàm `apiFetch()` trong file để lấy logic retry.
2. `GET /companies?limit=200` → liệt kê công ty, sinh link Facebook
   search từ `company_name` (xử lý client-side, không cần endpoint
   riêng cho việc này).
3. `GET /companies/{id}/contacts` → xem contact đã có (lazy load, chỉ
   gọi khi mở rộng 1 dòng công ty, tránh N+1 request lúc load trang).
4. `POST /companies/{id}/contacts` → lưu contact mới tìm được.

### Rủi ro đã phát hiện lúc test tay — frontend nên giữ lại UX này

Kết quả search đôi khi trúng nhầm 1 pháp nhân/thương hiệu khác cùng tên
(vd tìm "LG Electronics Development VN" ra bài của 1 agency phân phối
khác, không phải công ty đúng trong DB). → **Luôn nhắc người dùng tự
xác nhận domain email/tên khớp đúng công ty trước khi lưu** — không tự
động lưu thẳng kết quả search mà không qua bước người xác nhận.

### Hướng mở rộng sau (chưa làm, chỉ làm khi thực tế cần)

- **Mức 2** — danh sách group Facebook hay dùng, sinh thêm nút search
  riêng từng group (hiện chỉ có search toàn Facebook).
- **Mức 3** — đánh dấu "đã thử tìm nhưng không ra" cho 1 công ty — cần
  thêm cột/bảng mới vì `company_contacts.contact_name` bắt buộc NOT
  NULL, không lưu được kiểu "không có kết quả" vào bảng hiện tại.
