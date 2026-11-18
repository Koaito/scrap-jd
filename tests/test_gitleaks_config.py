"""
Giữ cho việc quét bí mật (gitleaks) không bị nới lỏng dần — KHÔNG cần database, KHÔNG cần internet.

Cấu hình ở .gitleaks.toml, job CI ở .github/workflows/test.yml (job `secret-scan`). Việc dễ làm hỏng nhất theo
thời gian là allowlist: gặp một phát hiện giả, người sau thêm `paths = ['^tests/']` hoặc cho qua cả một rule cho
nhanh, và từ đó khoá thật dán nhầm vào test không còn bị bắt. Các test dưới đây khoá đúng điểm đó. Chúng không chạy
gitleaks (binary chỉ có trong CI), chỉ kiểm hình dạng của cấu hình và của job.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / ".gitleaks.toml"
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"


def _config() -> dict:
    return tomllib.loads(CONFIG.read_text(encoding="utf-8"))


def _secret_scan_job() -> str:
    """Phần văn bản của job `secret-scan` (từ tên job tới hết file; job này nằm cuối workflow)."""
    text = WORKFLOW.read_text(encoding="utf-8")
    m = re.search(r"^  secret-scan:\n(.*)\Z", text, re.S | re.M)
    assert m, "workflow thiếu job secret-scan"
    return m.group(1)


def test_cau_hinh_dung_rule_mac_dinh():
    # Tắt rule mặc định là mất gần hết khả năng phát hiện.
    assert _config()["extend"]["useDefault"] is True


def test_allowlist_chi_la_chuoi_gia_da_biet_trong_dung_mot_file_test():
    cfg = _config()
    # Không có allowlist toàn cục (`[allowlist]`) và không cho qua rule nào nguyên cả rule.
    assert "allowlist" not in cfg
    lists = cfg.get("allowlists", [])
    assert lists, "không còn allowlist nào: nếu đã bỏ chuỗi giả thì xoá luôn test này"
    for al in lists:
        # Phải khớp đồng thời đường dẫn VÀ nội dung; thiếu một trong hai là cho qua quá rộng.
        assert al.get("condition") == "AND", al
        assert not al.get("rules") and not al.get("ruleIDs"), "không cho qua nguyên một rule"
        paths, regexes = al.get("paths"), al.get("regexes")
        assert paths and regexes, al
        for p in paths:
            # Một file cụ thể trong tests/, neo hai đầu, không có ký tự đại diện.
            assert re.fullmatch(r"\^tests/[A-Za-z0-9_]+\\\.py\$", p), f"đường dẫn quá rộng: {p}"
        for r in regexes:
            # Neo hai đầu: chỉ khớp nguyên giá trị bí mật, không khớp một phần của chuỗi dài hơn.
            assert r.startswith("^") and r.endswith("$"), f"regex chưa neo hai đầu: {r}"
            assert ".*" not in r and ".+" not in r, f"regex quá rộng: {r}"
        for p in paths:
            target = ROOT / p.removeprefix("^").replace("\\.", ".").removesuffix("$")
            assert target.is_file(), f"allowlist trỏ vào file không còn tồn tại: {target}"


def test_job_ci_quet_toan_bo_lich_su_va_chan_khi_co_phat_hien():
    job = _secret_scan_job()
    # Checkout đủ lịch sử, nếu không chỉ quét được 1 commit.
    assert re.search(r"fetch-depth:\s*0\b", job)
    cmd = re.search(r"^\s*run: (\./gitleaks git .*)$", job, re.M)
    assert cmd, "không tìm thấy lệnh quét"
    line = cmd.group(1)
    assert '--log-opts="--all"' in line  # mọi nhánh, không chỉ nhánh hiện tại
    assert "--config .gitleaks.toml" in line
    assert "--exit-code 1" in line  # có phát hiện thì job đỏ
    assert "--redact" in line  # log của lượt chạy là công khai


def test_job_ci_ghim_phien_ban_va_kiem_checksum():
    job = _secret_scan_job()
    version = re.search(r'GITLEAKS_VERSION:\s*"(\d+\.\d+\.\d+)"', job)
    sha = re.search(r'GITLEAKS_SHA256:\s*"([0-9a-f]{64})"', job)
    assert version, "phải ghim phiên bản gitleaks, không dùng latest"
    assert sha, "phải có sha256 của bản tải về"
    assert "sha256sum -c" in job
    # Binary được tải từ đúng phiên bản đã ghim (không phải từ đường dẫn latest).
    assert "releases/download/v${GITLEAKS_VERSION}/" in job
    assert "releases/latest" not in job
    # Chỉ đọc nội dung repo, không có quyền ghi.
    assert re.search(r"permissions:\n\s+contents: read\b", job)
