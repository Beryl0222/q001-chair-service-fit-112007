"""文本解析：中文数字、毫米单位归一、别名、部件号与限制短语。

所有尺寸在内部统一为毫米。原文保留在 raw 字段，便于回溯供应商原话。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 铭牌批次，如 CY-B17、CY-C17、CY-D18
PLATE_RE = re.compile(r"CY-[A-Z]\d{2}")
# 物料号，如 R48、J03、F20
PART_RE = re.compile(r"\b[RFJ]\d{2}\b")

_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}

# 数字（阿拉伯或中文）后接可选小数与单位
_NUM = r"\d+(?:\.\d+)?|[零一两二三四五六七八九十百千]+(?:点[零一二三四五六七八九]+)?"
_MEASUREMENT_RE = re.compile(rf"({_NUM})\s*(毫米|厘米|公分|mm|MM|cm|CM)?")
_BARE_RE = re.compile(r"(?<![A-Za-z0-9])(\d{2})(?![A-Za-z0-9])")

# 限制短语 → 约束算子。仅在工程说明上生效。
_RESTRICTION_PATTERNS = [
    ("禁止与", "prohibit_combo"),
    ("不得与", "prohibit_combo"),
    ("不得使用", "prohibit"),
    ("不能", "prohibit"),
    ("仅用于", "only_with"),
    ("仅供", "only_with"),
    ("仅适用", "only_with"),
    ("仅在", "only_with"),
    ("只适配", "only_with"),
    ("需配", "require"),
    ("需要", "require"),
    ("需使用", "require"),
    ("加转接", "adapter_required"),
    ("转接座后可用", "adapter_required"),
    ("可直接配", "allowed"),
    ("可直接", "allowed"),
    ("可用", "allowed"),
]

# 留言中的猜测标记：出现这些措辞时整句降级为客户猜测。
_GUESS_MARKERS = ("我猜", "猜测", "大概", "估计", "应该是", "听说")
# 工程师/维修体系确认标记。
_ENGINEER_MARKERS = ("维修回单", "师傅", "初检", "复测", "确认", "读到", "抄牌")
# 临时测试标记：换件不是最终装配事实。
_TEMP_MARKERS = ("临时", "试着", "测试", "试装", "后来拆下")
# 请求标记：客户要求不等于现状。
_REQUEST_MARKERS = ("要短杆", "要换", "要求", "希望")


@dataclass(frozen=True)
class Measurement:
    """归一后的尺寸，value 单位为毫米。"""

    value_mm: float
    raw: str
    unit: str | None

    @property
    def is_bare(self) -> bool:
        return self.unit is None


def chinese_number(text: str) -> float:
    """把中文数字转为数值，支持小数（点）与十/百/千位。"""
    if "点" in text:
        whole, fraction = text.split("点", 1)
        return float(f"{int(chinese_number(whole) if whole else 0)}."
                     + "".join(str(_CN_DIGITS[ch]) for ch in fraction))
    total, section = 0, 0
    for ch in text:
        if ch in _CN_DIGITS:
            section = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            total += (section or 1) * unit
            section = 0
    return total + section


def _to_mm(number_text: str, unit: str | None) -> float:
    value = float(number_text) if number_text[0].isdigit() else chinese_number(number_text)
    if unit in ("厘米", "公分", "cm", "CM"):
        value *= 10
    return value


def extract_measurements(text: str) -> list[Measurement]:
    """抽取带单位的尺寸，统一为毫米；另外收集无单位裸数字并标注。"""
    found: list[Measurement] = []
    for match in _MEASUREMENT_RE.finditer(text):
        number_text, unit = match.group(1), match.group(2)
        found.append(Measurement(_to_mm(number_text, unit), match.group(0).strip(), unit))
    covered = {m.raw for m in found}
    for match in _BARE_RE.finditer(text):
        if any(match.group(0) in c for c in covered):
            continue
        found.append(Measurement(float(match.group(1)), match.group(1), None))
    return found


def extract_parts(text: str) -> list[str]:
    """抽取物料号（R48、J03、F20 等），保持出现顺序去重。"""
    seen: set[str] = set()
    result: list[str] = []
    for part in PART_RE.findall(text):
        if part not in seen:
            seen.add(part)
            result.append(part)
    return result


def extract_plates(text: str) -> list[str]:
    """抽取铭牌批次号。"""
    return PLATE_RE.findall(text)


def restriction_operator(sentence: str) -> str | None:
    """识别一句工程说明中的限制算子，未命中返回 None。"""
    for phrase, operator in _RESTRICTION_PATTERNS:
        if phrase in sentence:
            return operator
    return None


def split_sentences(text: str) -> list[str]:
    """按中英文标点切句，逗号也切分，以便同条记录分离事实与诉求。"""
    return [piece for piece in re.split(r"[。；;！!？?，,]", text) if piece.strip()]


def has_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def guess_markers() -> tuple[str, ...]:
    return _GUESS_MARKERS


def engineer_markers() -> tuple[str, ...]:
    return _ENGINEER_MARKERS


def temp_markers() -> tuple[str, ...]:
    return _TEMP_MARKERS


def request_markers() -> tuple[str, ...]:
    return _REQUEST_MARKERS
