"""
Gửi email xác thực đăng ký qua Resend — thêm 08/2026 (Phần 2, xem
sql/migration_add_email_verification.sql).

CHỈ dùng domain mặc định của Resend (onboarding@resend.dev) — team
CHƯA có domain riêng verify với Resend (xem lịch sử trao đổi trước khi
code phần này). Đổi sang domain riêng sau này CHỈ cần đổi biến môi
trường EMAIL_FROM, KHÔNG cần sửa code ở đây.

Route gọi module này (api/routers/auth.py) KHÔNG await/raise nếu gửi
lỗi — xem docstring send_verification_email() bên dưới để hiểu lý do."""

import logging
import os

import resend

logger = logging.getLogger(__name__)

resend.api_key = os.getenv("RESEND_API_KEY", "")

# onboarding@resend.dev: domain TEST mặc định của Resend, dùng được
# ngay KHÔNG cần verify DNS gì — xem lịch sử trao đổi (team chưa có
# domain riêng). Đổi qua biến môi trường khi có domain thật, vd
# no-reply@ssteam.edu.vn, KHÔNG sửa trực tiếp dòng này.
EMAIL_FROM = os.getenv("EMAIL_FROM", "onboarding@resend.dev")

# Base URL để dựng link xác thực trong email — PHẢI trỏ đúng domain API
# thật đang chạy (vd https://scrap-jd-api.onrender.com), KHÔNG có dấu
# '/' cuối. Đọc từ env vì domain khác nhau giữa môi trường local/Render.
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000").rstrip("/")


def send_verification_email(*, to_email: str, full_name: str, verify_token: str) -> bool:
    """Gửi email chứa link xác thực (GET /auth/verify-email?token=...).

    Trả True/False thay vì raise — LỖI GỬI EMAIL KHÔNG ĐƯỢC LÀM HỎNG
    LUỒNG ĐĂNG KÝ: tài khoản vẫn đã tạo thành công trong DB (transaction
    đăng ký đã commit trước khi gọi hàm này, xem api/routers/auth.py),
    chỉ là email báo có thể chưa tới tay người dùng. Route sẽ log lỗi
    và người dùng có thể tự gọi lại POST /auth/resend-verification sau,
    KHÔNG cần đăng ký lại từ đầu nếu email bị thất lạc/gửi lỗi tạm thời
    (vd Resend rate-limit, mạng chập chờn)."""
    verify_url = f"{API_BASE_URL}/auth/verify-email?token={verify_token}"

    try:
        resend.Emails.send({
            "from": EMAIL_FROM,
            "to": to_email,
            "subject": "Xác thực tài khoản Scrap JD",
            "html": (
                f"<p>Chào {full_name},</p>"
                f"<p>Bấm vào đường dẫn dưới đây để xác thực tài khoản "
                f"Scrap JD của bạn (hết hạn sau 24 giờ):</p>"
                f'<p><a href="{verify_url}">{verify_url}</a></p>'
                f"<p>Nếu bạn không yêu cầu đăng ký tài khoản này, có "
                f"thể bỏ qua email này.</p>"
            ),
        })
        return True
    except Exception:
        # Bắt MỌI lỗi (mạng, rate-limit, key sai, Resend down...) —
        # đây là lời gọi ra dịch vụ NGOÀI, không được để 1 lỗi ở đây
        # kéo sập cả request đăng ký (đã tạo user thành công trong DB).
        logger.exception(
            "Gửi email xác thực thất bại cho %s — tài khoản vẫn đã tạo, "
            "user có thể tự gọi POST /auth/resend-verification sau.",
            to_email,
        )
        return False
