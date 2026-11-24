"""远程售后换件决定引擎。

读取 ``data/records.json`` 中的维修往来（service）、供应商配件说明（catalog）、
远程检查卡（inspection_cards）、客户答复（replies）和出库记录（shipment），
输出每张维修单（= 同一把椅）当前可成立的装配解释、换件安全结论、
下一步安全且有区分度的检查，以及承诺/出库/复测后的联络员处置档案。

设计原则（与任务要求一一对应）：

* 别名先解析到实物型号；同一系列名可能对应不同底盘接口。
* 尺寸和单位归一（四十八毫米=48mm，五点二厘米=52mm），缺单位的测量值单独标注。
* 每条表述带来源与可信度：客户猜测 / 客户答复 / 来件抄录 / 工程师记录 / 工程复测。
* 配件说明解析成“仅用于 / 需配 / 禁止同装 / 标牌禁用”等组合约束；
  advertising 来源（营销页）不得覆盖 engineering 禁令。
* 用“可能世界”枚举尚未被证据排除的装配；只要有一个世界会让某组合不安全，
  该组合就不能作为可交付换件。
* 答复只有在给出正向观察时才收窄候选；“挡住 / 没看出来”不视为否定。
* 检查卡按当前证据判断适用性与区分度；C3 以排除底盘松脱为前提。
* 跨维修单的 associated 引用只保留上下文，不传播挂起；同一来信中的另一把椅、
  另一处问题独立处理。

仅依赖 Python 3.11 标准库。
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

# 保证从任意工作目录、甚至 ``python3 -I`` 下都能导入同目录的 inspect_records；
# 这里加入的是本脚本所在的受信任项目目录，而不是当前工作目录。
_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from inspect_records import load_records  # noqa: E402

REPO_ROOT = _REPO_ROOT
DEFAULT_DATA = REPO_ROOT / "data" / "records.json"

# ---------------------------------------------------------------------------
# 中文数字 / 尺寸单位
# ---------------------------------------------------------------------------

_CN_DIGIT = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2,
    "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
    "八": 8, "九": 9,
}


def cn_number(text: str) -> float:
    """把简单中文数字转成 float，支持 四十八、十二、二十、五点二。"""
    text = text.strip()
    if "点" in text:
        integer_part, _, frac_part = text.partition("点")
        integer = _cn_integer(integer_part) if integer_part else 0
        frac_digits = "".join(str(_CN_DIGIT[c]) for c in frac_part if c in _CN_DIGIT)
        return float(f"{integer}.{frac_digits}") if frac_digits else float(integer)
    return float(_cn_integer(text))


def _cn_integer(text: str) -> int:
    if not text:
        return 0
    if text.isdigit():
        return int(text)
    if "十" in text:
        left, _, right = text.partition("十")
        tens = _CN_DIGIT.get(left, 1) if left else 1
        ones = _CN_DIGIT.get(right, 0) if right else 0
        return tens * 10 + ones
    value = 0
    for ch in text:
        if ch in _CN_DIGIT:
            value = value * 10 + _CN_DIGIT[ch]
    return value


_DIMENSION_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?|[零〇一二两三四五六七八九十]+(?:点[零〇一二两三四五六七八九]+)?)"
    r"\s*(?P<unit>毫米|厘米|mm|cm|MM|CM)?"
)

_UNIT_TO_MM = {"毫米": 1.0, "mm": 1.0, "MM": 1.0, "厘米": 10.0, "cm": 10.0, "CM": 10.0}


def parse_dimensions(text: str) -> list[dict[str, Any]]:
    """抽取文本中的尺寸并归一为毫米。

    返回每个尺寸的原文、数值（毫米）和原始单位。没有单位时 ``value_mm`` 为
    None，避免把假设当成测量事实。
    """
    out: list[dict[str, Any]] = []
    for match in _DIMENSION_RE.finditer(text):
        raw_num = match.group("num")
        try:
            num = float(raw_num) if re.fullmatch(r"\d+(?:\.\d+)?", raw_num) else cn_number(raw_num)
        except (ValueError, KeyError):
            continue
        unit = match.group("unit")
        snippet = match.group(0)
        # 避开 CY-D18 / F20 这类编号：编号后不会跟长度单位，且这里要求单位或
        # “数字+毫米/厘米”语境；无单位数字仅在调用方需要时另行匹配。
        if unit:
            out.append(
                {
                    "raw": snippet.replace(" ", ""),
                    "number": num,
                    "unit": unit.lower() if unit.lower() in {"mm", "cm"} else unit,
                    "value_mm": round(num * _UNIT_TO_MM[unit], 6),
                    "unit_missing": False,
                }
            )
    return out


def parse_measurement_claim(text: str) -> dict[str, Any] | None:
    """解析“杆的肩部要 52，单位没记”这类缺单位测量。"""
    match = re.search(r"(?:肩部|杆)[^。；，,]*?要\s*(\d+(?:\.\d+)?)\s*([^。；，,]*)", text)
    unit_missing = ("单位没记" in text) or ("单位未记" in text)
    if not match:
        return None
    value = float(match.group(1))
    return {
        "raw": match.group(0),
        "number": value,
        "unit": None,
        "value_mm": value if not unit_missing else None,
        "unit_missing": unit_missing,
        # 与工程规格比对后给出的唯一合理归一（5.2 厘米 = 52 毫米）
        "spec_match_part": "R52",
        "normalized_value_mm": 52.0,
        "normalization_basis": "I3: 五点二厘米 = 52 mm，且物料号 R52 对应",
    }


# ---------------------------------------------------------------------------
# 型号 / 别名
# ---------------------------------------------------------------------------

def build_alias_index(models: list[dict[str, Any]]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for model in models:
        for alias in model.get("aliases", []):
            index.setdefault(alias, []).append(model["id"])
        index[model["plate"]] = [model["id"]]
    return index


def resolve_name(name: str, groups: dict[str, Any]) -> list[dict[str, Any]]:
    """把销售名/系列名/铭牌（含 C17 简写）解析为型号行。"""
    models = groups["models"]
    plate_alias = name.strip().upper().replace(" ", "")
    hits: list[dict[str, Any]] = []
    for model in models:
        plates = {model["plate"].upper(), model["plate"].removeprefix("CY-").upper()}
        if plate_alias in plates or name.strip() in model.get("aliases", []):
            hits.append(model)
    if not hits:
        for model in models:
            if any(name.strip() in alias or alias in name for alias in model.get("aliases", [])):
                hits.append(model)
    # 去重保序
    seen: set[str] = set()
    result = []
    for model in hits:
        if model["id"] not in seen:
            seen.add(model["id"])
            result.append(model)
    return result


def model_mouth(model: dict[str, Any]) -> str:
    note = model.get("note", "")
    if "小口" in note:
        return "small"
    if "大口" in note:
        return "large"
    return "unknown"


# ---------------------------------------------------------------------------
# 供应商说明 → 组合约束
# ---------------------------------------------------------------------------

@dataclass
class Rule:
    source: str
    part: str
    kind: str
    payload: dict[str, Any]
    quote: str
    authority: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_catalog(catalog: list[dict[str, Any]]) -> dict[str, Any]:
    """把配件说明解析为结构化规则，同时保留原文与来源。

    安全判断只使用 authority == engineering 的规则；advertising 条目单独保留，
    以便报告“营销通用宣称被工程禁令压过”。
    """
    rules: list[Rule] = []
    marketing: list[dict[str, Any]] = []
    dimensions: dict[str, list[dict[str, Any]]] = {}

    for row in catalog:
        text = row["note"]
        part = row["part"]
        dims = parse_dimensions(text)
        if dims:
            dimensions.setdefault(part, []).extend(dims)

        if row.get("authority") != "engineering":
            marketing.append(
                {
                    "source": row["id"],
                    "part": part,
                    "claim": text,
                    "authority": row.get("authority"),
                }
            )
            continue

        def add(kind: str, payload: dict[str, Any], quote: str) -> None:
            rules.append(Rule(row["id"], part, kind, payload, quote, "engineering"))

        if "仅用于小口底盘" in text:
            add("part_only_mouth", {"part": part, "mouth": "small"}, "仅用于小口底盘")
        m = re.search(r"大口如装此杆，需配(?P<adapter>J\d+)[^，。]*且脚架必须为(?P<base>F\d+)", text)
        if m:
            add(
                "large_rod_requires",
                {"part": part, "mouth": "large", "adapter": m.group("adapter"), "base": m.group("base")},
                m.group(0),
            )
        if f"配{part}" in text and part == "J03":
            add("adapter_pairs_with_rod", {"adapter": "J03", "rod": "R48"}, "配R48")
        m = re.search(r"禁止与(?P<base>F\d+)[^。，]*同装", text)
        if m:
            add("forbidden_pair", {"part": part, "other": m.group("base")}, m.group(0))
        m = re.search(r"标牌出现(?P<plate>CY-[A-Z]\d+)[^。]*不得使用", text)
        if m:
            add("forbidden_plate", {"part": part, "plate": m.group("plate")}, m.group(0))
        m = re.search(r"(?P<dim>[^，。]*?厘米)杆配大口底盘，可直接配(?P<base>F\d+)", text)
        if m:
            add(
                "rod_direct_fit",
                {"part": part, "mouth": "large", "base": m.group("base"), "dim_phrase": m.group("dim")},
                m.group(0),
            )
        m = re.search(r"(?P<base>F\d+)原脚架只适配(?P<mouth>小口)组合", text)
        if m:
            add(
                "base_only_mouth",
                {"base": m.group("base"), "mouth": "small"},
                m.group(0),
            )
        if "仅供原批小口" in text:
            add("base_only_mouth", {"base": part, "mouth": "small"}, "仅供原批小口")
        if "无转接座版本" in text:
            add("base_no_adapter", {"base": part}, "无转接座版本")
        if "更新脚架" in text and "通告允许" in text:
            add(
                "large_base_noticed_relations_only",
                {"base": part, "mouth": "large"},
                "和大口组合需使用通告允许的杆与转接关系",
            )

    return {
        "rules": [r.as_dict() for r in rules],
        "marketing": marketing,
        "dimensions": dimensions,
        "catalog_rows": catalog,
    }


# ---------------------------------------------------------------------------
# 证据与表述分类
# ---------------------------------------------------------------------------

PROVENANCE_RANK = {
    "customer_guess": 0,
    "customer_reply": 1,
    "customer_intake": 2,
    "engineer_record": 3,
    "engineer_initial": 3,
    "branch_commitment": 3,
    "engineer_retest": 4,
}

PROVENANCE_ZH = {
    "customer_guess": "客户猜测",
    "customer_reply": "客户远程答复",
    "customer_intake": "来件留言/抄录",
    "engineer_record": "工程师维修记录",
    "engineer_initial": "出库/初检确认",
    "branch_commitment": "网点承诺",
    "engineer_retest": "工程复测（最高优先级）",
}

PLATE_RE = re.compile(r"CY-[A-Z]\d{2}")
SHORT_PLATE_RE = re.compile(r"(?<![A-Za-z0-9-])([BCD]\d{2})\b")
PART_RE = re.compile(r"\b([RFJ]\d{2})\b")


def classify_row(row: dict[str, Any], replies_by_id: dict[str, dict[str, Any]] | None = None) -> str:
    phase = row.get("phase")
    text = row.get("text", "")
    if phase == "复测":
        return "engineer_retest"
    if phase == "出库":
        return "engineer_initial"
    if phase == "承诺":
        return "branch_commitment"
    if phase == "答复":
        return "customer_reply"
    if any(marker in text for marker in ("维修回单", "初检", "师傅让我", "维修员")):
        # “师傅让我试着换”出现在客户答复里时整行仍是 customer_reply，
        # 由调用方按 replies 分组分类；这里只处理 service 行。
        return "engineer_record"
    if "我猜" in text or "大概" in text:
        return "customer_guess"
    return "customer_intake"


def detects_looseness_excluded(text: str) -> bool:
    """识别“底盘/脚架松脱已排除”的正向证据。

    刻意不把“底盘没有挪过”“底盘还是会晃”算作排除松脱。
    """
    if re.search(r"松脱已排除|已排除[^。；，]{0,8}松脱|排除(底盘|脚架)松脱", text):
        return True
    if re.search(r"(螺丝|螺栓)[^。；，]{0,12}(拧紧|紧固|上紧|固定好|已经紧)", text):
        return True
    if re.search(r"(底盘|脚架)[^。；，]{0,14}(紧固|拧紧|固定好|无松动|没有松动|不松了|不晃了|不再晃|已经不晃)", text):
        return True
    return False


def extract_plates(text: str) -> list[str]:
    plates = [m.group(0) for m in PLATE_RE.finditer(text)]
    for m in SHORT_PLATE_RE.finditer(text):
        plates.append(f"CY-{m.group(1)}")
    # 去重保序
    seen: set[str] = set()
    out = []
    for plate in plates:
        if plate not in seen:
            seen.add(plate)
            out.append(plate)
    return out


@dataclass
class Fact:
    variable: str
    value: Any
    source: str
    provenance: str
    detail: str = ""
    rank: int = 0

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["provenance_zh"] = PROVENANCE_ZH.get(self.provenance, self.provenance)
        return d


# ---------------------------------------------------------------------------
# 可能世界（尚未排除的装配解释）
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class World:
    model_id: str
    plate: str
    mouth: str
    base: str | None          # F19 / F20 / None(未知)
    adapter: bool | None      # J03 是否在场：True / False / None
    rod: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _dominant(facts: list[Fact], variable: str) -> Fact | None:
    candidates = [f for f in facts if f.variable == variable]
    if not candidates:
        return None
    return max(candidates, key=lambda f: (f.rank, f.source))


def build_worlds(facts: list[Fact], models_by_plate: dict[str, dict[str, Any]]) -> list[World]:
    """根据当前证据枚举所有仍成立的装配世界。

    冲突的铭牌表述按可信度优先级取当前结论（旧表述保留在 facts/时间线中）；
    未知变量展开为全部可能取值。
    """
    plate_fact = _dominant(facts, "plate")
    base_fact = _dominant(facts, "base")
    adapter_fact = _dominant(facts, "adapter")

    if plate_fact is not None:
        model = models_by_plate[plate_fact.value]
        model_choices = [model]
    else:
        model_choices = list(models_by_plate.values())

    base_value = base_fact.value if base_fact else None
    if base_value in ("F19", "F20"):
        base_choices: list[str | None] = [base_value]
    else:
        # 未知 = 两个具体可能世界都尚未排除；不额外枚举 None 世界
        base_choices = ["F19", "F20"]

    if adapter_fact is None or not isinstance(adapter_fact.value, bool):
        adapter_choices: list[bool | None] = [True, False]
    else:
        adapter_choices = [adapter_fact.value]

    worlds: list[World] = []
    for model in model_choices:
        for base in base_choices:
            for adapter in adapter_choices:
                worlds.append(
                    World(
                        model_id=model["id"],
                        plate=model["plate"],
                        mouth=model_mouth(model),
                        base=base,
                        adapter=adapter,
                    )
                )
    return worlds


# ---------------------------------------------------------------------------
# 组合安全评估
# ---------------------------------------------------------------------------

@dataclass
class Verdict:
    status: str                 # safe / unsafe / needs_onsite / invalid_world
    reasons: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "safe"

    def add(self, status: str, code: str, message: str, sources: Iterable[str]) -> None:
        order = {"safe": 0, "needs_onsite": 1, "invalid_world": 2, "unsafe": 3}
        if order[status] > order[self.status]:
            self.status = status
        self.reasons.append({"code": code, "message": message, "sources": list(sources)})

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reasons": self.reasons}


def assess_kit(kit: list[str], world: World, parsed_catalog: dict[str, Any]) -> dict[str, Any]:
    """评估把 kit 装到 world 这把椅上的结果是否合规、安全。

    每条结论都带 catalog 来源编号，便于输出“产品说明依据”。
    """
    v = Verdict(status="safe")
    kit_set = set(kit)
    has_j03 = ("J03" in kit_set) or (world.adapter is True)
    rods_in_kit = [p for p in kit if p in {"R48", "R52"}]

    src = {
        "R48": ["I1"], "J03": ["I2", "I5"], "R52": ["I3"],
        "F19": ["I3", "I6"], "F20": ["I4", "I1", "I3"],
    }

    # 世界本身已经处于工程禁止组合：属于“仍可能不安全的现有装配”
    if has_j03 and world.base == "F19":
        v.add(
            "unsafe", "J03_F19_FORBIDDEN",
            "J03 转接座禁止与 F19 旧脚架同装（工程禁令，营销页“即插即用”不能覆盖）。",
            ["I2", "I5"],
        )
    if has_j03 and world.plate == "CY-D18":
        v.add(
            "unsafe", "J03_D18_FORBIDDEN",
            "标牌为 CY-D18 时 J03 也不得使用。",
            ["I2"],
        )
    if world.base == "F19" and world.mouth == "large":
        v.add(
            "invalid_world", "F19_LARGE_MOUTH_INVALID",
            "F19 原脚架只适配小口组合，大口底盘 + F19 本身就不是合规装配。",
            ["I3", "I6"],
        )

    # R48：仅小口；大口必须 J03 + F20
    if "R48" in kit_set:
        if world.mouth == "small":
            if has_j03:
                v.add(
                    "unsafe", "R48_SMALL_WITH_ADAPTER",
                    "小口底盘的 F19 原批组合是无转接座版本，加装 J03 会落入 J03×F19 禁令。",
                    ["I6", "I2"],
                )
            else:
                v.add("safe", "R48_SMALL_OK", "R48 四十八毫米短杆仅用于小口底盘，可直接装原小口组合。", ["I1", "I6"])
        elif world.mouth == "large":
            if world.plate == "CY-D18":
                v.add(
                    "unsafe", "R48_D18_NO_LEGAL_ROUTE",
                    "大口装 R48 必须配 J03 且脚架为 F20，但 CY-D18 又禁止 J03，因此 D18 没有使用 R48 的合规路径。",
                    ["I1", "I2"],
                )
            elif not has_j03:
                v.add(
                    "unsafe", "R48_LARGE_NEEDS_J03",
                    "大口底盘如装 R48，必须配 J03 且脚架必须为 F20；当前组合缺少 J03。",
                    ["I1"],
                )
            elif world.base != "F20":
                v.add(
                    "unsafe", "R48_LARGE_NEEDS_F20",
                    "R48 大口转接组合要求脚架必须为 F20；F19/未知脚架不能放行。",
                    ["I1", "I2"],
                )
            else:
                v.add("safe", "R48_LARGE_J03_F20_OK", "R48 + J03 + F20 是 C17 大口底盘允许的转接组合。", ["I1", "I2", "I4"])
        else:
            v.add("needs_onsite", "MOUTH_UNKNOWN", "底盘开口未知，需 C4 用批次卡对照后才能判定 R48 路径。", ["C4"])

    # R52：5.2 厘米 = 52mm，大口可直接配 F20
    if "R52" in kit_set:
        if world.mouth == "small":
            v.add(
                "unsafe", "R52_SMALL_FIT",
                "R52（52mm）工程说明只配大口底盘；小口（48mm 接口）不能使用。",
                ["I3", "I1"],
            )
        elif world.mouth == "large":
            if world.base == "F19":
                v.add(
                    "unsafe", "R52_F19_INVALID",
                    "F19 只适配小口组合，不能与大口 R52 组合。",
                    ["I3", "I6"],
                )
            elif world.base is None:
                v.add(
                    "needs_onsite", "R52_BASE_UNKNOWN",
                    "R52 可直接配 F20；脚架型号尚未确认，需现场核对 F20 与紧固状态。",
                    ["I3", "C4"],
                )
            elif has_j03:
                v.add(
                    "needs_onsite", "R52_WITH_EXISTING_J03",
                    "R52 的工程关系是大口直接配 F20；若车上已有 J03，需维修员确认/拆除转接座后安装，不能按客户自助直装放行。",
                    ["I3", "I2", "C4"],
                )
            else:
                v.add("safe", "R52_LARGE_F20_OK", "R52 五点二厘米（52mm）杆配大口底盘，可直接配 F20。", ["I3"])
        else:
            v.add("needs_onsite", "MOUTH_UNKNOWN", "底盘开口未知，需 C4 核对。", ["C4"])

    # J03 单独出现时必须与 R48 配对
    if "J03" in kit_set and "R48" not in kit_set and world.rod != "R48":
        v.add(
            "needs_onsite", "J03_PAIR_R48",
            "J03 工程说明为“配 R48”；未见 R48 时需现场核对杆型。",
            ["I2"],
        )

    return {
        "world": world.as_dict(),
        "verdict": v.as_dict(),
        "sources": sorted({s for r in v.reasons for s in r["sources"] if s.startswith("I")}),
    }


def assess_kit_across_worlds(kit: list[str], worlds: list[World], parsed: dict[str, Any]) -> dict[str, Any]:
    details = [assess_kit(kit, w, parsed) for w in worlds]
    statuses = {d["verdict"]["status"] for d in details}
    if "unsafe" in statuses:
        overall = "blocked_unsafe"
    elif "invalid_world" in statuses:
        overall = "existing_unsafe_assembly"
    elif "needs_onsite" in statuses:
        overall = "needs_onsite_fit"
    elif statuses == {"safe"}:
        overall = "safe_to_deliver"
    else:
        overall = "mixed"
    blocking_raw = [
        reason
        for d in details
        if d["verdict"]["status"] in {"unsafe", "invalid_world"}
        for reason in d["verdict"]["reasons"]
    ]
    seen_codes: set[tuple[str, str]] = set()
    blocking = []
    for reason in blocking_raw:
        key = (reason["code"], reason["message"])
        if key in seen_codes:
            continue
        seen_codes.add(key)
        blocking.append(reason)
    return {
        "kit": kit,
        "overall": overall,
        "world_verdicts": details,
        "blocking_reasons": blocking,
    }


# ---------------------------------------------------------------------------
# 从某张维修单收集证据
# ---------------------------------------------------------------------------

def _index_records(groups: dict[str, Any]) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
    service_by_id = {r["id"]: r for r in groups["service"]}
    replies_by_id = {r["id"]: r for r in groups.get("replies", [])}
    cards_by_id = {r["id"]: r for r in groups["inspection_cards"]}
    return service_by_id, replies_by_id, cards_by_id


def collect_order_rows(order: str, groups: dict[str, Any]) -> list[dict[str, Any]]:
    """本单 service 行 + associated 指向的较早收件行（可能跨单，仅作上下文）。"""
    service_by_id, _, _ = _index_records(groups)
    rows = [r for r in groups["service"] if r.get("order") == order]
    known = {r["id"] for r in rows}
    frontier = list(rows)
    while frontier:
        row = frontier.pop()
        ref = row.get("associated")
        if not ref:
            continue
        refs = ref if isinstance(ref, list) else [ref]
        for rid in refs:
            parent = service_by_id.get(rid)
            if parent and parent["id"] not in known:
                known.add(parent["id"])
                rows.append(parent)
                frontier.append(parent)
    # 按资料中的出现顺序输出，时间线稳定
    order_index = {r["id"]: i for i, r in enumerate(groups["service"])}
    rows.sort(key=lambda r: order_index[r["id"]])
    return rows


def extract_facts_for_order(order: str, groups: dict[str, Any]) -> dict[str, Any]:
    """抽取一张单的全部证据、表述分类、症状与历史歧义。"""
    rows = collect_order_rows(order, groups)
    replies = [r for r in groups.get("replies", []) if r.get("order") == order]
    facts: list[Fact] = []
    statements: list[dict[str, Any]] = []
    symptoms: list[dict[str, Any]] = []
    measurements: list[dict[str, Any]] = []
    ambiguous_history: list[dict[str, Any]] = []
    secondary_issues: list[dict[str, Any]] = []

    def add_statement(source_id: str, text: str, provenance: str, kind: str, interpretation: str) -> None:
        statements.append(
            {
                "source": source_id,
                "text": text,
                "provenance": provenance,
                "provenance_zh": PROVENANCE_ZH[provenance],
                "kind": kind,
                "interpretation": interpretation,
            }
        )

    for row in rows:
        sid = row["id"]
        text = row.get("text", "")
        prov = classify_row(row)
        rank = PROVENANCE_RANK[prov]

        if text:
            add_statement(sid, text, prov, "service", text)

        # 铭牌
        for plate in extract_plates(text):
            # 客户明确说“我猜”的整句不应当成铭牌事实
            if prov == "customer_guess":
                continue
            facts.append(Fact("plate", plate, sid, prov, text, rank))

        # 脚架当前状态
        current_base: str | None = None
        m = re.search(r"现装\s*(F\d+)", text)
        if m:
            current_base = m.group(1)
        m = re.search(r"架仍为\s*(F\d+)", text)
        if m:
            current_base = m.group(1)
        m = re.search(r"装的是\s*(F\d+)", text)
        if m and "拆下" in text:
            current_base = m.group(1)
            removed = re.search(r"拆下\s*(F\d+)", text)
            if removed:
                ambiguous_history.append(
                    {
                        "source": sid,
                        "reading": f"{removed.group(1)} 已拆下，最终安装 {current_base}",
                        "temporary": False,
                    }
                )
        if re.search(r"初检[^。]*?(F\d+)\s*脚架", text):
            current_base = re.search(r"(F\d+)\s*脚架", text).group(1)
        if current_base:
            facts.append(Fact("base", current_base, sid, prov, text, rank))

        if "原脚还在" in text and "没有动过" in text:
            # B17 原批小口的原厂脚架由 I6 识别为 F19
            plate = extract_plates(text)
            base = "F19" if (not plate or plate[0] == "CY-B17") else None
            if base:
                facts.append(Fact("base", base, sid, prov, "原脚未动；I6: F19 为原批小口脚架", rank))
                facts.append(Fact("adapter", False, sid, prov, "原脚未动且 F19 为无转接座版本(I6)", rank))

        # “换过旧架”的临时测试解释
        if "试着换上旧架" in text and "后来拆下" in text:
            facts.append(Fact("base_trial_removed", "F19", sid, prov, "客户曾按师傅要求临时试装 F19，随后拆下", rank))
            ambiguous_history.append(
                {
                    "source": sid,
                    "reading": "“换过旧架”= 临时试装 F19，已拆下；不是最新装配事实",
                    "temporary": True,
                }
            )

        # 症状（“与摇晃无关”里的“晃”不算摇晃投诉）
        for clause in re.split(r"[。；;，,、]", text):
            if "晃" not in clause:
                continue
            if "无关" in clause or re.search(r"不晃|没有晃|没晃", clause):
                continue
            location = "chassis" if ("底盘" in clause or "底座" in clause) else "unknown"
            symptoms.append({"type": "wobble", "location": location, "source": sid, "text": clause.strip() or text})
        if "缝线脱开" in text or "缝线" in text:
            symptoms.append({"type": "upholstery_seam", "location": "seat", "source": sid, "text": text})
        if "与摇晃无关" in text or "另提到" in text:
            secondary_issues.append(
                {"source": sid, "description": text, "separable": True, "reason": "明文说明与摇晃无关/同信另提"}
            )

        if detects_looseness_excluded(text):
            facts.append(Fact("looseness_excluded", True, sid, prov, text, rank))

        meas = parse_measurement_claim(text)
        if meas:
            meas["source"] = sid
            measurements.append(meas)

        if "要短杆" in text or "短一点的杆" in text:
            measurements.append(
                {"source": sid, "customer_request": "更短的杆", "candidate_part": "R48",
                 "note": "客户诉求/猜测，不能压过测量与工程规格"}
            )

    # 客户答复
    for reply in replies:
        rid = reply["id"]
        text = reply.get("text", "")
        card = reply.get("card")
        add_statement(rid, text, "customer_reply", f"reply:{card}", text)

        for plate in extract_plates(text):
            facts.append(Fact("plate", plate, rid, "customer_reply", text, PROVENANCE_RANK["customer_reply"]))

        if "底盘没有挪过" in text or "没有挪过" in text:
            facts.append(Fact("chassis_moved", False, rid, "customer_reply", text, 1))

        # C2 银环观察：只有正向/明确否定才收窄；挡住/没看出来 = 未知
        if card == "C2":
            blocked = any(m in text for m in ("挡住", "遮住", "没看出来", "无法辨认", "看不清"))
            saw_ring = bool(re.search(r"(看到|有|露出).{0,6}(银环|银弧|转接座)", text))
            no_ring = bool(re.search(r"(没有|未见|看不到).{0,6}(银环|银弧|转接座)", text)) and not blocked
            if blocked:
                facts.append(Fact("c2_observation", "blocked", rid, "customer_reply", text, 1))
                symptoms.append({"type": "c2_unobservable", "source": rid, "text": text})
            elif saw_ring:
                facts.append(Fact("adapter", True, rid, "customer_reply", "C2 看到银环=J03 在场", 1))
            elif no_ring:
                facts.append(Fact("adapter", False, rid, "customer_reply", "C2 未见银环", 1))
            for clause in re.split(r"[。；;，,、]", text):
                if "晃" in clause and "无关" not in clause and not re.search(r"不晃|没晃", clause):
                    location = "chassis" if ("底盘" in clause or "底座" in clause) else "unknown"
                    symptoms.append({"type": "wobble", "location": location, "source": rid, "text": clause.strip()})

        if "试着换上旧架" in text and "后来拆下" in text:
            facts.append(Fact("base_trial_removed", "F19", rid, "customer_reply", text, 1))
            ambiguous_history.append(
                {"source": rid, "reading": "“换过旧架”= 临时试装 F19，已拆下；不能当成当前装 F19", "temporary": True}
            )

        if detects_looseness_excluded(text):
            facts.append(Fact("looseness_excluded", True, rid, "customer_reply", text, 1))

    looseness_excluded = any(f.variable == "looseness_excluded" and f.value is True for f in facts)
    chassis_wobble_reported = any(s["type"] == "wobble" and s.get("location") == "chassis" for s in symptoms)

    # 别名提及：记录它解析到哪些型号，并保留表述可信度（猜测不收口为事实）
    alias_index = build_alias_index(groups["models"])
    alias_mentions: list[dict[str, Any]] = []
    for st in statements:
        for alias, model_ids in alias_index.items():
            if alias.startswith("CY-"):
                continue
            if alias in st["text"]:
                alias_mentions.append(
                    {
                        "alias": alias,
                        "resolves_to": model_ids,
                        "source": st["source"],
                        "provenance": st["provenance"],
                        "provenance_zh": st["provenance_zh"],
                        "note": "同一系列名可对应不同接口；客户猜测不用来排除其他可能，铭牌证据优先",
                    }
                )

    return {
        "rows": rows,
        "replies": replies,
        "facts": facts,
        "fact_dicts": [f.as_dict() for f in facts],
        "statements": statements,
        "alias_mentions": alias_mentions,
        "symptoms": symptoms,
        "measurements": measurements,
        "ambiguous_history": ambiguous_history,
        "secondary_issues": secondary_issues,
        "looseness_excluded": looseness_excluded,
        "chassis_wobble_reported": chassis_wobble_reported,
    }


# ---------------------------------------------------------------------------
# 检查卡推荐
# ---------------------------------------------------------------------------

CANDIDATE_KITS: dict[str, list[str]] = {
    "R48 单杆": ["R48"],
    "R48+J03": ["R48", "J03"],
    "R52 单杆": ["R52"],
}


def _safe_kit_names(worlds: list[World], parsed: dict[str, Any]) -> set[str]:
    safe: set[str] = set()
    for name, kit in CANDIDATE_KITS.items():
        result = assess_kit_across_worlds(kit, worlds, parsed)
        if result["overall"] == "safe_to_deliver":
            safe.add(name)
    return safe


def _variable_discriminates(variable: str, worlds: list[World], parsed: dict[str, Any]) -> bool:
    """配对比较：只改变检查卡能观察的变量、其余坐标相同的两个世界，
    是否会让某个候选组合的逐世界判定不同。不同即“问法能区分不相容决定”。
    """
    from collections import defaultdict

    buckets: dict[tuple[Any, ...], dict[Any, tuple[str, ...]]] = defaultdict(dict)
    for world in worlds:
        if variable == "model":
            x_value: Any = world.model_id
            other = (world.base, world.adapter)
        elif variable == "adapter":
            x_value = world.adapter
            other = (world.model_id, world.base)
        elif variable == "base":
            x_value = world.base
            other = (world.model_id, world.adapter)
        else:
            return False
        statuses = tuple(
            assess_kit(kit, world, parsed)["verdict"]["status"]
            for kit in CANDIDATE_KITS.values()
        )
        # 同一其他坐标下，若该变量已有另一个取值且判定签名不同 → 有区分度
        for prior_x, prior_sig in buckets[other].items():
            if prior_x != x_value and prior_sig != statuses:
                return True
        buckets[other][x_value] = statuses
    return False


def recommend_cards(
    order: str,
    groups: dict[str, Any],
    facts_bundle: dict[str, Any],
    worlds: list[World],
    parsed: dict[str, Any],
) -> dict[str, Any]:
    """按当前证据判断每张卡：现在是否安全适用、是否区分不相容决定、用时排序。"""
    _, _, cards_by_id = _index_records(groups)
    replies = facts_bundle["replies"]
    answered_cards = {r["card"]: r for r in replies}

    model_values = {w.model_id for w in worlds}
    adapter_values = {w.adapter for w in worlds}
    safe_now = _safe_kit_names(worlds, parsed)

    # 每个具体世界各自允许的安全组合；若不同世界签名不同，说明仍有
    # “目前不相容的决定”需要检查来区分。
    def _safe_signature(sub_worlds: list[World]) -> tuple[str, ...]:
        return tuple(sorted(_safe_kit_names(sub_worlds, parsed)))

    world_signatures = {_safe_signature([w]) for w in worlds}
    has_decision_ambiguity = len(world_signatures) > 1

    # C1 区分铭牌/型号；C2 区分转接座是否在场（配对世界比较）
    c1_discriminates = _variable_discriminates("model", worlds, parsed)
    c2_discriminates = _variable_discriminates("adapter", worlds, parsed)

    c3_blocked = facts_bundle["chassis_wobble_reported"] and not facts_bundle["looseness_excluded"]
    wobble_present = any(s["type"] == "wobble" for s in facts_bundle["symptoms"])

    cards: list[dict[str, Any]] = []

    c1 = cards_by_id["C1"]
    cards.append({
        **c1,
        "safe_for_all": True,
        "applicable_now": True,
        "discriminating_now": c1_discriminates,
        "already_reply": answered_cards.get("C1", {}).get("id"),
        "recommendation": (
            "优先：站立查看并抄完整外露铭牌，不推椅。"
            + ("其结果会改变可换组合。" if c1_discriminates else "当前铭牌证据已收窄型号，此卡主要是留证复核。")
        ),
    })

    c2 = cards_by_id["C2"]
    c2_reply = answered_cards.get("C2")
    c2_blocked = any(f.variable == "c2_observation" and f.value == "blocked" for f in facts_bundle["facts"])
    c2_applicable = not c2_blocked
    cards.append({
        **c2,
        "safe_for_all": True,
        "applicable_now": c2_applicable,
        "discriminating_now": c2_discriminates,
        "already_reply": c2_reply.get("id") if c2_reply else None,
        "blocked_observation": c2_blocked,
        "recommendation": (
            "C2 已尝试但视线被挡，答复按“无法辨认”处理，不能当成没有银环/J03。"
            "可请顾客从不被遮挡的另一侧伸手机再拍一次（仍不挪椅、一次回复）；"
            "若整圈都被遮住，远程无法收窄，转 C4。"
            if c2_blocked else
            "不挪椅拍摄外侧银环，用于确认 J03 是否在场。"
        ),
    })

    c3 = cards_by_id["C3"]
    cards.append({
        **c3,
        "safe_for_all": False,
        "applicable_now": not c3_blocked,
        # C3 只在确有摇晃投诉时才区分“杆/转接座/底盘”晃动来源
        "discriminating_now": wobble_present,
        "already_reply": answered_cards.get("C3", {}).get("id"),
        "precondition": "已经排除底盘与脚架松脱并确认直立",
        "precondition_met": not c3_blocked,
        "recommendation": (
            "现在不能让顾客转动靠背、坐上去或翻动座椅：答复仍报告底盘晃，"
            "底盘/脚架松脱尚未排除。先做无负载的松脱排查；远程无法明确排除时转 C4。"
            if c3_blocked else
            "松脱已排除且确认直立后，可空椅转动靠背定位晃动来源。"
            if wobble_present else
            "本单没有摇晃症状，C3 不能区分当前的换件决定，无需安排。"
        ),
    })

    c4 = cards_by_id["C4"]
    cards.append({
        **c4,
        "safe_for_all": True,
        "applicable_now": True,
        "discriminating_now": True,
        "already_reply": answered_cards.get("C4", {}).get("id"),
        "recommendation": "到店/上门由维修员固定椅身后终核：批次卡对底盘开口、J03 刻印、底盘与脚架紧固/松脱。",
    })

    # 排序：当前安全适用且有区分度的远程卡，按 effort 比较；C4 兜底
    actionable_remote = [
        c for c in cards
        if c["channel"] == "远程" and c["applicable_now"] and c["discriminating_now"] and not c["already_reply"]
    ]
    actionable_remote.sort(key=lambda c: c["effort"])

    # 对所有候选都安全、但当前不改变决定的留证型问法（如已给铭牌后的 C1）
    optional_safe = [
        c["id"] for c in cards
        if c.get("safe_for_all") and c["channel"] == "远程"
        and c["applicable_now"] and not c["discriminating_now"] and not c["already_reply"]
    ]

    blocked_remote = [c["id"] for c in cards if c["channel"] == "远程" and not c["applicable_now"]]
    # 只有确实存在未区分的不相容决定时，才谈得上“只剩 C4 能区分”
    onsite_only = (
        has_decision_ambiguity
        and not actionable_remote
        and any(c["discriminating_now"] for c in cards if c["id"] == "C4")
    )

    return {
        "cards": cards,
        "next_remote_questions": actionable_remote,
        "optional_safe_checks": optional_safe,
        "blocked_question_ids": blocked_remote,
        "has_decision_ambiguity": has_decision_ambiguity,
        "onsite_required_for_discrimination": onsite_only,
        "onsite_confirmations": _onsite_list(facts_bundle, worlds),
        "current_safe_kit_names": sorted(safe_now),
    }


def _onsite_list(facts_bundle: dict[str, Any], worlds: list[World]) -> list[str]:
    items = [
        "用批次卡对照底盘开口，终核 CY-B17/C17/D18 与小口/大口（C4）",
        "读取转接座刻印，确认 J03 到底在不在场（C4）",
    ]
    if facts_bundle["chassis_wobble_reported"] and not facts_bundle["looseness_excluded"]:
        items.insert(0, "先固定椅身并检查/紧固底盘与 F19/F20 脚架连接，排除松脱后再做晃动来源核对；在此之前不要让顾客坐试或翻椅")
    bases = {w.base for w in worlds}
    if None in bases:
        items.append("确认脚架具体型号（F19 旧架 / F20 更新架）")
    return items


# ---------------------------------------------------------------------------
# 承诺 / 出库 / 复测 / 寄送 档案
# ---------------------------------------------------------------------------

def build_dossiers(groups: dict[str, Any], parsed: dict[str, Any]) -> list[dict[str, Any]]:
    _, replies_by_id, cards_by_id = _index_records(groups)
    service_by_id = {r["id"]: r for r in groups["service"]}
    shipments_by_order: dict[str, list[dict[str, Any]]] = {}
    for ship in groups.get("shipment", []):
        shipments_by_order.setdefault(ship["order"], []).append(ship)

    # 每个部件对应的工程说明依据（I5 是营销来源，单独标记为非安全依据）
    part_basis = {
        "R48": ["I1"],
        "J03": ["I2"],
        "R52": ["I3"],
        "F20": ["I4", "I1", "I3"],
        "F19": ["I3", "I6"],
    }
    marketing_ids = {m["source"] for m in parsed["marketing"]}

    dossiers = []
    for row in groups["service"]:
        if row.get("phase") not in {"承诺", "出库"} or not row.get("parts"):
            continue
        order = row["order"]
        checks_passed: list[dict[str, Any]] = []
        for rid in row.get("verified", []) or []:
            reply = replies_by_id.get(rid)
            if not reply:
                continue
            card = cards_by_id.get(reply["card"], {})
            checks_passed.append(
                {
                    "reply": rid,
                    "card": reply["card"],
                    "received": reply.get("received"),
                    "instruction": card.get("instruction"),
                    "customer_text": reply.get("text"),
                }
            )
        # 出库行自带的初检结论也算当时通过的检查
        initial = re.search(r"初检([^。]*)。?", row.get("text", ""))
        if initial:
            checks_passed.append(
                {"source_record": row["id"], "kind": "初检", "finding": initial.group(1).strip("，, ")}
            )

        # associated 指向的较早工程/收件记录（如承诺 S5 → 回单 S2）
        associated_records: list[dict[str, Any]] = []
        ref = row.get("associated")
        for rid in (ref if isinstance(ref, list) else ([ref] if ref else [])):
            parent = service_by_id.get(rid)
            if parent:
                associated_records.append(
                    {"source": rid, "phase": parent.get("phase"), "text": parent.get("text", "")}
                )

        basis_ids: list[str] = []
        for part in row["parts"]:
            basis_ids.extend(part_basis.get(part, []))
        basis_ids = sorted(set(basis_ids))
        dossiers.append(
            {
                "order": order,
                "record": row["id"],
                "phase": row["phase"],
                "branch": row.get("branch"),
                "chosen_parts": row["parts"],
                "associated": row.get("associated"),
                "associated_records": associated_records,
                "checks_passed_at_time": checks_passed,
                "record_text": row.get("text", ""),
                "engineering_basis": basis_ids,
                "marketing_sources_not_authority": sorted(
                    marketing_ids & {"I5"}
                ),
                "shipments": shipments_by_order.get(order, []),
                "retests": [
                    {"source": r["id"], "text": r.get("text", "")}
                    for r in groups["service"]
                    if r.get("phase") == "复测" and r.get("order") == order
                ],
            }
        )
    return dossiers


# ---------------------------------------------------------------------------
# 单张维修单的完整决定
# ---------------------------------------------------------------------------

def recommended_part_safe_ish(recommended_verdict: dict[str, Any] | None) -> bool:
    """推荐件至少没有在任何剩余世界触发工程禁令（可能仍需现场装配条件）。"""
    return bool(
        recommended_verdict
        and recommended_verdict["overall"] in {"safe_to_deliver", "needs_onsite_fit"}
    )


def advise_order(order: str, groups: dict[str, Any], parsed: dict[str, Any] | None = None) -> dict[str, Any]:
    parsed = parsed or parse_catalog(groups["catalog"])
    models_by_plate = {m["plate"]: m for m in groups["models"]}

    bundle = extract_facts_for_order(order, groups)
    worlds = build_worlds(bundle["facts"], models_by_plate)
    questions = recommend_cards(order, groups, bundle, worlds, parsed)

    # 该单实际承诺/出库的组合
    commitments = [
        r for r in groups["service"]
        if r.get("order") == order and r.get("phase") == "承诺" and r.get("parts")
    ]
    outbound = [
        r for r in groups["service"]
        if r.get("order") == order and r.get("phase") == "出库" and r.get("parts")
    ]
    shipments = [s for s in groups.get("shipment", []) if s.get("order") == order]
    retests = [r for r in groups["service"] if r.get("order") == order and r.get("phase") == "复测"]

    committed_kit = commitments[0]["parts"] if commitments else (outbound[0]["parts"] if outbound else None)
    kit_verdict = assess_kit_across_worlds(committed_kit, worlds, parsed) if committed_kit else None

    # 候选正确组合（用于给出复核后的工程方向，不自动放行）
    candidate_kits = {
        name: assess_kit_across_worlds(kit, worlds, parsed)
        for name, kit in CANDIDATE_KITS.items()
    }
    deliverable_kits = [name for name, r in candidate_kits.items() if r["overall"] == "safe_to_deliver"]
    onsite_kits = [name for name, r in candidate_kits.items() if r["overall"] == "needs_onsite_fit"]

    # 规格锚定的推荐组合：优先实测尺寸匹配，其次客户“短杆”诉求 + 已收窄的小口证据
    spec_part = next(
        (m.get("spec_match_part") for m in bundle["measurements"] if m.get("spec_match_part")),
        None,
    )
    wants_short = any(m.get("customer_request") for m in bundle["measurements"])
    dominant_plate_for_kit = _dominant(bundle["facts"], "plate")
    plate_model = (
        models_by_plate.get(dominant_plate_for_kit.value) if dominant_plate_for_kit else None
    )
    if spec_part:
        recommended_kit = [spec_part]
        recommend_basis = "measurement"
    elif wants_short and plate_model and model_mouth(plate_model) == "small":
        recommended_kit = ["R48"]
        recommend_basis = "short_request_plus_small_mouth"
    else:
        recommended_kit = None
        recommend_basis = None
    recommended_verdict = (
        assess_kit_across_worlds(recommended_kit, worlds, parsed) if recommended_kit else None
    )

    # 摇晃症状的三种竞争解释（客户杆理论 / 转接件 / 底盘松脱）
    symptom_hypotheses: list[dict[str, Any]] = []
    issue_types = {s["type"] for s in bundle["symptoms"]}
    if "wobble" in issue_types:
        adapter_state = _dominant(bundle["facts"], "adapter")
        symptom_hypotheses = [
            {
                "id": "H_rod",
                "explanation": "杆肩规格不匹配或杆-底盘接口旷量，即客户“换短一点的杆就行”的猜测",
                "status": "spec_points_R52" if spec_part == "R52" else "unconfirmed_customer_theory",
            },
            {
                "id": "H_adapter",
                "explanation": "后来加装的 J03 银弧转接件旷动，视频里可能分不清摇的是脚架还是转接件",
                "status": (
                    "excluded_absent" if adapter_state and adapter_state.value is False
                    else "confirmed_present" if adapter_state and adapter_state.value is True
                    else "unexcluded_C2_blocked_or_unasked"
                ),
            },
            {
                "id": "H_chassis_loose",
                "explanation": "底盘或脚架连接松脱；A1 报告“底盘还是会晃”，该解释尚未排除",
                "status": "live_and_blocks_C3_sit_flip" if bundle["chassis_wobble_reported"]
                else ("excluded" if bundle["looseness_excluded"] else "unexcluded"),
            },
        ]

    # 生命周期状态机
    shipped = bool(shipments)
    dominant_plate = dominant_plate_for_kit
    dominant_base = _dominant(bundle["facts"], "base")
    dominant_adapter = _dominant(bundle["facts"], "adapter")
    retest_overturn = bool(retests) and dominant_plate is not None and dominant_plate.provenance == "engineer_retest"

    # 在用部件辨认（当前证据下）
    all_text = " ".join(s["text"] for s in bundle["statements"])
    parts_in_use = {
        "base": dominant_base.value if dominant_base else None,
        "base_source": dominant_base.source if dominant_base else None,
        "adapter_J03_present": (
            dominant_adapter.value if dominant_adapter and isinstance(dominant_adapter.value, bool) else None
        ),
        "rod_spec_part": spec_part,
        "mentioned_parts": sorted(set(PART_RE.findall(all_text))),
    }

    # 现在就能确定的结论（不依赖未返回的答复）
    is_upholstery_only = "upholstery_seam" in issue_types and not (issue_types & {"wobble"})
    certain_now: list[str] = []
    if dominant_plate and plate_model:
        mouth_zh = "小口" if model_mouth(plate_model) == "small" else "大口"
        certain_now.append(
            f"当前最高优先级铭牌证据为 {dominant_plate.value}（{mouth_zh}底盘，{dominant_plate.source}/"
            f"{PROVENANCE_ZH[dominant_plate.provenance]}）"
        )
    if dominant_base:
        certain_now.append(f"在用脚架按当前证据为 {dominant_base.value}（{dominant_base.source}）")
    if spec_part == "R52":
        certain_now.append("“52”对应 R52：工程说明为五点二厘米=52 mm，大口可直接配 F20（I3），不应按客户“短一点”的说法改发 R48+J03")
    if order == "OX3" or (bundle["symptoms"] and any(s["type"] == "c2_unobservable" for s in bundle["symptoms"])):
        certain_now.append("C2 的“挡住/没看出来”是无法辨认，不是“没有银环/J03”的否定证据")
    if bundle["chassis_wobble_reported"]:
        certain_now.append("底盘/脚架松脱尚未排除：C3、坐试、翻动座椅现在都不安全")
    if is_upholstery_only:
        certain_now.append("该单只涉及饰面缝线，与摇晃/底盘兼容无关，可独立核实")

    status_code: str
    status_zh: str
    actions: list[str] = []
    can_deliver = False
    hold = False
    liaison_escalation = False

    # 推荐件在任何剩余世界都不触发工程禁令 → 可先按规格备货；
    # 但只有跨世界 safe 且无未排除松脱/问法时，才允许客户自助装并承诺单一组合。
    part_can_prepare = recommended_part_safe_ish(recommended_verdict)
    self_fit_cleared = False
    committed_blocked = bool(
        kit_verdict and kit_verdict["overall"] in {"blocked_unsafe", "existing_unsafe_assembly"}
    )

    independent_checks: list[str] = []
    if is_upholstery_only:
        status_code = "independent_proceed"
        status_zh = "独立事项：可单独安排（缝线/饰面），不被他单挂起"
        actions.append("按饰面缝线维修独立核实与报价；底盘兼容/挂起结论不适用于这把椅")
        independent_checks.append("请客户在不搬椅、不试坐的情况下拍缝线脱开处近照，饰面问题单独流转")
        questions["compatibility_relevant"] = False
        questions["next_remote_questions"] = []
        questions["optional_safe_checks"] = []
        questions["blocked_question_ids"] = []
        questions["onsite_required_for_discrimination"] = False
    elif shipped and committed_blocked:
        status_code = "shipped_liaison_review"
        status_zh = "已寄出且复测认错批次/组合：交联络员判断暂停使用或安排上门"
        liaison_escalation = True
        actions += [
            "联络员调取档案：当时确认了什么、寄出了什么、现在残留哪种不安全装配可能",
            "由联络员决定是否请客户暂停使用该组合，并安排上门复核/更换；不远程指导客户自行拆除 J03",
        ]
    elif commitments and not shipped and committed_blocked:
        status_code = "promised_suspended_review"
        status_zh = "已承诺未寄出：挂起待工程复核，不得按原承诺发出"
        hold = True
        actions += [
            "冻结该承诺组合的拣货/寄发",
            "以工程复测和 C4 现场终核为准重新出决定；复核通过前不向客户确认发货",
        ]
    elif (
        recommended_verdict
        and recommended_verdict["overall"] == "safe_to_deliver"
        and not questions["next_remote_questions"]
        and not bundle["chassis_wobble_reported"]
    ):
        status_code = "ready_to_deliver"
        status_zh = "候选已收敛：规格锚定组合对所有剩余解释都安全，可交付"
        can_deliver = True
        self_fit_cleared = True
        actions.append("按推荐组合拣发，不带入未被依据支持的转接件")
    elif recommended_part_safe_ish(recommended_verdict):
        status_code = "part_determined_conditions_apply"
        status_zh = "规格已确定，但仍有适用条件/未排除解释，不能按客户自助无条件交付"
        if part_can_prepare and recommended_kit:
            actions.append(
                f"{'/'.join(recommended_kit)} 规格件可先备货；但在转接件是否在场、底盘紧固明确前，"
                "不向客户承诺单一组合或自助安装，装配/终核走网点或上门"
            )
        else:
            actions.append("按检查建议排除条件后，以网点/上门装配方式交付")
    elif deliverable_kits and not questions["next_remote_questions"] and not bundle["chassis_wobble_reported"]:
        status_code = "ready_to_deliver"
        status_zh = "候选已收敛：存在对所有剩余解释都安全的组合，可交付"
        can_deliver = True
        self_fit_cleared = True
        actions.append("按可交付组合拣发，不带入未被依据支持的转接件")
    else:
        status_code = "needs_evidence"
        status_zh = "候选尚未收敛到对所有剩余世界都安全的组合，暂不交付"
        actions.append("按推荐检查收窄候选；若只剩 C4 可区分，则安排到店/上门")

    if questions["onsite_required_for_discrimination"] and status_code not in {
        "shipped_liaison_review", "promised_suspended_review", "independent_proceed"
    }:
        actions.append("远程可区分手段已用尽或不安全：安排 C4 到店/上门终核")

    return {
        "order": order,
        "status": status_code,
        "status_zh": status_zh,
        "can_deliver": can_deliver,
        "part_can_prepare": part_can_prepare,
        "customer_self_fit_cleared": self_fit_cleared,
        "hold": hold,
        "liaison_escalation": liaison_escalation,
        "independent_proceed": is_upholstery_only,
        "actions": actions,
        "independent_checks": independent_checks,
        "parts_in_use": parts_in_use,
        "certain_now": certain_now,
        "recommended_kit": recommended_kit,
        "recommend_basis": recommend_basis,
        "recommended_kit_verdict": recommended_verdict,
        "symptom_hypotheses": symptom_hypotheses,
        "models": [models_by_plate[w.plate] for w in {w.plate: w for w in worlds}.values()],
        "worlds": [w.as_dict() for w in worlds],
        "facts": bundle["fact_dicts"],
        "statements": bundle["statements"],
        "alias_mentions": bundle["alias_mentions"],
        "symptoms": bundle["symptoms"],
        "looseness_excluded": bundle["looseness_excluded"],
        "measurements": bundle["measurements"],
        "ambiguous_history": bundle["ambiguous_history"],
        "secondary_issues": bundle["secondary_issues"],
        "committed_kit": committed_kit,
        "committed_kit_verdict": kit_verdict,
        "candidate_kit_verdicts": candidate_kits,
        "deliverable_kits": deliverable_kits,
        "onsite_kits": onsite_kits,
        "questions": questions,
        "retests": retests,
        "commitments": commitments,
        "outbound": outbound,
        "shipments": shipments,
    }


def analyze(groups: dict[str, Any]) -> dict[str, Any]:
    parsed = parse_catalog(groups["catalog"])
    orders: list[str] = []
    for r in groups["service"]:
        if r.get("order") not in orders:
            orders.append(r["order"])
    decisions = {order: advise_order(order, groups, parsed) for order in orders}
    dossiers = build_dossiers(groups, parsed)
    liaison = liaison_index(groups, decisions, dossiers)
    return {
        "alias_index": build_alias_index(groups["models"]),
        "catalog_rules": parsed["rules"],
        "marketing_claims_overridden": parsed["marketing"],
        "dimensions": parsed["dimensions"],
        "orders": decisions,
        "delivery_dossiers": dossiers,
        "liaison": liaison,
    }


class Session:
    """一张维修单的可增量问答会话。

    新答复通过 :meth:`add_reply` 到达后，:meth:`advice` 总是从当前已知情况
    重新计算候选世界、检查适用性和下一问，而不是沿用上一轮脚本。
    “挡住/没看出来”类答复不会收窄候选。
    """

    def __init__(self, groups: dict[str, Any], order: str):
        self.groups = copy.deepcopy(groups)
        self.groups.setdefault("replies", [])
        self.order = order
        self._parsed = parse_catalog(self.groups["catalog"])

    @classmethod
    def without_replies(cls, groups: dict[str, Any], order: str) -> "Session":
        g = copy.deepcopy(groups)
        g["replies"] = [r for r in g.get("replies", []) if r.get("order") != order]
        return cls(g, order)

    def add_reply(self, reply: dict[str, Any]) -> dict[str, Any]:
        reply = dict(reply)
        reply.setdefault("order", self.order)
        if "id" not in reply:
            existing = {r["id"] for r in self.groups["replies"]}
            n = 1
            while f"X{n}" in existing:
                n += 1
            reply["id"] = f"X{n}"
        self.groups["replies"].append(reply)
        return self.advice()

    def advice(self) -> dict[str, Any]:
        return advise_order(self.order, self.groups, self._parsed)

    def next_questions(self) -> list[dict[str, Any]]:
        return self.advice()["questions"]["next_remote_questions"]


# ---------------------------------------------------------------------------
# 联络员查询
# ---------------------------------------------------------------------------

def liaison_index(
    groups: dict[str, Any],
    decisions: dict[str, dict[str, Any]] | None = None,
    dossiers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if decisions is None or dossiers is None:
        full = analyze(groups)
        decisions = decisions or full["orders"]
        dossiers = dossiers or full["delivery_dossiers"]

    shipped: list[dict[str, Any]] = []
    pending_holds: list[dict[str, Any]] = []
    for order, decision in decisions.items():
        if decision["shipments"]:
            shipped.append(liaison_dossier(groups, order, decision, dossiers))
        if decision["hold"]:
            codes = sorted({
                r["code"]
                for v in (decision["committed_kit_verdict"] or {}).get("world_verdicts", [])
                for r in v["verdict"]["reasons"]
                if v["verdict"]["status"] == "unsafe"
            })
            pending_holds.append(
                {
                    "order": order,
                    "promised_parts": decision["committed_kit"],
                    "status_zh": decision["status_zh"],
                    "reason_codes": codes,
                }
            )
    return {"shipped_cases": shipped, "promised_holds": pending_holds}


def liaison_dossier(
    groups: dict[str, Any],
    order: str,
    decision: dict[str, Any] | None = None,
    dossiers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    parsed = parse_catalog(groups["catalog"])
    decision = decision or advise_order(order, groups, parsed)
    dossiers = dossiers or build_dossiers(groups, parsed)
    order_dossiers = [d for d in dossiers if d["order"] == order]

    # 当时确认：复测之前的铭牌/脚架证据
    then_facts = [
        f for f in decision["facts"]
        if f["variable"] in {"plate", "base"} and f["provenance"] != "engineer_retest"
    ]
    now_facts = [
        f for f in decision["facts"]
        if f["variable"] in {"plate", "base"} and f["provenance"] == "engineer_retest"
    ]

    unsafe_possibilities: list[dict[str, Any]] = []
    kit_verdict = decision.get("committed_kit_verdict")
    committed = set(decision.get("committed_kit") or [])
    if kit_verdict:
        for wv in kit_verdict["world_verdicts"]:
            if wv["verdict"]["status"] in {"unsafe", "invalid_world"}:
                unsafe_possibilities.append(
                    {
                        "world": wv["world"],
                        "proposed_kit_adds": sorted(committed),
                        "note": (
                            "即使该世界中 J03 原不在场，拟寄/已寄组合本身包含 J03，"
                            "装上后仍形成禁用组合。"
                            if "J03" in committed and wv["world"]["adapter"] is False
                            else ""
                        ),
                        "problems": [
                            {"code": r["code"], "message": r["message"], "sources": r["sources"]}
                            for r in wv["verdict"]["reasons"]
                            if r["code"] not in {"R48_SMALL_OK", "R52_LARGE_F20_OK", "R48_LARGE_J03_F20_OK"}
                        ],
                    }
                )

    recommendation = (
        "J03 在 CY-D18 上有明确工程禁用（I2），且 R48 大口路径又强制 J03（I1），"
        "属于工程通告禁止组合：建议联络员优先评估请客户暂停使用该椅/该组合，"
        "并尽快安排上门由维修员核对铭牌、底盘开口与刻印后更换为允许的大口直接组合（如 R52+F20）。"
        "不要远程引导客户自行拆除转接座或继续坐试。最终是否立即停用由联络员判定。"
    )

    return {
        "order": order,
        "shipped": decision["shipments"],
        "outbound_records": decision["outbound"],
        "confirmed_at_the_time": then_facts,
        "checks_passed_at_time": [c for d in order_dossiers for c in d["checks_passed_at_time"]],
        "chosen_combo_at_branch": [d for d in order_dossiers],
        "later_findings": now_facts or [{"text": r.get("text"), "source": r["id"]} for r in decision["retests"]],
        "unsafe_assemblies_that_remain_possible": unsafe_possibilities,
        "marketing_claim_not_authority": [m for m in parsed["marketing"] if m["part"] == "J03"],
        "liaison_options": ["暂停使用该组合", "安排上门检查/更换", "继续观察（需记录理由）"],
        "recommendation": recommendation,
        "certain_now": [
            "复测铭牌 CY-D18、大口底盘",
            "J03 对 CY-D18 为工程禁用（I2）；营销页“适配轻帆二代/旧椅即插即用”（I5）不能覆盖",
        ],
    }


def independent_items(groups: dict[str, Any]) -> list[dict[str, Any]]:
    """列出可独立核实/处理、不应被同信他单挂起的椅或问题。"""
    result = []
    seen_orders: set[str] = set()
    for row in groups["service"]:
        text = row.get("text", "")
        if ("另提到" in text or "与摇晃无关" in text or "另一" in text) and row["order"] not in seen_orders:
            seen_orders.add(row["order"])
            decision = advise_order(row["order"], groups)
            result.append(
                {
                    "order": row["order"],
                    "source": row["id"],
                    "associated": row.get("associated"),
                    "text": text,
                    "status": decision["status"],
                    "status_zh": decision["status_zh"],
                    "hold": decision["hold"],
                    "separable_reason": "资料格式规定同一维修单按一把椅处理；跨单 associated 仅引用上下文，挂起不跨单传播",
                }
            )
    return result


# ---------------------------------------------------------------------------
# 文本报告
# ---------------------------------------------------------------------------

def render_markdown(result: dict[str, Any], groups: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# 远程售后换件决定报告（由 advisor.py 生成）")
    lines.append("")
    lines.append("> 型号、地点、业务均为合成资料。安全判定只采用 catalog 中 authority=engineering 的说明；")
    lines.append("> advertising 的“通用/即插即用”宣称不覆盖工程禁令。")
    lines.append("")

    lines.append("## 别名索引")
    lines.append("")
    for alias, ids in result["alias_index"].items():
        models = [next(m for m in groups["models"] if m["id"] == i) for i in ids]
        desc = "、".join(f"{m['id']} {m['plate']}（{'小口' if model_mouth(m)=='small' else '大口'}）" for m in models)
        lines.append(f"- “{alias}” → {desc}")
    lines.append("")

    for order, d in result["orders"].items():
        lines.append(f"## {order}　{d['status_zh']}")
        lines.append("")
        lines.append(f"- 状态码：`{d['status']}`；可交付：**{'是' if d['can_deliver'] else '否'}**；规格件可备货：**{'是' if d.get('part_can_prepare') else '否'}**；允许客户自助装：**{'是' if d.get('customer_self_fit_cleared') else '否'}**；挂起：**{'是' if d['hold'] else '否'}**；联络员升级：**{'是' if d['liaison_escalation'] else '否'}**")
        if d["actions"]:
            lines.append("- 建议动作：")
            for a in d["actions"]:
                lines.append(f"  - {a}")
        if d.get("certain_now"):
            lines.append("- 现在即可确定的结论：")
            for c in d["certain_now"]:
                lines.append(f"  - {c}")

        guesses = [s for s in d["statements"] if s["provenance"] == "customer_guess"]
        if guesses:
            lines.append("- 客户猜测（只作线索，不作为装配/铭牌事实）：")
            for s in guesses:
                lines.append(f"  - {s['source']}：{s['text']}")

        if d.get("parts_in_use"):
            p = d["parts_in_use"]
            adapter_zh = {True: "J03 在", False: "无 J03（证据收窄）", None: "J03 未知"}[p["adapter_J03_present"]]
            lines.append(
                f"- 在用部件辨认：脚架={p['base'] or '未知'}；转接座={adapter_zh}；"
                f"杆规格匹配={p['rod_spec_part'] or '未测得'}；文本出现物料号={','.join(p['mentioned_parts']) or '无'}"
            )

        if d["facts"]:
            lines.append("- 当前采用的证据：")
            for f in d["facts"]:
                if f["variable"] in {"plate", "base", "adapter"}:
                    lines.append(f"  - `{f['variable']}={f['value']}` — {f['source']}（{f['provenance_zh']}）")
        if d["ambiguous_history"]:
            lines.append("- 历史表述消歧：")
            for h in d["ambiguous_history"]:
                lines.append(f"  - {h['source']}：{h['reading']}")
        if d["measurements"]:
            lines.append("- 尺寸/诉求辨认：")
            for m in d["measurements"]:
                if "normalized_value_mm" in m:
                    lines.append(f"  - {m['source']}：“{m['raw']}”数字 {m['number']} 缺单位；按工程规格唯一对应 {m.get('spec_match_part')}（5.2 厘米=52 mm），单位缺失已留痕")
                elif "customer_request" in m:
                    lines.append(f"  - {m['source']}：{m['customer_request']} → 可能被理解为 {m['candidate_part']}，但属客户诉求，不能压过实测/工程规格")
        lines.append(f"- 剩余装配解释数：{len(d['worlds'])}")
        if d.get("recommended_kit"):
            rv = d["recommended_kit_verdict"]
            lines.append(
                f"- 规格锚定推荐件：**{' + '.join(d['recommended_kit'])}**"
                f"（依据 {d['recommend_basis']}）；跨解释结论：`{rv['overall']}`"
            )
        if d["committed_kit"]:
            cv = d["committed_kit_verdict"]
            lines.append(f"- 网点实际选择组合：**{' + '.join(d['committed_kit'])}**；跨解释结论：`{cv['overall']}`")
            for br in cv["blocking_reasons"]:
                lines.append(f"  - ⛔ {br['message']}（依据 {', '.join(br['sources'])}）")
        if d["deliverable_kits"]:
            lines.append(f"- 在全部剩余解释下都不触发禁令的候选：**{'、'.join(d['deliverable_kits'])}**（“允许”不等于“规格推荐”）")
        if d["onsite_kits"]:
            lines.append(f"- 规格成立但需现场条件的组合：{'、'.join(d['onsite_kits'])}")
        if d.get("symptom_hypotheses"):
            lines.append("- 仍可成立的症状解释：")
            for h in d["symptom_hypotheses"]:
                lines.append(f"  - `{h['id']}` [{h['status']}]：{h['explanation']}")
        if d.get("independent_checks"):
            for chk in d["independent_checks"]:
                lines.append(f"- 独立核实方式：{chk}")

        q = d["questions"]
        if q.get("compatibility_relevant", True) is False:
            lines.append("- 本单不涉及底盘/脚架兼容性，检查卡 C1–C4 与该饰面问题无关，不得被 OX1/OX4 的挂起连带。")
        if q["next_remote_questions"] and d["status"] not in {"shipped_liaison_review", "promised_suspended_review"}:
            lines.append("- 现在可问的安全/有区分度远程检查（安全且能区分时才比较 effort，按用时排序）：")
            for c in q["next_remote_questions"]:
                lines.append(f"  - `{c['id']}`（{c['channel']}/effort {c['effort']}）{c['instruction']}")
        elif q["next_remote_questions"]:
            lines.append("- 若联络员判断需要补证（客户沟通应由联络员统一安全口径），可使用：")
            for c in q["next_remote_questions"]:
                lines.append(f"  - `{c['id']}`（{c['channel']}/effort {c['effort']}）{c['instruction']}")
        if q.get("optional_safe_checks"):
            lines.append(
                "- 对所有候选都安全、可作出库留证但当前不改变决定的问法："
                + "、".join(f"`{cid}`" for cid in q["optional_safe_checks"])
                + "（C1 查看外露完整铭牌、不推椅）"
            )
        for c in q["cards"]:
            if c["id"] in q["blocked_question_ids"]:
                lines.append(f"  - 🚫 `{c['id']}` 现在不适用：{c['recommendation']}")
        if q["onsite_required_for_discrimination"]:
            lines.append("- 远程已无法安全区分，需到店/上门确认：")
            for item in q["onsite_confirmations"]:
                lines.append(f"  - {item}")
        lines.append("")

    lines.append("## 交付档案：网点实际选择 / 当时通过的检查 / 说明依据")
    lines.append("")
    for d in result["delivery_dossiers"]:
        lines.append(f"### {d['order']} / {d['record']}（{d['phase']}，{d.get('branch') or '未记网点'}）")
        lines.append(f"- 实际选择部件：**{' + '.join(d['chosen_parts'])}**")
        if d["checks_passed_at_time"]:
            lines.append("- 当时通过/留档的检查：")
            for c in d["checks_passed_at_time"]:
                if "card" in c:
                    lines.append(f"  - {c['reply']} → {c['card']}：{c['instruction']} 客户原话：“{c['customer_text']}”（{c.get('received')}）")
                else:
                    lines.append(f"  - {c['source_record']} {c['kind']}：{c['finding']}")
        else:
            lines.append("- 当时没有 verified 远程答复；仅有单据文字。")
        for ar in d.get("associated_records", []):
            lines.append(f"- 关联较早记录 {ar['source']}（{ar['phase']}）：{ar['text']}")
        lines.append(f"- 产品说明依据（工程）：{', '.join(d['engineering_basis'])}；营销来源 {', '.join(d['marketing_sources_not_authority']) or '无'} 不作为安全依据。")
        if d["shipments"]:
            for s in d["shipments"]:
                lines.append(f"- 实际寄出：{s['id']} {' + '.join(s['parts'])}（{s.get('branch')}）——出库事实不按复测改写")
        if d["retests"]:
            lines.append("- 后续复测：")
            for r in d["retests"]:
                lines.append(f"  - {r['source']}：{r['text']}")
        lines.append("")

    lines.append("## 联络员视图")
    lines.append("")
    for sh in result["liaison"]["shipped_cases"]:
        lines.append(f"### 已寄出：{sh['order']}")
        for s in sh["shipped"]:
            lines.append(f"- 包裹 {s['id']}：{' + '.join(s['parts'])}（{s.get('branch')}）")
        lines.append("- 当初确认：")
        for f in sh["confirmed_at_the_time"]:
            lines.append(f"  - `{f['variable']}={f['value']}` — {f['source']}（{f['provenance_zh']}）")
        for c in sh["checks_passed_at_time"]:
            if "card" in c:
                lines.append(f"  - 检查 {c['card']}（{c['reply']}）：“{c['customer_text']}”")
            else:
                lines.append(f"  - 初检：{c.get('finding')}")
        lines.append("- 后续发现：")
        for f in sh["later_findings"]:
            if "value" in f:
                lines.append(f"  - `{f['variable']}={f['value']}` — {f['source']}（{f['provenance_zh']}）")
            else:
                lines.append(f"  - {f.get('source')}：{f.get('text')}")
        lines.append("- 仍可能不安全的装配（含已寄组合装上后的结果）：")
        for up in sh["unsafe_assemblies_that_remain_possible"]:
            w = up["world"]
            mouth_zh = {"small": "小口", "large": "大口"}.get(w["mouth"], w["mouth"])
            adapter_zh = {True: "有 J03", False: "无 J03", None: "J03 未知"}[w["adapter"]]
            lines.append(f"  - 现场解释：{w['plate']} / {mouth_zh} / base={w['base']} / 原车{adapter_zh}；该决定寄出的组合={'+'.join(up['proposed_kit_adds'])}")
            if up.get("note"):
                lines.append(f"    - {up['note']}")
            for p in up["problems"]:
                lines.append(f"    - ⛔ {p['message']}（{', '.join(p['sources'])}）")
        lines.append("- 现在可确定的结论：")
        for c in sh["certain_now"]:
            lines.append(f"  - {c}")
        lines.append(f"- 联络员可选处置：{'、'.join(sh['liaison_options'])}")
        lines.append(f"- 系统建议（最终由联络员判断）：{sh['recommendation']}")
    for h in result["liaison"]["promised_holds"]:
        lines.append(f"- 挂起待复核：{h['order']} 承诺件 {'+'.join(h['promised_parts'] or [])}；原因码 {', '.join(h['reason_codes'])}")
    lines.append("")

    lines.append("## 独立事项（挂起不跨单传播）")
    lines.append("")
    for item in independent_items(groups):
        lines.append(f"- {item['order']}（{item['source']}→associated {item.get('associated')}）：{item['text']}")
        lines.append(f"  - 结论：{item['status_zh']}；挂起={item['hold']}。{item['separable_reason']}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="座椅换件兼容与安全交付决定引擎")
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_DATA, help="records.json 路径")
    parser.add_argument("--order", help="只输出某张维修单的 JSON 决定，例如 OX3")
    parser.add_argument("--liaison", nargs="?", const="ALL", help="联络员视图；可给单号，如 --liaison OX4")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    parser.add_argument("--report", type=Path, help="把中文 Markdown 报告写入该路径")
    args = parser.parse_args(argv)

    groups = load_records(args.path)

    if args.order:
        parsed = parse_catalog(groups["catalog"])
        print(json.dumps(advise_order(args.order, groups, parsed), ensure_ascii=False, indent=2))
        return 0

    result = analyze(groups)

    if args.liaison:
        if args.liaison == "ALL":
            payload = result["liaison"]
        else:
            payload = liaison_dossier(groups, args.liaison)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if args.report:
        args.report.write_text(render_markdown(result, groups), encoding="utf-8")
        print(f"已写入 {args.report}")
        return 0

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0

    print(render_markdown(result, groups))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
