"""逐单换件决策：候选收窄、安全交付、挂起与联络员交接。

可交付判据：一个部件组合必须在“仍可成立的全部装配解释”下都通过工程规则。
只要有一种尚未排除的解释会让换件不安全，就不进入可交付清单。
答不上来/看不清只收窄有证据的候选，不视为否定。
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict

from .catalog import PartCatalog, Violation, evaluate_combo, sanctioned_combos
from .checks import plan_checks, reply_is_inconclusive
from .evidence import Event, current_assembly, order_timeline
from .parsing import (
    extract_measurements,
    extract_parts,
    extract_plates,
    split_sentences,
)

_WOBBLE_MARKERS = ("晃", "摇", "松动", "松脱")
_WOBBLE_NEGATION = ("与摇晃无关", "与晃动无关", "不晃", "不摇")
_LOOSENESS_RULED_OUT_MARKERS = ("排除", "没有松", "不松", "无松")
_ROD_PARTS = ("R48", "R52")


@dataclass
class ComboVerdict:
    parts: tuple[str, ...]
    deliverable: bool
    blocking_violations: list[dict] = field(default_factory=list)
    bases: tuple[str, ...] = ()  # 产品说明依据（catalog 编号）


@dataclass
class OrderCase:
    order: str
    records: tuple[str, ...]
    symptom: str | None
    unrelated_issues: list[str]
    customer_request: tuple[str, ...]
    evidence_ledger: list[dict]
    current_plates: tuple[str, ...]
    historical_plates: tuple[str, ...]
    residual_plates: tuple[str, ...]
    current_parts: tuple[str, ...]
    temporary_history: list[str]
    options: list[ComboVerdict]
    promised: dict | None
    shipment: dict | None
    status: str
    hold_reason: str | None
    liaison: dict | None
    checks: list[dict]
    independent_orders: tuple[str, ...]
    notes: list[str]


def _symptom(events: list[Event]) -> str | None:
    """提取本工单的投诉症状；明确声明“与摇晃无关”的同信问题不计入。"""
    for event in events:
        if event.kind not in ("customer_report", "customer_guess"):
            continue
        if any(neg in event.text for neg in _WOBBLE_NEGATION):
            continue
        if any(marker in event.text for marker in _WOBBLE_MARKERS):
            return "底盘晃动类投诉（原话：" + event.text + "）"
    return None


def _unrelated_issues(events: list[Event]) -> list[str]:
    """同一条来信中可独立核实的另一张椅/另一处问题。"""
    issues = []
    for event in events:
        if any(neg in event.text for neg in _WOBBLE_NEGATION):
            issues.append(f"{event.record_id}: {event.text}")
    return issues


def _looseness_ruled_out(records: dict, order: str) -> bool:
    for reply in records["replies"]:
        if reply["order"] == order and any(
            marker in reply["text"] for marker in _LOOSENESS_RULED_OUT_MARKERS
        ):
            return True
    return False


def _blocked_cards(records: dict, order: str) -> tuple[str, ...]:
    blocked = []
    for reply in records["replies"]:
        if reply["order"] == order and reply_is_inconclusive(reply["text"]):
            blocked.append(reply["card"])
    return tuple(blocked)


def _plate_candidates(catalog: PartCatalog, events: list[Event]) -> dict:
    """铭牌事实分三层：

    - current_plates：每个铭牌取最后一次读数；只有被明确纠正措辞否定的旧读数才进历史档；
    - historical_plates：被更晚读数明确取代（如“其实是/才是”）的铭牌，仅留档；
    - residual_plates：同名别名下从未出现过、因而尚未排除的批次解释。
    安全判定以 current + residual 为准，保证旧解释若未被明确否定仍被覆盖。
    """
    readings: list[tuple[int, str, Event]] = []
    ever_seen_plates: set[str] = set()
    seen_aliases: set[str] = set()
    for seq, event in enumerate(events):
        ever_seen_plates.update(event.plates)
        if event.kind == "customer_guess":
            seen_aliases.update(
                alias for model in catalog.models for alias in model.aliases
                if alias in event.text
            )
            continue
        for plate in event.plates:
            readings.append((seq, plate, event))

    last_seq_of: dict[str, int] = {}
    last_event_of: dict[str, Event] = {}
    for seq, plate, event in readings:
        last_seq_of[plate] = seq
        last_event_of[plate] = event

    # 只有明确纠正措辞才把更早的铭牌读数移入历史档；
    # 没有纠正措辞时，两个铭牌读数都保留为当前未锁定的解释
    correction_markers = ("其实", "实际", "应为", "才是", "误抄", "错抄")
    superseded: set[str] = set()
    for seq, _plate, event in readings:
        if any(marker in event.text for marker in correction_markers):
            superseded.update(p for p, s in last_seq_of.items() if s < seq)

    current = tuple(p for p in last_seq_of if p not in superseded)
    historical = tuple(sorted(superseded))
    residual = tuple(
        model.plate for model in catalog.models
        if model.plate not in ever_seen_plates
        and any(alias in seen_aliases for alias in model.aliases)
    )
    return {
        "current_plates": current,
        "historical_plates": historical,
        "residual_plates": residual,
        "reading_records": {plate: last_event_of[plate].record_id for plate in last_event_of},
    }


def _requested_parts(catalog: PartCatalog, events: list[Event]) -> tuple[str, ...]:
    """客户点名的部件与尺寸诉求；猜测句不采信，工程师句不算客户诉求，
    “现装/还在”等现状描述也不算诉求。同条记录半句现状半句诉求时按句分离。"""
    requested: list[str] = []
    for event in events:
        if event.kind != "request":
            continue
        for sentence in split_sentences(event.text):
            if any(verb in sentence for verb in
                   ("现装", "还在", "装的是", "没有动过", "仍为")):
                continue
            requested.extend(extract_parts(sentence))
            for measurement in extract_measurements(sentence):
                if measurement.is_bare:
                    requested.extend(catalog.match_part_by_mm(measurement.value_mm))
    return tuple(dict.fromkeys(requested))


def _rod_combos_to_test(catalog: PartCatalog, events: list[Event]) -> list[tuple[str, ...]]:
    """候选组合：恰好一根杆 + 至多一个脚架 + 至多一个转接座。

    双杆、双脚架这类物理上不成立的组合不进入评估；
    客户/记录中点名过的部件保证至少被评估到。
    """
    rods = ("R48", "R52")
    bases = ("F19", "F20", None)
    adapters = ("J03", None)
    combos: set[tuple[str, ...]] = set()
    for rod in rods:
        for base in bases:
            for adapter in adapters:
                combo = tuple(p for p in (rod, base, adapter) if p is not None)
                combos.add(combo)
    mentioned = {p for event in events for p in event.parts}
    for part in mentioned:
        if part in rods or part in ("J03", "F19", "F20"):
            combos.add((part,))
    return sorted(combos, key=lambda c: (len(c), c))


def _evaluate_across(catalog: PartCatalog, combo, plates) -> list[Violation]:
    violations: list[Violation] = []
    for plate in plates:
        violations.extend(evaluate_combo(catalog, combo, plate))
    return violations


def analyze_order(records: dict, order: str) -> OrderCase:
    catalog = PartCatalog(records)
    events = order_timeline(records, order)
    assembly = current_assembly(events)

    plate_info = _plate_candidates(catalog, events)
    current_plates = plate_info["current_plates"]
    historical_plates = plate_info["historical_plates"]
    residual_plates = plate_info["residual_plates"]
    # 安全判定覆盖当前铭牌与所有尚未排除的解释；历史铭牌仅在 notes 留档
    live_plates = current_plates + residual_plates
    current_parts = tuple(sorted(assembly["installed"]))
    temporary_history = [f"{e.record_id}: {e.text}" for e in assembly["temporary_notes"]]
    unrelated = _unrelated_issues(events)

    # 候选组合必须在所有仍可成立的铭牌解释下都安全；
    # 与换件无关的工单（如缝线脱开）不评估换杆组合
    options: list[ComboVerdict] = []
    if not unrelated:
        for combo in _rod_combos_to_test(catalog, events):
            if not any(part in _ROD_PARTS for part in combo):
                continue
            violations = _evaluate_across(catalog, combo, live_plates) if live_plates else []
            bases = tuple(sorted({v.source for v in violations})) or _bases_for(catalog, combo)
            options.append(ComboVerdict(
                parts=combo,
                deliverable=not violations,
                blocking_violations=[asdict(v) for v in violations],
                bases=bases,
            ))

    promised_row, shipment_row = _delivery_rows(records, order)
    symptom = _symptom(events)
    needs_wobble_source = symptom is not None
    checks = plan_checks(
        records,
        candidate_plates=live_plates,
        needs_wobble_source=needs_wobble_source,
        looseness_ruled_out=_looseness_ruled_out(records, order),
        blocked_cards=_blocked_cards(records, order),
    )

    status, hold_reason, liaison = _status(
        records, order, catalog, promised_row, shipment_row,
        current_plates, residual_plates,
    )

    independent = tuple(sorted({
        row["order"] for row in records["service"]
        if row.get("associated") in {e.record_id for e in events} and row["order"] != order
    }))

    ledger = [
        {
            "record": e.record_id, "phase": e.phase, "kind": e.kind,
            "temporary_test": e.temporary, "text": e.text,
        }
        for e in events
    ]
    notes = _notes(catalog, current_plates, historical_plates, residual_plates,
                   current_parts, temporary_history, symptom, unrelated,
                   promised_row, shipment_row)

    return OrderCase(
        order=order,
        records=tuple(e.record_id for e in events),
        symptom=symptom,
        unrelated_issues=unrelated,
        customer_request=_requested_parts(catalog, events),
        evidence_ledger=ledger,
        current_plates=current_plates,
        historical_plates=historical_plates,
        residual_plates=residual_plates,
        current_parts=current_parts,
        temporary_history=temporary_history,
        options=options,
        promised=_promise_payload(promised_row),
        shipment=_shipment_payload(shipment_row),
        status=status,
        hold_reason=hold_reason,
        liaison=liaison,
        checks=[asdict(c) for c in checks],
        independent_orders=independent,
        notes=notes,
    )


def _bases_for(catalog: PartCatalog, combo) -> tuple[str, ...]:
    bases = set()
    for rule in catalog.rules:
        if rule.part in combo:
            bases.add(rule.source)
    return tuple(sorted(bases))


def _delivery_rows(records: dict, order: str):
    promised = next((r for r in records["service"]
                     if r["order"] == order and r["phase"] == "承诺"), None)
    shipment = next((r for r in records["shipment"] if r["order"] == order), None)
    return promised, shipment


def _promise_payload(row):
    if not row:
        return None
    return {"record": row["id"], "parts": tuple(row.get("parts", ())),
            "branch": row.get("branch"), "verified": tuple(row.get("verified", ()))}


def _shipment_payload(row):
    if not row:
        return None
    return {"record": row["id"], "parts": tuple(row.get("parts", ())),
            "branch": row.get("branch")}


def _status(records, order, catalog: PartCatalog, promised_row, shipment_row,
            current_plates, residual_plates):
    live_plates = current_plates + residual_plates
    # 已寄出组合：交联络员判断暂停使用或安排上门
    if shipment_row is not None:
        violations = _evaluate_across(catalog, shipment_row["parts"], live_plates)
        return "liaison", None, {
            "shipment_record": shipment_row["id"],
            "shipped_parts": tuple(shipment_row["parts"]),
            "unsafe_interpretation": [asdict(v) for v in violations],
            "what_was_confirmed": _confirmed_history(records, order),
            "action_choices": ["暂停使用并通知客户", "安排上门核实底盘开口后更换"],
        }
    # 已承诺未寄：若复测推翻适用条件，挂起待复核
    if promised_row is not None:
        violations = _evaluate_across(catalog, promised_row["parts"], live_plates)
        if violations:
            return "held", "承诺组合在仍成立的铭牌解释下触发工程禁用", None
        return "promised_ok", None, None
    if any(catalog.mouth_of(p) == "大口" for p in residual_plates):
        return "needs_remote_verification", None, None
    return "deliverable", None, None


def _confirmed_history(records: dict, order: str) -> list[dict]:
    history = []
    for row in records["service"]:
        if row["order"] == order and row["phase"] in ("来件", "承诺", "复测", "出库"):
            history.append({"record": row["id"], "phase": row["phase"], "text": row.get("text"),
                            "parts": row.get("parts")})
    return history


def _notes(catalog, current_plates, historical_plates, residual_plates,
           current_parts, temporary_history, symptom, unrelated,
           promised_row, shipment_row) -> list[str]:
    notes: list[str] = []
    for plate in current_plates:
        model = catalog.model_by_plate(plate)
        if model:
            notes.append(f"当前铭牌 {plate}（{ '、'.join(model.aliases) }）底盘为{model.mouth}：{model.note}")
    for plate in historical_plates:
        model = catalog.model_by_plate(plate)
        if model:
            notes.append(f"历史抄牌 {plate} 已被更晚读数取代，仅留档，不再作为当前装配依据")
    if residual_plates:
        notes.append("尚未排除的铭牌解释：" + "、".join(residual_plates) +
                     "；这些解释未被证据否定前，换件必须同时满足它们")
    if temporary_history:
        notes.append("临时测试拆装不作为当前装配事实：" + "；".join(temporary_history))
    if symptom:
        notes.append("晃动来源尚未定位：未排除底盘松脱前，不能发起需要顾客坐或翻动座椅的检查（C3 锁定）")
    for issue in unrelated:
        notes.append(f"同信独立问题，不参与本单换件判定也不连带挂起：{issue}")
    if promised_row:
        notes.append(f"承诺记录 {promised_row['id']} 的依据检查：{promised_row.get('verified', [])}；"
                     "若新回复推翻其适用条件，后续来电应从当前已知情况重新提问")
    if shipment_row:
        notes.append("shipment 记录不随后续复测改写；联络员需对照当初确认内容与现存不安全解释")
    return notes


def analyze(records: dict) -> dict[str, OrderCase]:
    """分析全部工单；同一条来信里的独立问题只作为关联提示，不连带挂起。"""
    orders = sorted({row["order"] for row in records["service"]})
    return {order: analyze_order(records, order) for order in orders}


def case_to_dict(case: OrderCase) -> dict:
    return asdict(case)
