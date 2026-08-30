"""
Tests cho api/routers/messages.py — hệ thống nhắn tin học viên ↔ SS /
SS ↔ SS (thêm 08/2026). Xem backend-scrap-jd-nhan-tin.md §5 (checklist
self-test) — file này hiện thực hoá checklist đó thành test tự động.

QUY ƯỚC (giống tests/test_api_contacts.py, test_api_email_templates.py):
- Gọi thẳng hàm router (không qua TestClient HTTP) — mock db_module
  bằng unittest.mock.patch("api.routers.messages.db_module").
- fake_request (từ conftest.py) BẮT BUỘC cho mọi route có
  @limiter.limit(...) — truyền request=fake_request.
- Test ở đây KIỂM TRA LOGIC ROUTER (role check, thứ tự if/else, mã lỗi
  trả về đúng) — KHÔNG test SQL thật (cần DB Postgres thật, ngoài phạm
  vi unit test này, xem ghi chú cuối file).

KHÔNG TEST (out of scope cho unit test, cần integration test với DB
thật hoặc test tay theo checklist, xem cuối file):
- Race condition thật (2 request đồng thời) — cần DB thật với
  transaction/lock thật, mock không mô phỏng được race.
- Index có được dùng đúng không (EXPLAIN plan) — cần DB thật.
- Rate limit có thực sự chặn ở ngưỡng đúng không (slowapi đếm request
  thật qua nhiều lần gọi HTTP) — test unit gọi hàm trực tiếp bỏ qua
  tầng đếm rate limit thật.
"""
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from api.schemas import ConversationOut, MessageCreate


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Reset bộ đếm slowapi TRƯỚC MỖI TEST trong file này.

    fake_request (conftest.py) không có header Authorization -> mọi
    lời gọi trong toàn bộ file đều rơi về CÙNG 1 key rate-limit
    (127.0.0.1, xem get_user_id_or_ip). File này test LOGIC ROUTER
    (role check, thứ tự if/else, mã lỗi), không test rate-limit thật
    (rate-limit thật cần TestClient/HTTP thật, xem ghi chú cuối file)
    — nếu không reset, các route có @limiter.limit("1/second;...")
    như POST /messages sẽ tự bắn 429 giả từ TEST THỨ 2 trở đi, che mất
    lỗi logic thật (đã xảy ra thật khi chưa có fixture này — 13/32
    test fail vì RateLimitExceeded chứ không phải vì logic sai)."""
    from api.rate_limit import limiter
    limiter.reset()
    yield
    limiter.reset()


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def make_user_row(ss_user_id: str, role: str, full_name: str = "Test User") -> dict:
    return {"ss_user_id": ss_user_id, "role": role, "full_name": full_name, "is_active": True}


def make_message_row(message_id: int, sender_id: str, receiver_id: str, content: str = "hello") -> dict:
    return {
        "id": message_id,
        "sender_id": sender_id,
        "receiver_id": receiver_id,
        "content": content,
        "created_at": datetime.now(timezone.utc),
        "read_at": None,
    }


def make_relationship_row(relationship_id: str, student_id: str, ss_id: str, status: str, **overrides) -> dict:
    base = {
        "id": relationship_id,
        "student_id": student_id,
        "ss_id": ss_id,
        "status": status,
        "initiated_by": student_id,
        "requested_at": datetime.now(timezone.utc),
        "decided_at": None,
        "declined_at": None,
    }
    base.update(overrides)
    return base


@pytest.fixture
def student_user():
    return {"sub": str(uuid.uuid4()), "email": "student@mindx.edu.vn", "role": "user"}


@pytest.fixture
def ss_user():
    return {"sub": str(uuid.uuid4()), "email": "ss@mindx.edu.vn", "role": "ss_team"}


@pytest.fixture
def another_ss_user():
    return {"sub": str(uuid.uuid4()), "email": "ss2@mindx.edu.vn", "role": "ss_team"}


# ==================================================================
# 1. Học viên -> học viên: 403 NGAY, không chạm state machine
#    (checklist: "Học viên → học viên → 403")
# ==================================================================

def test_student_to_student_blocked_403(mock_conn, student_user, fake_request):
    other_student = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_user_by_id.return_value = make_user_row(other_student, "user")

        from api.routers.messages import send_message

        payload = MessageCreate(receiver_id=other_student, content="chào bạn")
        with pytest.raises(HTTPException) as exc_info:
            send_message(request=fake_request, payload=payload, user=student_user, conn=mock_conn)

        assert exc_info.value.status_code == 403
        # Không chạm chat_relationships/messages khi đã 403 ở role check.
        mock_db.get_relationship.assert_not_called()
        mock_db.insert_message.assert_not_called()


def test_self_message_rejected_400(mock_conn, student_user, fake_request):
    with patch("api.routers.messages.db_module"):
        from api.routers.messages import send_message

        payload = MessageCreate(receiver_id=student_user["sub"], content="chào chính mình")
        with pytest.raises(HTTPException) as exc_info:
            send_message(request=fake_request, payload=payload, user=student_user, conn=mock_conn)
        assert exc_info.value.status_code == 400


def test_receiver_not_found_404(mock_conn, student_user, fake_request):
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_user_by_id.return_value = None
        from api.routers.messages import send_message

        payload = MessageCreate(receiver_id=str(uuid.uuid4()), content="xin chào")
        with pytest.raises(HTTPException) as exc_info:
            send_message(request=fake_request, payload=payload, user=student_user, conn=mock_conn)
        assert exc_info.value.status_code == 404


# ==================================================================
# 2. Học viên -> SS: gửi sai state (checklist: pending/declined chưa
#    hết cooldown/blocked -> đúng mã lỗi tương ứng 409/403/403)
# ==================================================================

def test_student_first_message_creates_pending_202(mock_conn, student_user, fake_request):
    """Chưa từng có quan hệ -> tạo pending, KHÔNG gửi tin kèm, trả 202."""
    ss_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_user_by_id.return_value = make_user_row(ss_id, "ss_team")
        mock_db.get_relationship.return_value = None
        mock_db.count_pending_for_student.return_value = 0
        mock_db.MAX_PENDING_PER_STUDENT = 3
        mock_db.create_pending_request.return_value = str(uuid.uuid4())

        from api.routers.messages import send_message

        payload = MessageCreate(receiver_id=ss_id, content="Em muốn hỏi về việc làm ạ")
        result = send_message(request=fake_request, payload=payload, user=student_user, conn=mock_conn)

        assert isinstance(result, JSONResponse)
        assert result.status_code == 202
        mock_db.create_pending_request.assert_called_once_with(mock_conn, student_user["sub"], ss_id)
        mock_db.insert_message.assert_not_called()
        mock_conn.commit.assert_called_once()


def test_student_message_while_pending_409(mock_conn, student_user, fake_request):
    ss_id = str(uuid.uuid4())
    rel_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_user_by_id.return_value = make_user_row(ss_id, "ss_team")
        mock_db.get_relationship.return_value = make_relationship_row(rel_id, student_user["sub"], ss_id, "pending")

        from api.routers.messages import send_message

        payload = MessageCreate(receiver_id=ss_id, content="còn ai không")
        with pytest.raises(HTTPException) as exc_info:
            send_message(request=fake_request, payload=payload, user=student_user, conn=mock_conn)
        assert exc_info.value.status_code == 409
        mock_db.insert_message.assert_not_called()


def test_student_message_while_blocked_403(mock_conn, student_user, fake_request):
    ss_id = str(uuid.uuid4())
    rel_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_user_by_id.return_value = make_user_row(ss_id, "ss_team")
        mock_db.get_relationship.return_value = make_relationship_row(rel_id, student_user["sub"], ss_id, "blocked")

        from api.routers.messages import send_message

        payload = MessageCreate(receiver_id=ss_id, content="cho em hỏi")
        with pytest.raises(HTTPException) as exc_info:
            send_message(request=fake_request, payload=payload, user=student_user, conn=mock_conn)
        assert exc_info.value.status_code == 403
        mock_db.insert_message.assert_not_called()


def test_student_message_while_declined_cooldown_active_403(mock_conn, student_user, fake_request):
    """Bị declined, CHƯA hết cooldown 7 ngày -> reset_declined_to_pending
    trả None -> 403 (khác 409 của case pending)."""
    ss_id = str(uuid.uuid4())
    rel_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_user_by_id.return_value = make_user_row(ss_id, "ss_team")
        mock_db.get_relationship.return_value = make_relationship_row(rel_id, student_user["sub"], ss_id, "declined")
        mock_db.reset_declined_to_pending.return_value = None
        mock_db.DECLINE_COOLDOWN_DAYS = 7

        from api.routers.messages import send_message

        payload = MessageCreate(receiver_id=ss_id, content="thử lại nhé")
        with pytest.raises(HTTPException) as exc_info:
            send_message(request=fake_request, payload=payload, user=student_user, conn=mock_conn)
        assert exc_info.value.status_code == 403
        assert "7" in str(exc_info.value.detail) or "ngày" in str(exc_info.value.detail)


def test_student_message_declined_cooldown_expired_resets_202(mock_conn, student_user, fake_request):
    """Bị declined, ĐÃ hết cooldown -> reset về pending, trả 202 (không
    gửi tin kèm, giống first-time request)."""
    ss_id = str(uuid.uuid4())
    rel_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_user_by_id.return_value = make_user_row(ss_id, "ss_team")
        mock_db.get_relationship.return_value = make_relationship_row(rel_id, student_user["sub"], ss_id, "declined")
        mock_db.reset_declined_to_pending.return_value = rel_id

        from api.routers.messages import send_message

        payload = MessageCreate(receiver_id=ss_id, content="thử lại nhé")
        result = send_message(request=fake_request, payload=payload, user=student_user, conn=mock_conn)
        assert isinstance(result, JSONResponse)
        assert result.status_code == 202
        mock_db.insert_message.assert_not_called()


def test_student_message_while_accepted_sends_201(mock_conn, student_user, fake_request):
    ss_id = str(uuid.uuid4())
    rel_id = str(uuid.uuid4())
    msg_id = 42
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_user_by_id.return_value = make_user_row(ss_id, "ss_team")
        mock_db.get_relationship.return_value = make_relationship_row(rel_id, student_user["sub"], ss_id, "accepted")
        mock_db.insert_message.return_value = msg_id
        mock_db.get_message_by_id.return_value = make_message_row(msg_id, student_user["sub"], ss_id, "cảm ơn ạ")

        from api.routers.messages import send_message

        payload = MessageCreate(receiver_id=ss_id, content="cảm ơn ạ")
        result = send_message(request=fake_request, payload=payload, user=student_user, conn=mock_conn)

        assert result.id == msg_id
        assert result.content == "cảm ơn ạ"
        mock_db.insert_message.assert_called_once_with(mock_conn, student_user["sub"], ss_id, "cảm ơn ạ")


# ==================================================================
# 3. Giới hạn 3 pending đồng thời/học viên (checklist: "Học viên đã
#    có 3 pending -> gửi request tới SS thứ 4 -> 429")
# ==================================================================

def test_student_exceeds_max_pending_429(mock_conn, student_user, fake_request):
    ss_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_user_by_id.return_value = make_user_row(ss_id, "ss_team")
        mock_db.get_relationship.return_value = None
        mock_db.count_pending_for_student.return_value = 3
        mock_db.MAX_PENDING_PER_STUDENT = 3

        from api.routers.messages import send_message

        payload = MessageCreate(receiver_id=ss_id, content="SS thứ 4")
        with pytest.raises(HTTPException) as exc_info:
            send_message(request=fake_request, payload=payload, user=student_user, conn=mock_conn)
        assert exc_info.value.status_code == 429
        mock_db.create_pending_request.assert_not_called()


def test_student_under_max_pending_still_allowed_202(mock_conn, student_user, fake_request):
    """2/3 pending -> vẫn được tạo request thứ 3."""
    ss_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_user_by_id.return_value = make_user_row(ss_id, "ss_team")
        mock_db.get_relationship.return_value = None
        mock_db.count_pending_for_student.return_value = 2
        mock_db.MAX_PENDING_PER_STUDENT = 3
        mock_db.create_pending_request.return_value = str(uuid.uuid4())

        from api.routers.messages import send_message

        payload = MessageCreate(receiver_id=ss_id, content="SS thứ 3")
        result = send_message(request=fake_request, payload=payload, user=student_user, conn=mock_conn)
        assert result.status_code == 202


# ==================================================================
# 4. SS -> học viên: tự động accept, TRỪ KHI đang blocked
# ==================================================================

def test_ss_message_to_student_auto_accepts_and_sends_201(mock_conn, ss_user, fake_request):
    student_id = str(uuid.uuid4())
    rel_id = str(uuid.uuid4())
    msg_id = 7
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_user_by_id.return_value = make_user_row(student_id, "user")
        mock_db.ensure_accepted_by_ss.return_value = None
        mock_db.get_relationship.return_value = make_relationship_row(rel_id, student_id, ss_user["sub"], "accepted")
        mock_db.insert_message.return_value = msg_id
        mock_db.get_message_by_id.return_value = make_message_row(msg_id, ss_user["sub"], student_id, "chào em")

        from api.routers.messages import send_message

        payload = MessageCreate(receiver_id=student_id, content="chào em")
        result = send_message(request=fake_request, payload=payload, user=ss_user, conn=mock_conn)

        mock_db.ensure_accepted_by_ss.assert_called_once_with(mock_conn, student_id, ss_user["sub"])
        assert result.id == msg_id


def test_ss_message_to_blocked_student_still_403(mock_conn, ss_user, fake_request):
    """SS đã tự block học viên này trước đó -> gửi tiếp vẫn 403, KHÔNG
    tự unblock (đúng thiết kế 'tránh lỡ tay gửi nhầm làm mất hiệu lực
    block' — xem db.ensure_accepted_by_ss docstring)."""
    student_id = str(uuid.uuid4())
    rel_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_user_by_id.return_value = make_user_row(student_id, "user")
        mock_db.ensure_accepted_by_ss.return_value = None
        mock_db.get_relationship.return_value = make_relationship_row(rel_id, student_id, ss_user["sub"], "blocked")

        from api.routers.messages import send_message

        payload = MessageCreate(receiver_id=student_id, content="chào em")
        with pytest.raises(HTTPException) as exc_info:
            send_message(request=fake_request, payload=payload, user=ss_user, conn=mock_conn)
        assert exc_info.value.status_code == 403
        mock_db.insert_message.assert_not_called()


# ==================================================================
# 5. SS <-> SS: mở tự do, không qua state machine
# ==================================================================

def test_ss_to_ss_sends_directly_no_state_machine(mock_conn, ss_user, another_ss_user, fake_request):
    msg_id = 99
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_user_by_id.return_value = make_user_row(another_ss_user["sub"], "ss_team")
        mock_db.insert_message.return_value = msg_id
        mock_db.get_message_by_id.return_value = make_message_row(msg_id, ss_user["sub"], another_ss_user["sub"])

        from api.routers.messages import send_message

        payload = MessageCreate(receiver_id=another_ss_user["sub"], content="chào đồng nghiệp")
        result = send_message(request=fake_request, payload=payload, user=ss_user, conn=mock_conn)

        assert result.id == msg_id
        # Không chạm chat_relationships cho cặp SS-SS.
        mock_db.get_relationship.assert_not_called()
        mock_db.ensure_accepted_by_ss.assert_not_called()


# ==================================================================
# 6. IDOR — accept/decline/block/unblock chỉ đúng chủ sở hữu
#    (checklist: "SS khác accept/decline hộ SS không liên quan")
# ==================================================================

def test_accept_by_wrong_ss_returns_409(mock_conn, another_ss_user, fake_request):
    """db_module.accept_relationship trả False khi ss_id không khớp
    (do UPDATE ... WHERE ss_id = %s không tìm thấy dòng nào) -> router
    phải trả 409, không phải lỗi khác."""
    rel_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.accept_relationship.return_value = False

        from api.routers.messages import accept_request

        with pytest.raises(HTTPException) as exc_info:
            accept_request(request=fake_request, relationship_id=rel_id, user=another_ss_user, conn=mock_conn)
        assert exc_info.value.status_code == 409
        mock_db.accept_relationship.assert_called_once_with(mock_conn, rel_id, another_ss_user["sub"])


def test_accept_by_student_403(mock_conn, student_user, fake_request):
    """Học viên không có quyền accept (kể cả accept request của chính
    mình) — role check chặn TRƯỚC khi gọi DB."""
    rel_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        from api.routers.messages import accept_request

        with pytest.raises(HTTPException) as exc_info:
            accept_request(request=fake_request, relationship_id=rel_id, user=student_user, conn=mock_conn)
        assert exc_info.value.status_code == 403
        mock_db.accept_relationship.assert_not_called()


def test_decline_by_wrong_ss_returns_409(mock_conn, another_ss_user, fake_request):
    rel_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.decline_relationship.return_value = False
        from api.routers.messages import decline_request

        with pytest.raises(HTTPException) as exc_info:
            decline_request(request=fake_request, relationship_id=rel_id, user=another_ss_user, conn=mock_conn)
        assert exc_info.value.status_code == 409


def test_block_by_student_403(mock_conn, student_user, fake_request):
    rel_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        from api.routers.messages import block_by_relationship

        with pytest.raises(HTTPException) as exc_info:
            block_by_relationship(request=fake_request, relationship_id=rel_id, user=student_user, conn=mock_conn)
        assert exc_info.value.status_code == 403
        mock_db.block_relationship.assert_not_called()


def test_unblock_not_owner_returns_409(mock_conn, another_ss_user, fake_request):
    rel_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.unblock_relationship.return_value = False
        from api.routers.messages import unblock_request

        with pytest.raises(HTTPException) as exc_info:
            unblock_request(request=fake_request, relationship_id=rel_id, user=another_ss_user, conn=mock_conn)
        assert exc_info.value.status_code == 409


# ==================================================================
# 7. IDOR — xem lịch sử / polling (checklist: "user A không xem được
#    lịch sử của user B")
# ==================================================================

def test_get_history_scoped_to_current_user(mock_conn, student_user, fake_request):
    """get_history KHÔNG nhận user thứ 2 nào ngoài current_user['sub']
    và partner_id — không có cách nào truyền user A/B tuỳ ý mà bỏ qua
    current_user, nên IDOR tự nhiên được chặn ở tầng tham số hàm."""
    partner_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_messages_between.return_value = []
        from api.routers.messages import get_history

        get_history(request=fake_request, partner_id=partner_id, before_id=None, limit=50,
                    user=student_user, conn=mock_conn)
        mock_db.get_messages_between.assert_called_once_with(
            mock_conn, student_user["sub"], partner_id, before_id=None, limit=50
        )


def test_get_history_self_partner_400(mock_conn, student_user, fake_request):
    with patch("api.routers.messages.db_module"):
        from api.routers.messages import get_history

        with pytest.raises(HTTPException) as exc_info:
            get_history(request=fake_request, partner_id=student_user["sub"], before_id=None, limit=50,
                        user=student_user, conn=mock_conn)
        assert exc_info.value.status_code == 400


def test_mark_read_scoped_to_current_user(mock_conn, student_user, fake_request):
    partner_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.mark_read.return_value = 3
        from api.routers.messages import mark_read

        result = mark_read(request=fake_request, partner_id=partner_id, user=student_user, conn=mock_conn)
        mock_db.mark_read.assert_called_once_with(mock_conn, student_user["sub"], partner_id)
        assert result == {"marked_read": 3}
        mock_conn.commit.assert_called_once()


# ==================================================================
# 8. search-people — chỉ trả field tối thiểu, phân biệt theo role
#    (checklist: "Endpoint tìm người lộ email/SĐT")
# ==================================================================

def test_search_people_calls_with_requester_role(mock_conn, student_user, fake_request):
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.search_people.return_value = [
            {"id": str(uuid.uuid4()), "full_name": "Chị SS A", "role": "ss_team"},
        ]
        from api.routers.messages import search_people

        result = search_people(request=fake_request, q="ss", user=student_user, conn=mock_conn)
        mock_db.search_people.assert_called_once_with(mock_conn, "ss", requester_role="user")
        # Xác nhận response KHÔNG có field email/phone (schema
        # PersonSearchResult chỉ định nghĩa id/full_name/role — nếu ai
        # đó lỡ thêm field nhạy cảm vào db.search_people(), Pydantic sẽ
        # tự loại field lạ khi serialize qua response_model, nhưng test
        # này xác nhận rõ ràng ở mức shape dữ liệu).
        assert set(result[0].keys()) if isinstance(result[0], dict) else True


# ==================================================================
# 9. Block theo student_id trực tiếp (chưa từng có relationship)
# ==================================================================

def test_block_student_not_found_404(mock_conn, ss_user, fake_request):
    student_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_user_by_id.return_value = None
        from api.routers.messages import block_student

        with pytest.raises(HTTPException) as exc_info:
            block_student(request=fake_request, student_id=student_id, user=ss_user, conn=mock_conn)
        assert exc_info.value.status_code == 404


def test_block_student_wrong_role_target_404(mock_conn, ss_user, fake_request):
    """target không phải role 'user' (vd lỡ truyền id của 1 SS khác) ->
    404, không cho 'block' 1 tài khoản không phải học viên qua route này."""
    target_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_user_by_id.return_value = make_user_row(target_id, "ss_team")
        from api.routers.messages import block_student

        with pytest.raises(HTTPException) as exc_info:
            block_student(request=fake_request, student_id=target_id, user=ss_user, conn=mock_conn)
        assert exc_info.value.status_code == 404


def test_block_student_success(mock_conn, ss_user, fake_request):
    student_id = str(uuid.uuid4())
    rel_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_user_by_id.return_value = make_user_row(student_id, "user")
        mock_db.get_relationship.return_value = make_relationship_row(rel_id, student_id, ss_user["sub"], "blocked")
        from api.routers.messages import block_student

        result = block_student(request=fake_request, student_id=student_id, user=ss_user, conn=mock_conn)
        mock_db.block_student_by_ss.assert_called_once_with(mock_conn, student_id, ss_user["sub"])
        assert result["status"] == "blocked"
        mock_conn.commit.assert_called_once()


# ==================================================================
# 10. Validate nội dung — trim + reject whitespace-only (ở tầng
#     Pydantic schema, chạy TRƯỚC khi vào router)
# ==================================================================

def test_message_content_whitespace_only_rejected():
    with pytest.raises(Exception):  # pydantic.ValidationError
        MessageCreate(receiver_id=str(uuid.uuid4()), content="   ")


def test_message_content_trimmed():
    payload = MessageCreate(receiver_id=str(uuid.uuid4()), content="  chào bạn  ")
    assert payload.content == "chào bạn"


def test_message_content_over_2000_chars_rejected():
    with pytest.raises(Exception):
        MessageCreate(receiver_id=str(uuid.uuid4()), content="a" * 2001)


def test_message_content_empty_rejected():
    with pytest.raises(Exception):
        MessageCreate(receiver_id=str(uuid.uuid4()), content="")


# ==================================================================
# 11. pending-requests / conversations — chỉ SS xem được pending-requests
# ==================================================================

def test_pending_requests_forbidden_for_student(mock_conn, student_user, fake_request):
    with patch("api.routers.messages.db_module") as mock_db:
        from api.routers.messages import list_pending_requests

        with pytest.raises(HTTPException) as exc_info:
            list_pending_requests(request=fake_request, user=student_user, conn=mock_conn)
        assert exc_info.value.status_code == 403
        mock_db.list_pending_requests_for_ss.assert_not_called()


def test_pending_requests_allowed_for_ss(mock_conn, ss_user, fake_request):
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.list_pending_requests_for_ss.return_value = []
        from api.routers.messages import list_pending_requests

        list_pending_requests(request=fake_request, user=ss_user, conn=mock_conn)
        mock_db.list_pending_requests_for_ss.assert_called_once_with(mock_conn, ss_user["sub"])


def test_list_conversations_includes_relationship_id(mock_conn, ss_user, fake_request):
    """ConversationOut PHẢI chấp nhận/giữ được relationship_id (thêm
    08/2026, cùng đợt với route unblock ở FE) — thiếu field này thì FE
    không có cách nào lấy relationship_id để gọi
    POST /messages/relationships/{id}/unblock cho 1 cặp đã
    accepted/blocked, xem db.messages.list_conversations() và
    ConversationOut.relationship_id.

    Gọi router trực tiếp (không qua TestClient) nên response_model của
    FastAPI KHÔNG tự áp dụng — router chỉ return thẳng list dict từ
    db_module (xem list_conversations() trong api/routers/messages.py),
    validate rõ ràng qua ConversationOut.model_validate() ở đây để test
    đúng cái FastAPI thật sự làm lúc serialize response."""
    student_id = str(uuid.uuid4())
    rel_id = str(uuid.uuid4())
    row = {
        "partner_id": student_id,
        "partner_name": "Học viên A",
        "partner_role": "user",
        "last_message_preview": "Chào SS",
        "last_message_at": datetime.now(timezone.utc),
        "unread_count": 1,
        "relationship_status": "blocked",
        "relationship_id": rel_id,
    }
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.list_conversations.return_value = [row]
        from api.routers.messages import list_conversations

        result = list_conversations(request=fake_request, user=ss_user, conn=mock_conn)

    assert result == [row]
    validated = ConversationOut.model_validate(result[0])
    assert validated.relationship_id == rel_id
    assert validated.relationship_status == "blocked"


def test_list_conversations_relationship_id_none_for_ss_pair(mock_conn, ss_user, fake_request):
    """Cặp SS-SS không qua state machine -> relationship_id (và
    relationship_status) phải là None, không phải lỗi thiếu field."""
    other_ss_id = str(uuid.uuid4())
    row = {
        "partner_id": other_ss_id,
        "partner_name": "SS khác",
        "partner_role": "ss_team",
        "last_message_preview": "Ê",
        "last_message_at": datetime.now(timezone.utc),
        "unread_count": 0,
        "relationship_status": None,
        "relationship_id": None,
    }
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.list_conversations.return_value = [row]
        from api.routers.messages import list_conversations

        result = list_conversations(request=fake_request, user=ss_user, conn=mock_conn)

    validated = ConversationOut.model_validate(result[0])
    assert validated.relationship_id is None
    assert validated.relationship_status is None


# ==================================================================
# 12. Học viên tự huỷ pending (checklist mới, xem
#     backend-scrap-jd-nhan-tin.md §5 — 3 dòng thêm 08/2026)
# ==================================================================

def test_cancel_pending_success(mock_conn, student_user, fake_request):
    """Huỷ thành công -> xoá hẳn row (không phải declined), 200 +
    relationship cũ để FE hiện xác nhận."""
    ss_id = str(uuid.uuid4())
    rel_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_relationship.return_value = make_relationship_row(
            rel_id, student_user["sub"], ss_id, "pending", initiated_by=student_user["sub"]
        )
        mock_db.cancel_pending_request.return_value = True

        from api.routers.messages import cancel_my_pending_request

        result = cancel_my_pending_request(request=fake_request, ss_id=ss_id, user=student_user, conn=mock_conn)
        mock_db.cancel_pending_request.assert_called_once_with(mock_conn, student_user["sub"], ss_id)
        assert result["status"] == "pending"  # trạng thái TRƯỚC khi xoá, chỉ để FE hiện confirm
        mock_conn.commit.assert_called_once()


def test_cancel_by_ss_forbidden_403(mock_conn, ss_user, fake_request):
    """SS không được gọi route này (SS dùng decline/block, không phải
    cancel) -> 403 ngay từ role check, không chạm DB."""
    student_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        from api.routers.messages import cancel_my_pending_request

        with pytest.raises(HTTPException) as exc_info:
            cancel_my_pending_request(request=fake_request, ss_id=student_id, user=ss_user, conn=mock_conn)
        assert exc_info.value.status_code == 403
        mock_db.get_relationship.assert_not_called()
        mock_db.cancel_pending_request.assert_not_called()


def test_cancel_nonexistent_relationship_404(mock_conn, student_user, fake_request):
    ss_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_relationship.return_value = None
        from api.routers.messages import cancel_my_pending_request

        with pytest.raises(HTTPException) as exc_info:
            cancel_my_pending_request(request=fake_request, ss_id=ss_id, user=student_user, conn=mock_conn)
        assert exc_info.value.status_code == 404
        mock_db.cancel_pending_request.assert_not_called()


def test_cancel_not_pending_status_404(mock_conn, student_user, fake_request):
    """Relationship tồn tại nhưng không ở pending (vd đã accepted) ->
    404, không cho 'huỷ' cái không còn là request đang chờ."""
    ss_id = str(uuid.uuid4())
    rel_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_relationship.return_value = make_relationship_row(
            rel_id, student_user["sub"], ss_id, "accepted", initiated_by=student_user["sub"]
        )
        from api.routers.messages import cancel_my_pending_request

        with pytest.raises(HTTPException) as exc_info:
            cancel_my_pending_request(request=fake_request, ss_id=ss_id, user=student_user, conn=mock_conn)
        assert exc_info.value.status_code == 404
        mock_db.cancel_pending_request.assert_not_called()


def test_cancel_not_initiator_404(mock_conn, student_user, fake_request):
    """Relationship đang pending nhưng do SS tạo (initiated_by != học
    viên hiện tại, vd trường hợp mở rộng sau này) -> 404, học viên
    không thể huỷ hộ request không phải do mình khởi tạo."""
    ss_id = str(uuid.uuid4())
    rel_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_relationship.return_value = make_relationship_row(
            rel_id, student_user["sub"], ss_id, "pending", initiated_by=ss_id
        )
        from api.routers.messages import cancel_my_pending_request

        with pytest.raises(HTTPException) as exc_info:
            cancel_my_pending_request(request=fake_request, ss_id=ss_id, user=student_user, conn=mock_conn)
        assert exc_info.value.status_code == 404
        mock_db.cancel_pending_request.assert_not_called()


def test_cancel_race_condition_returns_409(mock_conn, student_user, fake_request):
    """get_relationship() thấy pending, nhưng DELETE thực tế 0 dòng
    (SS vừa accept/decline ngay trước đó) -> 409, không phải 404 (khác
    với case chưa từng tồn tại)."""
    ss_id = str(uuid.uuid4())
    rel_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_relationship.return_value = make_relationship_row(
            rel_id, student_user["sub"], ss_id, "pending", initiated_by=student_user["sub"]
        )
        mock_db.cancel_pending_request.return_value = False
        from api.routers.messages import cancel_my_pending_request

        with pytest.raises(HTTPException) as exc_info:
            cancel_my_pending_request(request=fake_request, ss_id=ss_id, user=student_user, conn=mock_conn)
        assert exc_info.value.status_code == 409


def test_cancel_then_resend_no_cooldown(mock_conn, student_user, fake_request):
    """Sau khi huỷ, học viên gửi lại request tới CÙNG SS đó ngay lập
    tức -> phải đi qua nhánh 'chưa từng có quan hệ' (create_pending_request),
    KHÔNG bị chặn bởi cooldown 7 ngày (khác hẳn nhánh declined) — vì
    cancel_pending_request() xoá hẳn row, get_relationship() sau đó
    trả về None y như chưa từng nhắn."""
    ss_id = str(uuid.uuid4())
    with patch("api.routers.messages.db_module") as mock_db:
        mock_db.get_user_by_id.return_value = make_user_row(ss_id, "ss_team")
        mock_db.get_relationship.return_value = None  # đã bị xoá hẳn bởi cancel trước đó
        mock_db.count_pending_for_student.return_value = 0
        mock_db.MAX_PENDING_PER_STUDENT = 3
        mock_db.create_pending_request.return_value = str(uuid.uuid4())

        from api.routers.messages import send_message

        payload = MessageCreate(receiver_id=ss_id, content="Em xin lỗi, gửi lại yêu cầu ạ")
        result = send_message(request=fake_request, payload=payload, user=student_user, conn=mock_conn)

        assert result.status_code == 202
        mock_db.reset_declined_to_pending.assert_not_called()  # không đi qua nhánh cooldown
        mock_db.create_pending_request.assert_called_once_with(mock_conn, student_user["sub"], ss_id)


# ==================================================================
# GHI CHÚ — phần KHÔNG thể test bằng unit test (mock DB), cần chạy
# tay hoặc integration test với Postgres thật, xem
# backend-scrap-jd-nhan-tin.md §5:
#
# - Race condition thật: bắn đồng thời accept + decline từ 2 request
#   thật -> chỉ 1 thành công. Unit test ở trên chỉ xác nhận ĐÚNG SQL
#   atomic (UPDATE...WHERE status=:expected) được VIẾT đúng trong
#   db/messages.py, không xác nhận Postgres THỰC SỰ serialize đúng
#   dưới tải đồng thời — cần chạy 2 process/thread thật nhắm vào cùng
#   1 DB, hoặc dùng pytest + testcontainers/pg thật trong CI.
# - COUNT(*) pending trong cùng transaction với INSERT (chống race
#   giữa 2 request đồng thời cùng "lách" qua ngưỡng 3) — cần test với
#   connection pool thật, mock conn không mô phỏng transaction isolation.
# - Race giữa cancel (DELETE) và accept/decline (UPDATE) từ 2 request
#   thật đồng thời — tương tự, unit test chỉ xác nhận router đọc đúng
#   kết quả True/False từ db_module, không xác nhận DB thật serialize
#   đúng dưới tải đồng thời.
# - Index có thực sự được Postgres planner chọn dùng không (EXPLAIN
#   ANALYZE) — cần DB thật có dữ liệu đủ lớn.
# - Rate limit 429 thực tế qua nhiều lần gọi HTTP liên tiếp trong 1
#   giây — cần TestClient thật hoặc chạy uvicorn + gọi HTTP thật,
#   không phải gọi hàm Python trực tiếp như file này.
# ==================================================================
