"""
db.job_levels — luật đóng dấu level của job_postings (tách từ db/jobs.py,
10/2026): mỗi level_id đi kèm level_source (suy từ đâu) và level_rule_version
(theo bộ quy tắc phiên bản nào). Xem sql/migration_add_job_level_source.sql.

Ở đây chỉ có logic THUẦN + sinh mảnh câu SQL, không tự chạy SQL. Mọi nơi ghi
level (db/jobs.py, db/job_recrawl.py, lệnh tính lại level sau này) dùng chung
các hàm này để luật "ghi tự động không đè level người đã sửa" chỉ nằm ở MỘT chỗ.
"""

import json
from typing import Optional

from normalize import LEVEL_SIGNAL_KEYS, LEVEL_SOURCE_MANUAL, LEVEL_SOURCES

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


def _check_level_signals(level_source: Optional[str], level_signals: Optional[dict]) -> Optional[dict]:
    """Kiểm tra level_signals (tín hiệu thô, xem normalize.build_level_signals) cho
    level sắp ghi tự động. None hợp lệ (= chưa biết tín hiệu). Có giá trị thì phải
    là dict đúng các khoá LEVEL_SIGNAL_KEYS, giá trị là chuỗi, và đi kèm một căn cứ do
    máy suy (không phải None/'manual': tín hiệu chỉ có nghĩa khi giải thích được một
    level do derive_level suy ra). Lỗi lập trình, báo sớm như _check_level_stamp."""
    if level_signals is None:
        return None
    if level_source is None or level_source == LEVEL_SOURCE_MANUAL:
        raise ValueError("level_signals chỉ đi kèm level do máy suy (level_source khác None/'manual')")
    if not isinstance(level_signals, dict) or set(level_signals) != set(LEVEL_SIGNAL_KEYS):
        raise ValueError(f"level_signals phải là dict đủ đúng các khoá {LEVEL_SIGNAL_KEYS}")
    if not all(isinstance(v, str) for v in level_signals.values()):
        raise ValueError("level_signals chỉ chứa giá trị chuỗi")
    return level_signals


def _derived_level_assignments(level_id: int, level_source: Optional[str],
                                level_rule_version: Optional[int],
                                level_signals: Optional[dict] = None) -> tuple:
    """Các vế SET (và giá trị) để GHI LEVEL TỰ ĐỘNG vào job_postings: level_id +
    cặp đóng dấu + tín hiệu thô. Dòng đã là 'manual' thì cả bốn cột giữ nguyên (CASE
    tính theo giá trị CŨ của dòng), nên ghi tự động không bao giờ đè lên level người
    đã sửa, kể cả khi nơi gọi quên kiểm tra. Đây là chỗ DUY NHẤT thi hành luật đó.

    level_signals None được ghi thành NULL (không giữ giá trị cũ): level vừa đổi mà
    không rõ nó suy từ tín hiệu nào thì tín hiệu cũ sẽ nói dối về level mới."""
    level_source, level_rule_version = _check_level_stamp(level_id, level_source, level_rule_version)
    level_signals = _check_level_signals(level_source, level_signals)
    sets = [
        "level_id = CASE WHEN level_source = 'manual' THEN level_id ELSE %s END",
        "level_source = CASE WHEN level_source = 'manual' THEN level_source ELSE %s END",
        "level_rule_version = CASE WHEN level_source = 'manual' THEN level_rule_version ELSE %s END",
        "level_signals = CASE WHEN level_source = 'manual' THEN level_signals ELSE %s::jsonb END",
    ]
    signals_json = json.dumps(level_signals, ensure_ascii=False) if level_signals is not None else None
    return sets, [level_id, level_source, level_rule_version, signals_json]
