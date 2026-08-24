"""
api/storage.py — Module upload, tạo signed URL và xóa file PDF trên Supabase Storage
Sử dụng Supabase Storage REST API qua HTTP requests (nhẹ, không cần cài thêm SDK).
"""
import logging
from typing import Optional
import requests
from config import SUPABASE_CV_BUCKET, SUPABASE_SERVICE_ROLE_KEY, SUPABASE_URL

logger = logging.getLogger(__name__)

_TIMEOUT = 30  # seconds


def _headers() -> dict:
    """Tạo headers chung cho mọi request tới Supabase Storage."""
    return {
        "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        "apikey": SUPABASE_SERVICE_ROLE_KEY,
    }


def upload_cv(file_bytes: bytes, user_id: str, application_id: str) -> str:
    """
    Upload file PDF lên Supabase Storage.
    Đường dẫn lưu: cv-files/{user_id}/{application_id}.pdf
    Trả về: relative path lưu vào database (vd: "cv-files/.../....pdf")
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError(
            "Chưa cấu hình SUPABASE_URL hoặc SUPABASE_SERVICE_ROLE_KEY trên server."
        )

    object_path = f"{user_id}/{application_id}.pdf"
    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_CV_BUCKET}/{object_path}"

    res = requests.post(
        url,
        headers={
            **_headers(),
            "Content-Type": "application/pdf",
            "x-upsert": "true",
        },
        data=file_bytes,
        timeout=_TIMEOUT,
    )

    if res.status_code not in (200, 201):
        logger.error(
            "Supabase Storage upload error %s: %s", res.status_code, res.text[:300]
        )
        raise RuntimeError(f"Lỗi tải file lên storage (HTTP {res.status_code})")

    return f"{SUPABASE_CV_BUCKET}/{object_path}"


def get_signed_url(cv_path: str, expires_in: int = 3600) -> Optional[str]:
    """
    Tạo link tải file tạm thời (Signed URL) có thời hạn (mặc định 1 giờ).
    cv_path: Path lưu trong DB (dạng "cv-files/{user_id}/{application_id}.pdf")
    """
    if not cv_path:
        return None

    parts = cv_path.split("/", 1)
    if len(parts) != 2:
        return None

    bucket, object_path = parts
    url = f"{SUPABASE_URL}/storage/v1/object/sign/{bucket}/{object_path}"

    try:
        res = requests.post(
            url,
            headers={**_headers(), "Content-Type": "application/json"},
            json={"expiresIn": expires_in},
            timeout=_TIMEOUT,
        )
        if res.status_code == 200:
            signed = res.json().get("signedURL", "")
            if signed:
                if signed.startswith("/"):
                    # Supabase trả về path KHÔNG có tiền tố "/storage/v1"
                    # (vd "/object/sign/cv-files/...") — phải tự thêm vào,
                    # nếu không URL cuối cùng sẽ thiếu "/storage/v1" và
                    # Storage API trả lỗi "requested path is invalid".
                    return f"{SUPABASE_URL}/storage/v1{signed}"
                return signed
    except Exception as exc:
        logger.error("Lỗi khi tạo signed URL: %s", exc)

    return None


def delete_cv(cv_path: str) -> None:
    """
    Xóa file PDF khỏi Supabase Storage khi học viên hủy đơn.
    """
    if not cv_path:
        return

    parts = cv_path.split("/", 1)
    if len(parts) != 2:
        return

    bucket, object_path = parts
    url = f"{SUPABASE_URL}/storage/v1/object/{bucket}"

    try:
        requests.delete(
            url,
            headers={**_headers(), "Content-Type": "application/json"},
            json={"prefixes": [object_path]},
            timeout=_TIMEOUT,
        )
    except Exception as exc:
        logger.warning("Không thể xóa file CV trên storage: %s", exc)
