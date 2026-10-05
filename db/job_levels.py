"""
db.job_levels — luật đóng dấu level của job_postings (tách từ db/jobs.py,
10/2026): mỗi level_id đi kèm level_source (suy từ đâu) và level_rule_version
(theo bộ quy tắc phiên bản nào). Xem sql/migration_add_job_level_source.sql.

Ở đây chỉ có logic THUẦN + sinh mảnh câu SQL, không tự chạy SQL. Mọi nơi ghi
level (db/jobs.py, db/job_recrawl.py, lệnh tính lại level sau này) dùng chung
các hàm này để luật "ghi tự động không đè level người đã sửa" chỉ nằm ở MỘT chỗ.
"""

from typing import Optional

from normalize import LEVEL_SOURCE_MANUAL, LEVEL_SOURCES

_DERIVED_LEVEL_SOURCES = frozenset(LEVEL_SOURCES) - {LEVEL_SOURCE_MANUAL}


def _check_level_stamp(level_id: Optional[int], level_source: Optional[str],
                        level_rule_version: Optional[int]) -> tuple:
    """Kiểm tra cặp (level_source, level_rule_version) cho level_id sắp ghi, trả
    lại (level_source, level_rule_version). Cùng luật với CHECK ở DB nhưng báo lỗi
    sớm, rõ nguyên nhân (lỗi lập trình, không phải lỗi dữ liệu người dùng):

      - level_source None  -> "chưa biết", version phải None;
      - 'manual'           -> version phải None;
      - nguồn do máy suy   -> bắt buộc có version VÀ level_id (không có level thì
                              không thể "suy ra" level).
    """
    if level_source is None:
        if level_rule_version is not None:
            raise ValueError("level_rule_version cần đi kèm level_source")
        return None, None
    if level_source not in LEVEL_SOURCES:
        raise ValueError(f"level_source không hợp lệ: {level_source!r} (cho phép: {LEVEL_SOURCES})")
    if level_source == LEVEL_SOURCE_MANUAL:
        if level_rule_version is not None:
            raise ValueError("level_source='manual' không có level_rule_version")
        return level_source, None
    if level_rule_version is None:
        raise ValueError(f"level_source={level_source!r} bắt buộc kèm level_rule_version")
    if level_id is None:
        raise ValueError(f"level_source={level_source!r} cần level_id (không suy ra được 'không có level')")
    return level_source, int(level_rule_version)


def _derived_level_assignments(level_id: int, level_source: Optional[str],
                                level_rule_version: Optional[int]) -> tuple:
    """Các vế SET (và giá trị) để GHI LEVEL TỰ ĐỘNG vào job_postings: level_id +
    cặp đóng dấu. Dòng đã là 'manual' thì cả ba cột giữ nguyên (CASE tính theo giá
    trị CŨ của dòng), nên ghi tự động không bao giờ đè lên level người đã sửa, kể
    cả khi nơi gọi quên kiểm tra. Đây là chỗ DUY NHẤT thi hành luật đó."""
    level_source, level_rule_version = _check_level_stamp(level_id, level_source, level_rule_version)
    sets = [
        "level_id = CASE WHEN level_source = 'manual' THEN level_id ELSE %s END",
        "level_source = CASE WHEN level_source = 'manual' THEN level_source ELSE %s END",
        "level_rule_version = CASE WHEN level_source = 'manual' THEN level_rule_version ELSE %s END",
    ]
    return sets, [level_id, level_source, level_rule_version]
