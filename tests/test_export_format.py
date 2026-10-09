"""
D3 đợt 1: giờ trong file export (CSV/XLSX) theo GIỜ VIỆT NAM, "YYYY-MM-DD HH:MM:SS".

Trước đây xuất isoformat thô: giờ UTC không kèm nhãn (nhân viên ở VN đọc nhầm thành
giờ VN), và sau D3 sẽ thêm hậu tố "+00:00". created_at/updated_at hiện là TIMESTAMP
naive lưu UTC, sau D3 là TIMESTAMPTZ; hàm format phải đúng với cả hai nên viết test
cho cả hai dạng đầu vào.
"""
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from scrapjd.api.services import export_query
from scrapjd.api.services.export_query import _format_export_value, _format_rows


def test_naive_datetime_is_treated_as_utc_and_shown_in_vn_time():
    # Cột TIMESTAMP hiện tại: 12:34:56 (UTC) -> 19:34:56 giờ VN.
    assert _format_export_value(datetime(2026, 10, 7, 12, 34, 56, 123456)) == "2026-10-07 19:34:56"


def test_aware_utc_datetime_gives_the_same_text_as_naive_utc():
    # Cột TIMESTAMPTZ sau D3: cùng thời điểm, cùng chuỗi, nên đổi kiểu cột không đổi file xuất.
    naive = datetime(2026, 10, 7, 12, 34, 56)
    aware = naive.replace(tzinfo=timezone.utc)
    assert _format_export_value(aware) == _format_export_value(naive)


def test_aware_datetime_in_another_offset_is_converted_not_relabelled():
    ict = timezone(timedelta(hours=7))
    est = timezone(timedelta(hours=-5))
    assert _format_export_value(datetime(2026, 10, 7, 19, 34, 56, tzinfo=ict)) == "2026-10-07 19:34:56"
    assert _format_export_value(datetime(2026, 10, 7, 7, 34, 56, tzinfo=est)) == "2026-10-07 19:34:56"


@pytest.mark.parametrize("utc_hour,expected_day", [(16, "2026-09-30"), (17, "2026-10-01")])
def test_the_day_rolls_over_at_17_utc(utc_hour, expected_day):
    # 17:00 UTC = 00:00 giờ VN của ngày kế tiếp.
    got = _format_export_value(datetime(2026, 9, 30, utc_hour, 0, 0))
    assert got.startswith(expected_day)


def test_plain_date_is_unchanged():
    # deadline là DATE: không có giờ để quy đổi.
    assert _format_export_value(date(2026, 10, 7)) == "2026-10-07"


def test_other_values_pass_through():
    assert _format_export_value(15_000_000) == 15_000_000
    assert _format_export_value("Data Analyst") == "Data Analyst"


def test_format_rows_formats_created_and_updated_and_keeps_bool_and_none_rules():
    rows = [{
        "job_id": uuid.UUID("00000000-0000-0000-0000-000000000001"),
        "job_title": "Data Analyst",
        "deadline": date(2026, 11, 1),
        "ss_team_notes": None,
        "created_at": datetime(2026, 10, 7, 12, 0, 0),
        "updated_at": datetime(2026, 10, 7, 17, 0, 0, tzinfo=timezone.utc),
    }]
    out = _format_rows(rows, "job")[0]
    assert out["created_at"] == "2026-10-07 19:00:00"
    assert out["updated_at"] == "2026-10-08 00:00:00"
    assert out["deadline"] == "2026-11-01"
    assert out["ss_team_notes"] is None
    assert out["job_title"] == "Data Analyst"


def test_company_is_active_bool_still_becomes_true_false_text():
    out = _format_rows([{"is_active": True, "created_at": datetime(2026, 1, 1, 0, 0, 0)}], "company")[0]
    assert out["is_active"] == "true"
    assert out["created_at"] == "2026-01-01 07:00:00"


def test_module_uses_a_fixed_plus7_offset():
    # Việt Nam không có giờ mùa hè: offset cố định, không phụ thuộc gói tzdata trên máy chạy.
    assert export_query._VN_TZ.utcoffset(None) == timedelta(hours=7)
