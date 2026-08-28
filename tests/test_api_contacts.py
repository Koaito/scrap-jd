"""
Tests cho api/routers/contacts.py — route CRUD company_contacts.

Test coverage:
- Authentication/authorization (ss_team required)
- Validation (UUID format, contact_status enum, required fields)
- Business logic (mandatory note for update/delete/assign, soft delete)
- Error cases (404, 422, 409)
"""
import uuid
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException, Request

from api.routers.contacts import (
    _validate_assignee,
)
from db.contacts import ContactHasLinksError
from conftest import (
    make_company_record,
    make_contact_record,
)


# ------------------------------------------------------------------
# Helper: _validate_assignee()
# ------------------------------------------------------------------


def test_validate_assignee_invalid_uuid(mock_conn):
    """assigned_ss_user không phải UUID -> 400"""
    with pytest.raises(HTTPException) as exc_info:
        _validate_assignee(mock_conn, "not-a-uuid")
    assert exc_info.value.status_code == 400
    assert "không đúng định dạng UUID" in exc_info.value.detail


def test_validate_assignee_user_not_found(mock_conn):
    """assigned_ss_user UUID hợp lệ nhưng không tồn tại -> 404"""
    with patch("api.routers.contacts.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        mock_db.get_user_by_id.return_value = None

        with pytest.raises(HTTPException) as exc_info:
            _validate_assignee(mock_conn, str(uuid.uuid4()))
        assert exc_info.value.status_code == 404
        assert "Không tìm thấy tài khoản" in exc_info.value.detail


def test_validate_assignee_insufficient_role(mock_conn):
    """assigned_ss_user tồn tại nhưng role='user' (không đủ quyền) -> 422"""
    with patch("api.routers.contacts.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        mock_db.get_user_by_id.return_value = {"role": "user"}

        with pytest.raises(HTTPException) as exc_info:
            _validate_assignee(mock_conn, str(uuid.uuid4()))
        assert exc_info.value.status_code == 422
        assert "ss_team" in exc_info.value.detail or "admin" in exc_info.value.detail


def test_validate_assignee_valid_ss_team(mock_conn):
    """assigned_ss_user hợp lệ với role ss_team -> pass"""
    with patch("api.routers.contacts.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        mock_db.get_user_by_id.return_value = {"role": "ss_team"}

        # Không raise exception
        _validate_assignee(mock_conn, str(uuid.uuid4()))


def test_validate_assignee_valid_admin(mock_conn):
    """assigned_ss_user hợp lệ với role admin -> pass"""
    with patch("api.routers.contacts.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        mock_db.get_user_by_id.return_value = {"role": "admin"}

        # Không raise exception
        _validate_assignee(mock_conn, str(uuid.uuid4()))


# ------------------------------------------------------------------
# GET /contacts (list_all_contacts)
# ------------------------------------------------------------------


def test_list_all_contacts_invalid_contact_status(
    mock_conn, ss_team_user, test_company_id
):
    """contact_status không hợp lệ -> 400"""
    with patch("api.routers.contacts.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True

        from api.routers.contacts import list_all_contacts

        with pytest.raises(HTTPException) as exc_info:
            list_all_contacts(
                request=MagicMock(spec=Request),
                contact_status="INVALID_STATUS",
                include_inactive=False,
                company_id=None,
                search=None,
                created_by=None,
                assigned_ss_user=None,
                user=ss_team_user,
                conn=mock_conn,
            )
        assert exc_info.value.status_code == 400
        assert "không hợp lệ" in exc_info.value.detail


def test_list_all_contacts_invalid_company_id(mock_conn, ss_team_user):
    """company_id không đúng UUID -> 400"""
    with patch("api.routers.contacts.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = False

        from api.routers.contacts import list_all_contacts

        with pytest.raises(HTTPException) as exc_info:
            list_all_contacts(
                request=MagicMock(spec=Request),
                contact_status=None,
                include_inactive=False,
                company_id="not-a-uuid",
                search=None,
                created_by=None,
                assigned_ss_user=None,
                user=ss_team_user,
                conn=mock_conn,
            )
        assert exc_info.value.status_code == 400
        assert "company_id" in exc_info.value.detail


def test_list_all_contacts_company_not_found(
    mock_conn, ss_team_user, test_company_id
):
    """company_id hợp lệ nhưng không tồn tại -> 404"""
    with patch("api.routers.contacts.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        mock_db.get_company_by_id.return_value = None

        from api.routers.contacts import list_all_contacts

        with pytest.raises(HTTPException) as exc_info:
            list_all_contacts(
                request=MagicMock(spec=Request),
                contact_status=None,
                include_inactive=False,
                company_id=test_company_id,
                search=None,
                created_by=None,
                assigned_ss_user=None,
                user=ss_team_user,
                conn=mock_conn,
            )
        assert exc_info.value.status_code == 404
        assert "công ty" in exc_info.value.detail


def test_list_all_contacts_success(mock_conn, ss_team_user, test_company_id):
    """list_all_contacts thành công trả về danh sách"""
    with patch("api.routers.contacts.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        mock_db.get_company_by_id.return_value = make_company_record(test_company_id)
        mock_db.list_all_contacts.return_value = [
            make_contact_record(str(uuid.uuid4()), test_company_id)
        ]

        from api.routers.contacts import list_all_contacts

        result = list_all_contacts(
            request=MagicMock(spec=Request),
            contact_status="UNCONTACTED",
            include_inactive=False,
            company_id=test_company_id,
            search=None,
            created_by=None,
            assigned_ss_user=None,
            user=ss_team_user,
            conn=mock_conn,
        )
        assert len(result) == 1
        mock_db.list_all_contacts.assert_called_once()


# ------------------------------------------------------------------
# POST /companies/{company_id}/contacts (create_contact)
# ------------------------------------------------------------------


def test_create_contact_invalid_company_id(mock_conn, ss_team_user):
    """company_id không đúng UUID -> 400"""
    with patch("api.routers.contacts.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = False

        from api.routers.contacts import create_contact
        from api.schemas import CompanyContactCreate

        with pytest.raises(HTTPException) as exc_info:
            create_contact(
                company_id="not-a-uuid",
                payload=CompanyContactCreate(contact_name="Test"),
                user=ss_team_user,
                conn=mock_conn,
            )
        assert exc_info.value.status_code == 400


def test_create_contact_company_not_found(
    mock_conn, ss_team_user, test_company_id
):
    """company_id không tồn tại -> 404"""
    with patch("api.routers.contacts.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        mock_db.get_company_by_id.return_value = None

        from api.routers.contacts import create_contact
        from api.schemas import CompanyContactCreate

        with pytest.raises(HTTPException) as exc_info:
            create_contact(
                company_id=test_company_id,
                payload=CompanyContactCreate(contact_name="Test"),
                user=ss_team_user,
                conn=mock_conn,
            )
        assert exc_info.value.status_code == 404


def test_create_contact_success(
    mock_conn, ss_team_user, test_company_id, test_contact_id
):
    """Tạo contact thành công"""
    with patch("api.routers.contacts.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        mock_db.get_company_by_id.return_value = make_company_record(test_company_id)
        mock_db.create_company_contact.return_value = test_contact_id
        mock_db.get_company_contact_by_id.return_value = make_contact_record(
            test_contact_id, test_company_id
        )

        from api.routers.contacts import create_contact
        from api.schemas import CompanyContactCreate

        result = create_contact(
            company_id=test_company_id,
            payload=CompanyContactCreate(
                contact_name="Nguyễn Văn A", note="Thêm contact mới"
            ),
            user=ss_team_user,
            conn=mock_conn,
        )
        assert result["contact_name"] == "Nguyễn Văn A"
        mock_db.log_action.assert_called_once()
        mock_conn.commit.assert_called_once()


# ------------------------------------------------------------------
# PATCH /companies/{company_id}/contacts/{contact_id} (update_contact)
# ------------------------------------------------------------------


def test_update_contact_missing_note_with_changes(
    mock_conn, ss_team_user, test_company_id, test_contact_id
):
    """Sửa contact có thay đổi field nhưng thiếu note -> 422"""
    with patch("api.routers.contacts.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        existing = make_contact_record(test_contact_id, test_company_id)
        mock_db.get_company_contact_by_id.return_value = existing
        # diff_changed_fields trả về có thay đổi
        mock_db.diff_changed_fields.return_value = {"job_title": {"old": "HR", "new": "Manager"}}

        from api.routers.contacts import update_contact
        from api.schemas import CompanyContactUpdate

        with pytest.raises(HTTPException) as exc_info:
            update_contact(
                company_id=test_company_id,
                contact_id=test_contact_id,
                payload=CompanyContactUpdate(job_title="Manager", note=None),
                user=ss_team_user,
                conn=mock_conn,
            )
        assert exc_info.value.status_code == 422
        assert "note" in exc_info.value.detail.lower()


def test_update_contact_no_changes_no_note_required(
    mock_conn, ss_team_user, test_company_id, test_contact_id
):
    """Không có thay đổi thật -> không cần note, pass"""
    with patch("api.routers.contacts.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        existing = make_contact_record(test_contact_id, test_company_id)
        mock_db.get_company_contact_by_id.return_value = existing
        mock_db.diff_changed_fields.return_value = {}  # Không có thay đổi
        mock_db.update_company_contact.return_value = True

        from api.routers.contacts import update_contact
        from api.schemas import CompanyContactUpdate

        update_contact(
            company_id=test_company_id,
            contact_id=test_contact_id,
            payload=CompanyContactUpdate(note=None),  # Không cần note
            user=ss_team_user,
            conn=mock_conn,
        )
        # Không raise exception, không ghi log (vì không có changes)
        mock_db.log_action.assert_not_called()
        mock_conn.commit.assert_called_once()


def test_update_contact_invalid_status(
    mock_conn, ss_team_user, test_company_id, test_contact_id
):
    """contact_status không hợp lệ -> 400"""
    with patch("api.routers.contacts.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        existing = make_contact_record(test_contact_id, test_company_id)
        mock_db.get_company_contact_by_id.return_value = existing

        from api.routers.contacts import update_contact
        from api.schemas import CompanyContactUpdate

        with pytest.raises(HTTPException) as exc_info:
            update_contact(
                company_id=test_company_id,
                contact_id=test_contact_id,
                payload=CompanyContactUpdate(
                    contact_status="INVALID", note="test"
                ),
                user=ss_team_user,
                conn=mock_conn,
            )
        assert exc_info.value.status_code == 400
        assert "contact_status" in exc_info.value.detail


# ------------------------------------------------------------------
# PATCH /companies/{company_id}/contacts/{contact_id}/assign
# ------------------------------------------------------------------


def test_assign_contact_missing_note_with_change(
    mock_conn, ss_team_user, test_company_id, test_contact_id
):
    """Gán contact có thay đổi người phụ trách nhưng thiếu note -> 422"""
    with patch("api.routers.contacts.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        existing = make_contact_record(
            test_contact_id, test_company_id, assigned_ss_user=None
        )
        mock_db.get_company_contact_by_id.return_value = existing
        mock_db.get_user_by_id.return_value = {"role": "ss_team"}

        from api.routers.contacts import assign_contact
        from api.schemas import ContactAssignUpdate

        new_assignee = str(uuid.uuid4())
        with pytest.raises(HTTPException) as exc_info:
            assign_contact(
                company_id=test_company_id,
                contact_id=test_contact_id,
                payload=ContactAssignUpdate(assigned_ss_user=new_assignee, note=None),
                user=ss_team_user,
                conn=mock_conn,
            )
        assert exc_info.value.status_code == 422
        assert "note" in exc_info.value.detail.lower()


def test_assign_contact_no_change_no_note_required(
    mock_conn, ss_team_user, test_company_id, test_contact_id
):
    """Gán lại người cũ (không thay đổi) -> không cần note"""
    assignee_id = str(uuid.uuid4())
    with patch("api.routers.contacts.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        existing = make_contact_record(
            test_contact_id, test_company_id, assigned_ss_user=uuid.UUID(assignee_id)
        )
        mock_db.get_company_contact_by_id.return_value = existing
        mock_db.get_user_by_id.return_value = {"role": "ss_team"}

        from api.routers.contacts import assign_contact
        from api.schemas import ContactAssignUpdate

        assign_contact(
            company_id=test_company_id,
            contact_id=test_contact_id,
            payload=ContactAssignUpdate(assigned_ss_user=assignee_id, note=None),
            user=ss_team_user,
            conn=mock_conn,
        )
        # Không raise exception, không ghi log
        mock_db.log_action.assert_not_called()
        mock_conn.commit.assert_called_once()


# ------------------------------------------------------------------
# DELETE /companies/{company_id}/contacts/{contact_id} (soft delete)
# ------------------------------------------------------------------


def test_delete_contact_missing_note(
    mock_conn, ss_team_user, test_company_id, test_contact_id
):
    """Xoá contact thiếu note -> 422 (từ Pydantic validation, không qua route)"""
    # ContactDeleteRequest.note không có default, Pydantic sẽ raise trước
    from api.schemas import ContactDeleteRequest

    with pytest.raises(Exception):  # Pydantic ValidationError
        ContactDeleteRequest()  # Missing required field 'note'


def test_delete_contact_success(
    mock_conn, ss_team_user, test_company_id, test_contact_id
):
    """Xoá mềm contact thành công"""
    with patch("api.routers.contacts.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        existing = make_contact_record(
            test_contact_id, test_company_id, is_active=True
        )
        mock_db.get_company_contact_by_id.return_value = existing

        from api.routers.contacts import delete_contact
        from api.schemas import ContactDeleteRequest

        result = delete_contact(
            company_id=test_company_id,
            contact_id=test_contact_id,
            payload=ContactDeleteRequest(note="Xoá contact không còn làm việc"),
            user=ss_team_user,
            conn=mock_conn,
        )
        assert result is None
        mock_db.soft_delete_company_contact.assert_called_once()
        mock_db.log_action.assert_called_once()
        mock_conn.commit.assert_called_once()


def test_delete_contact_already_inactive(
    mock_conn, ss_team_user, test_company_id, test_contact_id
):
    """Xoá contact đã inactive -> 204, không ghi log lần 2"""
    with patch("api.routers.contacts.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        existing = make_contact_record(
            test_contact_id, test_company_id, is_active=False
        )
        mock_db.get_company_contact_by_id.return_value = existing

        from api.routers.contacts import delete_contact
        from api.schemas import ContactDeleteRequest

        result = delete_contact(
            company_id=test_company_id,
            contact_id=test_contact_id,
            payload=ContactDeleteRequest(note="Test"),
            user=ss_team_user,
            conn=mock_conn,
        )
        assert result is None
        mock_db.log_action.assert_not_called()  # Không ghi log lần 2
        mock_conn.commit.assert_called_once()


# ------------------------------------------------------------------
# DELETE /companies/{company_id}/contacts/{contact_id}/hard
# ------------------------------------------------------------------


def test_hard_delete_contact_still_active(
    mock_conn, ss_team_user, test_company_id, test_contact_id
):
    """Hard delete contact vẫn active -> 409"""
    with patch("api.routers.contacts.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        existing = make_contact_record(
            test_contact_id, test_company_id, is_active=True
        )
        mock_db.get_company_contact_by_id.return_value = existing

        from api.routers.contacts import hard_delete_contact

        with pytest.raises(HTTPException) as exc_info:
            hard_delete_contact(
                company_id=test_company_id,
                contact_id=test_contact_id,
                user=ss_team_user,
                conn=mock_conn,
            )
        assert exc_info.value.status_code == 409
        assert "xoá mềm" in exc_info.value.detail.lower()


def test_hard_delete_contact_has_links(
    mock_conn, ss_team_user, test_company_id, test_contact_id
):
    """Hard delete contact có job_contact_links -> 409"""
    with patch("api.routers.contacts.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        existing = make_contact_record(
            test_contact_id, test_company_id, is_active=False
        )
        mock_db.get_company_contact_by_id.return_value = existing
        # QUAN TRỌNG: patch("...db_module") mock TOÀN BỘ module, kể cả
        # class exception — mock_db.ContactHasLinksError mặc định chỉ
        # là 1 MagicMock tự sinh (KHÔNG phải class Exception thật), gọi
        # nó ra 1 MagicMock instance chứ không phải exception, nên gán
        # instance đó vào .side_effect sẽ KHÔNG raise gì cả (mock chỉ
        # return giá trị đó). Phải gán lại mock_db.ContactHasLinksError
        # = class THẬT trước, để cả (a) .side_effect raise đúng và (b)
        # `except db_module.ContactHasLinksError` trong router khớp
        # đúng class đang được raise (router dùng CHUNG object đã bị
        # patch này).
        mock_db.ContactHasLinksError = ContactHasLinksError
        mock_db.hard_delete_company_contact.side_effect = (
            ContactHasLinksError("Contact có job links")
        )

        from api.routers.contacts import hard_delete_contact

        with pytest.raises(HTTPException) as exc_info:
            hard_delete_contact(
                company_id=test_company_id,
                contact_id=test_contact_id,
                user=ss_team_user,
                conn=mock_conn,
            )
        assert exc_info.value.status_code == 409


def test_hard_delete_contact_success(
    mock_conn, ss_team_user, test_company_id, test_contact_id
):
    """Hard delete contact thành công"""
    with patch("api.routers.contacts.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        existing = make_contact_record(
            test_contact_id, test_company_id, is_active=False
        )
        mock_db.get_company_contact_by_id.return_value = existing

        from api.routers.contacts import hard_delete_contact

        result = hard_delete_contact(
            company_id=test_company_id,
            contact_id=test_contact_id,
            user=ss_team_user,
            conn=mock_conn,
        )
        assert result is None
        mock_db.hard_delete_company_contact.assert_called_once()
        mock_conn.commit.assert_called_once()
