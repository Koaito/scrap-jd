"""
Tests cho api/routers/import_export.py — route import/export CSV/XLSX.

Test coverage:
- Export validation (entity_type, format)
- Import preview validation (file format, row validation, permissions)
- Import confirm validation (preview ownership, row resolutions)
- Error handling (422 validation errors, rollback on failure)
"""
import io
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException, UploadFile

from api.routers.import_export import _check_entity_type, _VALID_ENTITY_TYPES
from api.services.file_parser import FileTooLargeError, UnsupportedFileFormatError
from api.services.import_executor import RowResolutionError
from api.services.preview_manager import (
    PreviewExpiredError,
    PreviewNotFoundError,
    PreviewOwnershipError,
)
from conftest import (
    make_preview_record,
)


# ------------------------------------------------------------------
# Helper: _check_entity_type()
# ------------------------------------------------------------------


def test_check_entity_type_valid():
    """entity_type hợp lệ -> không raise"""
    for valid_type in _VALID_ENTITY_TYPES:
        _check_entity_type(valid_type)  # Không raise exception


def test_check_entity_type_invalid():
    """entity_type không hợp lệ -> 400"""
    with pytest.raises(HTTPException) as exc_info:
        _check_entity_type("invalid_type")
    assert exc_info.value.status_code == 400
    assert "không hợp lệ" in exc_info.value.detail


# ------------------------------------------------------------------
# GET /export/{entity_type}
# ------------------------------------------------------------------


def test_export_entity_invalid_type(mock_conn, ss_team_user):
    """Export với entity_type không hợp lệ -> 400"""
    from api.routers.import_export import export_entity

    with pytest.raises(HTTPException) as exc_info:
        export_entity(
            entity_type="invalid",
            format="csv",
            conn=mock_conn,
            user=ss_team_user,
        )
    assert exc_info.value.status_code == 400


def test_export_entity_success_csv(mock_conn, ss_team_user):
    """Export CSV thành công"""
    with patch("api.routers.import_export.export_query") as mock_export_query:
        with patch("api.routers.import_export.file_parser") as mock_file_parser:
            with patch("api.routers.import_export.get_spec") as mock_get_spec:
                # Setup mocks
                mock_export_query.QUERY_FUNCS = {
                    "job": MagicMock(return_value=[{"job_id": "1", "job_title": "Test"}])
                }
                mock_get_spec.return_value = MagicMock(
                    export_columns=["job_id", "job_title"]
                )
                mock_file_parser.generate_export_file.return_value = io.BytesIO(
                    b"job_id,job_title\n1,Test"
                )
                mock_file_parser.content_type_for_format.return_value = "text/csv"

                from api.routers.import_export import export_entity

                response = export_entity(
                    entity_type="job",
                    format="csv",
                    # filter_params dùng Depends(_export_filter_params)
                    # trong route thật (FastAPI tự resolve khi gọi qua
                    # HTTP) — gọi hàm TRỰC TIẾP như unit test ở đây thì
                    # không có DI nào chạy, phải tự truyền dict thay
                    # thế, ĐỦ 7 KEY giống hệt _export_filter_params()
                    # trả về (_build_export_filters() nhận qua
                    # **filter_params, thiếu key nào là TypeError thiếu
                    # positional argument).
                    filter_params={
                        "status": None, "is_active": None, "company_id": None,
                        "date_field": "created_at", "from_date": None,
                        "to_date": None, "limit": None,
                    },
                    conn=mock_conn,
                    user=ss_team_user,
                )
                # StreamingResponse returned
                assert response is not None
                mock_file_parser.generate_export_file.assert_called_once()


def test_export_entity_success_xlsx(mock_conn, ss_team_user):
    """Export XLSX thành công"""
    with patch("api.routers.import_export.export_query") as mock_export_query:
        with patch("api.routers.import_export.file_parser") as mock_file_parser:
            with patch("api.routers.import_export.get_spec") as mock_get_spec:
                mock_export_query.QUERY_FUNCS = {
                    "contact": MagicMock(return_value=[])
                }
                mock_get_spec.return_value = MagicMock(export_columns=[])
                mock_file_parser.generate_export_file.return_value = io.BytesIO(b"")
                mock_file_parser.content_type_for_format.return_value = (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                )

                from api.routers.import_export import export_entity

                response = export_entity(
                    entity_type="contact",
                    format="xlsx",
                    filter_params={  # xem giải thích ở test_export_entity_success_csv
                        "status": None, "is_active": None, "company_id": None,
                        "date_field": "created_at", "from_date": None,
                        "to_date": None, "limit": None,
                    },
                    conn=mock_conn,
                    user=ss_team_user,
                )
                assert response is not None


# ------------------------------------------------------------------
# POST /import/{entity_type}/preview
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_import_preview_invalid_entity_type(mock_conn, ss_team_user):
    """Import preview với entity_type không hợp lệ -> 400"""
    from api.routers.import_export import import_preview

    mock_file = UploadFile(filename="test.csv", file=io.BytesIO(b"test"))

    with pytest.raises(HTTPException) as exc_info:
        await import_preview(
            entity_type="invalid",
            file=mock_file,
            conn=mock_conn,
            user=ss_team_user,
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_import_preview_unsupported_file_format(mock_conn, ss_team_user):
    """Import file không hỗ trợ (không phải CSV/XLSX) -> 400"""
    with patch("api.routers.import_export.file_parser") as mock_file_parser:
        # Gán class exception THẬT (không phải mock_file_parser.Xxx tự
        # sinh) — xem giải thích chi tiết ở test_hard_delete_contact_has_links
        # (test_api_contacts.py): patch cả module thì .side_effect gán
        # bằng exception giả sẽ không raise, và `except
        # file_parser.UnsupportedFileFormatError` trong router cũng cần
        # khớp đúng class này.
        mock_file_parser.UnsupportedFileFormatError = UnsupportedFileFormatError
        mock_file_parser.parse_file.side_effect = UnsupportedFileFormatError()

        from api.routers.import_export import import_preview

        mock_file = UploadFile(filename="test.txt", file=io.BytesIO(b"test"))

        with pytest.raises(HTTPException) as exc_info:
            await import_preview(
                entity_type="job",
                file=mock_file,
                conn=mock_conn,
                user=ss_team_user,
            )
        assert exc_info.value.status_code == 400
        assert "Unsupported file format" in exc_info.value.detail


@pytest.mark.asyncio
async def test_import_preview_file_too_large(mock_conn, ss_team_user):
    """Import file quá 5000 dòng -> 400"""
    with patch("api.routers.import_export.file_parser") as mock_file_parser:
        # UnsupportedFileFormatError CŨNG phải gán thật dù test này
        # không raise nó, vì router có except UnsupportedFileFormatError
        # đứng TRƯỚC except FileTooLargeError — Python cần cả 2 là
        # class Exception hợp lệ để đánh giá tuần tự except, không chỉ
        # class đang thật sự raise (xem test_get_import_preview_not_found
        # để hiểu rõ cơ chế này).
        mock_file_parser.UnsupportedFileFormatError = UnsupportedFileFormatError
        mock_file_parser.FileTooLargeError = FileTooLargeError
        mock_file_parser.parse_file.side_effect = FileTooLargeError(row_count=5001)

        from api.routers.import_export import import_preview

        mock_file = UploadFile(filename="test.csv", file=io.BytesIO(b"test"))

        with pytest.raises(HTTPException) as exc_info:
            await import_preview(
                entity_type="job",
                file=mock_file,
                conn=mock_conn,
                user=ss_team_user,
            )
        assert exc_info.value.status_code == 400
        assert "5000 rows" in exc_info.value.detail


@pytest.mark.asyncio
async def test_import_preview_validation_errors(mock_conn, ss_team_user):
    """Import file có dòng không hợp lệ -> 422 với chi tiết lỗi"""
    with patch("api.routers.import_export.file_parser") as mock_file_parser:
        with patch(
            "api.routers.import_export.validate_dataframe"
        ) as mock_validate:
            import pandas as pd

            mock_file_parser.parse_file.return_value = pd.DataFrame(
                {"contact_name": ["Test"]}
            )

            # Mock validation result có lỗi
            mock_validation_result = MagicMock()
            mock_validation_result.is_valid = False
            mock_error = MagicMock()
            mock_error.row_number = 1
            mock_error.field_name = "contact_name"
            mock_error.rule = "required"
            mock_error.message = "Thiếu tên contact"
            mock_validation_result.errors = [mock_error]
            mock_validate.return_value = mock_validation_result

            from api.routers.import_export import import_preview

            mock_file = UploadFile(filename="test.csv", file=io.BytesIO(b"test"))

            with pytest.raises(HTTPException) as exc_info:
                await import_preview(
                    entity_type="contact",
                    file=mock_file,
                    conn=mock_conn,
                    user=ss_team_user,
                )
            assert exc_info.value.status_code == 422
            assert "errors" in exc_info.value.detail


@pytest.mark.asyncio
async def test_import_preview_success(mock_conn, ss_team_user, test_preview_id):
    """Import preview thành công"""
    with patch("api.routers.import_export.file_parser") as mock_file_parser:
        with patch(
            "api.routers.import_export.validate_dataframe"
        ) as mock_validate:
            with patch(
                "api.routers.import_export.preview_manager"
            ) as mock_preview_mgr:
                import pandas as pd

                mock_file_parser.parse_file.return_value = pd.DataFrame(
                    {"contact_name": ["Test 1", "Test 2"]}
                )

                mock_validation_result = MagicMock()
                mock_validation_result.is_valid = True
                mock_validation_result.errors = []
                mock_validate.return_value = mock_validation_result

                preview_data = {
                    "summary": {"total": 2, "ready": 2, "needs_resolution": 0},
                    "rows": [
                        {"row_index": 0, "status": "ready_to_create", "data": {}},
                        {"row_index": 1, "status": "ready_to_create", "data": {}},
                    ],
                }
                mock_preview_mgr.build_preview.return_value = preview_data
                mock_preview_mgr.save_preview.return_value = test_preview_id

                from api.routers.import_export import import_preview

                mock_file = UploadFile(filename="test.csv", file=io.BytesIO(b"test"))

                result = await import_preview(
                    entity_type="contact",
                    file=mock_file,
                    conn=mock_conn,
                    user=ss_team_user,
                )
                assert result.preview_id == test_preview_id
                assert result.entity_type == "contact"
                assert result.summary["total"] == 2
                mock_conn.commit.assert_called_once()


# ------------------------------------------------------------------
# GET /import/{entity_type}/preview/{preview_id}
# ------------------------------------------------------------------


def test_get_import_preview_invalid_preview_id(mock_conn, ss_team_user):
    """GET preview với preview_id không đúng UUID -> 400"""
    # KHÔNG patch _load_owned_preview() ở đây (khác 3 test bên dưới) —
    # chính hàm này mới là nơi thật sự gọi db_module.is_valid_uuid() và
    # raise 400 (xem source). Patch cả 2 như bản cũ khiến
    # _load_owned_preview bị thay bằng MagicMock rỗng, patch db_module
    # trở nên vô nghĩa vì code thật không bao giờ chạy tới -> test
    # không bao giờ raise được HTTPException.
    with patch("api.routers.import_export.db_module") as mock_db:
        mock_db.is_valid_uuid.return_value = False

        from api.routers.import_export import get_import_preview

        with pytest.raises(HTTPException) as exc_info:
            get_import_preview(
                entity_type="contact",
                preview_id="not-a-uuid",
                conn=mock_conn,
                user=ss_team_user,
            )
        assert exc_info.value.status_code == 400


def test_get_import_preview_not_found(mock_conn, ss_team_user, test_preview_id):
    """GET preview không tồn tại -> 404"""
    with patch(
        "api.routers.import_export.preview_manager"
    ) as mock_preview_mgr:
        with patch("api.routers.import_export.db_module") as mock_db:
            mock_db.is_valid_uuid.return_value = True
            # xem giải thích ở test_import_preview_unsupported_file_format
            # — CẢ 3 class phải gán thật (không chỉ class đang raise):
            # _load_owned_preview() có 3 except clause nối tiếp
            # (NotFound/Ownership/Expired), Python cần match TUẦN TỰ
            # từng except — muốn match được except đầu tiên (đúng
            # trường hợp raise ở đây) thì except đó phải là class hợp
            # lệ, nhưng 2 except SAU cũng phải là class hợp lệ để
            # Python còn tiếp tục đánh giá nếu except đầu KHÔNG khớp
            # (test_get_import_preview_ownership_error/_expired bên
            # dưới minh hoạ đúng trường hợp này).
            mock_preview_mgr.PreviewNotFoundError = PreviewNotFoundError
            mock_preview_mgr.PreviewOwnershipError = PreviewOwnershipError
            mock_preview_mgr.PreviewExpiredError = PreviewExpiredError
            mock_preview_mgr.get_preview.side_effect = PreviewNotFoundError()

            from api.routers.import_export import get_import_preview

            with pytest.raises(HTTPException) as exc_info:
                get_import_preview(
                    entity_type="contact",
                    preview_id=test_preview_id,
                    conn=mock_conn,
                    user=ss_team_user,
                )
            assert exc_info.value.status_code == 404


def test_get_import_preview_ownership_error(
    mock_conn, ss_team_user, test_preview_id
):
    """GET preview của người khác -> 404 (cố ý giống not found)"""
    with patch(
        "api.routers.import_export.preview_manager"
    ) as mock_preview_mgr:
        with patch("api.routers.import_export.db_module") as mock_db:
            mock_db.is_valid_uuid.return_value = True
            # xem giải thích ở test_get_import_preview_not_found — cả 3
            # class đều phải thật vì except NotFound (đứng TRƯỚC
            # Ownership trong router) vẫn được Python đánh giá dù không
            # khớp.
            mock_preview_mgr.PreviewNotFoundError = PreviewNotFoundError
            mock_preview_mgr.PreviewOwnershipError = PreviewOwnershipError
            mock_preview_mgr.PreviewExpiredError = PreviewExpiredError
            mock_preview_mgr.get_preview.side_effect = PreviewOwnershipError()

            from api.routers.import_export import get_import_preview

            with pytest.raises(HTTPException) as exc_info:
                get_import_preview(
                    entity_type="contact",
                    preview_id=test_preview_id,
                    conn=mock_conn,
                    user=ss_team_user,
                )
            assert exc_info.value.status_code == 404


def test_get_import_preview_expired(mock_conn, ss_team_user, test_preview_id):
    """GET preview đã hết hạn -> 410"""
    with patch(
        "api.routers.import_export.preview_manager"
    ) as mock_preview_mgr:
        with patch("api.routers.import_export.db_module") as mock_db:
            mock_db.is_valid_uuid.return_value = True
            # xem giải thích ở test_get_import_preview_not_found — cả 3
            # class đều phải thật vì 2 except NotFound/Ownership (đứng
            # TRƯỚC Expired trong router) vẫn được Python đánh giá dù
            # không khớp.
            mock_preview_mgr.PreviewNotFoundError = PreviewNotFoundError
            mock_preview_mgr.PreviewOwnershipError = PreviewOwnershipError
            mock_preview_mgr.PreviewExpiredError = PreviewExpiredError
            mock_preview_mgr.get_preview.side_effect = PreviewExpiredError()

            from api.routers.import_export import get_import_preview

            with pytest.raises(HTTPException) as exc_info:
                get_import_preview(
                    entity_type="contact",
                    preview_id=test_preview_id,
                    conn=mock_conn,
                    user=ss_team_user,
                )
            assert exc_info.value.status_code == 410


def test_get_import_preview_success(mock_conn, ss_team_user, test_preview_id):
    """GET preview thành công"""
    preview_record = make_preview_record(test_preview_id, ss_team_user["sub"])

    with patch("api.routers.import_export._load_owned_preview") as mock_load:
        mock_load.return_value = preview_record

        from api.routers.import_export import get_import_preview

        result = get_import_preview(
            entity_type="contact",
            preview_id=test_preview_id,
            conn=mock_conn,
            user=ss_team_user,
        )
        assert result.preview_id == str(preview_record["preview_id"])
        assert result.entity_type == preview_record["entity_type"]


# ------------------------------------------------------------------
# POST /import/{entity_type}/confirm
# ------------------------------------------------------------------


def test_import_confirm_entity_type_mismatch(
    mock_conn, ss_team_user, test_preview_id
):
    """Confirm với entity_type không khớp preview -> 400"""
    preview_record = make_preview_record(
        test_preview_id, ss_team_user["sub"], entity_type="job"
    )

    with patch("api.routers.import_export._load_owned_preview") as mock_load:
        mock_load.return_value = preview_record

        from api.routers.import_export import import_confirm
        from api.schemas import ImportConfirmRequest

        with pytest.raises(HTTPException) as exc_info:
            import_confirm(
                entity_type="contact",  # Khác "job"
                payload=ImportConfirmRequest(
                    preview_id=test_preview_id, resolutions={}, note="Test"
                ),
                conn=mock_conn,
                user=ss_team_user,
            )
        assert exc_info.value.status_code == 400
        assert "entity_type" in exc_info.value.detail


def test_import_confirm_row_resolution_error(
    mock_conn, ss_team_user, test_preview_id
):
    """Confirm với resolution không hợp lệ -> 422"""
    preview_record = make_preview_record(test_preview_id, ss_team_user["sub"])

    with patch("api.routers.import_export._load_owned_preview") as mock_load:
        with patch(
            "api.routers.import_export.import_executor"
        ) as mock_executor:
            mock_load.return_value = preview_record
            # xem giải thích ở test_import_preview_unsupported_file_format
            mock_executor.RowResolutionError = RowResolutionError
            mock_executor.execute_import.side_effect = RowResolutionError("Invalid resolution")

            from api.routers.import_export import import_confirm
            from api.schemas import ImportConfirmRequest

            with pytest.raises(HTTPException) as exc_info:
                import_confirm(
                    entity_type="contact",
                    payload=ImportConfirmRequest(
                        preview_id=test_preview_id, resolutions={}, note="Test"
                    ),
                    conn=mock_conn,
                    user=ss_team_user,
                )
            assert exc_info.value.status_code == 422
            mock_conn.rollback.assert_called_once()


def test_import_confirm_database_error(mock_conn, ss_team_user, test_preview_id):
    """Confirm gặp lỗi DB -> 500, rollback"""
    preview_record = make_preview_record(test_preview_id, ss_team_user["sub"])

    with patch("api.routers.import_export._load_owned_preview") as mock_load:
        with patch(
            "api.routers.import_export.import_executor"
        ) as mock_executor:
            mock_load.return_value = preview_record
            # RowResolutionError vẫn phải gán class THẬT dù test này
            # KHÔNG raise nó — router có `except
            # import_executor.RowResolutionError as exc:` bọc quanh
            # execute_import(), Python cần class hợp lệ để so khớp
            # except clause TRƯỚC KHI biết exception nào đang bay
            # (không khớp thì mới rơi xuống except Exception chung bên
            # dưới) — mock_executor.RowResolutionError mặc định không
            # phải class Exception nên khớp except sẽ lỗi TypeError.
            mock_executor.RowResolutionError = RowResolutionError
            mock_executor.execute_import.side_effect = Exception("DB error")

            from api.routers.import_export import import_confirm
            from api.schemas import ImportConfirmRequest

            with pytest.raises(HTTPException) as exc_info:
                import_confirm(
                    entity_type="contact",
                    payload=ImportConfirmRequest(
                        preview_id=test_preview_id, resolutions={}, note="Test"
                    ),
                    conn=mock_conn,
                    user=ss_team_user,
                )
            assert exc_info.value.status_code == 500
            assert "database error" in exc_info.value.detail.lower()
            mock_conn.rollback.assert_called_once()


def test_import_confirm_success(mock_conn, ss_team_user, test_preview_id):
    """Import confirm thành công"""
    preview_record = make_preview_record(test_preview_id, ss_team_user["sub"])

    with patch("api.routers.import_export._load_owned_preview") as mock_load:
        with patch(
            "api.routers.import_export.import_executor"
        ) as mock_executor:
            with patch(
                "api.routers.import_export.preview_manager"
            ) as mock_preview_mgr:
                with patch("api.routers.import_export.db_module") as mock_db:
                    mock_load.return_value = preview_record

                    mock_summary = MagicMock()
                    mock_summary.created = 2
                    mock_summary.updated = 0
                    mock_summary.skipped = 0
                    mock_executor.execute_import.return_value = mock_summary

                    from api.routers.import_export import import_confirm
                    from api.schemas import ImportConfirmRequest

                    result = import_confirm(
                        entity_type="contact",
                        payload=ImportConfirmRequest(
                            preview_id=test_preview_id,
                            resolutions={},
                            note="Import batch 1",
                        ),
                        conn=mock_conn,
                        user=ss_team_user,
                    )
                    assert result.created == 2
                    assert result.updated == 0
                    assert result.skipped == 0
                    mock_db.log_action.assert_called_once()
                    mock_preview_mgr.delete_preview.assert_called_once_with(
                        mock_conn, test_preview_id
                    )
                    mock_conn.commit.assert_called_once()


# ------------------------------------------------------------------
# GET /import/{entity_type}/preview/{preview_id}/company-suggestions
# ------------------------------------------------------------------


def test_get_company_suggestions_row_not_found(
    mock_conn, ss_team_user, test_preview_id
):
    """GET suggestions cho row_index không tồn tại -> 404"""
    preview_record = make_preview_record(test_preview_id, ss_team_user["sub"])

    with patch("api.routers.import_export._load_owned_preview") as mock_load:
        mock_load.return_value = preview_record

        from api.routers.import_export import get_company_suggestions

        with pytest.raises(HTTPException) as exc_info:
            get_company_suggestions(
                entity_type="contact",
                preview_id=test_preview_id,
                row_index=999,  # Không tồn tại
                conn=mock_conn,
                user=ss_team_user,
            )
        assert exc_info.value.status_code == 404


def test_get_company_suggestions_success(
    mock_conn, ss_team_user, test_preview_id
):
    """GET company suggestions thành công"""
    preview_record = make_preview_record(test_preview_id, ss_team_user["sub"])

    with patch("api.routers.import_export._load_owned_preview") as mock_load:
        with patch(
            "api.routers.import_export.company_resolver"
        ) as mock_resolver:
            mock_load.return_value = preview_record
            mock_resolver.suggest_companies.return_value = []

            from api.routers.import_export import get_company_suggestions

            result = get_company_suggestions(
                entity_type="contact",
                preview_id=test_preview_id,
                row_index=0,  # Tồn tại trong preview_record
                conn=mock_conn,
                user=ss_team_user,
            )
            assert result.suggestions == []
            mock_resolver.suggest_companies.assert_called_once()
