"""
Logic THUẦN của lệnh gộp job trùng (merge_duplicates.py, Phần 3b nửa 1/2) — không cần DB.
Phần SQL đọc nằm ở tests/test_pg_merge_duplicates.py.

Kiểm tra: đọc file --only, chọn nhóm (mặc định / --only), luật hợp nhất trường (lương, hạn,
hồi sinh, level, ghi chú), kế hoạch chuyển dữ liệu con (xung đột UNIQUE), báo cáo, CSV, và mã
nguồn không có câu ghi/commit (nửa này CHỈ ĐỌC).
"""
import csv
import io
import re
from datetime import date, datetime
from pathlib import Path

import duplicate_report as dr
import merge_duplicates as md

ROOT = Path(__file__).resolve().parent.parent

A = "aaaaaaaa-0000-4000-8000-000000000001"
B = "bbbbbbbb-0000-4000-8000-000000000002"
C = "cccccccc-0000-4000-8000-000000000003"
D = "dddddddd-0000-4000-8000-000000000004"


def _detail(job_id, *, status="OPEN", created=datetime(2026, 9, 1), deadline=date(2026, 10, 1),
            level_id=1, level_code="Junior", level_source=None, level_version=None, signals=None,
            editor=False, notes=None, smin=None, smax=None, currency=None, stype=None, speriod="MONTH",
            url=None, logs=(), saved=(), apps=(), links=(), province=1, company="c1",
            title="Business Analyst"):
    logs = [{"log_id": f"log-{job_id[:2]}-{i}", "source_url": u} for i, u in enumerate(logs)]
    saved = [{"saved_job_id": f"sv-{job_id[:2]}-{u}", "ss_user_id": u} for u in saved]
    apps = [{"application_id": f"ap-{job_id[:2]}-{u}", "ss_user_id": u, "has_cv": cv} for u, cv in apps]
    links = [{"link_id": f"ln-{job_id[:2]}-{c}", "contact_id": c, "n_interactions": n} for c, n in links]
    return {
        "job_id": job_id, "company_id": company, "job_title": title, "level_id": level_id,
        "level_code": level_code, "level_source": level_source, "level_rule_version": level_version,
        "level_signals": signals, "province_id": province, "currency": currency, "salary_min": smin,
        "salary_max": smax, "salary_type": stype, "salary_period": speriod, "deadline": deadline,
        "job_status": status, "ss_team_notes": notes, "source_url": url or f"https://www.topcv.vn/{job_id[:2]}",
        "created_at": created, "has_editor": editor, "has_notes": bool((notes or "").strip()),
        "logs": logs, "saved": saved, "applications": apps, "links": links,
        "n_applications": len(apps), "n_saved": len(saved), "n_contact_links": len(links),
    }


def _group_row(d):
    """Dòng kiểu db.list_duplicate_job_rows (3a) dựng từ một detail, để build_groups chạy được."""
    return {
        "job_id": d["job_id"], "company_id": d["company_id"], "company_name": "Công ty A",
        "company_active": True, "job_title": d["job_title"], "norm_title": d["job_title"].lower(),
        "level_code": d["level_code"], "province_id": d["province_id"], "province_name": "T1",
        "job_status": d["job_status"], "created_at": d["created_at"], "deadline": d["deadline"],
        "salary_min": d["salary_min"], "salary_max": d["salary_max"], "source_url": d["source_url"],
        "content_hash": f"h-{d['level_code']}-{d['province_id']}", "has_editor": d["has_editor"],
        "has_notes": d["has_notes"], "log_urls": [x["source_url"] for x in d["logs"]],
        "n_applications": d["n_applications"], "n_saved": d["n_saved"], "n_contact_links": d["n_contact_links"],
    }


def _plan(details: list, keeper=None):
    """Lập kế hoạch cho nhóm gồm `details`; keeper mặc định = đề xuất luật v0."""
    group = dr.build_groups([_group_row(d) for d in details])[0]
    sel = md.Selection(group, keeper or group.keeper_id,
                       md.KEEPER_FROM_FILE if keeper else md.KEEPER_FROM_RULE)
    return md.plan_merge(sel, {d["job_id"]: d for d in details})


# ------------------------------------------------------------------ file --only
def test_parse_only_plain_list_with_comments_and_noise():
    text = f"# duyệt ngày 6/10\n{A}\n\n{B.upper()}  # ghi chú\nkhông phải id\n"
    spec = md.parse_only(text)
    assert spec.job_ids == {A, B} and spec.keepers == set()
    assert spec.invalid_lines == [(5, "không phải id")]


def test_parse_only_csv_from_report_with_keeper_marks_and_bom():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["nhom", "tang", "do_chac", "de_xuat_giu", "job_id"])
    w.writerow([1, "strict", "cao", "", A])
    w.writerow([1, "strict", "cao", "x", B])
    w.writerow([2, "province", "thấp", "X", C])
    w.writerow([2, "province", "thấp", "", "không-phải-uuid"])
    spec = md.parse_only("\ufeff" + buf.getvalue())
    assert spec.job_ids == {A, B, C}
    assert spec.keepers == {B, C}                     # 'X' hoa cũng tính
    assert [n for n, _ in spec.invalid_lines] == [5]


def test_parse_only_empty_file_has_no_ids():
    assert md.parse_only("# chỉ có ghi chú\n\n").job_ids == set()


# ------------------------------------------------------------------ chọn nhóm
def _groups_for_selection():
    # g1: strict cao, 1 OPEN; g2: strict cần xem (2 OPEN); g3: khác tỉnh; g4: strict cao nhưng 2 job được bảo vệ
    g1 = [_detail("a1", company="c1", status="CLOSED"), _detail("a2", company="c1")]
    g2 = [_detail("b1", company="c2"), _detail("b2", company="c2")]
    g3 = [_detail("c1", company="c3", province=1), _detail("c2", company="c3", province=2)]
    g4 = [_detail("d1", company="c4", status="CLOSED", notes="n"), _detail("d2", company="c4", status="CLOSED", editor=True)]
    return dr.build_groups([_group_row(d) for d in g1 + g2 + g3 + g4])


def test_default_selection_only_high_confidence_non_province_without_double_protection():
    groups = _groups_for_selection()
    selected, skipped, unknown, not_in_file = md.select_groups(groups)
    reasons = {g.company_id: r for g, r in skipped}
    assert reasons["c2"] == md.SKIP_REVIEW
    assert reasons["c3"] == md.SKIP_PROVINCE
    assert unknown == [] and not_in_file == 0
    # c4 có 2 job được bảo vệ -> không tự gộp (dù độ chắc cao)
    assert "c4" in reasons and reasons["c4"] == md.SKIP_MANUAL_PICK
    assert [s.group.company_id for s in selected] == ["c1"]
    assert selected[0].keeper_id == "a2" and selected[0].keeper_source == md.KEEPER_FROM_RULE


def test_only_file_plain_list_merges_review_group_with_rule_keeper():
    groups = _groups_for_selection()
    only = md.OnlySpec(job_ids={"b1", "b2"})           # id ngắn của test (parse_only đã kiểm UUID riêng)
    selected, skipped, unknown, not_in_file = md.select_groups(groups, only)
    assert [s.group.company_id for s in selected] == ["c2"]
    assert not_in_file == 3 and skipped == []


def test_only_file_partial_group_is_skipped():
    groups = _groups_for_selection()
    only = md.OnlySpec(job_ids={"b1"})
    selected, skipped, _, _ = md.select_groups(groups, only)
    assert selected == [] and [r for _, r in skipped] == [md.SKIP_PARTIAL]


def test_only_file_province_and_double_protected_need_explicit_x():
    groups = _groups_for_selection()
    only = md.OnlySpec(job_ids={"c1", "c2", "d1", "d2"})
    selected, skipped, _, _ = md.select_groups(groups, only)
    assert selected == []
    assert {r for _, r in skipped} == {md.SKIP_PROVINCE, md.SKIP_MANUAL_PICK}

    only = md.OnlySpec(job_ids={"c1", "c2", "d1", "d2"}, keepers={"c2", "d1"})
    selected, skipped, _, _ = md.select_groups(groups, only)
    by_company = {s.group.company_id: s for s in selected}
    assert by_company["c3"].keeper_id == "c2" and by_company["c4"].keeper_id == "d1"
    assert all(s.keeper_source == md.KEEPER_FROM_FILE for s in selected) and skipped == []


def test_only_file_two_x_in_one_group_is_skipped_and_unknown_ids_reported():
    groups = _groups_for_selection()
    only = md.OnlySpec(job_ids={"b1", "b2", "zz"}, keepers={"b1", "b2"})
    selected, skipped, unknown, _ = md.select_groups(groups, only)
    assert selected == [] and [r for _, r in skipped] == [md.SKIP_MULTI_KEEPER]
    assert unknown == ["zz"]


# ------------------------------------------------------------------ hợp nhất: lương
def test_salary_taken_from_donor_when_keeper_has_none():
    keeper = _detail(A, created=datetime(2026, 9, 1))
    donor = _detail(B, status="CLOSED", smin=350, smax=550, currency="USD", stype="RANGE", speriod="MONTH")
    plan = _plan([keeper, donor])
    assert plan.keeper_id == A
    assert plan.changes["salary_min"] == {"old": None, "new": 350}
    assert plan.changes["currency"]["new"] == "USD" and plan.changes["salary_type"]["new"] == "RANGE"
    assert "salary_period" not in plan.changes          # cùng giá trị 'MONTH' -> không phải thay đổi
    assert plan.conflicts == []


def test_salary_conflict_keeps_keeper_value_and_records_other():
    keeper = _detail(A, smin=350, smax=600)
    donor = _detail(B, status="CLOSED", smin=350, smax=550)
    plan = _plan([keeper, donor])
    assert not any(c in plan.changes for c in md.SALARY_COLUMNS)
    assert len(plan.conflicts) == 1
    c = plan.conflicts[0]
    assert c["field"] == "salary" and c["kept_job_id"] == A and c["other_job_id"] == B
    assert c["other"]["salary_max"] == 550


def test_salary_equal_values_are_not_conflicts_and_two_donors_differing_are():
    keeper = _detail(A)
    d1 = _detail(B, status="CLOSED", created=datetime(2026, 9, 2), smin=10, smax=20)
    d2 = _detail(C, status="CLOSED", created=datetime(2026, 9, 3), smin=10, smax=20)
    d3 = _detail(D, status="CLOSED", created=datetime(2026, 9, 4), smin=15, smax=25)
    plan = _plan([keeper, d1, d2, d3])
    assert plan.changes["salary_min"]["new"] == 10
    assert [c["other_job_id"] for c in plan.conflicts] == [D]


# ------------------------------------------------------------------ hợp nhất: trạng thái, hạn
def test_open_keeper_keeps_its_deadline_and_status():
    plan = _plan([_detail(A), _detail(B, status="CLOSED", deadline=date(2026, 12, 1))])
    assert plan.keeper_id == A and plan.changes == {} and not plan.revives


def test_keeper_without_deadline_takes_latest_donor_deadline_preferring_open_donor():
    keeper = _detail(A, deadline=None)
    closed = _detail(B, status="CLOSED", deadline=date(2026, 12, 31), created=datetime(2026, 9, 2))
    plan = _plan([keeper, closed])
    assert plan.changes["deadline"]["new"] == date(2026, 12, 31)

    open_donor = _detail(C, deadline=date(2026, 11, 1), created=datetime(2026, 9, 3))
    plan = _plan([keeper, closed, open_donor], keeper=A)
    assert plan.changes["deadline"]["new"] == date(2026, 11, 1)   # job OPEN được ưu tiên


def test_revive_takes_status_deadline_and_source_url_from_open_donor():
    protected_closed = _detail(A, status="CLOSED", deadline=date(2026, 9, 1), notes="đã gọi HR",
                               url="https://www.topcv.vn/cu")
    open_donor = _detail(B, status="OPEN", deadline=date(2026, 11, 1), url="https://www.topcv.vn/moi",
                         created=datetime(2026, 9, 20))
    plan = _plan([protected_closed, open_donor])
    assert plan.keeper_id == A and plan.revives
    assert plan.changes["job_status"] == {"old": "CLOSED", "new": "OPEN"}
    assert plan.changes["deadline"]["new"] == date(2026, 11, 1)
    assert plan.changes["source_url"] == {"old": "https://www.topcv.vn/cu", "new": "https://www.topcv.vn/moi"}
    assert any("hồi sinh" in n for n in plan.notes)


def test_all_closed_group_stays_closed():
    plan = _plan([_detail(A, status="CLOSED"), _detail(B, status="CLOSED", created=datetime(2026, 9, 2))])
    assert "job_status" not in plan.changes and not plan.revives


# ------------------------------------------------------------------ hợp nhất: level
def test_level_comes_from_manual_donor_when_keeper_is_not_authoritative():
    keeper = _detail(A, level_id=1, level_code="Junior", level_source="title", level_version=1)
    manual = _detail(B, status="CLOSED", level_id=3, level_code="Senior", level_source="manual",
                     created=datetime(2026, 9, 2))
    plan = _plan([keeper, manual])
    assert plan.keeper_id == A
    assert plan.changes["level_id"] == {"old": 1, "new": 3}
    assert plan.changes["level_source"]["new"] == "manual"
    assert plan.changes["level_rule_version"] == {"old": 1, "new": None}


def test_level_from_editor_donor_and_same_level_manual_stamp_is_copied():
    keeper = _detail(A, level_id=2, level_code="Middle", level_source="title", level_version=1)
    editor = _detail(B, status="CLOSED", level_id=4, level_code="Lead", level_source=None, editor=True,
                     created=datetime(2026, 9, 2))
    # Job có người sửa tự thành job giữ theo luật v0, nên ép job giữ là A (người duyệt chọn).
    assert _plan([keeper, editor], keeper=A).changes["level_id"]["new"] == 4
    by_rule = _plan([keeper, editor])
    assert by_rule.keeper_id == B and "level_id" not in by_rule.changes and by_rule.revives

    same_level_manual = _detail(B, status="CLOSED", level_id=2, level_code="Middle", level_source="manual",
                                created=datetime(2026, 9, 2))
    plan = _plan([keeper, same_level_manual], keeper=A)
    assert "level_id" not in plan.changes and plan.changes["level_source"]["new"] == "manual"


def test_level_non_authoritative_donor_never_overrides_and_keeper_authoritative_records_conflict():
    keeper = _detail(A, level_id=1, level_code="Junior", level_source="title", level_version=1)
    other = _detail(B, status="CLOSED", level_id=3, level_code="Senior", level_source="years", level_version=1,
                    created=datetime(2026, 9, 2))
    plan = _plan([keeper, other])
    assert not any(c in plan.changes for c in md.LEVEL_COLUMNS) and plan.conflicts == []

    manual_keeper = _detail(A, level_id=1, level_code="Junior", level_source="manual", notes="x")
    manual_other = _detail(B, status="CLOSED", level_id=3, level_code="Senior", level_source="manual",
                           created=datetime(2026, 9, 2))
    plan = _plan([manual_keeper, manual_other], keeper=A)
    assert not any(c in plan.changes for c in md.LEVEL_COLUMNS)
    assert [c["field"] for c in plan.conflicts] == ["level"]


# ------------------------------------------------------------------ hợp nhất: ghi chú
def test_notes_taken_when_keeper_has_none_and_conflict_when_both_differ():
    keeper = _detail(A, saved=("u1",))                       # được bảo vệ nhờ lượt lưu
    donor = _detail(B, status="CLOSED", notes="gọi lại thứ 5", created=datetime(2026, 9, 2))
    plan = _plan([keeper, donor], keeper=A)
    assert plan.changes["ss_team_notes"]["new"] == "gọi lại thứ 5" and plan.conflicts == []

    both = _plan([_detail(A, notes="ghi chú A"), _detail(B, status="CLOSED", notes="ghi chú B",
                                                         created=datetime(2026, 9, 2))], keeper=A)
    assert "ss_team_notes" not in both.changes
    assert [(c["field"], c["other"]) for c in both.conflicts] == [("ss_team_notes", "ghi chú B")]

    same = _plan([_detail(A, notes=" y "), _detail(B, status="CLOSED", notes="y", created=datetime(2026, 9, 2))],
                 keeper=A)
    assert same.conflicts == []


# ------------------------------------------------------------------ dữ liệu con
def test_children_move_and_drop_on_unique_conflicts():
    keeper = _detail(A, logs=("u1",), saved=("s1",), apps=(("p1", False),), links=(("k1", 2),))
    donor = _detail(B, status="CLOSED", created=datetime(2026, 9, 2), logs=("u1", "u2", None),
                    saved=("s1", "s2"), apps=(("p1", True), ("p2", False)), links=(("k1", 3), ("k2", 1)))
    plan = _plan([keeper, donor], keeper=A)
    cp = plan.child
    assert cp.logs_drop == ["log-bb-0"] and cp.logs_move == ["log-bb-1", "log-bb-2"]  # URL NULL không bao giờ trùng
    assert cp.saved_drop == ["sv-bb-s1"] and cp.saved_move == ["sv-bb-s2"]
    assert cp.apps_drop == ["ap-bb-p1"] and cp.apps_move == ["ap-bb-p2"]
    assert plan.apps_with_cv_dropped == 1 and any("CV" in w for w in plan.warnings)
    assert cp.links_move == ["ln-bb-k2"]
    assert cp.links_merge == [{"donor_link_id": "ln-bb-k1", "keeper_link_id": "ln-aa-k1", "n_interactions": 3}]


def test_children_of_two_donors_do_not_both_move_the_same_key():
    keeper = _detail(A)
    d1 = _detail(B, status="CLOSED", created=datetime(2026, 9, 2), logs=("u9",), saved=("s9",), links=(("k9", 1),))
    d2 = _detail(C, status="CLOSED", created=datetime(2026, 9, 3), logs=("u9",), saved=("s9",), links=(("k9", 4),))
    plan = _plan([keeper, d1, d2], keeper=A)
    cp = plan.child
    assert cp.logs_move == ["log-bb-0"] and cp.logs_drop == ["log-cc-0"]
    assert cp.saved_move == ["sv-bb-s9"] and cp.saved_drop == ["sv-cc-s9"]
    assert cp.links_move == ["ln-bb-k9"]
    assert cp.links_merge == [{"donor_link_id": "ln-cc-k9", "keeper_link_id": "ln-bb-k9", "n_interactions": 4}]


# ------------------------------------------------------------------ cảnh báo, thứ tự
def test_warnings_for_file_keeper_override_and_non_strict_tier():
    a = _detail(A, level_id=1, level_code="Junior")
    b = _detail(B, status="CLOSED", level_id=2, level_code="Middle", created=datetime(2026, 9, 2))
    plan = _plan([a, b], keeper=B)                      # đề xuất là A (OPEN), người duyệt chọn B
    texts = " | ".join(plan.warnings)
    assert "do bạn chọn" in texts and "tầng 'level'" in texts
    assert plan.donor_ids == [A]


def test_donors_are_ordered_by_keeper_rule():
    keeper = _detail(A, notes="g")
    d_closed = _detail(B, status="CLOSED", created=datetime(2026, 9, 2))
    d_open = _detail(C, status="OPEN", created=datetime(2026, 9, 3))
    plan = _plan([keeper, d_closed, d_open])
    assert plan.keeper_id == A and plan.donor_ids == [C, B]    # OPEN trước CLOSED


# ------------------------------------------------------------------ báo cáo + CSV
def _sample_plans():
    p1 = _plan([_detail(A, status="CLOSED", notes="n", deadline=date(2026, 9, 1)),
                _detail(B, deadline=date(2026, 11, 1), smin=1, smax=2, created=datetime(2026, 9, 9))])
    p2 = _plan([_detail("e1", company="c2", saved=("s1",)),
                _detail("e2", company="c2", status="CLOSED", created=datetime(2026, 9, 2), saved=("s1",))])
    return [p1, p2]


def test_report_summarises_plans_and_states_it_is_read_only(capsys):
    plans = _sample_plans()
    skipped = [(plans[0].group, md.SKIP_REVIEW)]
    summary = md.Summary(plans, skipped, total_groups=3, only=False, unknown_ids=[], not_in_file=0,
                         invalid_lines=[])
    assert summary.planned == 2 and summary.jobs_deleted == 2
    assert summary.field_counts["hồi sinh (job giữ CLOSED -> OPEN)"] == 1
    assert summary.field_counts["lương lấy từ job bị gộp"] == 1
    assert summary.children["saved_jobs"] == (0, 1)

    md.print_report(summary, plans, skipped, show=5)
    out = capsys.readouterr().out
    assert "CHẠY THỬ" in out and "KHÔNG ghi DB" in out
    assert "Nhóm SẼ gộp:   2" in out and md.SKIP_REVIEW in out
    assert "hồi sinh" in out and "GỘP VÀO RỒI XOÁ" in out
    assert "chưa xoá job nào" in out


def test_report_with_no_plans_still_prints(capsys):
    summary = md.Summary([], [], total_groups=0, only=True, unknown_ids=["zz"], not_in_file=0,
                         invalid_lines=[(3, "rác")])
    md.print_report(summary, [], [])
    out = capsys.readouterr().out
    assert "Nhóm SẼ gộp:   0" in out and "zz" in out and "dòng 3: rác" in out


def test_csv_has_one_row_per_plan_with_bom():
    plans = _sample_plans()
    buf = io.StringIO()
    assert md.write_csv(plans, buf) == 2
    rows = list(csv.DictReader(io.StringIO(buf.getvalue())))
    assert rows[0]["job_giu"] == A and rows[0]["job_bi_gop"] == B
    assert rows[1]["luu_bo"] == "1"


# ------------------------------------------------------------------ nửa này chỉ đọc
def test_source_has_no_write_statements():
    for name in ("merge_duplicates.py", "db/job_merge.py"):
        code = (ROOT / name).read_text(encoding="utf-8")
        # bỏ chuỗi docstring/comment để chỉ xét câu SQL/lệnh thật
        stripped = re.sub(r'""".*?"""', "", code, flags=re.S)
        stripped = "\n".join(line.split("#", 1)[0] for line in stripped.splitlines())
        for forbidden in ("INSERT INTO", "UPDATE ", "DELETE FROM", ".commit(", "TRUNCATE", "ALTER "):
            assert forbidden not in stripped, f"{name} chứa '{forbidden}' (nửa 1/2 phải chỉ đọc)"
