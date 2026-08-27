"""
Pytest fixtures cho API tests — mock DB connection, auth user, và
TestClient FastAPI.
"""
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def mock_conn():
    """Mock psycopg2 connection — không kết nối DB thật, chỉ test logic
    router/validation. Integration test thật với DB cần môi trường
    riêng (Docker test container hoặc CI/CD pipeline)."""
    conn = MagicMock()
    conn.commit = MagicMock()
    conn.rollback = MagicMock()
    return conn


@pytest.fixture
def ss_team_user():
    """User có role ss_team — dùng cho các route yêu cầu require_role("ss_team")"""
    return {
        "sub": str(uuid.uuid4()),
        "email": "staff@mindx.edu.vn",
        "role": "ss_team",
    }


@pytest.fixture
def admin_user():
    """User có role admin"""
    return {
        "sub": str(uuid.uuid4()),
        "email": "admin@mindx.edu.vn",
        "role": "admin",
    }


@pytest.fixture
def regular_user():
    """User có role user (học viên) — không có quyền truy cập contacts/import"""
    return {
        "sub": str(uuid.uuid4()),
        "email": "student@mindx.edu.vn",
        "role": "user",
    }


@pytest.fixture
def test_company_id():
    """UUID công ty giả để test"""
    return str(uuid.uuid4())


@pytest.fixture
def test_contact_id():
    """UUID contact giả để test"""
    return str(uuid.uuid4())


@pytest.fixture
def test_preview_id():
    """UUID import preview giả để test"""
    return str(uuid.uuid4())


def make_contact_record(
    contact_id: str,
    company_id: str,
    contact_name: str = "Nguyễn Văn A",
    is_active: bool = True,
    **overrides,
) -> dict[str, Any]:
    """Helper tạo dict giả lập row company_contacts từ DB"""
    base = {
        "contact_id": uuid.UUID(contact_id) if isinstance(contact_id, str) else contact_id,
        "company_id": uuid.UUID(company_id) if isinstance(company_id, str) else company_id,
        "contact_name": contact_name,
        "job_title": "HR Manager",
        "work_email": "hr@company.com",
        "social_link": None,
        "phone_number": None,
        "found_source": None,
        "contact_status": "UNCONTACTED",
        "last_contacted_date": None,
        "assigned_ss_user": None,
        "is_active": is_active,
        "created_by": str(uuid.uuid4()),
        "updated_by": None,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    base.update(overrides)
    return base


def make_company_record(company_id: str, company_name: str = "Công ty Test") -> dict[str, Any]:
    """Helper tạo dict giả lập row companies từ DB"""
    return {
        "company_id": uuid.UUID(company_id) if isinstance(company_id, str) else company_id,
        "company_name": company_name,
        "tax_id": "0123456789",
        "company_size": None,
        "industry": None,
        "province_name": None,
        "website": None,
        "is_active": True,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }


def make_email_template_record(
    template_id: str,
    title: str = "Giới thiệu MindX",
    **overrides,
) -> dict[str, Any]:
    """Helper tạo dict giả lập row email_templates từ DB — dùng cho
    tests/test_api_email_templates.py (thêm 08/2026, xem
    sql/migration_add_email_templates.sql)."""
    base = {
        "template_id": uuid.UUID(template_id) if isinstance(template_id, str) else template_id,
        "title": title,
        "description": "Mở lời làm quen lần đầu.",
        "body": "Tiêu đề: ...\n\n{{LOI_CHAO}}\n\n...",
        "recommended_for": ["UNCONTACTED"],
        "display_order": 1,
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "created_by": str(uuid.uuid4()),
        "updated_by": None,
    }
    base.update(overrides)
    return base


def make_preview_record(
    preview_id: str, user_id: str, entity_type: str = "contact"
) -> dict[str, Any]:
    """Helper tạo dict giả lập row import_previews từ DB"""
    return {
        "preview_id": uuid.UUID(preview_id) if isinstance(preview_id, str) else preview_id,
        "user_id": uuid.UUID(user_id) if isinstance(user_id, str) else user_id,
        "entity_type": entity_type,
        "preview_data": {
            "summary": {"total": 2, "ready": 1, "needs_resolution": 1},
            "rows": [
                {
                    "row_index": 0,
                    "status": "ready_to_create",
                    "data": {"contact_name": "Test 1", "company_name": "Company A"},
                },
                {
                    "row_index": 1,
                    "status": "pending_company_resolution",
                    "data": {"contact_name": "Test 2", "company_name": "Company B"},
                },
            ],
        },
        "created_at": datetime.now(timezone.utc),
        "expires_at": datetime.now(timezone.utc),
    }


# Export helpers để các test file khác import dễ dàng
__all__ = [
    "mock_conn",
    "ss_team_user",
    "admin_user",
    "regular_user",
    "test_company_id",
    "test_contact_id",
    "test_preview_id",
    "make_contact_record",
    "make_company_record",
    "make_preview_record",
]
