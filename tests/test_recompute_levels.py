"""
Logic THUẦN của lệnh tính lại level (recompute_levels.py, Phần 2b) — không cần DB.
Phần chạy SQL thật nằm ở tests/test_pg_recompute_levels.py.
"""
import normalize
import recompute_levels as rl
from recompute_levels import (
    ACTION_CHANGE, ACTION_KEEP, ACTION_SKIP_EDITED, ACTION_STAMP, ACTION_UNCHANGED,
)

V = normalize.LEVEL_RULE_VERSION
SIG_3Y = {"experience_text": "3 năm", "level_hint": ""}
SIG_EMPTY = {"experience_text": "", "level_hint": ""}


def _row(title, *, level="Junior", source=None, version=None, signals=None, editor=False, job_id="j1"):
    return {"job_id": job_id, "job_title": title, "level_id": 3 if level else None,
            "level_code": level, "level_source": source, "level_rule_version": version,
            "level_signals": signals, "content_hash": f"h-{job_id}", "has_editor": editor}


# ------------------------------------------------------------------ plan_job: job cũ (không tín hiệu)
def test_old_job_title_with_clear_level_changes_level():
    # Ví dụ A trong bảng đã chốt: "Senior Data Engineer" đang Middle -> Senior (căn cứ title).
    plan = rl.plan_job(_row("Senior Data Engineer", level="Middle"))
    assert plan == rl.Plan(ACTION_CHANGE, "Senior", "title", None)


def test_old_job_without_title_basis_keeps_level_and_stamp_null():
    # Ví dụ B: "Data Analyst" đang Senior (suy từ số năm lúc crawl) -> giữ Senior, dấu NULL,
    # không rơi về Junior mặc định.
    assert rl.plan_job(_row("Data Analyst", level="Senior")).action == ACTION_KEEP


def test_old_job_title_range_is_not_trusted_without_signals():
    # "Senior/Leader" ra title_range; chỉ căn cứ 'title' mới được tin ở job cũ.
    assert rl.plan_job(_row("Senior/Leader", level="Middle")).action == ACTION_KEEP


def test_old_job_title_agrees_with_current_level_only_stamps():
    plan = rl.plan_job(_row("Senior Data Engineer", level="Senior"))
    assert plan == rl.Plan(ACTION_STAMP, "Senior", "title", None)


def test_internal_is_not_intern_for_old_job():
    # Lỗi quy tắc cũ ("Internal" ra Intern) không được quay lại.
    assert rl.plan_job(_row("Internal Audit", level="Senior")).action == ACTION_KEEP


# ------------------------------------------------------------------ plan_job: job có tín hiệu
def test_job_with_signals_recomputes_fully_and_keeps_signals():
    plan = rl.plan_job(_row("Data Analyst", level="Junior", source="default", version=0, signals=SIG_3Y))
    assert plan == rl.Plan(ACTION_CHANGE, "Middle", "years", SIG_3Y)


def test_job_with_empty_signals_is_trusted_as_default():
    # {} rỗng = đã lưu và nguồn không có tín hiệu: khác job cũ, tính lại ra default Junior.
    plan = rl.plan_job(_row("Data Analyst", level="Senior", source="years", version=0, signals=SIG_EMPTY))
    assert plan == rl.Plan(ACTION_CHANGE, "Junior", "default", SIG_EMPTY)


def test_job_with_signals_same_level_older_version_only_restamps():
    plan = rl.plan_job(_row("Data Analyst", level="Middle", source="years", version=V - 1, signals=SIG_3Y))
    assert plan == rl.Plan(ACTION_STAMP, "Middle", "years", SIG_3Y)


def test_already_current_job_is_unchanged():
    row = _row("Data Analyst", level="Middle", source="years", version=V, signals=SIG_3Y)
    assert rl.plan_job(row).action == ACTION_UNCHANGED


def test_job_without_level_gets_one():
    plan = rl.plan_job(_row("Senior Data Engineer", level=None))
    assert plan.action == ACTION_CHANGE and plan.new_level == "Senior"


# ------------------------------------------------------------------ bỏ qua job từng có người sửa
def test_unstamped_job_with_editor_is_skipped_even_if_title_is_clear():
    assert rl.plan_job(_row("Senior Data Engineer", level="Middle", editor=True)).action == ACTION_SKIP_EDITED


def test_stamped_job_with_editor_is_still_recomputed():
    # Có dấu máy suy: người sửa field khác (sửa level sẽ thành 'manual', không được chọn).
    row = _row("Data Analyst", level="Junior", source="default", version=0, signals=SIG_3Y, editor=True)
    assert rl.plan_job(row).action == ACTION_CHANGE


# ------------------------------------------------------------------ hợp đồng với normalize
def test_recompute_matches_crawl_time_decision():
    # Lưu gì thì tính lại ra đúng cái đó: crawl suy bằng derive_level, lệnh tính lại bằng
    # derive_level_from_signals từ tín hiệu đã lưu, hai bên phải cho cùng kết quả.
    for title, exp, hint in [("Data Analyst", "2 năm", ""), ("Data Engineer", "Trên 5 năm", ""),
                             ("Backend Dev", "", "Senior"), ("Sr. Specialist", "", "")]:
        crawled = normalize.derive_level(exp, title, hint)
        row = _row(title, level="Junior", source=crawled.source, version=V - 1,
                   signals=normalize.build_level_signals(exp, hint))
        plan = rl.plan_job(row)
        assert plan.new_level == crawled.level and plan.new_source == crawled.source


def test_build_plans_keeps_order_and_summary_counts():
    rows = [
        _row("Senior Data Engineer", level="Middle", job_id="a"),
        _row("Data Analyst", level="Senior", job_id="b"),
        _row("Data Analyst", level="Junior", source="default", version=0, signals=SIG_3Y, job_id="c"),
        _row("Senior Data Engineer", level="Middle", editor=True, job_id="d"),
    ]
    plans = rl.build_plans(rows)
    assert [r["job_id"] for r, _ in plans] == ["a", "b", "c", "d"]
    summary = rl.Summary(plans)
    assert summary.selected == 4 and summary.with_signals == 1
    assert summary.by_action == {ACTION_CHANGE: 2, ACTION_KEEP: 1, ACTION_SKIP_EDITED: 1}
    assert summary.transitions[("Middle", "Senior", "title")] == 1
    assert summary.transitions[("Junior", "Middle", "years")] == 1


# ------------------------------------------------------------------ nhóm job nghi trùng
def _m(job_id, old, new, title="T"):
    return {"job_id": job_id, "job_title": title, "old_hash": old, "new_hash": new}


def test_simulate_job_joins_existing_group():
    members = {"hA": {"x": "T"}, "hB": {"y": "T"}}
    out = rl.simulate_group_effects(members, [_m("x", "hA", "hB")])
    assert [e["job_id"] for e in out["joined"]] == ["x"] and out["joined"][0]["others"] == ["y"]
    assert out["left"] == []
    assert (out["groups_before"], out["groups_after"]) == (0, 1)


def test_simulate_job_leaves_group():
    members = {"hA": {"x": "T", "z": "T"}, "hC": {}}
    out = rl.simulate_group_effects(members, [_m("x", "hA", "hC")])
    assert out["left"] == [{"job_id": "x", "job_title": "T", "others": ["z"]}]
    assert out["joined"] == []
    assert (out["groups_before"], out["groups_after"]) == (1, 0)


def test_simulate_two_changing_jobs_land_on_same_new_hash():
    members = {"hA": {"x": "T"}, "hB": {"y": "T"}}
    out = rl.simulate_group_effects(members, [_m("x", "hA", "hN"), _m("y", "hB", "hN")])
    assert sorted(e["job_id"] for e in out["joined"]) == ["x", "y"]
    assert (out["groups_before"], out["groups_after"]) == (0, 1)


def test_simulate_ignores_moves_with_same_hash_and_no_moves():
    assert rl.simulate_group_effects({"hA": {"x": "T"}}, [_m("x", "hA", "hA")])["joined"] == []
    out = rl.simulate_group_effects({}, [])
    assert out == {"left": [], "joined": [], "groups_before": 0, "groups_after": 0}


# ------------------------------------------------------------------ báo cáo
def test_print_report_dry_run_says_nothing_written(capsys):
    plans = rl.build_plans([_row("Senior Data Engineer", level="Middle"),
                            _row("Data Analyst", level="Senior", job_id="b")])
    effects = {"left": [], "joined": [], "groups_before": 0, "groups_after": 0}
    rl.print_report(rl.Summary(plans), effects, apply=False, rule_version=V)
    out = capsys.readouterr().out
    assert "CHẠY THỬ" in out and "--apply" in out
    assert "Middle -> Senior" in out


def test_to_change_requires_known_level():
    row = _row("Senior Data Engineer", level="Middle")
    plan = rl.plan_job(row)
    change = rl.to_change(row, plan, {"Senior": 5}, V)
    assert change["new_level_id"] == 5 and change["old_level_source"] is None
    try:
        rl.to_change(row, plan, {}, V)
    except RuntimeError:
        pass
    else:
        raise AssertionError("phải báo lỗi khi bảng levels thiếu level")
