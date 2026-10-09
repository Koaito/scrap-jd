"""
D3 đợt 2: bộ lọc from_date/to_date của export tính theo NGÀY VIỆT NAM, gồm cả hai đầu, bằng khoảng
nửa mở [00:00 VN của from_date, 00:00 VN của ngày sau to_date).

Trước đây: `col >= from_date AND col <= to_date` với from_date/to_date là kiểu date, tức so với 00:00 của
ngày đó theo múi giờ của DB (UTC). Hai lỗi: (1) `<= to_date` loại gần hết ngày cuối; (2) ranh giới ngày là
UTC chứ không phải VN, nên dòng "01:00 sáng 08/10" trong file lại không lọc ra được khi chọn từ 08/10.

Test này không cần Postgres; phần chạy trên DB thật nằm ở tests/test_pg_export_date_filter.py.
"""
from datetime import date, datetime, timedelta, timezone

from scrapjd.api.services.export_query import ExportFilters, _build_where, vn_day_range

_VN = timezone(timedelta(hours=7))


def _where(filters):
    return _build_where(
        filters, status_column="jp.job_status", has_is_active=False,
        company_column="jp.company_id", date_table_alias="jp",
    )


def test_range_is_half_open_and_starts_and_ends_at_midnight_vn():
    start, end = vn_day_range(date(2026, 10, 7), date(2026, 10, 7))
    assert start == datetime(2026, 10, 7, 0, 0, tzinfo=_VN)
    assert end == datetime(2026, 10, 8, 0, 0, tzinfo=_VN)
    # Cùng thời điểm, nói theo UTC: từ 17:00 UTC ngày 06 đến 17:00 UTC ngày 07.
    assert start == datetime(2026, 10, 6, 17, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 10, 7, 17, 0, tzinfo=timezone.utc)


def test_a_range_covers_whole_last_day_and_spans_several_days():
    start, end = vn_day_range(date(2026, 10, 1), date(2026, 10, 7))
    assert end - start == timedelta(days=7)


def test_each_end_is_optional():
    assert vn_day_range(None, None) == (None, None)
    start, end = vn_day_range(date(2026, 10, 7), None)
    assert start is not None and end is None
    start, end = vn_day_range(None, date(2026, 10, 7))
    assert start is None and end == datetime(2026, 10, 8, 0, 0, tzinfo=_VN)


def test_month_and_year_boundaries():
    _, end = vn_day_range(None, date(2026, 12, 31))
    assert end == datetime(2027, 1, 1, 0, 0, tzinfo=_VN)
    _, end = vn_day_range(None, date(2028, 2, 28))      # 2028 nhuận
    assert end == datetime(2028, 2, 29, 0, 0, tzinfo=_VN)


def test_results_are_timezone_aware_so_comparison_does_not_depend_on_session_timezone():
    start, end = vn_day_range(date(2026, 10, 7), date(2026, 10, 7))
    assert start.utcoffset() == timedelta(hours=7)
    assert end.utcoffset() == timedelta(hours=7)


def test_extreme_dates_do_not_raise():
    # to_date = 9999-12-31 không có "ngày sau" để làm đầu mút: coi là không giới hạn trên.
    start, end = vn_day_range(date.min, date.max)
    assert start is not None and end is None
    # Ngày cuối cùng còn có ngày sau thì vẫn tính bình thường.
    _, end = vn_day_range(None, date.max - timedelta(days=1))
    assert end is not None


def test_where_clause_uses_half_open_bounds_on_the_bare_column():
    where, params = _where(ExportFilters(from_date=date(2026, 10, 7), to_date=date(2026, 10, 7)))
    assert where == "WHERE jp.created_at >= %s AND jp.created_at < %s"
    assert "<=" not in where
    assert params == [datetime(2026, 10, 7, 0, 0, tzinfo=_VN), datetime(2026, 10, 8, 0, 0, tzinfo=_VN)]


def test_where_clause_follows_date_field_and_keeps_param_order_with_other_filters():
    where, params = _where(ExportFilters(
        status="OPEN", company_id="c-1", date_field="updated_at",
        from_date=date(2026, 10, 7), to_date=date(2026, 10, 8),
    ))
    assert where == ("WHERE jp.job_status = %s AND jp.company_id = %s "
                     "AND jp.updated_at >= %s AND jp.updated_at < %s")
    assert params[:2] == ["OPEN", "c-1"]
    assert params[2] == datetime(2026, 10, 7, 0, 0, tzinfo=_VN)
    assert params[3] == datetime(2026, 10, 9, 0, 0, tzinfo=_VN)


def test_no_date_filter_adds_no_clause():
    where, params = _where(ExportFilters())
    assert where == "" and params == []
