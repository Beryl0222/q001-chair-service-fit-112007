"""证据分级与装配事实时间线。

三类信息严格分开：
- 客户留言中的措辞若带猜测标记，整句降级为“客户猜测”；
- 维修回单、初检、复测、抄牌属于工程师确认；
- catalog 的工程说明是技术说明，适用条件以铭牌/开口为准。

“换过旧脚架”这类话必须结合后续澄清判断是最终装配还是临时测试；
临时测试中的拆装不得当作当前装配事实。
"""

from __future__ import annotations

from dataclasses import dataclass

from .parsing import (
    engineer_markers,
    extract_parts,
    extract_plates,
    guess_markers,
    has_any,
    request_markers,
    temp_markers,
)

PHASE_ORDER = {"来件": 0, "承诺": 1, "出库": 2, "复测": 3}

_INSTALL_VERBS = ("装的是", "装上", "装了", "现装", "安装", "装新品", "换上了")
_REMOVE_VERBS = ("拆下", "拆除", "拆下旧", "取下")
_PRESENT_VERBS = ("原脚还在", "没有动过", "仍为", "还在")
# 旧架/旧脚架 指向 F19，新架/更新脚架 指向 F20
_OLD_TERMS = {"旧架", "旧脚架", "原脚", "原脚架", "旧脚"}
_NEW_TERMS = {"新架", "新脚架"}


@dataclass(frozen=True)
class Event:
    """时间线上的一条事实/声明。"""

    record_id: str
    order: str
    phase: str
    kind: str  # customer_guess / customer_report / engineer_confirm / promise / shipment / retest / request
    text: str
    plates: tuple[str, ...]
    parts: tuple[str, ...]
    temporary: bool
    source_type: str  # service / reply


def evidence_kind(text: str, phase: str) -> str:
    """给一条文本文字分级；猜测只降级带标记的那一类陈述。

    一条留言可能同时含铭牌抄录（事实）与换杆请求（诉求），
    因此先看工程师确认，再看请求，铭牌由调用方按字段单独采信。
    """
    if phase in ("承诺", "出库", "复测"):
        return {
            "承诺": "promise",
            "出库": "shipment",
            "复测": "retest",
        }[phase]
    if has_any(text, engineer_markers()):
        return "engineer_confirm"
    if has_any(text, guess_markers()):
        return "customer_guess"
    if has_any(text, request_markers()) or "要" in text:
        return "request"
    return "customer_report"


def is_temporary(text: str) -> bool:
    """识别“师傅让我试着换上旧架，后来拆下”这类临时测试。"""
    return has_any(text, temp_markers())


def _part_mentions(text: str) -> tuple[str, ...]:
    parts = list(extract_parts(text))
    if any(term in text for term in _OLD_TERMS) and "F19" not in parts:
        parts.append("F19")
    if any(term in text for term in _NEW_TERMS) and "F20" not in parts:
        parts.append("F20")
    return tuple(parts)


def order_timeline(records: dict, order: str) -> list[Event]:
    """按真实时间组装单一工单的事件时间线。

    service 行用 appointment（缺失时按 phase 兜底），reply 用 received；
    不能只按记录编号字符串排序，否则后到的答复会被排到旧来件之前。
    """
    events: list[Event] = []
    for row in records["service"]:
        if row["order"] != order or "text" not in row:
            continue
        text = row["text"]
        kind = evidence_kind(text, row["phase"])
        events.append((row.get("appointment") or f"9999-{PHASE_ORDER[row['phase']]}",
                       Event(
            record_id=row["id"], order=order, phase=row["phase"], kind=kind,
            text=text, plates=tuple(extract_plates(text)),
            parts=_part_mentions(text), temporary=is_temporary(text),
            source_type="service",
        )))
    for reply in records["replies"]:
        if reply["order"] != order:
            continue
        text = reply["text"]
        kind = evidence_kind(text, "来件")
        events.append((reply["received"], Event(
            record_id=reply["id"], order=order, phase="来件", kind=kind,
            text=text, plates=tuple(extract_plates(text)),
            parts=_part_mentions(text), temporary=is_temporary(text),
            source_type="reply",
        )))
    events.sort(key=lambda item: (item[0], PHASE_ORDER[item[1].phase], item[1].record_id))
    return [event for _, event in events]


def current_assembly(events: list[Event]) -> dict:
    """从时间线推导仍可成立的当前装配解释。

    返回 confirmed（工程师确认或客户明确报告的当前状态）与
    open_interpretations（尚未被排除的其他解释）。
    临时测试事件不进入装配状态，只作为历史注释。
    """
    plates: dict[str, Event] = {}
    installed: dict[str, Event] = {}
    removed: set[str] = set()
    temporary_notes: list[Event] = []

    for event in events:
        if event.temporary:
            temporary_notes.append(event)
            continue
        if event.kind != "customer_guess":
            for plate in event.plates:
                plates[plate] = event
        text = event.text
        for part in event.parts:
            if any(verb in text for verb in _REMOVE_VERBS):
                removed.add(part)
                installed.pop(part, None)
            elif any(verb in text for verb in _INSTALL_VERBS + _PRESENT_VERBS):
                if event.kind != "customer_guess":
                    installed[part] = event

    return {
        "plates": plates,
        "installed": installed,
        "removed": tuple(sorted(removed)),
        "temporary_notes": temporary_notes,
    }
