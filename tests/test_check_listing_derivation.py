"""
Báo cáo so job suy ra từ listing với job đang lưu (check_listing_derivation.py, C2 nửa 1/2). Phần so và phân
loại là hàm thuần; phần chạy dùng DB giả. Test trên Postgres thật nằm ở tests/test_pg_job_derivation.py.
"""
import io
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from scrapjd.cli import check_listing_derivation as cld

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def _listing(url, status="OPEN", *, deadline=None, first=0, reason=None, closed=None):
    day = lambda n: None if n is None else T0 + timedelta(days=n)
    return {"source_url": url, "listing_status": status, "deadline": deadline, "first_seen_at": day(first),
            "last_seen_at": day(first), "closed_reason": reason, "closed_at": day(closed)}


def _job(listings, *, status="OPEN", reason=None, deadline=None, url=None, job_id="j1"):
    return {"job_id": job_id, "job_title": "Data Analyst", "company_name": "ACME", "job_status": status,
            "closed_reason": reason, "deadline": deadline,
            "source_url": url if url is not None else (listings[0]["source_url"] if listings else None),
            "listings": listings}


def test_job_without_listings_is_counted_apart_not_as_mismatch():
    rep = cld.build_report([_job([], url="x")])
    assert (rep.total_jobs, rep.jobs_without_listings, rep.jobs_compared, rep.mismatches) == (1, 1, 0, [])


def test_matching_job_has_no_mismatch():
    rep = cld.build_report([_job([_listing("a", deadline=date(2026, 12, 1))], deadline=date(2026, 12, 1))])
    assert (rep.jobs_compared, rep.jobs_matching, rep.mismatches) == (1, 1, [])


def test_status_mismatch_both_directions_are_labelled():
    open_but_closed = _job([_listing("a", "CLOSED", reason="staff", closed=1)], status="OPEN", job_id="j1")
    closed_but_open = _job([_listing("a", "OPEN")], status="CLOSED", reason="staff", job_id="j2")
    rep = cld.build_report([open_but_closed, closed_but_open])
    kinds = {(m.job_id, m.kind) for m in rep.mismatches if m.field == "job_status"}
    assert kinds == {("j1", "job OPEN nhưng mọi listing CLOSED"),
                     ("j2", "job CLOSED nhưng có listing OPEN hoặc UNKNOWN")}


def test_closed_reason_compared_only_when_both_closed():
    ok = _job([_listing("a", "CLOSED", reason="staff", closed=1)], status="CLOSED", reason="staff")
    bad = _job([_listing("a", "CLOSED", reason="staff", closed=1)], status="CLOSED", reason="expired_auto",
               job_id="j2")
    rep = cld.build_report([ok, bad])
    assert [(m.job_id, m.field, m.stored, m.derived) for m in rep.mismatches] == [
        ("j2", "closed_reason", "expired_auto", "staff")]


def test_closed_reason_not_reported_when_status_already_differs():
    job = _job([_listing("a", "OPEN")], status="CLOSED", reason="staff")
    assert [m.field for m in cld.build_report([job]).mismatches] == ["job_status"]


def test_deadline_mismatch_kinds():
    base = date(2026, 12, 1)
    cases = {
        "job không có hạn, listing có": (None, base),
        "job có hạn, listing không có": (base, None),
        "cả hai có hạn, job muộn hơn": (date(2027, 1, 1), base),
        "cả hai có hạn, job sớm hơn": (date(2026, 11, 1), base),
    }
    for kind, (stored, listing_deadline) in cases.items():
        job = _job([_listing("a", deadline=listing_deadline)], deadline=stored)
        (m,) = cld.build_report([job]).mismatches
        assert (m.field, m.kind) == ("deadline", kind)


def test_source_url_mismatch_kinds():
    other = _job([_listing("old", first=0), _listing("new", first=3)], url="old")
    foreign = _job([_listing("new")], url="not-a-listing", job_id="j2")
    rep = cld.build_report([other, foreign])
    kinds = {m.job_id: m.kind for m in rep.mismatches if m.field == "source_url"}
    assert kinds == {"j1": "URL của job là một listing khác của job", "j2": "URL của job không có listing nào"}
    assert {m.job_id: m.derived for m in rep.mismatches} == {"j1": "new", "j2": "new"}


def test_counters_and_jobs_with_mismatch():
    j1 = _job([_listing("a", deadline=date(2026, 12, 1))], deadline=None, url="zzz")       # 2 trường lệch
    j2 = _job([_listing("a")], job_id="j2")                                                  # khớp
    rep = cld.build_report([j1, j2])
    assert rep.jobs_with_mismatch() == 1 and rep.jobs_matching == 1
    assert rep.by_field() == {"deadline": 1, "source_url": 1}


def test_csv_has_one_row_per_mismatched_field_with_header():
    j1 = _job([_listing("a", deadline=date(2026, 12, 1))], deadline=None)
    buf = io.StringIO()
    n = cld.write_csv(cld.build_report([j1]), buf)
    lines = buf.getvalue().strip().splitlines()
    assert n == 1 and len(lines) == 2
    assert lines[0].startswith("job_id,cong_ty,tieu_de,truong,loai,dang_luu,suy_ra,so_listing")
    assert "deadline" in lines[1] and "2026-12-01" in lines[1]


def test_print_report_mentions_counts_and_examples(capsys):
    j1 = _job([_listing("a", deadline=date(2026, 12, 1))], deadline=None)
    cld.print_report(cld.build_report([j1]), show=3)
    out = capsys.readouterr().out
    assert "Tổng số job: 1" in out and "job không có hạn, listing có" in out and "2026-12-01" in out


def test_print_report_when_everything_matches(capsys):
    cld.print_report(cld.build_report([_job([_listing("a")])]), show=3)
    assert "Không có trường nào lệch" in capsys.readouterr().out


def _run(jobs, **kw):
    conn = SimpleNamespace()
    with patch.object(cld.db, "list_jobs_with_listings", return_value=jobs):
        return cld.run(conn, **kw)


def test_run_exit_code_is_zero_by_default_even_with_mismatch(capsys):
    assert _run([_job([_listing("a", deadline=date(2026, 12, 1))], deadline=None)]) == 0


def test_run_strict_exits_2_only_when_mismatch(capsys):
    assert _run([_job([_listing("a", deadline=date(2026, 12, 1))], deadline=None)], strict=True) == 2
    assert _run([_job([_listing("a")])], strict=True) == 0


def test_run_writes_csv_file(tmp_path, capsys):
    path = tmp_path / "lech.csv"
    _run([_job([_listing("a", deadline=date(2026, 12, 1))], deadline=None)], csv_path=str(path))
    assert path.read_text(encoding="utf-8-sig").count("\n") == 2


def test_run_cli_rejects_negative_show(capsys):
    assert cld.run_cli(SimpleNamespace(show=-1, csv=None, strict=False)) == 1


def test_cli_command_is_registered_in_main(monkeypatch):
    import main
    called = {}
    monkeypatch.setattr(main.check_listing_derivation, "run_cli", lambda a: called.setdefault("args", a) or 0)
    monkeypatch.setattr("sys.argv", ["main.py", "check-listing-derivation", "--show", "2", "--strict"])
    try:
        main.main()
    except SystemExit:
        pass
    assert called["args"].show == 2 and called["args"].strict is True and called["args"].csv is None
