"""
`suggest_keep` của nhóm job nghi trùng (db.get_job_data_health) trên POSTGRES THẬT: deadline là kiểu DATE thật,
job_id là UUID thật, nên bắt được lỗi kiểu mà test giả (tests/test_db_data_health.py) không thấy.

Cách chạy như tests/test_pg_migrations.py: đặt TEST_DATABASE_URL (tên database chứa "test"), test tự tạo/xoá
database tạm; không đặt thì cả file được bỏ qua.
"""
import datetime
import os
import pathlib
import uuid
from urllib.parse import urlparse

import psycopg2
import pytest
from psycopg2 import sql as pgsql

from scrapjd import db
from scrapjd.api.schemas.jobs import JobDataHealth

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="Không đặt TEST_DATABASE_URL — bỏ qua test cần Postgres thật",
)

_SCHEMA = str(pathlib.Path(__file__).resolve().parent.parent / "sql" / "schema.sql")


@pytest.fixture(scope="module")
def database_url():
    parsed = urlparse(TEST_DATABASE_URL)
    base = parsed.path.lstrip("/")
    if "test" not in base.lower():
        pytest.fail(f"Từ chối chạy: database '{base}' không chứa 'test' trong tên.")
    name = f"{base}_sk_{uuid.uuid4().hex[:8]}"
    admin = psycopg2.connect(TEST_DATABASE_URL)
    admin.autocommit = True
    try:
        with admin.cursor() as cur:
            cur.execute(pgsql.SQL("CREATE DATABASE {}").format(pgsql.Identifier(name)))
        url = parsed._replace(path=f"/{name}").geturl()
        setup = psycopg2.connect(url)
        try:
            db.apply_schema(setup, _SCHEMA)
        finally:
            setup.close()
        yield url
    finally:
        with admin.cursor() as cur:
            cur.execute(pgsql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(pgsql.Identifier(name)))
        admin.close()


@pytest.fixture
def conn(database_url):
    c = psycopg2.connect(database_url)
    c.autocommit = False
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def _company(cur, name):
    cid = str(uuid.uuid4())
    cur.execute("INSERT INTO companies (company_id, company_name) VALUES (%s, %s)", (cid, name))
    return cid


def _job(cur, company_id, title, deadline, status="OPEN"):
    cur.execute(
        "INSERT INTO job_postings (company_id, job_title, deadline, job_status) VALUES (%s, %s, %s, %s) "
        "RETURNING job_id",
        (company_id, title, deadline, status),
    )
    return str(cur.fetchone()[0])


def test_suggest_keep_on_real_postgres(conn):
    far, near = datetime.date(2099, 12, 20), datetime.date(2099, 12, 1)
    with conn.cursor() as cur:
        c1 = _company(cur, "Có căn cứ")
        keep = _job(cur, c1, "Backend Dev", far)
        _job(cur, c1, "backend dev ", near)  # cùng vị trí (không phân biệt hoa/thường, khoảng trắng thừa)
        _job(cur, c1, "Backend Dev", None)  # không có hạn: ở trong nhóm nhưng không tham gia so sánh
        _job(cur, c1, "Backend Dev", datetime.date(2099, 12, 25), status="CLOSED")  # đã đóng: không vào nhóm
        c2 = _company(cur, "Hạn trùng hết")
        _job(cur, c2, "QA", far)
        _job(cur, c2, "QA", far)
        c3 = _company(cur, "Không ai có hạn")
        _job(cur, c3, "PM", None)
        _job(cur, c3, "PM", None)
    conn.commit()

    result = db.get_job_data_health(conn)
    by_company = {g["company"]: g for g in result["duplicate_job_groups"]}
    assert by_company["Có căn cứ"]["suggest_keep"] == keep
    assert len(by_company["Có căn cứ"]["jobs"]) == 3
    assert by_company["Hạn trùng hết"]["suggest_keep"] is None
    assert by_company["Không ai có hạn"]["suggest_keep"] is None
    # đúng hình dạng mà API trả ra
    parsed = JobDataHealth(**result)
    assert {g.company: g.suggest_keep for g in parsed.duplicate_job_groups}["Có căn cứ"] == keep
