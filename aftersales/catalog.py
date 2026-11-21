"""配件目录与工程规则。

工程说明（authority=engineering）解析为可执行约束；营销页（advertising）
的“通用/即插即用”说法只保留为声明，任何情况下不得压过工程禁用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .parsing import extract_measurements


@dataclass(frozen=True)
class ModelInfo:
    model_id: str
    plate: str
    aliases: tuple[str, ...]
    mouth: str  # 小口 / 大口
    note: str


@dataclass(frozen=True)
class PartInfo:
    catalog_id: str
    part: str
    authority: str
    note: str
    measurements_mm: tuple[float, ...]


@dataclass(frozen=True)
class Rule:
    """一条工程约束，source 指向 catalog 记录编号，保证结论可回溯。"""

    rule_id: str
    source: str
    operator: str  # mouth_scope / require / prohibit_combo / prohibit_plate
    part: str
    mouths: tuple[str, ...] = ()
    requires: tuple[str, ...] = ()
    other: str | None = None
    plate: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class Violation:
    rule_id: str
    source: str
    reason: str


# 从工程说明人工建模的规则表；建模依据写明原句，避免静默解释。
ENGINEERING_RULES: tuple[Rule, ...] = (
    Rule("R1", "I1", "mouth_scope", "R48", mouths=("小口",),
         reason="I1：短杆接口四十八毫米，仅用于小口底盘"),
    Rule("R2", "I1", "require", "R48", mouths=("大口",),
         requires=("J03", "F20"),
         reason="I1：大口如装此杆，需配J03银弧转接座且脚架必须为F20"),
    Rule("R3", "I2", "require", "J03", requires=("R48",),
         reason="I2：J03 配 R48"),
    Rule("R4", "I2", "prohibit_combo", "J03", other="F19",
         reason="I2：J03 禁止与 F19 旧脚架同装"),
    Rule("R5", "I2", "prohibit_plate", "J03", plate="CY-D18",
         reason="I2：标牌出现 CY-D18 也不得使用 J03"),
    Rule("R6", "I3", "mouth_scope", "R52", mouths=("大口",),
         reason="I3：五点二厘米杆配大口底盘"),
    Rule("R7", "I6", "mouth_scope", "F19", mouths=("小口",),
         reason="I6：F19 仅供原批小口；I3：F19 原脚架只适配小口组合"),
    Rule("R8", "I6", "prohibit_combo", "F19", other="J03",
         reason="I6：F19 无转接座版本"),
)


class PartCatalog:
    """从资料包构建的只读目录：型号铭牌、物料、工程规则与营销声明。"""

    def __init__(self, records: dict):
        self.models = tuple(
            ModelInfo(
                model_id=row["id"],
                plate=row["plate"],
                aliases=tuple(row["aliases"]),
                mouth=_mouth_from_note(row["note"]),
                note=row["note"],
            )
            for row in records["models"]
        )
        self.parts = tuple(
            PartInfo(
                catalog_id=row["id"],
                part=row["part"],
                authority=row["authority"],
                note=row["note"],
                measurements_mm=tuple(m.value_mm for m in extract_measurements(row["note"])),
            )
            for row in records["catalog"]
        )
        self.rules = ENGINEERING_RULES
        # 营销声明单列，评估时不参与
        self.advertising = tuple(p for p in self.parts if p.authority == "advertising")
        self._plate_to_model = {m.plate: m for m in self.models}
        self._part_catalog = {p.part: p for p in self.parts if p.authority == "engineering"}
        self._alias_index = self._build_alias_index()

    def _build_alias_index(self) -> dict[str, tuple[ModelInfo, ...]]:
        index: dict[str, list[ModelInfo]] = {}
        for model in self.models:
            for alias in model.aliases:
                index.setdefault(alias, []).append(model)
        return {alias: tuple(models) for alias, models in index.items()}

    def model_by_plate(self, plate: str) -> ModelInfo | None:
        return self._plate_to_model.get(plate)

    def mouth_of(self, plate: str) -> str | None:
        model = self.model_by_plate(plate)
        return model.mouth if model else None

    def resolve_alias(self, name: str) -> tuple[ModelInfo, ...]:
        """销售别名可能对应多个批次：返回全部候选，绝不猜测其中一个。"""
        return self._alias_index.get(name, ())

    def part_note(self, part: str) -> PartInfo | None:
        return self._part_catalog.get(part)

    def part_measurements(self, part: str) -> tuple[float, ...]:
        info = self._part_catalog.get(part)
        return info.measurements_mm if info else ()

    def match_part_by_mm(self, bare_value_mm: float) -> tuple[str, ...]:
        """用归一后的毫米值匹配杆类物料（用于“肩部要52”这类无单位描述）。"""
        return tuple(
            part for part in ("R48", "R52")
            if bare_value_mm in self.part_measurements(part)
        )


def _mouth_from_note(note: str) -> str:
    if "小口" in note:
        return "小口"
    if "大口" in note:
        return "大口"
    raise ValueError(f"型号说明缺少底盘开口信息：{note}")


def evaluate_combo(catalog: PartCatalog, parts, plate: str | None) -> list[Violation]:
    """按工程规则评估一个部件组合在指定铭牌批次下是否可装。

    只引用 engineering 规则；营销页的通用声明在此函数中没有任何权重。
    """
    parts = frozenset(parts)
    mouth = catalog.mouth_of(plate) if plate else None
    violations: list[Violation] = []
    for rule in catalog.rules:
        if rule.part not in parts:
            continue
        if rule.operator == "mouth_scope":
            if mouth is not None and mouth not in rule.mouths:
                # 同部件在该开口下存在显式 require 例外且已配齐时，scope 限制让位于例外
                exception_satisfied = any(
                    other.operator == "require" and other.part == rule.part
                    and mouth in (other.mouths or ())
                    and all(need in parts for need in other.requires)
                    for other in catalog.rules
                )
                if not exception_satisfied:
                    violations.append(Violation(
                        rule.rule_id, rule.source,
                        f"{rule.part} 限{'、'.join(rule.mouths)}，{plate} 为{mouth}；{rule.reason}"))
        elif rule.operator == "require":
            if rule.mouths and mouth not in rule.mouths:
                continue
            missing = [need for need in rule.requires if need not in parts]
            if missing:
                violations.append(Violation(
                    rule.rule_id, rule.source,
                    f"{rule.part} 在{mouth or '未知开口'}下还需 {'、'.join(missing)}；{rule.reason}"))
        elif rule.operator == "prohibit_combo":
            if rule.other in parts:
                violations.append(Violation(
                    rule.rule_id, rule.source,
                    f"{rule.part} 不得与 {rule.other} 同装；{rule.reason}"))
        elif rule.operator == "prohibit_plate":
            if plate == rule.plate:
                violations.append(Violation(
                    rule.rule_id, rule.source,
                    f"铭牌 {plate} 上{rule.reason}"))
    return violations


def sanctioned_combos(catalog: PartCatalog, plate: str) -> list[frozenset[str]]:
    """枚举在该铭牌下通过全部工程规则的组合（至少含一根杆）。"""
    universe = ("R48", "R52", "J03", "F19", "F20")
    allowed: list[frozenset[str]] = []
    for mask in range(1, 1 << len(universe)):
        combo = frozenset(universe[i] for i in range(len(universe)) if mask & (1 << i))
        if not any(part.startswith("R") for part in combo):
            continue
        if not evaluate_combo(catalog, combo, plate):
            allowed.append(combo)
    return allowed
