"""
Hợp đồng giữa scrapjd/pipeline.py, PipelineDB (scrapjd/pipeline_db.py), module db thật và Fake dùng chung (B2).

Mục đích: bốn thứ này không được lệch nhau mà không ai biết. Mỗi test dưới đây đỏ ở đúng một kiểu
lệch, thông báo nói rõ phải sửa chỗ nào. Không cần DB hay mạng.
"""
import ast
import inspect
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scrapjd import db as real_db
from scrapjd import pipeline
from scrapjd import pipeline_db
from scrapjd.pipeline_db import PIPELINE_DB_READS, PIPELINE_DB_WRITES, PipelineDB
from pipeline_fakes import make_pipeline_db


def _protocol_methods() -> set:
    return {name for name, member in vars(PipelineDB).items()
            if inspect.isfunction(member) and not name.startswith("_")}


def _db_names_used_by_pipeline() -> set:
    tree = ast.parse(Path(pipeline.__file__).read_text(encoding="utf-8"))
    return {
        node.attr for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
        and node.value.id == "db"
    }


def _shape(func, *, drop_self=False):
    """Chữ ký rút gọn để so: (tên, loại tham số, có giá trị mặc định?, giá trị mặc định).
    Bỏ qua chú thích kiểu: Protocol chú thích gọn hơn hàm thật."""
    params = list(inspect.signature(func).parameters.values())
    if drop_self:
        params = params[1:]
    return [(p.name, p.kind, p.default is not inspect.Parameter.empty,
             None if p.default is inspect.Parameter.empty else p.default) for p in params]


def test_protocol_lists_exactly_the_db_functions_pipeline_calls():
    used, declared = _db_names_used_by_pipeline(), _protocol_methods()
    assert not used - declared, (
        f"pipeline.py gọi hàm db chưa có trong PipelineDB: {sorted(used - declared)}. "
        "Thêm vào pipeline_db.PipelineDB (chữ ký khớp hàm thật), PIPELINE_DB_READS/WRITES và "
        "tests/pipeline_fakes.py."
    )
    assert not declared - used, (
        f"PipelineDB có hàm pipeline.py không còn gọi: {sorted(declared - used)}. Xoá khỏi "
        "pipeline_db.py và tests/pipeline_fakes.py."
    )


def test_every_function_is_classified_as_read_or_write_exactly_once():
    assert not PIPELINE_DB_READS & PIPELINE_DB_WRITES, "Hàm vừa đọc vừa ghi"
    assert PIPELINE_DB_READS | PIPELINE_DB_WRITES == _protocol_methods(), (
        "PIPELINE_DB_READS/WRITES phải phủ đúng các hàm của PipelineDB"
    )


@pytest.mark.parametrize("name", sorted(_protocol_methods()))
def test_protocol_signature_matches_the_real_db_function(name):
    real = _shape(getattr(real_db, name))
    declared = _shape(getattr(PipelineDB, name), drop_self=True)
    assert declared == real, (
        f"PipelineDB.{name} lệch với db.{name} thật. Hàm thật đổi chữ ký thì sửa lại Protocol "
        "(và xem pipeline.py có gọi đúng không)."
    )


def test_fake_has_exactly_the_protocol_functions():
    fake = make_pipeline_db()
    exposed = {name for name in dir(fake) if not name.startswith(("_", "assert_", "call", "mock", "reset"))
               and name not in {"attach_mock", "configure_mock", "method_calls", "side_effect",
                                "return_value"}}
    assert exposed == _protocol_methods()
    with pytest.raises(AttributeError):
        fake.get_job_by_id  # hàm có thật trong db nhưng pipeline không cần
    with pytest.raises(AttributeError):
        fake.some_new_function = lambda *a, **k: None  # spec_set: không gán thêm hàm lạ


def test_fake_rejects_calls_the_real_function_would_reject():
    fake = make_pipeline_db()
    with pytest.raises(TypeError):
        fake.lock_job_dedup_key(object(), company_id="c", job_title="t")  # thiếu province_id
    with pytest.raises(TypeError):
        fake.find_repost_candidate(object(), company_id="c", job_title="t", province_id=1, lvl=2)
    with pytest.raises(TypeError):
        fake.get_level_id(object(), "SENIOR", "thừa")


def test_fake_defaults_describe_the_normal_nothing_duplicated_case():
    fake, conn = make_pipeline_db(), object()
    assert fake.get_job_probe_by_source_url(conn, "u") is None
    assert fake.find_repost_candidate(conn, company_id="c", job_title="t", province_id=1) is None
    assert fake.find_jobs_by_source_url_regex(conn, source_name="s", url_regex="r") == []
    assert fake.get_or_create_company_by_profile(conn, "ACME", 1) == "company-1"
    # Hai hàm thuần dùng bản thật, không phải mock rỗng.
    assert fake.job_needs_detail_enrichment(("job-1", None, None, None, None)) is True


def test_fake_overrides_and_rejects_unknown_names():
    fake = make_pipeline_db(get_level_id=9)
    assert fake.get_level_id(object(), "SENIOR") == 9
    with pytest.raises(AttributeError, match="no_such_function"):
        make_pipeline_db(no_such_function=1)


def test_pipeline_db_fixture_replaces_pipeline_db_and_is_restored(pipeline_db):
    assert pipeline.db is pipeline_db


def test_pipeline_db_fixture_is_undone_after_the_test():
    assert pipeline.db is real_db


def test_module_documents_the_contract_it_enforces():
    assert pipeline_db.__doc__ and "test_pipeline_db_contract" in pipeline_db.__doc__
