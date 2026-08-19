"""
Gửi email xác thực đăng ký qua Resend — thêm 08/2026 (Phần 2, xem
sql/migration_add_email_verification.sql).

CHỈ dùng domain mặc định của Resend (onboarding@resend.dev) — team
CHƯA có domain riêng verify với Resend (xem lịch sử trao đổi trước khi
code phần này). Đổi sang domain riêng sau này CHỈ cần đổi biến môi
trường EMAIL_FROM, KHÔNG cần sửa code ở đây.

Route gọi module này (api/routers/auth.py) KHÔNG await/raise nếu gửi
lỗi — xem docstring send_verification_email() bên dưới để hiểu lý do."""

import html
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

# Base URL của FRONTEND (mindx-jobs, deploy Vercel) — dùng RIÊNG cho link
# reset mật khẩu (KHÁC link xác thực email ở trên trỏ thẳng về backend):
# reset mật khẩu cần 1 FORM để user nhập mật khẩu mới, trong khi xác
# thực email chỉ cần bấm là xong (GET thuần) — nên reset phải trỏ về
# trang có form thật (frontend), không thể trả HTML tĩnh từ backend như
# verify-email. Đọc từ env vì domain frontend khác nhau giữa môi trường.
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "http://localhost:5000").rstrip("/")


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
    # full_name do NGƯỜI DÙNG tự nhập lúc đăng ký — escape trước khi ghép
    # vào HTML (sửa bảo mật: trước đây ghép thẳng, ai đăng ký với
    # full_name kiểu "<img src=x onerror=...>" sẽ chèn được HTML/script
    # vào chính email gửi cho họ, ảnh hưởng tới email client của người
    # nhận email đó). verify_url không cần escape vì tự dựng từ
    # API_BASE_URL (biến môi trường cố định) + token (đã qua urlsafe).
    safe_full_name = html.escape(full_name)

    try:
        resend.Emails.send({
            "from": EMAIL_FROM,
            "to": to_email,
            "subject": "Xác thực tài khoản Scrap JD",
            "html": (
                f"<p>Chào {safe_full_name},</p>"
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


def send_password_reset_email(*, to_email: str, full_name: str, reset_token: str) -> bool:
    """Gửi email chứa link đặt lại mật khẩu, trỏ về FRONTEND (khác
    send_verification_email() ở trên trỏ về chính backend) — xem
    docstring FRONTEND_BASE_URL phía trên để hiểu lý do. Link hết hạn
    sau PASSWORD_RESET_EXPIRE_HOURS (xem api/routers/auth.py, 1 giờ —
    ngắn hơn hẳn link xác thực email 24h vì reset mật khẩu nhạy cảm
    hơn).

    Cùng nguyên tắc KHÔNG raise khi gửi lỗi như send_verification_email()
    — nhưng ở đây quan trọng hơn: route gọi hàm này LUÔN trả cùng 1
    message chung dù gửi thành công hay thất bại (chống dò email), nên
    return True/False ở đây chỉ để LOG, không ảnh hưởng gì response trả
    về người dùng."""
    reset_url = f"{FRONTEND_BASE_URL}/reset-password?token={reset_token}"
    # Escape full_name — cùng lý do send_verification_email() ở trên.
    safe_full_name = html.escape(full_name)

    try:
        resend.Emails.send({
            "from": EMAIL_FROM,
            "to": to_email,
            "subject": "Đặt lại mật khẩu — Scrap JD",
            "html": (
                f"<p>Chào {safe_full_name},</p>"
                f"<p>Có yêu cầu đặt lại mật khẩu cho tài khoản Scrap JD "
                f"của bạn. Bấm vào đường dẫn dưới đây để đặt mật khẩu "
                f"mới (hết hạn sau 1 giờ):</p>"
                f'<p><a href="{reset_url}">{reset_url}</a></p>'
                f"<p>Nếu bạn không yêu cầu đặt lại mật khẩu, có thể bỏ "
                f"qua email này — mật khẩu hiện tại của bạn vẫn an toàn, "
                f"không có gì thay đổi cho tới khi link trên được dùng.</p>"
            ),
        })
        return True
    except Exception:
        logger.exception(
            "Gửi email reset mật khẩu thất bại cho %s — user có thể tự "
            "gọi lại POST /auth/forgot-password sau.",
            to_email,
        )
        return False
