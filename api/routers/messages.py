"""
Hệ thống nhắn tin học viên ↔ SS / SS ↔ SS — thêm 08/2026.
Xem backend-scrap-jd-nhan-tin.md cho toàn bộ kế hoạch (data model, state
machine, bảo mật). File này gộp 3 phần: Việc 3 (gửi/đọc tin), Việc 4
(danh sách hội thoại + tiện ích), Việc 5 (quản lý quan hệ pending/
accepted/declined/blocked).

QUY ƯỚC ROLE: role học viên trong hệ thống là 'user' (không phải
'student') — biến/tham số student_id chỉ là tên gọi theo vai trò
nghiệp vụ. 'ss_id' áp dụng cho cả 'ss_team' lẫn 'admin' (2 role đều
được coi là "SS" theo nghĩa nhắn tin — không phân biệt thêm ở tầng
này, vì cả 2 đều có quyền ngang nhau trong luồng chat).

Toàn bộ route yêu cầu JWT hợp lệ (Depends(get_current_user)) — không
dùng require_role() chung cho cả router vì quyền khác nhau THEO ROLE
NGAY TRONG CÙNG 1 route (vd POST /messages: học viên bị chặn nhắn học
viên, nhưng SS thì không) — check role thủ công trong từng handler
thay vì ở tầng dependency.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

import db as db_module
from api.deps import get_current_user, get_db
from api.rate_limit import get_user_id_or_ip, limiter
from api.schemas import (
    ChatMessageOut,
    ConversationOut,
    MessageCreate,
    PendingRequestOut,
    PersonSearchResult,
    RelationshipOut,
    UnreadCountOut,
)

router = APIRouter(prefix="/messages", tags=["messages"])


def _is_ss(role: str) -> bool:
    """SS/admin — cả 2 role đều coi là "SS" trong luồng nhắn tin."""
    return role in ("ss_team", "admin")


def _resolve_student_ss_pair(sender_id: str, sender_role: str, receiver_id: str, receiver_role: str):
    """Suy ra (student_id, ss_id) từ 1 cặp gửi/nhận theo role thật —
    dùng ở mọi chỗ cần tra/tạo chat_relationships. Trả None nếu cặp
    này là SS-SS (không qua state machine, xem §1 phạm vi MVP)."""
    if sender_role == "user" and _is_ss(receiver_role):
        return sender_id, receiver_id
    if _is_ss(sender_role) and receiver_role == "user":
        return receiver_id, sender_id
    return None


@router.post("", status_code=201)
@limiter.limit("1/second;20/minute", key_func=get_user_id_or_ip)
def send_message(
    request: Request,
    payload: MessageCreate,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """Gửi 1 tin nhắn. sender_id LUÔN lấy từ JWT (user['sub']), KHÔNG
    bao giờ nhận từ body — chặn giả mạo người gửi.

    Response KHÔNG đồng nhất 1 shape — 2 trường hợp:
      - 201 + ChatMessageOut: tin nhắn thật đã được lưu.
      - 202 + {"status": "pending", "message": ...}: học viên vừa TẠO
        hoặc GỬI LẠI request, chưa có tin nhắn nào được lưu — FE cần
        tự phân biệt qua status_code (không dùng response_model chung
        ở decorator vì lý do này).

    Thứ tự check (dừng sớm nhất có thể, tránh chạm DB khi không cần):
      1. receiver tồn tại + không tự nhắn cho chính mình.
      2. Học viên -> học viên: 403 NGAY, trước khi chạm state machine.
      3. Học viên -> SS: nếu chưa có relationship -> tạo pending (sau
         khi check giới hạn MAX_PENDING_PER_STUDENT) và DỪNG LẠI —
         KHÔNG gửi tin nhắn kèm request đầu tiên (v1: request và tin
         nhắn tách biệt, học viên phải đợi SS accept mới nhắn được).
         Nếu đã pending -> 409. Nếu declined -> reset nếu hết cooldown
         (vẫn KHÔNG gửi kèm tin, chỉ reset về pending) hoặc 403 nếu
         còn cooldown. Nếu blocked -> 403. Nếu accepted -> cho gửi.
      4. SS -> học viên: tự động ensure accepted (trừ khi blocked) rồi
         cho gửi luôn (SS chủ động liên hệ không cần xin phép).
      5. SS -> SS: cho gửi thẳng, không qua state machine.
    """
    sender_id = user["sub"]
    sender_role = user["role"]

    if payload.receiver_id == sender_id:
        raise HTTPException(status_code=400, detail="Không thể tự nhắn tin cho chính mình.")

    receiver = db_module.get_user_by_id(conn, payload.receiver_id)
    if receiver is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy người nhận.")
    receiver_role = receiver["role"]

    # 2. Học viên -> học viên: 403 ngay, không chạm chat_relationships.
    if sender_role == "user" and receiver_role == "user":
        raise HTTPException(status_code=403, detail="Học viên không thể nhắn tin cho học viên khác.")

    pair = _resolve_student_ss_pair(sender_id, sender_role, payload.receiver_id, receiver_role)

    if pair is not None:
        student_id, ss_id = pair
        is_student_sending = sender_role == "user"

        if is_student_sending:
            relationship = db_module.get_relationship(conn, student_id, ss_id)

            if relationship is None:
                # Chưa từng có quan hệ -> đây là request đầu tiên.
                if db_module.count_pending_for_student(conn, student_id) >= db_module.MAX_PENDING_PER_STUDENT:
                    raise HTTPException(
                        status_code=429,
                        detail=f"Bạn đang có quá nhiều yêu cầu nhắn tin đang chờ xử lý "
                               f"(tối đa {db_module.MAX_PENDING_PER_STUDENT} cùng lúc). "
                               f"Vui lòng đợi SS phản hồi trước khi gửi yêu cầu mới.",
                    )
                db_module.create_pending_request(conn, student_id, ss_id)
                conn.commit()
                return JSONResponse(
                    status_code=202,
                    content={
                        "status": "pending",
                        "message": "Đã gửi yêu cầu nhắn tin — chờ SS chấp nhận trước khi có thể nhắn tiếp.",
                    },
                )

            status = relationship["status"]
            if status == "blocked":
                raise HTTPException(status_code=403, detail="Bạn đã bị chặn nhắn tin với người này.")
            if status == "pending":
                raise HTTPException(status_code=409, detail="Yêu cầu nhắn tin đang chờ SS phản hồi.")
            if status == "declined":
                reset_id = db_module.reset_declined_to_pending(conn, student_id, ss_id)
                if reset_id is None:
                    raise HTTPException(
                        status_code=403,
                        detail=f"Yêu cầu trước đã bị từ chối — vui lòng thử lại sau "
                               f"{db_module.DECLINE_COOLDOWN_DAYS} ngày kể từ lúc bị từ chối.",
                    )
                conn.commit()
                return JSONResponse(
                    status_code=202,
                    content={
                        "status": "pending",
                        "message": "Đã gửi lại yêu cầu nhắn tin — chờ SS chấp nhận.",
                    },
                )
            # status == 'accepted' -> rơi xuống dưới để gửi tin thật.
        else:
            # SS gửi trước cho học viên -> tự động accept (trừ khi blocked).
            db_module.ensure_accepted_by_ss(conn, student_id, ss_id)
            relationship = db_module.get_relationship(conn, student_id, ss_id)
            if relationship is not None and relationship["status"] == "blocked":
                raise HTTPException(
                    status_code=403,
                    detail="Bạn đã tự chặn học viên này — bấm Unblock trước khi nhắn tiếp.",
                )

    # SS-SS (pair is None) hoặc học viên đã 'accepted' -> gửi tin thật.
    message_id = db_module.insert_message(conn, sender_id, payload.receiver_id, payload.content)
    conn.commit()
    message = db_module.get_message_by_id(conn, message_id)
    return ChatMessageOut.model_validate(message)


@router.get("/conversations", response_model=list[ConversationOut])
@limiter.limit("10/minute", key_func=get_user_id_or_ip)
def list_conversations(
    request: Request,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    return db_module.list_conversations(conn, user["sub"])


@router.get("/pending-requests", response_model=list[PendingRequestOut])
@limiter.limit("10/minute", key_func=get_user_id_or_ip)
def list_pending_requests(
    request: Request,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """Mục "Yêu cầu đang chờ" riêng cho SS — học viên pending mà chưa
    từng nhắn tin nên không xuất hiện trong /conversations."""
    if not _is_ss(user["role"]):
        raise HTTPException(status_code=403, detail="Chỉ SS/admin mới xem được danh sách yêu cầu.")
    return db_module.list_pending_requests_for_ss(conn, user["sub"])


@router.get("/unread-count", response_model=UnreadCountOut)
@limiter.limit("6/minute", key_func=get_user_id_or_ip)
def unread_count(
    request: Request,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    return {"count": db_module.get_unread_count(conn, user["sub"])}


@router.get("/search-people", response_model=list[PersonSearchResult])
@limiter.limit("20/minute", key_func=get_user_id_or_ip)
def search_people(
    request: Request,
    q: str = Query(..., min_length=1, max_length=100),
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """Chỉ trả id/full_name/role — KHÔNG email/phone. Học viên chỉ
    thấy role ss_team/admin; SS/admin thấy mọi role."""
    return db_module.search_people(conn, q, requester_role=user["role"])


@router.get("/with/{partner_id}", response_model=list[ChatMessageOut])
@limiter.limit("20/minute", key_func=get_user_id_or_ip)
def get_history(
    request: Request,
    partner_id: str,
    before_id: int | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """Lịch sử đầy đủ, phân trang cursor. Cho xem kể cả khi quan hệ
    đang declined/blocked (chỉ chặn GỬI, không chặn XEM) — không cần
    check state machine ở đây, chỉ cần current_user là 1 trong 2 người
    của hội thoại (IDOR check tự nhiên: query luôn ép theo user['sub'],
    không nhận user_id thứ 2 từ đâu khác ngoài current_user)."""
    if partner_id == user["sub"]:
        raise HTTPException(status_code=400, detail="partner_id không hợp lệ.")
    return db_module.get_messages_between(conn, user["sub"], partner_id, before_id=before_id, limit=limit)


@router.get("/since/{partner_id}", response_model=list[ChatMessageOut])
@limiter.limit("30/minute", key_func=get_user_id_or_ip)
def get_new_messages(
    request: Request,
    partner_id: str,
    after_id: int = Query(...),
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """Polling nhẹ trong lúc mở khung chat — chỉ trả tin id > after_id.

    TODO (khi làm FE, xem backend-scrap-jd-nhan-tin.md §3): response
    header Cache-Control: no-store — chưa set ở đây vì router hiện tại
    trả list trực tiếp qua response_model (FastAPI tự serialize),
    không đi qua Response object thủ công. Nếu cần set header thật,
    đổi return sang JSONResponse(..., headers={"Cache-Control": "no-store"})
    hoặc thêm middleware riêng cho path này."""
    return db_module.get_messages_since(conn, user["sub"], partner_id, after_id)


@router.post("/read/{partner_id}")
@limiter.limit("20/minute", key_func=get_user_id_or_ip)
def mark_read(
    request: Request,
    partner_id: str,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    updated = db_module.mark_read(conn, user["sub"], partner_id)
    conn.commit()
    return {"marked_read": updated}


@router.post("/cancel/{ss_id}", response_model=RelationshipOut)
@limiter.limit("20/minute", key_func=get_user_id_or_ip)
def cancel_my_pending_request(
    request: Request,
    ss_id: str,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """Học viên TỰ HUỶ request đang 'pending' do chính mình tạo với
    ss_id này (gửi nhầm SS / đổi ý) — xem db.cancel_pending_request()
    và backend-scrap-jd-nhan-tin.md §2 transition (h).

    KHÁC với decline (do SS thực hiện): huỷ ở đây XOÁ HẲN row, KHÔNG
    áp cooldown DECLINE_COOLDOWN_DAYS — học viên được gửi lại ngay lập
    tức, và suất trong MAX_PENDING_PER_STUDENT được giải phóng ngay.

    Chỉ role 'user' mới gọi được (SS không có gì để "huỷ" theo nghĩa
    này — SS dùng decline/block). 0 dòng ảnh hưởng ở tầng DB (không
    tồn tại / không phải initiated_by=current_user / không còn pending)
    -> 404 chung, không phân biệt lý do cụ thể để không rò rỉ thông
    tin về trạng thái relationship của người khác.

    LƯU Ý: trả về RelationshipOut nhưng row đã bị xoá khỏi DB — response
    body chỉ để FE hiện thông báo xác nhận (dùng lại state trước khi
    xoá), KHÔNG dùng để query lại relationship này sau đó."""
    if user["role"] != "user":
        raise HTTPException(status_code=403, detail="Chỉ học viên mới có thể huỷ yêu cầu nhắn tin của mình.")

    relationship = db_module.get_relationship(conn, user["sub"], ss_id)
    if relationship is None or relationship["status"] != "pending" or relationship["initiated_by"] != user["sub"]:
        raise HTTPException(
            status_code=404,
            detail="Không tìm thấy yêu cầu đang chờ để huỷ.",
        )

    ok = db_module.cancel_pending_request(conn, user["sub"], ss_id)
    if not ok:
        # Hiếm: bị xử lý bởi thao tác khác (SS vừa accept/decline) giữa
        # lúc get_relationship() ở trên và DELETE — 409 chính xác hơn 404
        # ở đây vì ta VỪA xác nhận nó tồn tại 1 dòng lệnh trước.
        raise HTTPException(
            status_code=409,
            detail="Yêu cầu vừa được xử lý (có thể SS đã phản hồi) — vui lòng tải lại.",
        )
    conn.commit()
    return relationship


# ============================================================
# Quản lý quan hệ (Việc 5) — accept / decline / block / unblock.
# Chỉ ss_id sở hữu relationship mới được thao tác — không cho SS khác
# accept/decline/block hộ. Riêng cancel (route ngay phía trên) là
# thao tác NGƯỢC LẠI dành cho học viên — huỷ request do chính mình
# tạo, tách khỏi nhóm route SS-only này.
# ============================================================

@router.post("/relationships/{relationship_id}/accept", response_model=RelationshipOut)
@limiter.limit("20/minute", key_func=get_user_id_or_ip)
def accept_request(
    request: Request,
    relationship_id: str,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    if not _is_ss(user["role"]):
        raise HTTPException(status_code=403, detail="Chỉ SS/admin mới có quyền chấp nhận yêu cầu.")
    ok = db_module.accept_relationship(conn, relationship_id, user["sub"])
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="Không thể chấp nhận — yêu cầu không tồn tại, không thuộc về bạn, "
                   "hoặc đã được xử lý bởi thao tác khác.",
        )
    conn.commit()
    return db_module.get_relationship_by_id(conn, relationship_id)


@router.post("/relationships/{relationship_id}/decline", response_model=RelationshipOut)
@limiter.limit("20/minute", key_func=get_user_id_or_ip)
def decline_request(
    request: Request,
    relationship_id: str,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    if not _is_ss(user["role"]):
        raise HTTPException(status_code=403, detail="Chỉ SS/admin mới có quyền từ chối yêu cầu.")
    ok = db_module.decline_relationship(conn, relationship_id, user["sub"])
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="Không thể từ chối — yêu cầu không tồn tại, không thuộc về bạn, "
                   "hoặc đã được xử lý bởi thao tác khác.",
        )
    conn.commit()
    return db_module.get_relationship_by_id(conn, relationship_id)


@router.post("/relationships/{relationship_id}/block", response_model=RelationshipOut)
@limiter.limit("20/minute", key_func=get_user_id_or_ip)
def block_by_relationship(
    request: Request,
    relationship_id: str,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    if not _is_ss(user["role"]):
        raise HTTPException(status_code=403, detail="Chỉ SS/admin mới có quyền chặn.")
    ok = db_module.block_relationship(conn, relationship_id, user["sub"])
    if not ok:
        raise HTTPException(status_code=404, detail="Không tìm thấy quan hệ này, hoặc không thuộc về bạn.")
    conn.commit()
    return db_module.get_relationship_by_id(conn, relationship_id)


@router.post("/block/{student_id}", response_model=RelationshipOut)
@limiter.limit("20/minute", key_func=get_user_id_or_ip)
def block_student(
    request: Request,
    student_id: str,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    """Biến thể block theo student_id trực tiếp — cho trường hợp SS
    muốn chặn TRƯỚC 1 học viên chưa từng có quan hệ nào (chưa có
    relationship_id để gọi route trên)."""
    if not _is_ss(user["role"]):
        raise HTTPException(status_code=403, detail="Chỉ SS/admin mới có quyền chặn.")
    student = db_module.get_user_by_id(conn, student_id)
    if student is None or student["role"] != "user":
        raise HTTPException(status_code=404, detail="Không tìm thấy học viên này.")
    db_module.block_student_by_ss(conn, student_id, user["sub"])
    conn.commit()
    return db_module.get_relationship(conn, student_id, user["sub"])


@router.post("/relationships/{relationship_id}/unblock", response_model=RelationshipOut)
@limiter.limit("20/minute", key_func=get_user_id_or_ip)
def unblock_request(
    request: Request,
    relationship_id: str,
    user: dict = Depends(get_current_user),
    conn=Depends(get_db),
):
    if not _is_ss(user["role"]):
        raise HTTPException(status_code=403, detail="Chỉ SS/admin mới có quyền bỏ chặn.")
    ok = db_module.unblock_relationship(conn, relationship_id, user["sub"])
    if not ok:
        raise HTTPException(
            status_code=409,
            detail="Không thể bỏ chặn — quan hệ không tồn tại, không thuộc về bạn, "
                   "hoặc hiện không ở trạng thái đang chặn.",
        )
    conn.commit()
    return db_module.get_relationship_by_id(conn, relationship_id)
