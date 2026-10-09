"""
Luật suy ra job từ listing (scrapjd/db/job_derivation.py, C2 nửa 1/2). Hàm thuần, không cần DB.
Các luật 1 đến 3 do bạn duyệt 08/10/2026; luật closed_reason (khi mọi listing CLOSED) là đề xuất của mình.
"""
from datetime import date, datetime, timedelta, timezone

import pytest

from scrapjd.db.job_derivation import DerivedJob, derive_job_from_listings

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


def L(url, status="OPEN", *, deadline=None, first=0, last=None, reason=None, closed=None):
    """Listing giả: first/last/closed tính bằng số ngày sau T0."""
    day = lambda n: None if n is None else T0 + timedelta(days=n)
    return {
        "source_url": url, "listing_status": status, "deadline": deadline,
        "first_seen_at": day(first), "last_seen_at": day(first if last is None else last),
        "closed_reason": reason, "closed_at": day(closed),
    }


def derive(*listings):
    return derive_job_from_listings(list(listings))


def test_no_listings_cannot_be_derived():
    assert derive_job_from_listings([]) is None


# ------------------------------------------------------------ trạng thái
def test_any_open_listing_makes_job_open():
    d = derive(L("a", "CLOSED", reason="staff", closed=1), L("b", "OPEN"))
    assert (d.job_status, d.closed_reason) == ("OPEN", None)


def test_only_unknown_listings_keep_job_open():
    """Luật 3: chưa có bằng chứng đã chết thì không đóng job."""
    d = derive(L("a", "UNKNOWN"), L("b", "CLOSED", reason="expired_auto", closed=2))
    assert (d.job_status, d.closed_reason) == ("OPEN", None)


def test_all_closed_makes_job_closed_with_reason():
    d = derive(L("a", "CLOSED", reason="expired_auto", closed=1))
    assert (d.job_status, d.closed_reason) == ("CLOSED", "expired_auto")


def test_closed_reason_is_that_of_the_latest_closed_listing():
    d = derive(L("a", "CLOSED", reason="staff", closed=1), L("b", "CLOSED", reason="expired_auto", closed=5))
    assert d.closed_reason == "expired_auto"


def test_closed_reason_tie_prefers_human_decision():
    same = dict(closed=3)
    d = derive(L("a", "CLOSED", reason="expired_auto", **same), L("b", "CLOSED", reason="staff", **same),
               L("c", "CLOSED", reason="unknown", **same))
    assert d.closed_reason == "staff"
    d = derive(L("a", "CLOSED", reason="expired_auto", **same), L("b", "CLOSED", reason="merged", **same),
               L("c", "CLOSED", reason="unknown", **same))
    assert d.closed_reason == "merged"


def test_closed_listing_without_closed_at_is_oldest():
    d = derive(L("a", "CLOSED", reason="staff", closed=None), L("b", "CLOSED", reason="expired_auto", closed=1))
    assert d.closed_reason == "expired_auto"


def test_closed_listing_missing_reason_falls_back_to_unknown():
    assert derive(L("a", "CLOSED", reason=None, closed=1)).closed_reason == "unknown"


# ------------------------------------------------------------ hạn
def test_deadline_is_latest_among_open_listings_ignoring_closed_ones():
    d = derive(L("a", "OPEN", deadline=date(2026, 11, 1)), L("b", "OPEN", deadline=date(2026, 12, 1)),
               L("c", "CLOSED", deadline=date(2027, 6, 1), reason="staff", closed=2))
    assert d.deadline == date(2026, 12, 1)


def test_deadline_without_open_listing_is_latest_among_all():
    d = derive(L("a", "CLOSED", deadline=date(2026, 9, 1), reason="staff", closed=1),
               L("b", "CLOSED", deadline=date(2026, 10, 1), reason="staff", closed=1))
    assert d.deadline == date(2026, 10, 1)


def test_open_listing_without_deadline_does_not_hide_the_other_open_deadline():
    d = derive(L("a", "OPEN", deadline=None), L("b", "OPEN", deadline=date(2026, 12, 1)))
    assert d.deadline == date(2026, 12, 1)


def test_no_deadline_anywhere_is_none():
    assert derive(L("a", "OPEN"), L("b", "CLOSED", reason="staff", closed=1)).deadline is None


def test_unknown_only_job_uses_deadline_of_all_listings():
    d = derive(L("a", "UNKNOWN", deadline=date(2026, 9, 1)), L("b", "UNKNOWN", deadline=date(2026, 9, 9)))
    assert (d.job_status, d.deadline) == ("OPEN", date(2026, 9, 9))


# ------------------------------------------------------------ URL
def test_source_url_is_newest_open_listing_by_first_seen():
    d = derive(L("old", "OPEN", first=0), L("new", "OPEN", first=5), L("dead", "CLOSED", first=9,
                                                                       reason="staff", closed=9))
    assert d.source_url == "new"


def test_source_url_open_tie_on_first_seen_uses_last_seen_then_url():
    assert derive(L("a", first=1, last=2), L("b", first=1, last=4)).source_url == "b"
    assert derive(L("b", first=1, last=2), L("a", first=1, last=2)).source_url == "a"


def test_source_url_without_open_listing_is_most_recently_seen():
    d = derive(L("a", "CLOSED", first=0, last=9, reason="staff", closed=9),
               L("b", "UNKNOWN", first=3, last=4))
    assert d.source_url == "a"


def test_result_does_not_depend_on_input_order():
    ls = [L("a", "OPEN", first=1, last=1, deadline=date(2026, 11, 1)),
          L("b", "OPEN", first=1, last=1, deadline=date(2026, 12, 1)),
          L("c", "CLOSED", first=0, last=0, reason="staff", closed=1)]
    assert derive(*ls) == derive(*reversed(ls))


def test_single_open_listing_matches_what_insert_job_stores():
    d = derive(L("u", "OPEN", deadline=date(2099, 12, 31)))
    assert d == DerivedJob("OPEN", None, date(2099, 12, 31), "u")


@pytest.mark.parametrize("n", [1, 2, 3])
def test_closed_job_always_has_a_reason_and_open_job_never(n):
    closed = derive(*[L(f"c{i}", "CLOSED", reason="staff", closed=i) for i in range(n)])
    opened = derive(*[L(f"o{i}", "OPEN") for i in range(n)])
    assert closed.closed_reason is not None and opened.closed_reason is None
