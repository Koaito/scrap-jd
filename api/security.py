"""
Module bảo mật cho lớp đăng nhập TỪNG NGƯỜI (JWT + refresh token xoay
vòng) — KHÁC api/auth.py (API_KEY tĩnh dùng chung cho mọi client).

3 việc chính:
  1. Hash/verify mật khẩu bằng Argon2id (argon2-cffi) — thuật toán được
     khuyến nghị hiện tại (thắng Password Hashing Competition), chống
     tấn công GPU/ASIC tốt hơn bcrypt.
  2. Tạo/verify JWT access token (pyjwt) — tự chứa ss_user_id + role,
     verify chỉ cần kiểm chữ ký, KHÔNG cần query DB mỗi request.
  3. Sinh + hash refresh token — refresh token THÔ chỉ trả về đúng 1 lần
     lúc login/refresh, DB chỉ lưu bản hash (sha256), giống cách không
     bao giờ lưu mật khẩu thô.

Biến môi trường bắt buộc (xem .env.example):
  JWT_SECRET_KEY — khoá ký JWT, PHẢI đủ dài/ngẫu nhiên (vd
    `python -c "import secrets; print(secrets.token_urlsafe(32))"`).
    Fail-closed giống API_KEY: thiếu -> mọi thao tác JWT lỗi ngay khi
    import (không âm thầm dùng khoá yếu mặc định).

THAM SỐ CÓ THỂ CHỈNH (hằng số bên dưới, không cần đổi qua .env vì hiếm
khi cần đổi runtime):
  ACCESS_TOKEN_EXPIRE_MINUTES — access token sống ngắn (mặc định 30
    phút): hết hạn nhanh nếu bị lộ, refresh token mới là thứ sống lâu.
  REFRESH_TOKEN_EXPIRE_DAYS — refresh token sống 30 ngày.
  FAILED_LOGIN_LOCK_THRESHOLD / FAILED_LOGIN_LOCK_MINUTES — số lần sai
    liên tiếp trước khi khoá tạm + khoá bao lâu (dùng bởi
    db.record_failed_login(), xem api/routers/auth.py).
"""

import os
import secrets
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError, InvalidHashError

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Fail-closed: thiếu JWT_SECRET_KEY -> lỗi ngay lúc import module, không
# để tới lúc verify token mới phát hiện thiếu cấu hình (giống nguyên tắc
# API_KEY ở api/auth.py, nhưng ở đây raise ngay thay vì đợi tới request
# đầu tiên, vì JWT_SECRET_KEY không đổi được runtime nên phát hiện sớm
# càng tốt — sập lúc khởi động server còn hơn sập lúc user đang dùng).
_JWT_SECRET_KEY = os.getenv("JWT_SECRET_KEY", "")
if not _JWT_SECRET_KEY:
    raise RuntimeError(
        "Thiếu biến môi trường JWT_SECRET_KEY — xem .env.example. "
        "Tạo bằng: python -c \"import secrets; print(secrets.token_urlsafe(32))\""
    )

_JWT_ALGORITHM = "HS256"

ACCESS_TOKEN_EXPIRE_MINUTES = 30
REFRESH_TOKEN_EXPIRE_DAYS = 30

# Ngưỡng khoá tài khoản tạm thời sau nhiều lần đăng nhập sai liên tiếp —
# xem db.record_failed_login(). 5 lần / khoá 15 phút: đủ chặn brute-force
# thô sơ, không quá khắt khe với người dùng thật gõ nhầm vài lần.
FAILED_LOGIN_LOCK_THRESHOLD = 5
FAILED_LOGIN_LOCK_MINUTES = 15

_password_hasher = PasswordHasher()  # dùng tham số mặc định của argon2-cffi (đã được tinh chỉnh hợp lý sẵn cho web app)


# ------------------------------------------------------------------
# Mật khẩu — Argon2id
# ------------------------------------------------------------------

def hash_password(plain_password: str) -> str:
    """Trả chuỗi hash ĐẦY ĐỦ (đã bao gồm salt + tham số thuật toán, đúng
    định dạng PHC string) — lưu thẳng vào cột password_hash, KHÔNG cần
    lưu salt riêng (argon2-cffi tự quản lý)."""
    return _password_hasher.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    """So khớp mật khẩu người dùng gõ với hash đã lưu. Trả False cho MỌI
    lỗi (sai mật khẩu, hash hỏng/định dạng cũ...) — KHÔNG để lộ nguyên
    nhân cụ thể ra ngoài, tránh dò được thông tin qua thông báo lỗi."""
    if not password_hash:
        return False
    try:
        return _password_hasher.verify(password_hash, plain_password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """True nếu hash cũ được tạo bằng tham số Argon2 YẾU HƠN tham số hiện
    tại (vd sau này tăng độ khó) — route login gọi hàm này SAU KHI verify
    đúng mật khẩu, nếu True thì hash lại bằng tham số mới và ghi đè, để
    mật khẩu cũ dần được nâng cấp độ an toàn mà KHÔNG bắt người dùng đổi
    mật khẩu thủ công. Không có gì cần làm ở bản đầu này (tham số chưa
    từng đổi) nhưng thêm sẵn cho tương lai."""
    try:
        return _password_hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def generate_temp_password() -> str:
    """Sinh mật khẩu tạm ngẫu nhiên, đủ mạnh, DỄ ĐỌC/GÕ LẠI TAY (admin
    đưa trực tiếp cho người dùng qua kênh khác — Slack/nói miệng — vì
    KHÔNG có luồng gửi email, xem README.md mục Auth) — dùng bảng ký tự
    bỏ các ký tự dễ nhầm lẫn khi đọc (0/O, 1/l/I)."""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alphabet) for _ in range(16))


# ------------------------------------------------------------------
# JWT access token
# ------------------------------------------------------------------

def create_access_token(*, ss_user_id: str, role: str, email: str) -> str:
    """Access token TỰ CHỨA ss_user_id/role/email — verify chỉ cần kiểm
    chữ ký (không query DB). Sống ngắn (ACCESS_TOKEN_EXPIRE_MINUTES) —
    hết hạn nhanh nếu bị lộ; muốn phiên dài hơn thì dùng refresh token
    lấy access token mới, không kéo dài access token."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": ss_user_id,
        "role": role,
        "email": email,
        "iat": now,
        "exp": now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        "type": "access",
    }
    return jwt.encode(payload, _JWT_SECRET_KEY, algorithm=_JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    """Trả payload (dict) nếu token hợp lệ + chưa hết hạn + đúng
    type='access' (chặn nhầm lẫn nếu ai đó lỡ đưa refresh token vào chỗ
    này). Trả None cho MỌI lỗi (hết hạn, sai chữ ký, sai định dạng...) —
    route (api/deps.py) tự quyết định raise 401 với thông báo phù hợp,
    hàm này không raise gì cả để nơi gọi xử lý thống nhất 1 chỗ."""
    try:
        payload = jwt.decode(token, _JWT_SECRET_KEY, algorithms=[_JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("type") != "access":
        return None
    return payload


# ------------------------------------------------------------------
# Refresh token — sinh ngẫu nhiên, DB chỉ lưu bản hash
# ------------------------------------------------------------------

def generate_refresh_token() -> str:
    """Chuỗi ngẫu nhiên URL-safe, KHÔNG phải JWT (không cần tự chứa gì
    — chỉ là 1 khoá tra cứu ngẫu nhiên đủ dài để không đoán được, DB
    lưu bản hash của nó để đối chiếu lúc refresh)."""
    return secrets.token_urlsafe(48)


def hash_refresh_token(token: str) -> str:
    """SHA-256 hex (64 ký tự, deterministic — cùng input luôn ra cùng
    hash, KHÁC hash_password() vốn có salt ngẫu nhiên mỗi lần) — cần
    deterministic vì phải TRA NGƯỢC LẠI được token thô người dùng gửi
    lên khi refresh (chỉ có 1 hash entry, không thể vét cạn so khớp như
    Argon2id thường làm), đồng thời refresh token thô đã tự đủ ngẫu
    nhiên/dài (48 byte) nên không cần salt để chống rainbow table."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def refresh_token_expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS)
