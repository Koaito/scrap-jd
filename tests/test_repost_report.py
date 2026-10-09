"""
Báo cáo đo tỷ lệ gộp nhầm (A5) — phần logic THUẦN của scrapjd/cli/repost_report.py, không cần DB:
tách phần "Mô tả/Yêu cầu" khỏi "Quyền lợi", tách từ, so độ giống, phân loại, dựng kết quả theo job,
tổng hợp, CSV, kiểm tra tham số CLI. Phần SQL và chạy thật ở tests/test_pg_repost_report.py.
"""
import io
from argparse import Namespace

import pytest

from scrapjd.cli import repost_report as rr

BODY_A = ("quản lý chiến dịch quảng cáo trên facebook và google phân tích dữ liệu hiệu quả "
          "lập báo cáo hằng tuần cho trưởng phòng marketing phối hợp với đội thiết kế nội dung")
BODY_B = ("xây dựng và vận hành hệ thống backend bằng python và postgresql viết api tối ưu truy vấn "
          "triển khai lên máy chủ đám mây theo dõi lỗi và hiệu năng của dịch vụ")
PERKS = "bảo hiểm đầy đủ thưởng tháng mười ba du lịch hằng năm làm việc tại văn phòng hiện đại"


def raw(desc="", req="", perks=""):
    parts = []
    if desc:
        parts.append("=== Mô tả công việc ===\n" + desc)
    if req:
        parts.append("=== Yêu cầu ứng viên ===\n" + req)
    if perks:
        parts.append("=== Quyền lợi ứng viên ===\n" + perks)
    return "\n\n".join(parts)


def row(job="j1", log="l1", url="https://www.topcv.vn/a", day="2026-09-01", text=None, company="Công ty A",
        title="Marketing Executive"):
    return {"job_id": job, "company_id": "c1", "company_name": company, "job_title": title,
            "level_code": "Junior", "province_name": "Hà Nội", "job_status": "OPEN",
            "job_created_at": None, "log_id": log, "source_name": "TopCV", "source_url": url,
            "collected_date": day, "raw_jd_content": text}


# ------------------------------------------------------------------ tách phần, tách từ
def test_role_text_keeps_description_and_requirements_but_not_perks():
    text = rr.role_text(raw("mô tả một", "yêu cầu hai", PERKS))
    assert "mô tả một" in text and "yêu cầu hai" in text and "bảo hiểm" not in text


def test_role_text_only_perks_is_empty_not_a_fallback_to_everything():
    assert rr.role_text(raw(perks=PERKS)) == ""


def test_role_text_without_any_heading_uses_the_whole_text():
    assert rr.role_text("  nội dung cũ không có tiêu đề  ") == "nội dung cũ không có tiêu đề"


@pytest.mark.parametrize("value", [None, "", "   \n"])
def test_role_text_blank(value):
    assert rr.role_text(value) == ""


def test_heading_match_is_case_and_unicode_form_insensitive():
    import unicodedata
    heading = unicodedata.normalize("NFD", "=== MÔ TẢ CÔNG VIỆC ===")
    assert rr.role_text(heading + "\nnội dung chính") == "nội dung chính"


def test_tokenize_lowercases_drops_punctuation_and_keeps_vietnamese_marks():
    assert rr.tokenize("Quản lý, Dữ-liệu!  (SQL)") == ["quản", "lý", "dữ", "liệu", "sql"]


def test_tokenize_normalizes_decomposed_accents():
    import unicodedata
    assert rr.tokenize(unicodedata.normalize("NFD", "Quản lý")) == ["quản", "lý"]


def test_shingles_and_short_input():
    assert rr.shingles(["a", "b", "c", "d"]) == frozenset({"a b c", "b c d"})
    assert rr.shingles(["a", "b"]) == frozenset()


# ------------------------------------------------------------------ độ giống, phân loại
def test_jaccard_identical_disjoint_partial_and_empty():
    a = frozenset({"x", "y", "z"})
    assert rr.jaccard(a, a) == 1.0
    assert rr.jaccard(a, frozenset({"p", "q"})) == 0.0
    assert rr.jaccard(a, frozenset({"x", "y", "w"})) == pytest.approx(2 / 4)
    assert rr.jaccard(frozenset(), frozenset()) == 0.0


@pytest.mark.parametrize("score,expected", [
    (1.0, rr.CLASS_SAME), (0.80, rr.CLASS_SAME),
    (0.79, rr.CLASS_SIMILAR), (0.50, rr.CLASS_SIMILAR),
    (0.49, rr.CLASS_DIFFERENT), (0.20, rr.CLASS_DIFFERENT),
    (0.19, rr.CLASS_SUSPECT), (0.0, rr.CLASS_SUSPECT),
])
def test_classify_default_thresholds(score, expected):
    assert rr.classify(score) == expected


def test_classify_custom_threshold():
    assert rr.classify(0.25, suspect_below=0.3) == rr.CLASS_SUSPECT
    assert rr.classify(0.25, suspect_below=0.2) == rr.CLASS_DIFFERENT


def test_site_of_url():
    assert rr.site_of_url("https://www.topcv.vn/viec-lam/x") == "topcv.vn"
    assert rr.site_of_url("manual://abc") == "abc"
    assert rr.site_of_url("") == "?"
    assert rr.site_of_url(None) == "?"


# ------------------------------------------------------------------ dựng kết quả theo job
def test_same_listing_reposted_is_same_and_perks_do_not_matter():
    rows = [row(log="l1", text=raw(BODY_A, "", PERKS)),
            row(log="l2", url="https://www.topcv.vn/b", day="2026-09-10", text=raw(BODY_A, "", "quyền lợi khác hẳn " * 6))]
    (res,) = rr.build_results(rows)
    assert res.klass == rr.CLASS_SAME and res.score == 1.0 and len(res.pairs) == 1


def test_different_positions_under_one_job_are_flagged():
    rows = [row(log="l1", text=raw(BODY_A, "", PERKS)),
            row(log="l2", url="https://www.topcv.vn/b", text=raw(BODY_B, "", PERKS))]
    (res,) = rr.build_results(rows)
    assert res.klass == rr.CLASS_SUSPECT and res.score == 0.0
    assert [s.klass for s in rr.suspects([res])] == [rr.CLASS_SUSPECT]


def test_log_without_enough_content_is_not_comparable_and_job_may_be_unrated():
    rows = [row(log="l1", text=raw(BODY_A)), row(log="l2", url="https://www.topcv.vn/b", text=None)]
    (res,) = rr.build_results(rows)
    assert res.klass == rr.CLASS_NOT_COMPARABLE and res.pairs == [] and res.n_comparable_logs == 1
    assert res.worst is None and res.score is None


def test_perks_only_log_is_not_compared_against_a_role_text():
    rows = [row(log="l1", text=raw(BODY_A)), row(log="l2", url="https://www.topcv.vn/b", text=raw(perks=PERKS))]
    (res,) = rr.build_results(rows)
    assert res.klass == rr.CLASS_NOT_COMPARABLE


def test_worst_pair_decides_and_pairs_are_sorted_low_first():
    rows = [row(log="l1", text=raw(BODY_A)),
            row(log="l2", url="https://www.topcv.vn/b", text=raw(BODY_A)),
            row(log="l3", url="https://www.topcv.vn/c", text=raw(BODY_B))]
    (res,) = rr.build_results(rows)
    assert len(res.pairs) == 3
    assert res.pairs[0].similarity <= res.pairs[1].similarity <= res.pairs[2].similarity
    assert res.score == 0.0 and res.klass == rr.CLASS_SUSPECT


def test_cross_site_and_merge_origin_flags():
    rows = [row(log="l1", url="https://www.topcv.vn/a", text=raw(BODY_A)),
            row(log="l2", url="https://vietnamworks.com/b", text=raw(BODY_B))]
    moved = {"l2": {"donor_job_id": "donor123456", "keeper_job_id": "j1", "merged_at": None}}
    (res,) = rr.build_results(rows, moved)
    p = res.worst
    assert not p.same_site and p.involves_merge
    assert {p.a.origin, p.b.origin} == {rr.ORIGIN_PIPELINE, rr.ORIGIN_MERGE}
    assert "donor12" in rr._origin_label(p.b if p.b.origin == rr.ORIGIN_MERGE else p.a)


def test_more_logs_than_the_cap_only_the_oldest_are_compared():
    rows = [row(log=f"l{i:02d}", url=f"https://www.topcv.vn/{i}", text=raw(BODY_A)) for i in range(rr.MAX_LOGS_PER_JOB + 3)]
    (res,) = rr.build_results(rows)
    assert res.n_logs == rr.MAX_LOGS_PER_JOB + 3 and len(res.logs) == rr.MAX_LOGS_PER_JOB and res.truncated


def test_jobs_are_grouped_in_input_order():
    rows = [row(job="j1", log="a1", text=raw(BODY_A)), row(job="j1", log="a2", url="https://x.vn/2", text=raw(BODY_A)),
            row(job="j2", log="b1", text=raw(BODY_B)), row(job="j2", log="b2", url="https://x.vn/3", text=raw(BODY_B))]
    assert [r.job_id for r in rr.build_results(rows)] == ["j1", "j2"]


def test_reclassify_changes_labels_without_recomputing_scores():
    rows = [row(log="l1", text=raw(BODY_A + " " + BODY_B)), row(log="l2", url="https://x.vn/2", text=raw(BODY_A))]
    results = rr.build_results(rows)
    score = results[0].score
    assert 0.2 < score < 0.5
    rr.reclassify(results, 0.5)
    assert results[0].klass == rr.CLASS_SUSPECT and results[0].score == score


# ------------------------------------------------------------------ tổng hợp, histogram, CSV
def _mixed_results():
    rows = [
        row(job="same", log="s1", text=raw(BODY_A)), row(job="same", log="s2", url="https://www.topcv.vn/2", text=raw(BODY_A)),
        row(job="bad", log="b1", text=raw(BODY_A)), row(job="bad", log="b2", url="https://vietnamworks.com/2", text=raw(BODY_B)),
        row(job="thin", log="t1", text=raw(BODY_A)), row(job="thin", log="t2", url="https://www.topcv.vn/3", text=None),
    ]
    moved = {"b2": {"donor_job_id": "d" * 8, "keeper_job_id": "bad", "merged_at": None}}
    return rr.build_results(rows, moved)


def test_summary_counts_and_splits():
    s = rr.summarize(_mixed_results(), total_jobs=50, merge_moved=1, merge_dropped=2)
    assert (s.total_jobs, s.n_multi, s.n_comparable, s.n_not_comparable) == (50, 3, 2, 1)
    assert s.by_class[rr.CLASS_SAME] == 1 and s.by_class[rr.CLASS_SUSPECT] == 1
    assert s.split_same_site == (1, 0) and s.split_cross_site == (1, 1)
    assert s.split_with_merge == (1, 1) and s.split_without_merge == (1, 0)
    assert s.n_logs_unusable == 1 and s.merge_logs_moved == 1 and s.merge_logs_dropped == 2


def test_histogram_places_one_in_the_last_bin_and_skips_unrated():
    h = rr.histogram(_mixed_results())
    assert len(h) == 10 and h[0] == 1 and h[9] == 1 and sum(h) == 2


def test_csv_has_header_one_row_per_pair_and_worst_job_first():
    buf = io.StringIO()
    n = rr.write_csv(_mixed_results(), buf)
    lines = buf.getvalue().strip().splitlines()
    assert n == 2 and len(lines) == 3
    assert lines[0].startswith("job_id,cong_ty") and lines[1].startswith("bad,") and lines[2].startswith("same,")
    assert ",nghi gộp nhầm," in lines[1] and "merge-duplicates" in lines[1]


def test_export_csv_writes_utf8_bom(tmp_path):
    path = tmp_path / "gop.csv"
    rr.export_csv(_mixed_results(), str(path))
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")


def test_print_report_runs_and_shows_the_suspect(capsys):
    results = _mixed_results()
    s = rr.summarize(results, total_jobs=50)
    rr.print_report(s, results, show=5, suspect_below=0.2)
    out = capsys.readouterr().out
    assert "nghi gộp nhầm" in out and "Tỷ lệ nghi gộp nhầm: 1 / 2 = 50.0%" in out and "Công ty A" in out


def test_print_report_show_zero_hides_details(capsys):
    results = _mixed_results()
    rr.print_report(rr.summarize(results, total_jobs=1), results, show=0, suspect_below=0.2)
    assert "--- Job nghi gộp nhầm" not in capsys.readouterr().out


def test_print_report_with_nothing_to_compare_does_not_divide_by_zero(capsys):
    rr.print_report(rr.summarize([], total_jobs=0), [], show=5, suspect_below=0.2)
    assert "0 / 0 = —" in capsys.readouterr().out


# ------------------------------------------------------------------ CLI
@pytest.mark.parametrize("show,threshold", [(-1, 0.2), (5, 0.0), (5, 0.6), (5, -0.1)])
def test_run_cli_rejects_bad_arguments_before_touching_the_db(show, threshold, monkeypatch):
    def boom():
        raise AssertionError("không được mở kết nối khi tham số sai")
    monkeypatch.setattr(rr.db, "get_connection", boom)
    assert rr.run_cli(Namespace(show=show, csv=None, threshold=threshold)) == 1
