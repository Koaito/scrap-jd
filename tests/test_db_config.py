"""Kiểm `build_db_config` (scrapjd/config.py): DATABASE_URL ưu tiên, nhóm PG* làm dự phòng."""

import pytest

from scrapjd.config import build_db_config


def test_pg_vars_when_no_database_url():
    cfg = build_db_config(
        {
            "PGHOST": "h",
            "PGPORT": "6543",
            "PGDATABASE": "d",
            "PGUSER": "u",
            "PGPASSWORD": "p",
            "PGSSLMODE": "disable",
        }
    )
    assert cfg == {
        "host": "h",
        "port": "6543",
        "dbname": "d",
        "user": "u",
        "password": "p",
        "sslmode": "disable",
    }


def test_pg_defaults_unchanged():
    cfg = build_db_config({})
    assert cfg["host"] == "localhost"
    assert cfg["port"] == "5432"
    assert cfg["user"] == "postgres"
    assert cfg["password"] == ""
    assert cfg["sslmode"] == "require"


def test_database_url_parsed():
    cfg = build_db_config({"DATABASE_URL": "postgresql://u:p@db.example.com:6543/mydb?sslmode=verify-full"})
    assert cfg == {
        "user": "u",
        "password": "p",
        "host": "db.example.com",
        "port": "6543",
        "dbname": "mydb",
        "sslmode": "verify-full",
    }


def test_database_url_wins_over_pg_vars_without_mixing():
    cfg = build_db_config(
        {
            "DATABASE_URL": "postgresql://u:p@urlhost/urldb",
            "PGHOST": "oldhost",
            "PGPORT": "1111",
            "PGUSER": "olduser",
            "PGPASSWORD": "oldpass",
            "PGSSLMODE": "disable",
        }
    )
    assert cfg["host"] == "urlhost"
    assert cfg["dbname"] == "urldb"
    assert cfg["user"] == "u"
    assert cfg["password"] == "p"
    # PG* không lọt vào: port không ghi trong URL thì để libpq dùng mặc định, không lấy PGPORT.
    assert "port" not in cfg
    # sslmode không ghi trong URL thì mặc định require, không lấy PGSSLMODE=disable.
    assert cfg["sslmode"] == "require"


def test_database_url_percent_encoded_password():
    cfg = build_db_config({"DATABASE_URL": "postgresql://u%40x:p%40ss%2Fw%3Ard@h/db"})
    assert cfg["user"] == "u@x"
    assert cfg["password"] == "p@ss/w:rd"


def test_postgres_scheme_alias_accepted():
    assert build_db_config({"DATABASE_URL": "postgres://u:p@h/db"})["host"] == "h"


@pytest.mark.parametrize("empty", ["", "   "])
def test_blank_database_url_falls_back_to_pg_vars(empty):
    cfg = build_db_config({"DATABASE_URL": empty, "PGHOST": "h"})
    assert cfg["host"] == "h"


def test_invalid_database_url_error_does_not_leak_password():
    with pytest.raises(ValueError) as exc:
        build_db_config({"DATABASE_URL": "sekret-pass tên=sai ==="})
    assert "sekret-pass" not in str(exc.value)
    assert exc.value.__cause__ is None
