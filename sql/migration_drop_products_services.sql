-- Migration đã ngưng hiệu lực: KHÔNG làm gì.
--
-- Bản gốc của file này xoá cột companies.products_services, nhưng cột đó
-- đang được dùng: pipeline crawl và enrich_company_profile_from_website.py
-- ghi vào nó, db/companies.py đọc và cập nhật nó. Xoá cột sẽ làm crawl lỗi
-- ngay. File được giữ lại (không xoá) để tên file vẫn khớp với các dòng đã
-- ghi trong bảng schema_migrations của những DB từng áp dụng nó.
--
-- Cột products_services nằm trong sql/schema.sql.

SELECT 1;
