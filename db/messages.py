"""
db.messages — tầng truy vấn DB cho hệ thống nhắn tin học viên ↔ SS /
SS ↔ SS (08/2026). Xem backend-scrap-jd-nhan-tin.md cho toàn bộ kế
hoạch/state machine, sql/migration_add_chat_messages.sql cho schema.

QUY ƯỚC: mọi hàm ở đây CHỈ thao tác DB, KHÔNG tự conn.commit() (theo
đúng convention db/contacts.py, db/companies.py...) — commit là trách
nhiệm của router sau khi gọi xong (và log_action nếu cần), để router
gộp nhiều thao tác vào 1 transaction khi cần.

'app_users' là tên bảng người dùng thật (xem
sql/migration_rename_ss_team_members.sql), role học viên là 'user'
(không phải 'student') — tên tham số student_id/ss_id trong file này
chỉ để dễ đọc theo đúng vai trò nghiệp vụ, không phải tên cột DB khác.
"""

import logging
from typing import Optional

import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

# Giới hạn số 'pending' đồng thời/học viên — chặn spam request tới
# hàng loạt SS khác nhau (cooldown 7 ngày chỉ chặn per-pair, không đủ
# nếu học viên đổi sang SS khác mỗi lần). Xem backend-scrap-jd-nhan-tin.md §2(a).
MAX_PENDING_PER_STUDENT = 3

# Cooldown trước khi học viên được gửi lại request sau khi bị declined.
DECLINE_COOLDOWN_DAYS = 7


# ============================================================
# chat_relationships — state machine
# ============================================================

def get_relationship(conn, student_id: str, ss_id: str) -> Optional[dict]:
    """Trả row chat_relationships giữa 1 cặp student/ss cụ thể, hoặc
    None nếu chưa từng có quan hệ nào (chưa ai nhắn/request)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT * FROM chat_relationships WHERE student_id = %s AND ss_id = %s",
            (student_id, ss_id),
        )
        return cur.fetchone()


def get_relationship_by_id(conn, relationship_id: str) -> Optional[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM chat_relationships WHERE id = %s", (relationship_id,))
        return cur.fetchone()


def count_pending_for_student(conn, student_id: str) -> int:
    """Đếm số request đang 'pending' của 1 học viên, TRÊN TOÀN BỘ SS
    (không chỉ 1 cặp) — dùng để enforce MAX_PENDING_PER_STUDENT trước
    khi tạo request mới. Gọi TRONG CÙNG transaction với INSERT ở
    create_pending_request() để tránh race (2 request đồng thời cùng
    đếm ra <3 rồi cùng insert, vượt ngưỡng)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM chat_relationships WHERE student_id = %s AND status = 'pending'",
            (student_id,),
        )
        return cur.fetchone()[0]


def create_pending_request(conn, student_id: str, ss_id: str) -> str:
    """(a) Học viên gửi request lần đầu (chưa có row) — INSERT thường,
    UNIQUE(student_id, ss_id) đảm bảo chỉ 1 request 'sống' tại 1 thời
    điểm cho 1 cặp. GỌI SAU KHI đã check count_pending_for_student() <
    MAX_PENDING_PER_STUDENT ở router — hàm này KHÔNG tự kiểm tra lại
    (giữ đơn giản, single-responsibility), và KHÔNG tự commit."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO chat_relationships (student_id, ss_id, status, initiated_by)
            VALUES (%s, %s, 'pending', %s)
            RETURNING id
            """,
            (student_id, ss_id, student_id),
        )
        return str(cur.fetchone()[0])


def reset_declined_to_pending(conn, student_id: str, ss_id: str) -> Optional[str]:
    """(b) Học viên gửi lại request SAU KHI đã bị 'declined' quá
    DECLINE_COOLDOWN_DAYS — reset về pending trên row cũ (giữ UNIQUE,
    không tạo row mới). Trả id nếu thành công, None nếu 0 dòng ảnh
    hưởng (còn cooldown / relationship không ở trạng thái declined) —
    router tự SELECT lại status hiện tại để trả lỗi đúng."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE chat_relationships
            SET status = 'pending', requested_at = now(), decided_at = NULL, declined_at = NULL
            WHERE student_id = %s AND ss_id = %s
              AND status = 'declined' AND declined_at < now() - interval '%s days'
            RETURNING id
            """,
            (student_id, ss_id, DECLINE_COOLDOWN_DAYS),
        )
        row = cur.fetchone()
        return str(row[0]) if row else None


def accept_relationship(conn, relationship_id: str, ss_id: str) -> bool:
    """(c) SS accept — CHỈ chuyển được từ đúng 'pending', và chỉ đúng
    ss_id sở hữu request đó (chặn SS khác accept hộ). Trả False nếu 0
    dòng ảnh hưởng (đã bị xử lý bởi thao tác khác / không đúng chủ sở
    hữu / không ở pending) — router trả 409 hoặc 403 tuỳ nguyên nhân."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE chat_relationships SET status = 'accepted', decided_at = now()
            WHERE id = %s AND ss_id = %s AND status = 'pending'
            RETURNING id
            """,
            (relationship_id, ss_id),
        )
        return cur.fetchone() is not None


def decline_relationship(conn, relationship_id: str, ss_id: str) -> bool:
    """(d) SS decline — cùng điều kiện atomic như accept."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE chat_relationships
            SET status = 'declined', decided_at = now(), declined_at = now()
            WHERE id = %s AND ss_id = %s AND status = 'pending'
            RETURNING id
            """,
            (relationship_id, ss_id),
        )
        return cur.fetchone() is not None


def block_relationship(conn, relationship_id: str, ss_id: str) -> bool:
    """(e) SS block — chặn được từ BẤT KỲ trạng thái nào của relationship
    ĐÃ TỒN TẠI (route block-by-relationship-id, dùng khi đã có lịch sử
    nhắn tin/request). Trả False nếu relationship không tồn tại hoặc
    không thuộc ss_id này."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE chat_relationships SET status = 'blocked', decided_at = now()
            WHERE id = %s AND ss_id = %s
            RETURNING id
            """,
            (relationship_id, ss_id),
        )
        return cur.fetchone() is not None


def block_student_by_ss(conn, student_id: str, ss_id: str) -> None:
    """Biến thể của (e) — SS chặn TRƯỚC 1 học viên chưa từng có quan
    hệ nào (chưa có row) — dùng ở route block theo student_id thay vì
    relationship_id, cho trường hợp SS muốn chặn trước khi học viên
    kịp nhắn. ON CONFLICT để vẫn hoạt động nếu đã có row ở trạng thái
    bất kỳ."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO chat_relationships (student_id, ss_id, status, initiated_by, decided_at)
            VALUES (%s, %s, 'blocked', %s, now())
            ON CONFLICT (student_id, ss_id) DO UPDATE
            SET status = 'blocked', decided_at = now()
            """,
            (student_id, ss_id, ss_id),
        )


def unblock_relationship(conn, relationship_id: str, ss_id: str) -> bool:
    """(f) SS unblock — về lại 'accepted' (không về 'pending', vì SS
    chủ động unblock nghĩa là SS đồng ý nhắn tiếp)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE chat_relationships SET status = 'accepted', decided_at = now()
            WHERE id = %s AND ss_id = %s AND status = 'blocked'
            RETURNING id
            """,
            (relationship_id, ss_id),
        )
        return cur.fetchone() is not None


def ensure_accepted_by_ss(conn, student_id: str, ss_id: str) -> None:
    """(g) SS nhắn TRƯỚC cho học viên (chưa có quan hệ, hoặc đang
    pending/declined) — tự động accept, TRỪ KHI đang 'blocked' (SS tự
    block thì gửi tiếp KHÔNG tự unblock — phải bấm Unblock riêng,
    tránh lỡ tay gửi nhầm làm mất hiệu lực block). Gọi trước khi
    insert_message() mỗi khi sender là SS/admin và receiver là học
    viên — no-op nếu quan hệ đã 'accepted' sẵn, và cố ý im lặng
    (không raise) nếu đang 'blocked': router sẽ đọc lại relationship
    ngay sau đó và tự quyết định 403 nếu cần."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO chat_relationships (student_id, ss_id, status, initiated_by, decided_at)
            VALUES (%s, %s, 'accepted', %s, now())
            ON CONFLICT (student_id, ss_id) DO UPDATE
            SET status = 'accepted', decided_at = now()
            WHERE chat_relationships.status != 'blocked'
            """,
            (student_id, ss_id, ss_id),
        )


# ============================================================
# messages
# ============================================================

def insert_message(conn, sender_id: str, receiver_id: str, content: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO messages (sender_id, receiver_id, content)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (sender_id, receiver_id, content),
        )
        return str(cur.fetchone()[0])


def get_message_by_id(conn, message_id: str) -> Optional[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM messages WHERE id = %s", (message_id,))
        return cur.fetchone()


def get_messages_between(
    conn, user_a: str, user_b: str, *, before_id: Optional[int] = None, limit: int = 50
) -> list[dict]:
    """Lịch sử đầy đủ giữa 2 người, mới nhất trước, phân trang cursor
    bằng before_id (id < before_id nếu có). Gọi CHỈ SAU KHI router đã
    xác nhận current_user thuộc về (user_a, user_b) — hàm này không tự
    check quyền (IDOR check là trách nhiệm router, xem
    api/routers/messages.py)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if before_id is not None:
            cur.execute(
                """
                SELECT * FROM messages
                WHERE ((sender_id = %s AND receiver_id = %s) OR (sender_id = %s AND receiver_id = %s))
                  AND id < %s
                ORDER BY id DESC
                LIMIT %s
                """,
                (user_a, user_b, user_b, user_a, before_id, limit),
            )
        else:
            cur.execute(
                """
                SELECT * FROM messages
                WHERE (sender_id = %s AND receiver_id = %s) OR (sender_id = %s AND receiver_id = %s)
                ORDER BY id DESC
                LIMIT %s
                """,
                (user_a, user_b, user_b, user_a, limit),
            )
        return cur.fetchall()


def get_messages_since(conn, user_a: str, user_b: str, after_id: int) -> list[dict]:
    """Polling nhẹ: chỉ tin có id > after_id giữa 2 người, cũ nhất
    trước (đúng thứ tự xuất hiện khi FE append vào khung chat)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT * FROM messages
            WHERE ((sender_id = %s AND receiver_id = %s) OR (sender_id = %s AND receiver_id = %s))
              AND id > %s
            ORDER BY id ASC
            """,
            (user_a, user_b, user_b, user_a, after_id),
        )
        return cur.fetchall()


def mark_read(conn, current_user_id: str, partner_id: str) -> int:
    """Đánh dấu đã đọc mọi tin partner_id gửi cho current_user_id còn
    unread. Trả số dòng bị ảnh hưởng."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE messages SET read_at = now()
            WHERE sender_id = %s AND receiver_id = %s AND read_at IS NULL
            """,
            (partner_id, current_user_id),
        )
        return cur.rowcount


def get_unread_count(conn, current_user_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM messages WHERE receiver_id = %s AND read_at IS NULL",
            (current_user_id,),
        )
        return cur.fetchone()[0]


def list_conversations(conn, current_user_id: str) -> list[dict]:
    """Danh sách hội thoại của current_user_id: mỗi partner từng nhắn
    qua lại, kèm tin nhắn cuối + unread_count + relationship_status
    (NULL nếu là cặp SS-SS, không qua state machine).

    v1 suy trực tiếp từ bảng messages bằng DISTINCT ON (không có bảng
    conversations riêng — xem backend-scrap-jd-nhan-tin.md §6 "cố tình
    để ngoài phạm vi", ghi chú nếu chậm dần khi data lớn thì chuyển
    sang bảng summary). Với quy mô hiện tại (nội bộ, số user nhỏ), query
    này đủ nhanh nhờ index idx_messages_sender_receiver /
    idx_messages_receiver_sender.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            WITH partners AS (
                SELECT sender_id AS partner_id, receiver_id AS me FROM messages WHERE receiver_id = %(me)s
                UNION
                SELECT receiver_id AS partner_id, sender_id AS me FROM messages WHERE sender_id = %(me)s
            ),
            last_msg AS (
                SELECT DISTINCT ON (p.partner_id)
                    p.partner_id,
                    m.content AS last_message_preview,
                    m.created_at AS last_message_at
                FROM partners p
                JOIN messages m
                  ON (m.sender_id = %(me)s AND m.receiver_id = p.partner_id)
                  OR (m.receiver_id = %(me)s AND m.sender_id = p.partner_id)
                ORDER BY p.partner_id, m.id DESC
            ),
            unread AS (
                SELECT sender_id AS partner_id, COUNT(*) AS unread_count
                FROM messages
                WHERE receiver_id = %(me)s AND read_at IS NULL
                GROUP BY sender_id
            )
            SELECT
                u.ss_user_id AS partner_id,
                u.full_name AS partner_name,
                u.role AS partner_role,
                lm.last_message_preview,
                lm.last_message_at,
                COALESCE(un.unread_count, 0) AS unread_count,
                r.status AS relationship_status
            FROM last_msg lm
            JOIN app_users u ON u.ss_user_id = lm.partner_id
            LEFT JOIN unread un ON un.partner_id = lm.partner_id
            LEFT JOIN chat_relationships r
              ON (r.student_id = %(me)s AND r.ss_id = lm.partner_id)
              OR (r.student_id = lm.partner_id AND r.ss_id = %(me)s)
            ORDER BY lm.last_message_at DESC
            """,
            {"me": current_user_id},
        )
        return cur.fetchall()


def list_pending_requests_for_ss(conn, ss_id: str) -> list[dict]:
    """Mục riêng "Yêu cầu đang chờ" cho SS — học viên nào đang pending
    với ss_id này, chưa từng nhắn nên KHÔNG nằm trong list_conversations()
    ở trên (list_conversations chỉ suy từ bảng messages đã có tin)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT r.id AS relationship_id, r.student_id, u.full_name AS student_name,
                   r.requested_at
            FROM chat_relationships r
            JOIN app_users u ON u.ss_user_id = r.student_id
            WHERE r.ss_id = %s AND r.status = 'pending'
            ORDER BY r.requested_at ASC
            """,
            (ss_id,),
        )
        return cur.fetchall()


def search_people(conn, query: str, *, requester_role: str) -> list[dict]:
    """Tìm người để bắt đầu hội thoại — CHỈ trả id/full_name/role,
    KHÔNG email/phone (xem backend-scrap-jd-nhan-tin.md §3, §4). Học
    viên ('user') chỉ thấy role ss_team/admin; SS/admin thấy mọi role."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if requester_role == "user":
            cur.execute(
                """
                SELECT ss_user_id AS id, full_name, role
                FROM app_users
                WHERE role IN ('ss_team', 'admin')
                  AND is_active = true
                  AND full_name ILIKE %s
                ORDER BY full_name
                LIMIT 20
                """,
                (f"%{query}%",),
            )
        else:
            cur.execute(
                """
                SELECT ss_user_id AS id, full_name, role
                FROM app_users
                WHERE is_active = true
                  AND full_name ILIKE %s
                ORDER BY full_name
                LIMIT 20
                """,
                (f"%{query}%",),
            )
        return cur.fetchall()
