"""
Tín hiệu thô của level (job_postings.level_signals, 10/2026) — phần THUẦN, không DB.
Phần chạy trên Postgres thật nằm ở tests/test_pg_job_level_signals.py.
"""
import pytest

from scrapjd import normalize
from db.job_levels import _check_level_signals, _derived_level_assignments

SIGNALS = {"experience_text": "3 năm", "level_hint": ""}


# ------------------------------------------------------------ normalize
def test_build_level_signals_has_exactly_the_declared_keys():
    assert tuple(normalize.build_level_signals("3 năm", "Manager")) == normalize.LEVEL_SIGNAL_KEYS


def test_build_level_signals_keeps_strings_untouched_and_turns_none_into_empty():
    assert normalize.build_level_signals("  3 năm ", " Senior") == {
        "experience_text": "  3 năm ", "level_hint": " Senior"}
    assert normalize.build_level_signals(None, None) == {"experience_text": "", "level_hint": ""}
    assert normalize.build_level_signals() == {"experience_text": "", "level_hint": ""}


@pytest.mark.parametrize("title,experience,hint", [
    ("Senior Data Engineer", "1 năm", ""),
    ("Data Analyst", "3 năm", ""),
    ("Data Analyst", "Trên 5 năm", ""),
    ("Data Analyst", "Dưới 1 năm", ""),
    ("Data Analyst", "Không yêu cầu", ""),
    ("Data Analyst", "", "Manager"),
    ("Data Analyst", "", ""),
    ("Data Engineer (Senior/Leader)", "", ""),
    ("Junior, Senior Developer", "2 năm", "Lead"),
    ("Data Analyst", "  3 năm ", " Senior"),   # khoảng trắng thừa: phải ra đúng như lúc crawl
])
def test_signals_round_trip_gives_same_decision_as_crawl_time(title, experience, hint):
    """Lưu rồi tính lại từ tín hiệu phải ra đúng quyết định derive_level lúc crawl."""
    at_crawl = normalize.derive_level(experience, title, hint)
    stored = normalize.build_level_signals(experience, hint)
    assert normalize.derive_level_from_signals(title, stored) == at_crawl


def test_derive_level_from_signals_without_signals_only_trusts_the_title():
    assert normalize.derive_level_from_signals("Senior Data Engineer", None) == \
        normalize.LevelDecision("Senior", normalize.LEVEL_SOURCE_TITLE)
    # Tiêu đề không có từ khoá + không có tín hiệu: rơi về 'default' (nơi gọi phải tự lọc).
    assert normalize.derive_level_from_signals("Data Analyst", None).source == normalize.LEVEL_SOURCE_DEFAULT
    assert normalize.derive_level_from_signals("Data Analyst", {}).source == normalize.LEVEL_SOURCE_DEFAULT


# ------------------------------------------------------------ db.job_levels
def test_check_level_signals_accepts_none_and_valid_dict():
    assert _check_level_signals("years", None) is None
    assert _check_level_signals(None, None) is None
    assert _check_level_signals("manual", None) is None
    assert _check_level_signals("years", SIGNALS) == SIGNALS
    assert _check_level_signals("default", {"experience_text": "", "level_hint": ""}) is not None


@pytest.mark.parametrize("source,signals", [
    (None, SIGNALS),                                   # chưa biết nguồn: tín hiệu vô nghĩa
    ("manual", SIGNALS),                               # người đặt: không có tín hiệu máy
    ("years", {"experience_text": "3 năm"}),           # thiếu khoá
    ("years", {**SIGNALS, "extra": "x"}),              # thừa khoá
    ("years", {"experience_text": "3 năm", "level_hint": None}),   # giá trị không phải chuỗi
    ("years", {"experience_text": 3, "level_hint": ""}),
    ("years", ["3 năm"]),                              # không phải dict
])
def test_check_level_signals_rejects_bad_input(source, signals):
    with pytest.raises(ValueError):
        _check_level_signals(source, signals)


def test_derived_level_assignments_writes_signals_and_never_overwrites_manual():
    sets, values = _derived_level_assignments(3, "years", 1, SIGNALS)
    assert len(sets) == 4 and len(values) == 4
    # Cả 4 cột đều đi qua cùng một chốt chặn 'manual'.
    assert all("level_source = 'manual' THEN" in s for s in sets)
    assert any(s.startswith("level_signals") and "::jsonb" in s for s in sets)
    assert values[:3] == [3, "years", 1]
    assert values[3] == '{"experience_text": "3 năm", "level_hint": ""}'


def test_derived_level_assignments_without_signals_writes_null_not_old_value():
    _sets, values = _derived_level_assignments(3, "years", 1)
    assert values[3] is None
    _sets, values = _derived_level_assignments(3, None, None)   # level 'chưa biết'
    assert values == [3, None, None, None]


def test_derived_level_assignments_keeps_non_ascii_readable():
    _sets, values = _derived_level_assignments(
        6, "label", 1, {"experience_text": "Trên 5 năm", "level_hint": ""})
    assert "Trên 5 năm" in values[3]          # ensure_ascii=False, giống parsed_content
