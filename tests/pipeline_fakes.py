"""
Fake dùng chung cho test pipeline (B2): thay module `db` mà pipeline.py gọi.

Dựng từ pipeline_db.PipelineDB bằng create_autospec(spec_set=True), nên:
  - chỉ có đúng các hàm pipeline cần (truy cập/gán tên khác là AttributeError);
  - gọi sai tên tham số hoặc thiếu tham số bắt buộc là TypeError, y như hàm thật;
  - vẫn là mock: return_value, side_effect, assert_called_once_with, mock_calls dùng như cũ,
    nên chuyển test từ MagicMock sang đây không phải viết lại phần assert.

Giá trị mặc định mô tả "tình huống bình thường, không trùng gì, DB trả đủ": job chưa có, công ty
chưa có, không có tin trùng. Test nào cần tình huống khác chỉ ghi đè đúng chỗ đó.
Dùng qua fixture `pipeline_db` (tests/conftest.py), đã gắn vào pipeline.db.
"""
from unittest.mock import create_autospec

from scrapjd import db as real_db
from scrapjd.db.job_recrawl import RepostLink
from pipeline_db import PipelineDB

DEFAULT_COMPANY_ID = "company-1"
DEFAULT_PROVINCE_ID = 7
DEFAULT_LEVEL_ID = 5
DEFAULT_NEW_JOB_ID = "new-job"


def make_pipeline_db(**returns):
    """Fake mới. `returns` ghi đè return_value theo tên hàm, vd
    make_pipeline_db(find_repost_candidate={...}). Hai hàm thuần (không đụng DB) dùng bản thật."""
    fake = create_autospec(PipelineDB, instance=True, spec_set=True)
    defaults = {
        "get_job_probe_by_source_url": None,
        "find_company_probe": None,
        "probe_needs_enrichment": False,
        "get_level_id": DEFAULT_LEVEL_ID,
        "find_repost_candidate": None,
        "find_jobs_by_source_url_regex": [],
        "get_or_create_province": DEFAULT_PROVINCE_ID,
        "get_or_create_company_by_profile": DEFAULT_COMPANY_ID,
        "insert_job": DEFAULT_NEW_JOB_ID,
        "link_repost_source": RepostLink(inserted=True),
        "update_job_from_recrawl": True,
    }
    unknown = set(returns) - set(dir(PipelineDB))
    if unknown:
        raise AttributeError(f"Không có hàm db nào tên {sorted(unknown)} trong PipelineDB")
    for name, value in {**defaults, **returns}.items():
        getattr(fake, name).return_value = value
    fake.job_needs_detail_enrichment.side_effect = real_db.job_needs_detail_enrichment
    return fake


def called_names(fake, only=None):
    """Tên các hàm db đã gọi, THEO THỨ TỰ gọi (mỗi lần gọi một phần tử). `only`: tập tên cần giữ."""
    names = [name for name, _, _ in fake.mock_calls]
    return [n for n in names if only is None or n in only]
