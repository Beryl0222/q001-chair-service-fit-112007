"""远程售后换件判定引擎。

模块划分：
- parsing：留言/说明文本解析，别名、部件、尺寸与单位归一、限制短语；
- catalog：工程规则、组合兼容性与工程/营销权威层级；
- evidence：客户猜测、工程师确认、技术说明分类与装配事实时间线；
- checks：检查卡适用条件、安全前置与候选区分能力；
- decisions：逐单候选收窄、可交付判定、挂起与联络员交接。
"""

from .parsing import (
    PLATE_RE,
    PART_RE,
    chinese_number,
    extract_measurements,
    extract_parts,
    extract_plates,
    restriction_operator,
)
from .catalog import PartCatalog, evaluate_combo, sanctioned_combos
from .evidence import Event, evidence_kind, order_timeline
from .checks import plan_checks, card_safety
from .decisions import analyze_order, analyze

__all__ = [
    "PLATE_RE",
    "PART_RE",
    "chinese_number",
    "extract_measurements",
    "extract_parts",
    "extract_plates",
    "restriction_operator",
    "PartCatalog",
    "evaluate_combo",
    "sanctioned_combos",
    "Event",
    "evidence_kind",
    "order_timeline",
    "plan_checks",
    "card_safety",
    "analyze_order",
    "analyze",
]
