"""
Tests cho api/routers/email_templates.py — router CRUD mẫu email liên
hệ doanh nghiệp (thêm 08/2026, xem sql/migration_add_email_templates.sql
+ lịch sử trao đổi "chia phần danh sách contact thành 2 phần").

Test coverage:
- GET /email-templates (list), GET /email-templates/{id} (404 nếu không
  có), GET /email-templates/placeholder-help
- POST: tạo mới, note tuỳ chọn (không bắt buộc)
- PATCH: bắt buộc note NẾU có thay đổi, KHÔNG bắt buộc nếu patch rỗng/
  trùng giá trị cũ, validate recommended_for
- DELETE: hard delete thật (gọi db_module.delete_email_template), note
  bắt buộc ngay từ Pydantic (422 trước khi chạm DB)
- Validation: UUID format, recommended_for chỉ nhận 4 giá trị hợp lệ
"""
import uuid
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from conftest import make_email_template_record


@pytest.fixture
def test_template_id():
    return str(uuid.uuid4())


# ------------------------------------------------------------------
# GET /email-templates
# ------------------------------------------------------------------


def test_list_email_templates_success(mock_conn, ss_team_user):
    with patch("api.routers.email_templates.db_module") as mock_db:
        mock_db.list_email_templates.return_value = [
            make_email_template_record(str(uuid.uuid4()), title="Giới thiệu MindX"),
            make_email_template_record(str(uuid.uuid4()), title="Xin JD Intern/Fresher"),
        ]

        from api.routers.email_templates import list_email_templates

        result = list_email_templates(conn=mock_conn, user=ss_team_user)
        assert len(result) == 2
        mock_db.list_email_templates.assert_called_once_with(mock_conn)


def test_get_email_template_not_found(mock_conn, ss_team_user, test_template_id):
    with patch("api.routers.email_templates.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        mock_db.get_email_template_by_id.return_value = None

        from api.routers.email_templates import get_email_template

        with pytest.raises(HTTPException) as exc_info:
            get_email_template(template_id=test_template_id, conn=mock_conn, user=ss_team_user)
        assert exc_info.value.status_code == 404


def test_get_email_template_invalid_uuid(mock_conn, ss_team_user):
    with patch("api.routers.email_templates.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = False

        from api.routers.email_templates import get_email_template

        with pytest.raises(HTTPException) as exc_info:
            get_email_template(template_id="not-a-uuid", conn=mock_conn, user=ss_team_user)
        assert exc_info.value.status_code == 400
        assert "UUID" in exc_info.value.detail


def test_get_placeholder_help_returns_5_fixed_placeholders(ss_team_user):
    from api.routers.email_templates import get_placeholder_help

    result = get_placeholder_help(user=ss_team_user)
    # Giữ nguyên đúng 5 placeholder cố định theo yêu cầu đã chốt —
    # không tự do thêm/bớt.
    assert set(result.placeholders.keys()) == {
        "{{LOI_CHAO}}", "{{TEN_CONG_TY}}", "{{TEN_NGUOI_LIEN_HE}}",
        "{{CHUC_DANH}}", "{{TEN_STAFF}}",
    }
    # Mỗi placeholder có ghi chú hướng dẫn thật (không rỗng).
    assert all(v.strip() for v in result.placeholders.values())


# ------------------------------------------------------------------
# POST /email-templates
# ------------------------------------------------------------------


def test_create_email_template_success_note_optional(mock_conn, ss_team_user, test_template_id):
    """Tạo mẫu mới KHÔNG kèm note vẫn thành công — CREATE_EMAIL_TEMPLATE
    không thuộc nhóm bắt buộc note (khác UPDATE/DELETE)."""
    with patch("api.routers.email_templates.db_module") as mock_db:
        mock_db.create_email_template.return_value = test_template_id
        created = make_email_template_record(test_template_id, title="Mẫu mới")
        mock_db.get_email_template_by_id.return_value = created

        from api.routers.email_templates import create_email_template
        from api.schemas import EmailTemplateCreate

        result = create_email_template(
            payload=EmailTemplateCreate(
                title="Mẫu mới", body="Nội dung {{TEN_CONG_TY}}",
                recommended_for=["UNCONTACTED"], display_order=7,
            ),
            conn=mock_conn, user=ss_team_user,
        )
        assert result["title"] == "Mẫu mới"
        mock_db.log_action.assert_called_once()
        _, kwargs = mock_db.log_action.call_args
        assert kwargs["action_type"] == "CREATE_EMAIL_TEMPLATE"
        assert kwargs["note"] is None
        mock_conn.commit.assert_called_once()


def test_create_email_template_invalid_recommended_for():
    """recommended_for chứa giá trị ngoài 4 trạng thái hợp lệ -> lỗi validate Pydantic"""
    from api.schemas import EmailTemplateCreate

    with pytest.raises(ValidationError):
        EmailTemplateCreate(title="X", body="Y", recommended_for=["KHONG_HOP_LE"])


def test_create_email_template_blank_title_rejected():
    from api.schemas import EmailTemplateCreate

    with pytest.raises(ValidationError):
        EmailTemplateCreate(title="   ", body="Nội dung")


# ------------------------------------------------------------------
# PATCH /email-templates/{template_id}
# ------------------------------------------------------------------


def test_patch_email_template_missing_note_with_changes(mock_conn, ss_team_user, test_template_id):
    """Sửa mẫu có thay đổi field thật nhưng thiếu note -> 422"""
    with patch("api.routers.email_templates.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        existing = make_email_template_record(test_template_id)
        mock_db.get_email_template_by_id.return_value = existing
        mock_db.diff_changed_fields.return_value = {"title": {"old": "Cũ", "new": "Mới"}}

        from api.routers.email_templates import patch_email_template
        from api.schemas import EmailTemplateUpdate

        with pytest.raises(HTTPException) as exc_info:
            patch_email_template(
                template_id=test_template_id,
                payload=EmailTemplateUpdate(title="Mới", note=None),
                conn=mock_conn, user=ss_team_user,
            )
        assert exc_info.value.status_code == 422
        assert "note" in exc_info.value.detail.lower()
        mock_db.patch_email_template.assert_not_called()


def test_patch_email_template_no_changes_no_note_required(mock_conn, ss_team_user, test_template_id):
    """Không có thay đổi thật -> không cần note, PATCH vẫn thành công"""
    with patch("api.routers.email_templates.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        existing = make_email_template_record(test_template_id)
        mock_db.get_email_template_by_id.return_value = existing
        mock_db.diff_changed_fields.return_value = {}
        mock_db.patch_email_template.return_value = True

        from api.routers.email_templates import patch_email_template
        from api.schemas import EmailTemplateUpdate

        patch_email_template(
            template_id=test_template_id,
            payload=EmailTemplateUpdate(note=None),
            conn=mock_conn, user=ss_team_user,
        )
        mock_db.log_action.assert_not_called()
        mock_conn.commit.assert_called_once()


def test_patch_email_template_with_note_success(mock_conn, ss_team_user, test_template_id):
    """Sửa có thay đổi + có note -> thành công, log đúng action + note"""
    with patch("api.routers.email_templates.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        existing = make_email_template_record(test_template_id, title="Cũ")
        mock_db.get_email_template_by_id.return_value = existing
        mock_db.diff_changed_fields.return_value = {"title": {"old": "Cũ", "new": "Mới"}}
        mock_db.patch_email_template.return_value = True

        from api.routers.email_templates import patch_email_template
        from api.schemas import EmailTemplateUpdate

        patch_email_template(
            template_id=test_template_id,
            payload=EmailTemplateUpdate(title="Mới", note="Sửa lại lời chào cho lịch sự hơn"),
            conn=mock_conn, user=ss_team_user,
        )
        mock_db.log_action.assert_called_once()
        _, kwargs = mock_db.log_action.call_args
        assert kwargs["action_type"] == "UPDATE_EMAIL_TEMPLATE"
        assert kwargs["note"] == "Sửa lại lời chào cho lịch sự hơn"
        mock_conn.commit.assert_called_once()


def test_patch_email_template_not_found(mock_conn, ss_team_user, test_template_id):
    with patch("api.routers.email_templates.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        mock_db.get_email_template_by_id.return_value = None

        from api.routers.email_templates import patch_email_template
        from api.schemas import EmailTemplateUpdate

        with pytest.raises(HTTPException) as exc_info:
            patch_email_template(
                template_id=test_template_id,
                payload=EmailTemplateUpdate(title="Mới"),
                conn=mock_conn, user=ss_team_user,
            )
        assert exc_info.value.status_code == 404


def test_patch_email_template_invalid_recommended_for():
    from api.schemas import EmailTemplateUpdate

    with pytest.raises(ValidationError):
        EmailTemplateUpdate(recommended_for=["SAI_GIA_TRI"])


# ------------------------------------------------------------------
# DELETE /email-templates/{template_id}
# ------------------------------------------------------------------


def test_delete_email_template_requires_note_at_schema_level():
    """note bắt buộc ngay từ Pydantic — thiếu note -> 422 KHÔNG chạm DB
    (khác pattern UPDATE, ở đây note luôn bắt buộc, không có ngoại lệ
    'không đổi gì thì thôi' vì XOÁ luôn là 1 thay đổi thật)."""
    from api.schemas import EmailTemplateDeleteRequest

    with pytest.raises(ValidationError):
        EmailTemplateDeleteRequest()

    with pytest.raises(ValidationError):
        EmailTemplateDeleteRequest(note="   ")


def test_delete_email_template_hard_deletes_and_logs(mock_conn, ss_team_user, test_template_id):
    """Xoá thành công -> gọi db_module.delete_email_template() (HARD
    DELETE thật, khác soft_delete_company/soft_delete_company_contact),
    log_action() gọi TRƯỚC đó trong cùng transaction."""
    with patch("api.routers.email_templates.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        existing = make_email_template_record(test_template_id, title="Mẫu cần xoá")
        mock_db.get_email_template_by_id.return_value = existing
        mock_db.delete_email_template.return_value = True

        from api.routers.email_templates import delete_email_template
        from api.schemas import EmailTemplateDeleteRequest

        delete_email_template(
            template_id=test_template_id,
            payload=EmailTemplateDeleteRequest(note="Không còn phù hợp, trùng với mẫu khác"),
            conn=mock_conn, user=ss_team_user,
        )

        mock_db.log_action.assert_called_once()
        _, kwargs = mock_db.log_action.call_args
        assert kwargs["action_type"] == "DELETE_EMAIL_TEMPLATE"
        assert kwargs["note"] == "Không còn phù hợp, trùng với mẫu khác"
        mock_db.delete_email_template.assert_called_once_with(mock_conn, test_template_id)
        mock_conn.commit.assert_called_once()


def test_delete_email_template_not_found(mock_conn, ss_team_user, test_template_id):
    with patch("api.routers.email_templates.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = True
        mock_db.get_email_template_by_id.return_value = None

        from api.routers.email_templates import delete_email_template
        from api.schemas import EmailTemplateDeleteRequest

        with pytest.raises(HTTPException) as exc_info:
            delete_email_template(
                template_id=test_template_id,
                payload=EmailTemplateDeleteRequest(note="lý do"),
                conn=mock_conn, user=ss_team_user,
            )
        assert exc_info.value.status_code == 404
        mock_db.delete_email_template.assert_not_called()


def test_delete_email_template_invalid_uuid(mock_conn, ss_team_user):
    with patch("api.routers.email_templates.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = False

        from api.routers.email_templates import delete_email_template
        from api.schemas import EmailTemplateDeleteRequest

        with pytest.raises(HTTPException) as exc_info:
            delete_email_template(
                template_id="not-a-uuid",
                payload=EmailTemplateDeleteRequest(note="lý do"),
                conn=mock_conn, user=ss_team_user,
            )
        assert exc_info.value.status_code == 400


# ------------------------------------------------------------------
# db/audit_logs.py — ACTION_LOG_RULES cho 3 action mới
# ------------------------------------------------------------------


def test_action_log_rules_email_template_actions():
    """CREATE không bắt buộc note; UPDATE/DELETE bắt buộc — đúng yêu cầu
    đã chốt, khớp hành vi 422 test ở trên."""
    from db.audit_logs import ACTION_LOG_RULES

    assert ACTION_LOG_RULES["CREATE_EMAIL_TEMPLATE"]["note_required"] is False
    assert ACTION_LOG_RULES["UPDATE_EMAIL_TEMPLATE"]["note_required"] is True
    assert ACTION_LOG_RULES["DELETE_EMAIL_TEMPLATE"]["note_required"] is True
    # Cả 3 đều là log thủ công (staff chủ động), giống CREATE_CONTACT/
    # UPDATE_CONTACT/DELETE_CONTACT — không tự động như CREATE_JOB (crawl).
    assert ACTION_LOG_RULES["CREATE_EMAIL_TEMPLATE"]["is_manual_log"] is True
    assert ACTION_LOG_RULES["UPDATE_EMAIL_TEMPLATE"]["is_manual_log"] is True
    assert ACTION_LOG_RULES["DELETE_EMAIL_TEMPLATE"]["is_manual_log"] is True
