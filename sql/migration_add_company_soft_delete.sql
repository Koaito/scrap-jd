-- Thêm xoá MỀM cho companies (08/2026) — trước migration này, companies
-- KHÔNG có cách xoá nào (xem docstring cũ trong api/routers/companies.py:
-- "KHÔNG có endpoint DELETE — company chưa có is_active/soft-delete").
--
-- Cùng pattern is_active đã dùng cho company_contacts (xem
-- sql/migration_add_role_hierarchy.sql mục 2) — xoá qua API là UPDATE
-- is_active=false, KHÔNG DELETE thật: company xoá "nhầm" vẫn cần giữ
-- lại (JD cũ, HR contact cũ, lịch sử liên hệ đều tham chiếu company_id
-- này qua FOREIGN KEY, xoá cứng sẽ vỡ FK hoặc phải CASCADE mất hết
-- lịch sử liên quan).
--
-- KHÔNG có bước "xoá cứng" (hard-delete) kiểu 2 bước như company_contacts
-- — company có nhiều bảng con phụ thuộc hơn contact nhiều (job_postings,
-- company_contacts...), xoá cứng company sẽ luôn vỡ FK trừ khi cố tình
-- CASCADE, rủi ro mất dữ liệu cao hơn nhiều so với hard-delete 1 contact
-- lẻ. Nếu sau này thật sự cần dọn hẳn company rác, xử lý tay qua DB
-- trực tiếp, không lộ ra API.
--
-- An toàn để chạy lại nhiều lần.

ALTER TABLE companies ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT true;

CREATE INDEX IF NOT EXISTS idx_companies_is_active ON companies(is_active);

-- ============================================================
-- HẾT FILE
-- ============================================================
