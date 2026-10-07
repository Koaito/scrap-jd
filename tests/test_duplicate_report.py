"""
Logic THUẦN của báo cáo job nghi trùng (duplicate_report.py, Phần 3a) — không cần DB. Phần
chạy SQL thật nằm ở tests/test_pg_duplicate_report.py. Các nhóm mẫu dựa trên dữ liệu thật đã
xem khi thiết kế (đăng lại cùng nguồn, chéo nguồn TopCV/VietnamWorks, nhóm khác tỉnh).
"""
import csv
import io
import re
from datetime import date, datetime
from pathlib import Path

import duplicate_report as dr
from duplicate_report import CONF_HIGH, CONF_LOW, CONF_REVIEW, TIER_LEVEL, TIER_PROVINCE, TIER_STRICT

ROOT = Path(__file__).resolve().parent.parent


def _row(job_id, *, company="c1", title="Business Analyst", level="Junior", province=1, status="OPEN",
         created=datetime(2026, 9, 1), deadline=date(2026, 10, 1), url=None, log_urls=None,
         editor=False, notes=False, apps=0, saved=0, contacts=0, h=None, company_name="Công ty A"):
    return {
        "job_id": job_id, "company_id": company, "company_name": company_name, "company_active": True,
        "job_title": title, "norm_title": " ".join(title.lower().split()), "level_code": level,
        "province_id": province, "province_name": f"T{province}" if province else None,
        "job_status": status, "created_at": created, "deadline": deadline, "salary_min": None,
        "salary_max": None, "source_url": url if url is not None else f"https://www.topcv.vn/{job_id}",
        "dedup_key": h or f"k-{company}-{' '.join(title.lower().split())}-{province}", "has_editor": editor, "has_notes": notes,
        "log_urls": log_urls or [], "n_applications": apps, "n_saved": saved, "n_contact_links": contacts,
    }


# ------------------------------------------------------------------ helper nhỏ
def test_site_of_url_strips_www_and_handles_missing():
    assert dr.site_of_url("https://www.vietnamworks.com/abc-jv") == "vietnamworks.com"
    assert dr.site_of_url("https://careerviet.vn/vi/tim-viec-lam/x.35C8.html") == "careerviet.vn"
    assert dr.site_of_url(None) == "?"
    assert dr.site_of_url("") == "?"


def test_protection_labels_and_is_protected():
    plain = _row("a")
    assert dr.protection_labels(plain) == [] and not dr.is_protected(plain)
    full = _row("b", editor=True, notes=True, apps=2, saved=1, contacts=3)
    assert dr.protection_labels(full) == ["người sửa", "ghi chú", "ứng tuyển 2", "đã lưu 1", "liên hệ 3"]
    assert dr.is_protected(full)
    assert dr.is_protected(_row("c", saved=1))


# ------------------------------------------------------------------ tầng
def test_classify_tier():
    assert dr.classify_tier([_row("a"), _row("b")]) == TIER_STRICT
    assert dr.classify_tier([_row("a", level="Junior"), _row("b", level="Middle")]) == TIER_LEVEL
    assert dr.classify_tier([_row("a", province=1), _row("b", province=2)]) == TIER_PROVINCE
    # Khác tỉnh thắng khác level (tin chi nhánh khác nhau quan trọng hơn).
    assert dr.classify_tier([_row("a", province=1, level="Junior"),
                             _row("b", province=2, level="Senior")]) == TIER_PROVINCE
    # Tỉnh trống là một giá trị: trống vs có tỉnh = khác tỉnh; cả hai trống = cùng tỉnh.
    assert dr.classify_tier([_row("a", province=None), _row("b", province=None)]) == TIER_STRICT
    assert dr.classify_tier([_row("a", province=None), _row("b", province=3)]) == TIER_PROVINCE


# ------------------------------------------------------------------ URL chung
def test_shared_url_detected_via_source_url_and_log():
    assert not dr.has_shared_url([_row("a"), _row("b")])
    assert dr.has_shared_url([_row("a", url="https://x/1"), _row("b", url="https://x/1")])
    # URL trong log của job này trùng source_url của job kia.
    assert dr.has_shared_url([_row("a", url="https://x/1"),
                              _row("b", url="https://x/2", log_urls=["https://x/1"])])
    # Hai URL log giống nhau trong CÙNG một job không tính là chung.
    assert not dr.has_shared_url([_row("a", url="https://x/1", log_urls=["https://x/1"]), _row("b")])


# ------------------------------------------------------------------ độ chắc
def test_confidence_rules():
    assert dr.confidence(TIER_PROVINCE, 1, False) == CONF_LOW
    assert dr.confidence(TIER_PROVINCE, 1, True) == CONF_REVIEW
    assert dr.confidence(TIER_STRICT, 1, True) == CONF_HIGH
    assert dr.confidence(TIER_STRICT, 2, True) == CONF_HIGH   # chung URL: cùng một tin dù cùng mở
    assert dr.confidence(TIER_LEVEL, 0, False) == CONF_REVIEW
    assert dr.confidence(TIER_LEVEL, 1, True) == CONF_HIGH
    assert dr.confidence(TIER_STRICT, 0, False) == CONF_HIGH   # tất cả đã đóng
    assert dr.confidence(TIER_STRICT, 1, False) == CONF_HIGH   # đăng lại điển hình
    assert dr.confidence(TIER_STRICT, 2, False) == CONF_REVIEW  # hai tin cùng mở


# ------------------------------------------------------------------ đề xuất job giữ
def test_keeper_prefers_protected_over_open():
    old_closed = _row("old", status="CLOSED", created=datetime(2026, 8, 1), apps=1)
    new_open = _row("new", status="OPEN", created=datetime(2026, 9, 1))
    keeper, why = dr.propose_keeper([new_open, old_closed])
    assert keeper == "old" and "bảo vệ" in why


def test_keeper_prefers_open_then_later_deadline_then_older():
    closed = _row("closed", status="CLOSED", created=datetime(2026, 8, 1))
    open_ = _row("open", status="OPEN", created=datetime(2026, 9, 1))
    assert dr.propose_keeper([closed, open_]) == ("open", "đang OPEN")

    a = _row("a", deadline=date(2026, 10, 1), created=datetime(2026, 9, 1))
    b = _row("b", deadline=date(2026, 11, 1), created=datetime(2026, 9, 2))
    assert dr.propose_keeper([a, b]) == ("b", "hạn nộp muộn hơn")

    c = _row("c", deadline=date(2026, 10, 1), created=datetime(2026, 9, 5))
    d = _row("d", deadline=date(2026, 10, 1), created=datetime(2026, 9, 2))
    assert dr.propose_keeper([c, d]) == ("d", "tạo sớm hơn")


def test_keeper_deadline_missing_is_last():
    no_deadline = _row("n", deadline=None, created=datetime(2026, 8, 1))
    with_deadline = _row("w", deadline=date(2026, 9, 1), created=datetime(2026, 9, 1))
    assert dr.propose_keeper([no_deadline, with_deadline])[0] == "w"


def test_keeper_is_deterministic_regardless_of_input_order():
    rows = [_row("x", created=datetime(2026, 9, 1)), _row("y", created=datetime(2026, 9, 1))]
    assert dr.propose_keeper(rows)[0] == dr.propose_keeper(list(reversed(rows)))[0] == "x"


# ------------------------------------------------------------------ nhóm mẫu từ dữ liệu thật
def test_group_repost_chain_same_site_one_open():
    # Kiểu "Lead Data Engineer / SICIX": 3 tin đăng lại cùng TopCV, hai cũ đã đóng, một đang mở.
    g = dr.analyze_group([
        _row("1", status="CLOSED", created=datetime(2026, 8, 26), url="https://www.topcv.vn/viec-lam/x/1.html"),
        _row("2", status="CLOSED", created=datetime(2026, 9, 8), url="https://www.topcv.vn/viec-lam/x/2.html"),
        _row("3", status="OPEN", created=datetime(2026, 9, 30), url="https://www.topcv.vn/viec-lam/x/3.html"),
    ])
    assert (g.tier, g.confidence, g.open_count, g.cross_source, g.shared_url) == (TIER_STRICT, CONF_HIGH, 1, False, False)
    assert g.keeper_id == "3" and g.keeper_why == "đang OPEN" and g.size == 3
    assert any("cùng nguồn" in r for r in g.reasons)


def test_group_cross_source_two_open_needs_review():
    # Kiểu "Business Analyst (Chinese) / MBBank": TopCV + VietnamWorks + TopCV, hai tin đang mở.
    g = dr.analyze_group([
        _row("1", status="CLOSED", created=datetime(2026, 9, 8), url="https://www.topcv.vn/a-j22"),
        _row("2", status="OPEN", created=datetime(2026, 9, 28), url="https://www.vietnamworks.com/a-jv"),
        _row("3", status="OPEN", created=datetime(2026, 9, 30), url="https://www.topcv.vn/a-j2315"),
    ])
    assert g.confidence == CONF_REVIEW and g.open_count == 2 and g.cross_source
    assert any("khác nguồn" in r for r in g.reasons)
    assert any("2 tin cùng đang OPEN" in r for r in g.reasons)


def test_group_different_province_has_no_proposal():
    g = dr.analyze_group([_row("1", province=1), _row("2", province=2)])
    assert g.tier == TIER_PROVINCE and g.confidence == CONF_LOW
    assert g.keeper_id is None and g.keeper_why == ""


def test_group_needs_manual_pick_when_two_protected():
    g = dr.analyze_group([_row("1", apps=1), _row("2", saved=2), _row("3")])
    assert g.protected_count == 2 and g.needs_manual_pick
    assert not dr.analyze_group([_row("1", apps=1), _row("2")]).needs_manual_pick


# ------------------------------------------------------------------ build_groups / thống kê
def test_build_groups_only_groups_with_two_or_more_and_never_mixes_companies():
    rows = [
        _row("a1", company="c1", title="Data Analyst"), _row("a2", company="c1", title="Data Analyst"),
        _row("b1", company="c2", title="Data Analyst"),                       # công ty khác, một mình
        _row("c1", company="c1", title="Data Engineer"),                      # cùng công ty, tiêu đề khác
    ]
    groups = dr.build_groups(rows)
    assert len(groups) == 1 and {m["job_id"] for m in groups[0].members} == {"a1", "a2"}


def test_build_groups_sorted_by_confidence_then_tier_then_size():
    rows = [
        # nhóm thấp (khác tỉnh)
        _row("p1", company="p", province=1, title="X"), _row("p2", company="p", province=2, title="X"),
        # nhóm cần xem (khác level)
        _row("l1", company="l", level="Junior", title="X"), _row("l2", company="l", level="Middle", title="X"),
        # nhóm cao, 3 job (đăng lại)
        _row("h1", company="h", status="CLOSED", title="X"), _row("h2", company="h", status="CLOSED", title="X"),
        _row("h3", company="h", status="OPEN", title="X"),
    ]
    assert [g.confidence for g in dr.build_groups(rows)] == [CONF_HIGH, CONF_REVIEW, CONF_LOW]


def test_count_key_groups_matches_view_definition():
    rows = [_row("a", h="H1"), _row("b", h="H1"), _row("c", h="H2"), _row("d", h="H2"), _row("e", h="H2"),
            _row("f", h="H3")]
    assert dr.count_key_groups(rows) == 2  # H1 và H2; H3 chỉ một job (khác tỉnh)


def test_default_key_ignores_level_but_not_province():
    """Khoá mặc định của helper phản ánh dedup_key: bỏ level, phân biệt tỉnh."""
    rows = [_row("a", level="Junior"), _row("b", level="Senior"), _row("c", level="Junior", province=2)]
    assert rows[0]["dedup_key"] == rows[1]["dedup_key"] != rows[2]["dedup_key"]
    assert dr.count_key_groups(rows) == 1


def test_summary_counts():
    groups = dr.build_groups([
        _row("a1", company="a", status="CLOSED", title="X", apps=1), _row("a2", company="a", title="X"),
        _row("b1", company="b", province=1, title="X"), _row("b2", company="b", province=2, title="X"),
    ])
    s = dr.Summary(groups, total_jobs=10, jobs_in_groups=4, key_groups=0, view_groups=0)
    assert s.groups == 2 and s.extra_jobs == 2
    assert s.by_tier[TIER_STRICT] == 1 and s.by_tier[TIER_PROVINCE] == 1
    assert (s.single_open, s.multi_open) == (1, 1)  # a: 1 OPEN + 1 CLOSED; b: cả hai OPEN
    assert s.with_protected == 1 and s.need_manual == 0 and s.no_proposal == 1
    assert s.by_size[2] == 2


# ------------------------------------------------------------------ in báo cáo / CSV
def test_print_report_contains_key_sections_and_tolerates_missing_fields(capsys):
    sparse = _row("zz", status=None, level=None, province=None, deadline=None)
    sparse["job_status"] = None
    groups = dr.build_groups([_row("a1", company="a", title="X"), _row("a2", company="a", title="X"),
                              dict(sparse, company_id="s", norm_title="y"), dict(sparse, company_id="s", norm_title="y",
                                                                                 job_id="zz2")])
    s = dr.Summary(groups, total_jobs=100, jobs_in_groups=4, key_groups=2, view_groups=2)
    dr.print_report(s, groups, show=5)
    out = capsys.readouterr().out
    assert "BÁO CÁO JOB NGHI TRÙNG" in out and "khớp" in out
    assert "Theo tầng:" in out and "Theo độ chắc" in out and "Dữ liệu cần bảo vệ" in out
    assert "CHỈ ĐỌC" in out and "Đề xuất giữ" in out and "chưa phải luật đã chốt" in out


def test_print_report_flags_view_mismatch_and_show_zero_prints_no_groups(capsys):
    groups = dr.build_groups([_row("a1", title="X"), _row("a2", title="X")])
    s = dr.Summary(groups, total_jobs=2, jobs_in_groups=2, key_groups=1, view_groups=5)
    dr.print_report(s, groups, show=0)
    out = capsys.readouterr().out
    assert "LỆCH" in out and "Độ chắc \"cao\"" not in out and "tầng: strict" not in out


def test_write_csv_one_row_per_job_with_marker_and_utf8_header():
    groups = dr.build_groups([
        _row("a1", status="CLOSED", created=datetime(2026, 8, 1), title="Kỹ sư dữ liệu", company_name="Công ty Ánh Dương"),
        _row("a2", status="OPEN", created=datetime(2026, 9, 1), title="Kỹ sư dữ liệu", company_name="Công ty Ánh Dương"),
    ])
    buf = io.StringIO()
    n = dr.write_csv(groups, buf)
    rows = list(csv.reader(io.StringIO(buf.getvalue())))
    assert n == 2 and tuple(rows[0]) == dr._CSV_HEADER and len(rows) == 3
    keep_idx = dr._CSV_HEADER.index("de_xuat_giu")
    marks = {r[dr._CSV_HEADER.index("job_id")]: r[keep_idx] for r in rows[1:]}
    assert marks == {"a1": "", "a2": "x"}
    assert rows[1][dr._CSV_HEADER.index("cong_ty")] == "Công ty Ánh Dương"


def test_export_csv_writes_utf8_with_bom(tmp_path):
    groups = dr.build_groups([_row("a1", title="X"), _row("a2", title="X")])
    path = tmp_path / "trung.csv"
    assert dr.export_csv(groups, str(path)) == 2
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")


# ------------------------------------------------------------------ run / run_cli
class _FakeConn:
    def __init__(self):
        self.rollbacks = 0
        self.commits = 0

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1


def test_run_reads_only_and_never_commits(monkeypatch, capsys, tmp_path):
    rows = [_row("a1", company="a", title="X", h="H"), _row("a2", company="a", title="X", h="H")]
    monkeypatch.setattr(dr.db, "list_duplicate_job_rows", lambda conn: rows)
    monkeypatch.setattr(dr.db, "count_jobs", lambda conn: 50)
    monkeypatch.setattr(dr.db, "count_duplicate_job_groups", lambda conn: 1)
    conn = _FakeConn()
    path = tmp_path / "out.csv"
    assert dr.run(conn, show=3, csv_path=str(path)) == 0
    out = capsys.readouterr().out
    assert "Tổng job trong DB: 50" in out and "khớp" in out and "Đã xuất 2 dòng job" in out
    assert conn.commits == 0 and conn.rollbacks >= 1
    assert path.exists()


def test_run_with_no_duplicates_prints_zero_groups(monkeypatch, capsys):
    monkeypatch.setattr(dr.db, "list_duplicate_job_rows", lambda conn: [])
    monkeypatch.setattr(dr.db, "count_jobs", lambda conn: 5)
    monkeypatch.setattr(dr.db, "count_duplicate_job_groups", lambda conn: 0)
    assert dr.run(_FakeConn()) == 0
    assert "Nhóm nghi trùng (cùng công ty + tiêu đề chuẩn hoá): 0" in capsys.readouterr().out


def test_run_cli_rejects_negative_show(capsys):
    class Args:
        show = -1
        csv = None

    assert dr.run_cli(Args()) == 1
    assert "--show" in capsys.readouterr().out


# ------------------------------------------------------------------ chỉ đọc (kiểm tra mã nguồn)
def test_report_code_is_read_only():
    """Báo cáo 3a không được chứa câu ghi hay commit: đây là lệnh chỉ đọc."""
    write_sql = re.compile(r"\b(INSERT|UPDATE|DELETE|TRUNCATE|ALTER|DROP|CREATE)\b")
    for rel in ("duplicate_report.py", "db/job_duplicates.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        code = "\n".join(line.split("#", 1)[0] for line in text.splitlines())
        assert not write_sql.search(code), f"{rel} có câu SQL ghi"
        assert ".commit(" not in code, f"{rel} có commit"
