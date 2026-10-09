"""
Test field_stats.EmptyFieldCounter (thống kê tỷ lệ trường rỗng) — thuần
Python, KHÔNG cần database, KHÔNG cần internet.
"""

import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scrapjd.field_stats import (
    DETAIL_FIELDS,
    LISTING_FIELDS,
    WARN_MIN_SAMPLES,
    EmptyFieldCounter,
    is_empty,
)
from scrapjd.models import RawJobRecord


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, True),
        ("", True),
        ("   \n\t", True),
        ([], True),
        ((), True),
        ({}, True),
        ("abc", False),
        (["a"], False),
        ({"k": 1}, False),
        (0, False),       # số 0 KHÔNG tính là rỗng
        (False, False),
    ],
)
def test_is_empty(value, expected):
    assert is_empty(value) is expected


def _raw(**overrides):
    base = dict(job_title="Data Analyst", company_name="ACME",
                source_url="https://x/1", source_name="X")
    base.update(overrides)
    return RawJobRecord(**base)


def test_summary_counts_listing_fields_from_raw_records():
    c = EmptyFieldCounter()
    c.record_listing(_raw(salary_text="10 triệu", province_text="Hà Nội"))
    c.record_listing(_raw(salary_text="", province_text="Hà Nội"))
    c.record_listing(_raw(salary_text="  ", province_text=""))
    c.record_listing(_raw(salary_text="Thoả thuận", raw_tags=["sql"]))

    listing = c.summary()["listing"]
    assert listing["total"] == 4
    f = listing["fields"]
    assert f["salary_text"] == {"empty": 2, "rate": 0.5}
    assert f["province_text"] == {"empty": 2, "rate": 0.5}
    assert f["job_title"] == {"empty": 0, "rate": 0.0}
    assert f["raw_tags"] == {"empty": 3, "rate": 0.75}
    # đo đủ các field đã khai báo, không thiếu field nào
    assert set(f) == set(LISTING_FIELDS)


def test_summary_counts_detail_fields_and_ignores_failed_fetch():
    c = EmptyFieldCounter()
    c.record_detail({"work_type": "Toàn thời gian", "deadline_text": "",
                     "job_description": "mô tả", "requirements": "",
                     "perks": "", "required_skills": []})
    c.record_detail(None)  # fetch thất bại: không tính vào mẫu số
    c.record_detail({"work_type": "", "deadline_text": "05/09/2026",
                     "job_description": "mô tả", "requirements": "yêu cầu",
                     "perks": "", "required_skills": ["sql"]})

    detail = c.summary()["detail"]
    assert detail["total"] == 2
    f = detail["fields"]
    assert f["work_type"]["empty"] == 1
    assert f["deadline_text"]["empty"] == 1
    assert f["job_description"]["empty"] == 0
    assert f["perks"] == {"empty": 2, "rate": 1.0}
    assert set(f) == set(DETAIL_FIELDS)


def test_summary_is_empty_when_nothing_recorded():
    c = EmptyFieldCounter()
    c.record_detail(None)
    assert c.summary() == {}


def test_summary_is_json_serializable():
    import json

    c = EmptyFieldCounter()
    c.record_listing(_raw())
    c.record_detail({"work_type": "x"})
    json.dumps(c.summary())  # không raise


def test_log_summary_warns_only_with_enough_samples(caplog):
    c = EmptyFieldCounter()
    for _ in range(WARN_MIN_SAMPLES - 1):  # chưa đủ mẫu -> không WARNING
        c.record_listing(_raw(salary_text=""))
    with caplog.at_level(logging.INFO, logger="scrapjd.field_stats"):
        c.log_summary()
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("Trường rỗng (listing" in r.getMessage() for r in caplog.records)

    caplog.clear()
    c.record_listing(_raw(salary_text=""))  # đủ mẫu, salary_text rỗng 100%
    with caplog.at_level(logging.INFO, logger="scrapjd.field_stats"):
        c.log_summary()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("salary_text" in w for w in warnings)
    assert not any("job_title" in w for w in warnings)  # field đủ dữ liệu không bị cảnh báo
