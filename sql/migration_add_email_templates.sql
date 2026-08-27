-- Thêm 08/2026 — bảng mẫu email liên hệ doanh nghiệp (trước đây 6 mẫu
-- này hardcode CỨNG trong public/app.js phía frontend, KHÔNG có trong
-- DB, không sửa/thêm được mà không đụng code — xem lịch sử trao đổi
-- "chia phần danh sách contact thành 2 phần, giống bên export/import").
--
-- Quyết định thiết kế (theo đúng yêu cầu đã chốt):
--   - ss_team VÀ admin đều được thêm/sửa/xoá — dùng require_role("ss_team")
--     ở API layer, KHÔNG cần bảng phân quyền riêng.
--   - Xoá là XOÁ HẲN (hard delete) — KHÔNG có is_active/soft-delete như
--     companies/company_contacts. Lịch sử ai xoá gì vẫn giữ được qua
--     audit_logs (entity_id vẫn còn trong log dù row đã mất khỏi bảng
--     này — giống pattern DELETE_JOB/DELETE_COMPANY).
--   - CREATE không bắt buộc note, UPDATE/DELETE bắt buộc note (xem
--     ACTION_LOG_RULES trong db/audit_logs.py phần Python đi kèm).
--   - recommended_for: mảng contact_status_enum (tái dùng enum đã có ở
--     company_contacts.contact_status, KHÔNG tạo enum mới) — "gợi ý cho
--     trạng thái hiện tại" khi staff mở popup chọn mẫu, đúng hành vi
--     recommendedFor: [...] cũ ở public/app.js. Mảng RỖNG = không gợi ý
--     riêng cho trạng thái nào (mẫu dùng chung, giống "Follow-up sau khi
--     gửi profile học viên" / "Hỏi nhu cầu tuyển dụng tháng/quý" cũ).
--
-- An toàn để chạy lại nhiều lần.
--
-- Cách chạy:
--   psql -U postgres -d "Student Success — Job Postings & Company Contacts" -f sql/migration_add_email_templates.sql

-- ============================================================
-- 1. Bảng email_templates
-- ============================================================

CREATE TABLE IF NOT EXISTS email_templates (
    template_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- title/description hiển thị ở danh sách chọn mẫu (etListView cũ ở
    -- app.js) — description là mô tả ngắn 1 dòng, KHÔNG phải nội dung
    -- email, giữ đúng vai trò "desc" cũ trong EMAIL_TEMPLATES.
    title           VARCHAR(255) NOT NULL,
    description     VARCHAR(500),

    -- body chứa nguyên placeholder dạng {{TEN_CONG_TY}} — KHÔNG parse/
    -- validate placeholder ở DB, việc điền giá trị thật xảy ra ở
    -- frontend lúc mở mẫu (fillPlaceholders() trong app.js), y hệt cơ
    -- chế cũ. 5 placeholder cố định (xem seed bên dưới + ghi chú hướng
    -- dẫn trong UI thêm/sửa mẫu — Task frontend sẽ hiển thị danh sách
    -- này cho staff): {{LOI_CHAO}}, {{TEN_CONG_TY}}, {{TEN_NGUOI_LIEN_HE}},
    -- {{CHUC_DANH}}, {{TEN_STAFF}}.
    body            TEXT NOT NULL,

    -- Gợi ý cho (các) trạng thái contact nào — mảng RỖNG hợp lệ (không
    -- gợi ý riêng). Dùng contact_status_enum có sẵn, KHÔNG tự do nhập
    -- text để tránh gõ sai lệch với 4 giá trị thật của contact_status.
    recommended_for contact_status_enum[] NOT NULL DEFAULT '{}',

    -- Thứ tự hiển thị trong danh sách chọn mẫu — staff sắp xếp lại được
    -- (mặc định theo thời gian tạo nếu không ai chỉnh display_order).
    display_order   INT NOT NULL DEFAULT 0,

    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by      UUID REFERENCES app_users(ss_user_id),
    updated_by      UUID REFERENCES app_users(ss_user_id)
);

CREATE INDEX IF NOT EXISTS idx_email_templates_display_order ON email_templates(display_order);

-- updated_at tự động (dùng lại trigger function trg_set_updated_at() đã
-- có sẵn trong schema.sql, KHÔNG định nghĩa lại).
DROP TRIGGER IF EXISTS trg_email_templates_updated_at ON email_templates;
CREATE TRIGGER trg_email_templates_updated_at
    BEFORE UPDATE ON email_templates
    FOR EACH ROW EXECUTE FUNCTION trg_set_updated_at();

-- ============================================================
-- 2. audit_action_enum — thêm 3 giá trị mới cho email_templates
-- ============================================================
-- ALTER TYPE ... ADD VALUE không chạy được trong transaction block ở
-- Postgres cũ hơn 12 — tách riêng câu lệnh, không bọc DO $$ BEGIN...END
-- như lúc TẠO enum lần đầu (ADD VALUE không hỗ trợ EXCEPTION catch
-- kiểu đó). Dùng IF NOT EXISTS (Postgres 12+) để chạy lại an toàn.

ALTER TYPE audit_action_enum ADD VALUE IF NOT EXISTS 'CREATE_EMAIL_TEMPLATE';
ALTER TYPE audit_action_enum ADD VALUE IF NOT EXISTS 'UPDATE_EMAIL_TEMPLATE';
ALTER TYPE audit_action_enum ADD VALUE IF NOT EXISTS 'DELETE_EMAIL_TEMPLATE';

-- ============================================================
-- 3. Seed 6 mẫu gốc (di chuyển nguyên văn từ public/app.js
--    EMAIL_TEMPLATES — giữ đúng title/desc/body/recommendedFor, chỉ
--    thêm display_order theo đúng thứ tự cũ trong mảng JS).
--    ON CONFLICT không áp dụng được ở đây (không có cột UNIQUE tự
--    nhiên để khớp) — dùng NOT EXISTS theo title để chạy lại migration
--    không tạo trùng lần 2.
-- ============================================================

INSERT INTO email_templates (title, description, body, recommended_for, display_order)
SELECT 'Giới thiệu MindX',
       'Mở lời làm quen lần đầu, đặt vấn đề hợp tác tuyển dụng Intern/Fresher.',
       E'Tiêu đề: MindX kết nối cơ hội thực tập/fresher cùng {{TEN_CONG_TY}}\n\n{{LOI_CHAO}}\n\nEm là {{TEN_STAFF}}, phụ trách kết nối doanh nghiệp của MindX — đơn vị đào tạo lập trình, Data Analysis và Business Analysis cho học viên trẻ, định hướng đi thực tập/fresher ngay sau khoá học.\n\nEm thấy {{TEN_CONG_TY}} là một trong những doanh nghiệp em rất muốn kết nối, nên xin phép chủ động liên hệ để tìm hiểu xem hiện tại công ty có đang có nhu cầu tuyển Intern/Fresher ở mảng nào không ạ. Nếu có, em rất mong được trao đổi thêm để giới thiệu những học viên phù hợp từ MindX.\n\nEm cảm ơn {{TEN_NGUOI_LIEN_HE}} đã dành thời gian đọc email, rất mong nhận được phản hồi ạ.\n\nTrân trọng,\n{{TEN_STAFF}}\nStudent Success — MindX',
       ARRAY['UNCONTACTED']::contact_status_enum[], 1
WHERE NOT EXISTS (SELECT 1 FROM email_templates WHERE title = 'Giới thiệu MindX');

INSERT INTO email_templates (title, description, body, recommended_for, display_order)
SELECT 'Xin JD Intern/Fresher',
       'Hỏi xin mô tả công việc cụ thể để giới thiệu đúng học viên.',
       E'Tiêu đề: Xin thông tin tuyển dụng Intern/Fresher từ {{TEN_CONG_TY}}\n\n{{LOI_CHAO}}\n\nEm là {{TEN_STAFF}} bên MindX, trước đó có liên hệ giới thiệu về chương trình kết nối việc làm cho học viên ạ.\n\nKhông biết hiện tại {{TEN_CONG_TY}} có JD (mô tả công việc) nào đang tuyển Intern/Fresher không ạ? Nếu có, {{TEN_NGUOI_LIEN_HE}} gửi giúp em JD chi tiết (vị trí, yêu cầu, mức lương/trợ cấp nếu có, deadline) để em lọc và giới thiệu đúng học viên phù hợp nhất bên MindX ạ.\n\nEm cảm ơn {{TEN_NGUOI_LIEN_HE}} nhiều ạ!\n\nTrân trọng,\n{{TEN_STAFF}}\nStudent Success — MindX',
       ARRAY['EMAIL_SENT']::contact_status_enum[], 2
WHERE NOT EXISTS (SELECT 1 FROM email_templates WHERE title = 'Xin JD Intern/Fresher');

INSERT INTO email_templates (title, description, body, recommended_for, display_order)
SELECT 'Giới thiệu học viên phù hợp',
       'Gửi kèm profile/CV học viên ứng với JD đã có.',
       E'Tiêu đề: MindX giới thiệu ứng viên cho vị trí Intern/Fresher tại {{TEN_CONG_TY}}\n\n{{LOI_CHAO}}\n\nEm là {{TEN_STAFF}} bên MindX. Dựa trên JD {{TEN_CONG_TY}} đang tuyển, em xin giới thiệu (các) học viên sau đây — CV/profile chi tiết em đính kèm trong email này ạ:\n\n- [Tên học viên] — [Kỹ năng/thế mạnh nổi bật, liên quan trực tiếp tới JD]\n\nCác bạn đều đã hoàn thành chương trình đào tạo tại MindX và sẵn sàng phỏng vấn/đi làm theo lịch phía công ty. {{TEN_NGUOI_LIEN_HE}} xem giúp em, có gì cần trao đổi thêm em luôn sẵn sàng hỗ trợ ạ.\n\nTrân trọng,\n{{TEN_STAFF}}\nStudent Success — MindX',
       ARRAY['IN_PARTNERSHIP']::contact_status_enum[], 3
WHERE NOT EXISTS (SELECT 1 FROM email_templates WHERE title = 'Giới thiệu học viên phù hợp');

INSERT INTO email_templates (title, description, body, recommended_for, display_order)
SELECT 'Follow-up sau khi gửi profile học viên',
       'Nhắc nhẹ khi chưa thấy phản hồi sau khi đã giới thiệu học viên.',
       E'Tiêu đề: Follow-up — hồ sơ học viên MindX gửi {{TEN_CONG_TY}}\n\n{{LOI_CHAO}}\n\nEm là {{TEN_STAFF}} bên MindX. Tuần trước em có gửi {{TEN_NGUOI_LIEN_HE}} profile một số học viên ứng với vị trí Intern/Fresher bên {{TEN_CONG_TY}} đang tuyển ạ.\n\nEm xin phép follow-up lại xem {{TEN_NGUOI_LIEN_HE}} đã có dịp xem qua chưa, và bên mình có cần em bổ sung thêm hồ sơ hay thông tin gì không ạ. Nếu vị trí đã tuyển đủ hoặc chưa phù hợp, {{TEN_NGUOI_LIEN_HE}} phản hồi giúp em 1 câu để em chủ động cập nhật lại phía học viên ạ.\n\nEm cảm ơn {{TEN_NGUOI_LIEN_HE}} nhiều!\n\nTrân trọng,\n{{TEN_STAFF}}\nStudent Success — MindX',
       ARRAY[]::contact_status_enum[], 4
WHERE NOT EXISTS (SELECT 1 FROM email_templates WHERE title = 'Follow-up sau khi gửi profile học viên');

INSERT INTO email_templates (title, description, body, recommended_for, display_order)
SELECT 'Cảm ơn sau khi doanh nghiệp phản hồi',
       'Ghi nhận + giữ nhịp trao đổi sau khi phía công ty trả lời.',
       E'Tiêu đề: Cảm ơn {{TEN_CONG_TY}} đã phản hồi\n\n{{LOI_CHAO}}\n\nEm là {{TEN_STAFF}} bên MindX, cảm ơn {{TEN_NGUOI_LIEN_HE}} đã dành thời gian phản hồi email trước của em ạ.\n\n[Điền nội dung theo đúng những gì phía công ty vừa phản hồi — ví dụ: xác nhận lịch trao đổi tiếp theo, thông tin JD sẽ gửi sau, hoặc bước tiếp theo hai bên đã thống nhất.]\n\nEm sẽ theo sát và phối hợp chặt chẽ với {{TEN_NGUOI_LIEN_HE}} trong các bước tiếp theo ạ. Rất mong được đồng hành cùng {{TEN_CONG_TY}} trong việc kết nối các bạn học viên tiềm năng.\n\nTrân trọng,\n{{TEN_STAFF}}\nStudent Success — MindX',
       ARRAY['RESPONDED']::contact_status_enum[], 5
WHERE NOT EXISTS (SELECT 1 FROM email_templates WHERE title = 'Cảm ơn sau khi doanh nghiệp phản hồi');

INSERT INTO email_templates (title, description, body, recommended_for, display_order)
SELECT 'Hỏi nhu cầu tuyển dụng tháng/quý',
       'Chủ động hỏi thăm định kỳ với các đối tác đã từng hợp tác.',
       E'Tiêu đề: {{TEN_CONG_TY}} có đang cần tuyển Intern/Fresher không ạ?\n\n{{LOI_CHAO}}\n\nEm là {{TEN_STAFF}} bên MindX. Lâu rồi em chưa có dịp cập nhật lại với {{TEN_NGUOI_LIEN_HE}}, không biết thời gian tới {{TEN_CONG_TY}} có kế hoạch tuyển thêm Intern/Fresher ở mảng nào không ạ?\n\nNếu có, em rất mong được {{TEN_NGUOI_LIEN_HE}} chia sẻ sớm để em chuẩn bị và giới thiệu học viên phù hợp kịp tiến độ tuyển dụng bên mình ạ. Em luôn sẵn sàng hỗ trợ bất cứ khi nào công ty cần ạ.\n\nTrân trọng,\n{{TEN_STAFF}}\nStudent Success — MindX',
       ARRAY[]::contact_status_enum[], 6
WHERE NOT EXISTS (SELECT 1 FROM email_templates WHERE title = 'Hỏi nhu cầu tuyển dụng tháng/quý');
