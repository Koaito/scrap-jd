"""
Hệ thống nhắn tin — schema request/response cho api/routers/messages.py
(thêm 08/2026). Xem backend-scrap-jd-nhan-tin.md cho toàn bộ kế hoạch.
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field, field_validator


class MessageCreate(BaseModel):
    receiver_id: str
    content: str = Field(..., min_length=1, max_length=2000)

    @field_validator("content")
    @classmethod
    def _trim_and_reject_blank(cls, v: str) -> str:
        # Validate NGAY Ở SCHEMA layer (trước khi vào router) — nhưng
        # router VẪN phải tự trim() lại trước khi ghi DB vì Pydantic
        # field_validator không tự động ghi giá trị đã sửa vào payload
        # gốc nếu router đọc payload.content thô ở chỗ khác; ở đây trả
        # về giá trị đã trim nên payload.content SAU validate đã sạch.
        trimmed = v.strip()
        if not trimmed:
            raise ValueError("Nội dung tin nhắn không được để trống hoặc chỉ toàn khoảng trắng.")
        return trimmed


class ChatMessageOut(BaseModel):
    # Tên "ChatMessageOut" (không phải "MessageOut") CỐ Ý — tránh đè
    # lên api.schemas.auth.MessageOut (response chung {"message": str}
    # cho các action xác nhận, đã tồn tại từ trước, tên trùng nếu dùng
    # "MessageOut" ở đây).
    id: int
    sender_id: str
    receiver_id: str
    content: str
    created_at: datetime
    read_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class ConversationOut(BaseModel):
    partner_id: str
    partner_name: str
    partner_role: str
    last_message_preview: Optional[str] = None
    last_message_at: Optional[datetime] = None
    unread_count: int
    relationship_status: Optional[str] = None
    """None nghĩa là cặp SS-SS (không qua state machine chat_relationships)."""
    relationship_id: Optional[str] = None
    """Thêm 08/2026 — cùng lý do None như relationship_status ở trên
    (cặp SS-SS). Cho FE gọi POST /messages/relationships/{id}/unblock
    khi relationship_status == 'blocked' mà không cần route/field nào
    khác — trước đó FE không có cách lấy relationship_id cho 1 cặp đã
    accepted/blocked ngoài lúc vừa gọi block_student() xong."""

    class Config:
        from_attributes = True


class PendingRequestOut(BaseModel):
    """Mục "Yêu cầu đang chờ" riêng cho SS — học viên pending nhưng
    chưa từng nhắn nên không nằm trong ConversationOut."""
    relationship_id: str
    student_id: str
    student_name: str
    requested_at: datetime

    class Config:
        from_attributes = True


class UnreadCountOut(BaseModel):
    count: int


class PersonSearchResult(BaseModel):
    """CHỈ 3 field — KHÔNG email/phone, xem
    backend-scrap-jd-nhan-tin.md §3, §4 (search-people)."""
    id: str
    full_name: str
    role: str

    class Config:
        from_attributes = True


class RelationshipOut(BaseModel):
    id: str
    student_id: str
    ss_id: str
    status: str
    initiated_by: str
    requested_at: datetime
    decided_at: Optional[datetime] = None
    declined_at: Optional[datetime] = None

    class Config:
        from_attributes = True
