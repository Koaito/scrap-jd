"""
Đợt B3: khoá QUY TẮC TRANSACTION của pipeline — "1 job = 1 transaction".
(Quy tắc được mô tả trong docstring pipeline._process_jobs.)

KHÔNG cần database, KHÔNG cần internet. Dùng conn giả đếm "ghi chưa commit":
mọi hàm db.* GHI đánh dấu conn bẩn, commit() làm sạch, rollback() cũng làm sạch.
Sau MỖI job (heartbeat chạy trong `finally`) conn phải sạch, nếu không nghĩa là
có nhánh ghi DB mà quên commit — dữ liệu dở sẽ bị rollback của job sau xoá mất
hoặc bị commit lẫn vào job sau.
"""

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import db as real_db
import pipeline
from adapters.base import BaseAdapter, CrawlBlockedError
from field_stats import EmptyFieldCounter
from models import RawJobRecord
from pipeline_stats import PipelineStats

# Phân loại MỌI hàm db.* mà pipeline.py gọi. Thêm lời gọi db.* mới vào
# pipeline.py thì phải thêm vào 1 trong 2 tập này (test_every_db_call_is_classified
# sẽ báo đỏ nếu quên) — đó là lúc phải nghĩ "hàm này có ghi không, ai commit?".
READ_FUNCS = {
    "get_job_probe_by_source_url", "job_needs_detail_enrichment",
    "find_company_probe", "probe_needs_enrichment", "get_level_id",
    "find_manual_job_duplicate", "find_jobs_by_source_url_regex",
}
WRITE_FUNCS = {
    "update_job_fields", "mark_source_detail_checked", "get_or_create_province",
    "get_or_create_company_by_profile", "update_company_profile",
    "link_repost_source", "extend_job_deadline", "insert_job",
    "update_job_from_recrawl",
}

FULL_DETAIL = {
    "work_type": "Toàn thời gian", "deadline_text": "05/09/2026",
    "job_description": "mô tả", "requirements": "yêu cầu", "perks": "",
    "required_skills": ["SQL"],
}
COMPLETE_PROBE = ("job-1", "Toàn thời gian", "2026-09-05", {"a": 1}, None)
NEEDS_PATCH_PROBE = ("job-2", None, None, None, None)


def test_every_db_call_is_classified():
    source = Path(pipeline.__file__).read_text(encoding="utf-8")
    used = set(re.findall(r"\bdb\.([A-Za-z_]\w*)\(", source))
    unclassified = used - READ_FUNCS - WRITE_FUNCS
    assert not unclassified, (
        f"pipeline.py gọi hàm db chưa phân loại đọc/ghi: {sorted(unclassified)}. "
        "Thêm vào READ_FUNCS/WRITE_FUNCS và đảm bảo nhánh có ghi tự commit."
    )
    stale = (READ_FUNCS | WRITE_FUNCS) - used
    assert not stale, f"Phân loại thừa, pipeline không còn gọi: {sorted(stale)}"


class FakeConn:
    def __init__(self):
        self.pending = 0
        self.commits = 0
        self.rollbacks = 0

    def commit(self):
        self.commits += 1
        self.pending = 0

    def rollback(self):
        self.rollbacks += 1
        self.pending = 0


class FakeDB:
    """Thay module db: hàm GHI làm conn bẩn; hàm ĐỌC trả giá trị cấu hình."""

    def __init__(self, probe=None, duplicate=None, needs_company_profile=False,
                 fail_on=None, code_rows=None, update_returns=True):
        self._probe, self._duplicate = probe, duplicate
        self._needs_company_profile = needs_company_profile
        self._fail_on = fail_on
        self._code_rows, self._update_returns = code_rows or [], update_returns

    def _write(self, name, conn, ret=None):
        conn.pending += 1  # ghi trước, lỗi sau: mô phỏng ghi dở rồi mới hỏng
        if self._fail_on == name:
            raise RuntimeError(f"boom in {name}")
        return ret

    # --- đọc
    def get_job_probe_by_source_url(self, conn, url):
        return self._probe

    def job_needs_detail_enrichment(self, probe):
        return real_db.job_needs_detail_enrichment(probe)

    def find_company_probe(self, conn, name):
        return None

    def probe_needs_enrichment(self, probe):
        return self._needs_company_profile

    def get_level_id(self, conn, code):
        return 3

    def find_manual_job_duplicate(self, conn, **kw):
        return self._duplicate

    def find_jobs_by_source_url_regex(self, conn, **kw):
        return list(self._code_rows)

    # --- ghi
    def update_job_fields(self, conn, *a, **k):
        return self._write("update_job_fields", conn)

    def mark_source_detail_checked(self, conn, *a, **k):
        return self._write("mark_source_detail_checked", conn)

    def get_or_create_province(self, conn, *a, **k):
        return self._write("get_or_create_province", conn, 7)

    def get_or_create_company_by_profile(self, conn, *a, **k):
        return self._write("get_or_create_company_by_profile", conn, "company-1")

    def update_company_profile(self, conn, *a, **k):
        return self._write("update_company_profile", conn)

    def link_repost_source(self, conn, *a, **k):
        return self._write("link_repost_source", conn)

    def extend_job_deadline(self, conn, *a, **k):
        return self._write("extend_job_deadline", conn, True)

    def insert_job(self, conn, **k):
        return self._write("insert_job", conn)

    def update_job_from_recrawl(self, conn, *a, **k):
        return self._write("update_job_from_recrawl", conn, self._update_returns)


class OneJobAdapter(BaseAdapter):
    source_name = "Fake"

    def __init__(self, raw, detail=FULL_DETAIL, detail_error=None, company_profile=None):
        super().__init__()
        self.raw, self.detail = raw, detail
        self.detail_error, self.company_profile = detail_error, company_profile

    def fetch_jobs(self, category_key, max_pages):
        yield self.raw

    def fetch_job_full_detail(self, source_url):
        if self.detail_error:
            raise self.detail_error
        return dict(self.detail) if self.detail is not None else None

    def fetch_company_profile(self, url):
        return self.company_profile


def _raw(**kw):
    base = dict(job_title="Data Analyst", company_name="ACME", source_url="u-1",
                source_name="Fake", salary_text="10-15 triệu", province_text="Hà Nội",
                experience_text="2 năm")
    base.update(kw)
    return RawJobRecord(**base)


class CodeAdapter(OneJobAdapter):
    """Giống VietnamWorks: có mã job ổn định trong URL (hook job_code_url_regex)."""

    def job_code_url_regex(self, source_url):
        return "-1-jv([/?#]|$)"


# (job_id, job_title, job_status, updated_by, created_at) — đúng thứ tự cột
# db.find_jobs_by_source_url_regex trả về.
def _code_row(title="Data Analyst", status="OPEN", updated_by=None, job_id="old-job"):
    return (job_id, title, status, updated_by, "2026-01-01")


def _run(monkeypatch, adapter, fake_db):
    """Chạy 1 job qua _process_jobs. Trả (conn, stats, pending_sau_job)."""
    conn, stats = FakeConn(), PipelineStats()
    monkeypatch.setattr(pipeline, "db", fake_db)
    seen = {}

    def heartbeat():  # chạy trong `finally` ngay sau job, sau mọi commit/rollback
        seen["pending"] = conn.pending

    try:
        pipeline._process_jobs(adapter, conn, "da", 1, None, stats, EmptyFieldCounter(), heartbeat)
    except CrawlBlockedError:
        pass
    return conn, stats, seen["pending"]


# (tên kịch bản, adapter, FakeDB, số commit mong đợi, số rollback mong đợi)
# Mọi kịch bản có đúng 1 rollback: lần đóng transaction đọc sau câu probe ở đầu job
# (pipeline._release_read_transaction), không phải rollback do lỗi.
def _scenarios():
    return [
        ("job mới -> insert", OneJobAdapter(_raw()), FakeDB(), 1, 1),
        ("job mới + enrich công ty", OneJobAdapter(_raw(company_url="https://x/c"),
         company_profile={"tax_id": "123", "description": "d"}),
         FakeDB(needs_company_profile=True), 1, 1),
        ("tin đăng lại", OneJobAdapter(_raw()), FakeDB(duplicate="old-job"), 1, 1),
        ("job cũ cần vá, fetch OK", OneJobAdapter(_raw()), FakeDB(probe=NEEDS_PATCH_PROBE), 1, 1),
        ("job cũ đã đủ field", OneJobAdapter(_raw()), FakeDB(probe=COMPLETE_PROBE), 0, 1),
        ("job cũ cần vá, fetch lỗi", OneJobAdapter(_raw(), detail=None),
         FakeDB(probe=NEEDS_PATCH_PROBE), 0, 1),
        ("nhà tuyển dụng ẩn danh", OneJobAdapter(_raw(company_name="Vietnamworks' Client")),
         FakeDB(), 0, 1),
        ("job mới, fetch chi tiết lỗi", OneJobAdapter(_raw(), detail=None), FakeDB(), 0, 1),
        # --- cùng mã job (adapter có hook job_code_url_regex). Nhánh cập nhật/chỉ ghi
        # URL dùng chung 1 transaction với câu tra cứu nên chỉ có 1 rollback (probe);
        # nhánh rơi xuống tạo job mới có thêm 1 rollback đóng transaction đọc của
        # câu tra cứu mã job (trước khi có thể chờ mạng ở bước công ty).
        ("cùng mã job, tiêu đề gần giống -> cập nhật", CodeAdapter(_raw()),
         FakeDB(code_rows=[_code_row()]), 1, 1),
        ("cùng mã job, job đã có người sửa tay -> chỉ ghi URL", CodeAdapter(_raw()),
         FakeDB(code_rows=[_code_row(updated_by="user-1")]), 1, 1),
        ("cùng mã job, job vừa bị sửa/đóng lúc xử lý -> chỉ ghi URL", CodeAdapter(_raw()),
         FakeDB(code_rows=[_code_row()], update_returns=False), 1, 1),
        ("cùng mã job, tiêu đề khác hẳn -> tạo job mới", CodeAdapter(_raw()),
         FakeDB(code_rows=[_code_row(title="Account Manager")]), 1, 2),
        ("cùng mã job nhưng job cũ đã CLOSED -> tạo job mới", CodeAdapter(_raw()),
         FakeDB(code_rows=[_code_row(status="CLOSED")]), 1, 2),
        ("adapter có mã job nhưng chưa có job nào cùng mã -> tạo job mới", CodeAdapter(_raw()),
         FakeDB(code_rows=[]), 1, 2),
    ]


@pytest.mark.parametrize("name,adapter,fake_db,commits,rollbacks", _scenarios(),
                         ids=[s[0] for s in _scenarios()])
def test_each_branch_commits_exactly_as_the_rule_says(monkeypatch, name, adapter, fake_db,
                                                      commits, rollbacks):
    conn, stats, pending = _run(monkeypatch, adapter, fake_db)
    assert pending == 0, f"[{name}] còn ghi DB chưa commit sau khi xử lý xong job"
    assert conn.commits == commits, f"[{name}] số lần commit"
    assert conn.rollbacks == rollbacks, f"[{name}] số lần rollback"
    assert stats.errors == 0


@pytest.mark.parametrize("fail_on,probe,duplicate", [
    ("get_or_create_province", None, None),
    ("get_or_create_company_by_profile", None, None),
    ("insert_job", None, None),
    ("link_repost_source", None, "old-job"),
    ("extend_job_deadline", None, "old-job"),
    ("update_job_fields", NEEDS_PATCH_PROBE, None),
    ("mark_source_detail_checked", NEEDS_PATCH_PROBE, None),
])
def test_error_in_any_write_rolls_back_everything_uncommitted(monkeypatch, fail_on, probe, duplicate):
    conn, stats, pending = _run(
        monkeypatch, OneJobAdapter(_raw()),
        FakeDB(probe=probe, duplicate=duplicate, fail_on=fail_on),
    )
    assert stats.errors == 1
    assert conn.rollbacks == 2, "1 lần đóng transaction đọc + 1 lần huỷ phần ghi dở"
    assert conn.commits == 0, "lỗi giữa chừng thì không được commit phần dở"
    assert pending == 0


@pytest.mark.parametrize("fail_on", ["link_repost_source", "update_job_from_recrawl",
                                     "extend_job_deadline"])
def test_error_in_job_code_update_rolls_back_everything_uncommitted(monkeypatch, fail_on):
    conn, stats, pending = _run(
        monkeypatch, CodeAdapter(_raw()),
        FakeDB(code_rows=[_code_row()], fail_on=fail_on),
    )
    assert stats.errors == 1
    assert conn.rollbacks == 2, "1 lần đóng transaction đọc + 1 lần huỷ phần ghi dở"
    assert conn.commits == 0, "lỗi giữa chừng thì không được commit phần dở"
    assert pending == 0
    assert stats.updated_by_job_code == 0, "chưa commit xong thì không được đếm là đã cập nhật"


def test_blocked_mid_job_rolls_back_partial_work_and_does_not_count_as_error(monkeypatch):
    conn, stats, pending = _run(
        monkeypatch, OneJobAdapter(_raw(), detail_error=CrawlBlockedError("bị chặn")), FakeDB(),
    )
    assert conn.rollbacks == 2 and conn.commits == 0
    assert stats.errors == 0
    assert pending == 0


def test_committed_job_is_not_undone_by_error_in_next_job(monkeypatch):
    """Job 1 commit xong; job 2 lỗi -> rollback chỉ huỷ phần của job 2."""
    class TwoJobs(OneJobAdapter):
        def fetch_jobs(self, category_key, max_pages):
            yield _raw(source_url="u-1", job_title="A")
            yield _raw(source_url="u-2", job_title="B")

    class FailsOnSecond(FakeDB):
        def insert_job(self, conn, **k):
            conn.pending += 1  # ghi dở rồi mới hỏng ở job B
            if k["job_title"] == "B":
                raise RuntimeError("boom")

    conn, stats = FakeConn(), PipelineStats()
    monkeypatch.setattr(pipeline, "db", FailsOnSecond())
    pipeline._process_jobs(TwoJobs(_raw()), conn, "da", 1, None, stats, EmptyFieldCounter(), lambda: None)

    assert stats.inserted == 1 and stats.errors == 1
    # 2 lần đóng transaction đọc (mỗi job 1) + 1 lần huỷ phần ghi dở của job B
    assert conn.commits == 1 and conn.rollbacks == 3
    assert conn.pending == 0


# ----------------------------------------------------------------------
# Không để transaction mở lúc chờ mạng ("idle in transaction")
# ----------------------------------------------------------------------
class TxnTrackingConn(FakeConn):
    """Thêm cờ `open`: bật khi có câu đọc/ghi, tắt khi commit/rollback.
    `lost_writes` đếm số lần rollback làm mất ghi chưa commit."""

    def __init__(self):
        super().__init__()
        self.open = False
        self.lost_writes = 0

    def commit(self):
        super().commit()
        self.open = False

    def rollback(self):
        if self.pending:
            self.lost_writes += 1
        super().rollback()
        self.open = False


class TxnTrackingDB(FakeDB):
    def get_job_probe_by_source_url(self, conn, url):
        conn.open = True  # SELECT cũng mở transaction (autocommit=False)
        return self._probe

    def _write(self, name, conn, ret=None):
        conn.open = True
        return super()._write(name, conn, ret)


class NetworkSpyAdapter(OneJobAdapter):
    """Ghi lại trạng thái transaction của conn đúng lúc adapter gọi mạng."""

    def __init__(self, conn, *a, **k):
        super().__init__(*a, **k)
        self.conn, self.open_during_detail = conn, None

    def fetch_job_full_detail(self, source_url):
        self.open_during_detail = self.conn.open
        return super().fetch_job_full_detail(source_url)


@pytest.mark.parametrize("probe", [None, NEEDS_PATCH_PROBE], ids=["job mới", "job cũ cần vá"])
def test_no_transaction_open_while_fetching_job_detail(monkeypatch, probe):
    conn, stats = TxnTrackingConn(), PipelineStats()
    adapter = NetworkSpyAdapter(conn, _raw())
    monkeypatch.setattr(pipeline, "db", TxnTrackingDB(probe=probe))

    pipeline._process_jobs(adapter, conn, "da", 1, None, stats, EmptyFieldCounter(), lambda: None)

    assert adapter.open_during_detail is False, (
        "câu probe đã mở transaction và chưa đóng khi chờ fetch chi tiết -> "
        "kết nối đứng 'idle in transaction'"
    )
    assert conn.lost_writes == 0, "việc đóng transaction đọc không được huỷ ghi chưa commit"
    assert stats.errors == 0


def test_known_url_checker_closes_read_transaction_after_probe(monkeypatch):
    conn = TxnTrackingConn()
    monkeypatch.setattr(pipeline, "db", TxnTrackingDB(probe=COMPLETE_PROBE))

    assert pipeline._make_known_url_checker(conn)("u-1") is True

    assert conn.open is False
    assert conn.rollbacks == 1


def test_job_code_lookup_does_not_leave_a_read_transaction_open_when_nothing_matches(monkeypatch):
    """Tra cứu mã job là câu SELECT nên mở transaction. Không có job nào khớp thì
    pipeline đi tiếp sang bước tỉnh/công ty (có thể chờ mạng): transaction đọc
    phải đã đóng, và việc đóng không được huỷ ghi chưa commit."""
    seen = {}

    class DB(TxnTrackingDB):
        def find_jobs_by_source_url_regex(self, conn, **kw):
            conn.open = True
            return super().find_jobs_by_source_url_regex(conn, **kw)

        def get_or_create_province(self, conn, *a, **k):
            seen["open_before_first_write"] = conn.open  # ghi đầu tiên sau tra cứu
            return super().get_or_create_province(conn, *a, **k)

    conn, stats = TxnTrackingConn(), PipelineStats()
    monkeypatch.setattr(pipeline, "db", DB(code_rows=[_code_row(title="Account Manager")]))

    pipeline._process_jobs(CodeAdapter(_raw()), conn, "da", 1, None, stats, EmptyFieldCounter(),
                           lambda: None)

    assert seen["open_before_first_write"] is False
    assert conn.lost_writes == 0
    assert stats.inserted == 1 and stats.job_code_title_mismatch == 1
