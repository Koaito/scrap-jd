"""
Những gì pipeline.py cần từ tầng DB — một interface mỏng (B2, kế hoạch backend).

pipeline.py gọi `db.xxx(...)` trên module `db`. Trước B2 không có chỗ nào ghi lại "pipeline cần
đúng những hàm nào"; mỗi file test tự dựng một MagicMock rỗng nên: (1) test gọi sai tên/sai
tham số vẫn xanh, (2) thêm một lời gọi db mới vào pipeline không buộc ai cập nhật gì ngoài
một test phân loại đọc/ghi, (3) mỗi file test lặp lại cùng đống giá trị mặc định.

Giờ có ba thứ khớp nhau và được test khoá lại (tests/test_pipeline_db_contract.py):
  - PipelineDB: Protocol liệt kê đúng các hàm db mà pipeline.py gọi, kèm chữ ký.
  - PIPELINE_DB_READS / PIPELINE_DB_WRITES: phân loại đọc/ghi của từng hàm (nguồn duy nhất,
    tests/test_pipeline_transactions.py dùng lại).
  - tests/pipeline_fakes.py: Fake dựng từ PipelineDB, dùng chung qua fixture `pipeline_db` ở
    tests/conftest.py. Fake chỉ có đúng các hàm này, gọi sai tham số là TypeError.

Thêm lời gọi db mới vào pipeline.py thì phải: thêm hàm vào PipelineDB (chữ ký khớp hàm thật),
xếp nó vào READS hoặc WRITES, và cho Fake một giá trị mặc định ở pipeline_fakes.py. Test hợp đồng
báo đỏ cho từng bước nếu quên.

Module `db` thật KHÔNG kế thừa Protocol (typing.Protocol là cấu trúc, khớp theo chữ ký); khớp hay
không do test hợp đồng so chữ ký với hàm thật.
"""

from typing import Optional, Protocol

from db.job_recrawl import RepostLink


class PipelineDB(Protocol):
    # ------------------------------------------------------------------ đọc
    def get_job_probe_by_source_url(self, conn, source_url: str): ...

    def job_needs_detail_enrichment(self, probe, *, now=None, recheck_days=None) -> bool: ...

    def find_company_probe(self, conn, company_name: str): ...

    def probe_needs_enrichment(self, probe) -> bool: ...

    def get_level_id(self, conn, level_code: str) -> Optional[int]: ...

    def find_repost_candidate(
        self, conn, *, company_id: str, job_title: str, province_id: Optional[int],
        level_id: Optional[int] = None,
    ) -> Optional[dict]: ...

    def find_jobs_by_source_url_regex(self, conn, *, source_name: str, url_regex: str) -> list: ...

    # ------------------------------------------------------------------ ghi
    # (kể cả hàm chỉ giữ transaction mở như lock_job_dedup_key: nhánh nào gọi phải tự commit)
    def get_or_create_province(self, conn, province_name: str) -> Optional[int]: ...

    def get_or_create_company_by_profile(
        self, conn, company_name: str, province_id: Optional[int], tax_id: str = "",
        created_by: Optional[str] = None,
    ) -> str: ...

    def update_company_profile(
        self, conn, company_id: str, *, tax_id: str = "", website: str = "", industry: str = "",
        company_size: str = "", address: str = "", partnership_potential: str = "",
        source_profile_url: str = "", products_services: str = "", updated_by: Optional[str] = None,
    ) -> None: ...

    def update_job_fields(
        self, conn, job_id: str, *, work_type: Optional[str] = None,
        parsed_content: Optional[dict] = None,
    ) -> None: ...

    def mark_source_detail_checked(self, conn, source_url: str, *, deadline=None) -> None: ...

    def lock_job_dedup_key(
        self, conn, *, company_id: str, job_title: str, province_id: Optional[int],
        timeout_ms: int = 10000,
    ) -> None: ...

    def link_repost_source(
        self, conn, job_id: str, *, source_name: str, source_url: str, raw_jd_content: str = "",
        salary_raw_text: str = "", deadline=None, today=None,
    ) -> RepostLink: ...

    def update_job_from_recrawl(
        self, conn, job_id: str, *, job_title: str, level_id: Optional[int] = None,
        work_type: Optional[str] = None, parsed_content: Optional[dict] = None,
        salary: Optional[dict] = None, level_source: Optional[str] = None,
        level_rule_version: Optional[int] = None, level_signals: Optional[dict] = None,
    ) -> bool: ...

    def insert_job(
        self, conn, *, company_id: str, job_title: str, matching_industry: str,
        level_id: Optional[int], province_id: Optional[int], work_type: Optional[str],
        currency: str, salary_min: Optional[int], salary_max: Optional[int], salary_type: str,
        source_url: str, source_name: str, salary_raw_text: str = "", deadline=None,
        parsed_content: Optional[dict] = None, raw_jd_content: str = "", salary_period: str = "MONTH",
        created_by: Optional[str] = None, detail_fetched: bool = False,
        level_source: Optional[str] = None, level_rule_version: Optional[int] = None,
        level_signals: Optional[dict] = None,
    ) -> str: ...


# Phân loại MỌI hàm của PipelineDB: đọc hay ghi. Ghi = hàm làm transaction "bẩn" (hoặc giữ nó
# mở), nhánh nào gọi phải tự commit ở cuối (quy tắc 1 job = 1 transaction, xem
# pipeline._process_jobs). Trước B2 hai tập này nằm ở tests/test_pipeline_transactions.py.
PIPELINE_DB_READS = frozenset({
    "get_job_probe_by_source_url", "job_needs_detail_enrichment",
    "find_company_probe", "probe_needs_enrichment", "get_level_id",
    "find_repost_candidate", "find_jobs_by_source_url_regex",
})
PIPELINE_DB_WRITES = frozenset({
    "update_job_fields", "mark_source_detail_checked", "get_or_create_province",
    "get_or_create_company_by_profile", "update_company_profile",
    "link_repost_source", "insert_job",
    "update_job_from_recrawl",
    # Giành khoá advisory cấp transaction (A4): không ghi dữ liệu nhưng giữ transaction mở tới
    # commit/rollback của nhánh, nên coi như ghi.
    "lock_job_dedup_key",
})
