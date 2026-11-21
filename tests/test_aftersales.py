"""售后判定引擎测试：覆盖解析、规则、证据、检查与交付/挂起全链路。"""

import copy
from pathlib import Path
import unittest

from inspect_records import load_records
from aftersales.parsing import (
    chinese_number,
    extract_measurements,
    extract_parts,
    extract_plates,
    restriction_operator,
    split_sentences,
)
from aftersales.catalog import PartCatalog, evaluate_combo, sanctioned_combos
from aftersales.evidence import (
    current_assembly,
    evidence_kind,
    is_temporary,
    order_timeline,
)
from aftersales.checks import card_safety, plan_checks, reply_is_inconclusive
from aftersales.decisions import analyze, analyze_order

DATA = Path(__file__).resolve().parents[1] / "data" / "records.json"


class ParsingTest(unittest.TestCase):
    def test_chinese_number_with_decimal(self):
        self.assertEqual(chinese_number("四十八"), 48)
        self.assertEqual(chinese_number("五点二"), 5.2)

    def test_measurement_unit_normalized_to_mm(self):
        notes = [
            "短杆接口四十八毫米",
            "五点二厘米杆配大口底盘",
            "杆的肩部要 52，单位没记",
        ]
        self.assertEqual([m.value_mm for m in extract_measurements(notes[0])], [48.0])
        cm = extract_measurements(notes[1])
        self.assertEqual(cm[0].value_mm, 52.0)
        bare = extract_measurements(notes[2])
        self.assertTrue(any(m.is_bare and m.value_mm == 52.0 for m in bare))

    def test_extract_plates_and_parts(self):
        self.assertEqual(extract_plates("初检抄牌 CY-C17"), ["CY-C17"])
        self.assertEqual(extract_parts("拆下 F19，装的是 F20"), ["F19", "F20"])

    def test_restriction_operators(self):
        self.assertEqual(restriction_operator("仅用于小口底盘"), "only_with")
        self.assertEqual(restriction_operator("禁止与F19旧脚架同装"), "prohibit_combo")
        self.assertIsNone(restriction_operator("即插即用，适配所有旧椅"))


class CatalogRuleTest(unittest.TestCase):
    def setUp(self):
        self.catalog = PartCatalog(load_records(DATA))

    def test_alias_resolves_to_multiple_batches(self):
        models = self.catalog.resolve_alias("轻帆二代")
        self.assertEqual({m.plate for m in models}, {"CY-B17", "CY-C17"})

    def test_bare_52_matches_r52_only(self):
        self.assertEqual(self.catalog.match_part_by_mm(52.0), ("R52",))

    def test_advertising_cannot_override_engineering_ban(self):
        # I5 营销页声称 J03 旧椅即插即用；工程规则 R4/R5 仍必须生效
        violations = evaluate_combo(self.catalog, ["J03", "F19"], "CY-C17")
        self.assertTrue(any(v.rule_id == "R4" for v in violations))
        self.assertTrue(evaluate_combo(self.catalog, ["J03"], "CY-D18"))

    def test_r48_combo_rules_by_mouth(self):
        # 小口：R48 单用即可；大口：必须 J03+F20；D18 上 J03 被禁
        self.assertEqual(evaluate_combo(self.catalog, ["R48"], "CY-B17"), [])
        self.assertTrue(evaluate_combo(self.catalog, ["R48"], "CY-C17"))
        self.assertEqual(
            evaluate_combo(self.catalog, ["R48", "J03", "F20"], "CY-C17"), [])
        self.assertTrue(
            any(v.rule_id == "R5" for v in
                evaluate_combo(self.catalog, ["R48", "J03", "F20"], "CY-D18")))

    def test_sanctioned_combos_per_plate(self):
        small = sanctioned_combos(self.catalog, "CY-B17")
        large = sanctioned_combos(self.catalog, "CY-D18")
        self.assertIn(frozenset({"R48", "F19"}), small)
        self.assertNotIn(frozenset({"R52"}), small)
        self.assertIn(frozenset({"R52", "F20"}), large)
        # D18 上任何含 J03 的组合都不允许
        self.assertFalse(any("J03" in combo for combo in large))


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        self.records = load_records(DATA)

    def test_guess_vs_engineer_vs_request(self):
        self.assertEqual(evidence_kind("我猜和轻帆二代一样，大概没换过", "来件"),
                         "customer_guess")
        self.assertEqual(evidence_kind("维修回单记得已拆下 F19", "来件"),
                         "engineer_confirm")
        self.assertEqual(evidence_kind("铭牌 CY-B17，要短杆", "来件"), "request")

    def test_temporary_test_not_current_assembly(self):
        events = order_timeline(self.records, "OX1")
        assembly = current_assembly(events)
        self.assertTrue(all(is_temporary(e.text) for e in assembly["temporary_notes"]))
        # F19 是临时试装后拆下，当前脚架只能是 F20
        self.assertEqual(tuple(assembly["installed"]), ("F20",))
        self.assertIn("F19", assembly["removed"])

    def test_retest_overrides_initial_reading(self):
        case = analyze_order(self.records, "OX1")
        self.assertEqual(case.current_plates, ("CY-D18",))
        self.assertEqual(case.historical_plates, ("CY-C17",))


class DecisionTest(unittest.TestCase):
    def setUp(self):
        self.records = load_records(DATA)
        self.cases = analyze(self.records)

    def _deliverable(self, order):
        return {"+".join(o.parts) for o in self.cases[order].options if o.deliverable}

    def test_ox1_promised_combo_held_after_retest(self):
        case = self.cases["OX1"]
        self.assertEqual(case.status, "held")
        self.assertEqual(case.promised["parts"], ("R48", "J03"))
        # D18 禁用 J03，且 B17 解释未排除，没有任何组合可交付
        self.assertEqual(self._deliverable("OX1"), set())
        self.assertIn("CY-B17", case.residual_plates)

    def test_ox2_small_mouth_delivers_r48_f19(self):
        case = self.cases["OX2"]
        deliverable = self._deliverable("OX2")
        self.assertIn("R48+F19", deliverable)
        self.assertNotIn("R52", deliverable)
        self.assertNotIn("R48+F19+J03", deliverable)  # J03 禁与 F19 同装

    def test_ox3_bare_52_resolves_to_r52_and_only_safe_combo(self):
        case = self.cases["OX3"]
        deliverable = self._deliverable("OX3")
        # R52 直接配大口 F20；R48 路径只有按 I1 配齐 J03+F20 才允许
        self.assertEqual(deliverable, {"R52", "R52+F20", "R48+F20+J03"})
        self.assertEqual(case.customer_request, ("R52",))

    def test_ox4_shipped_unsafe_combo_goes_to_liaison(self):
        case = self.cases["OX4"]
        self.assertEqual(case.status, "liaison")
        self.assertIsNotNone(case.shipment)
        rule_ids = {v["rule_id"] for v in case.liaison["unsafe_interpretation"]}
        self.assertIn("R5", rule_ids)  # D18 禁用 J03
        self.assertTrue(case.liaison["what_was_confirmed"])

    def test_ox5_independent_issue_not_suspended_and_no_combo_eval(self):
        case = self.cases["OX5"]
        self.assertNotEqual(case.status, "held")
        self.assertEqual(case.options, [])
        self.assertTrue(case.unrelated_issues)
        self.assertIsNone(case.symptom)

    def test_independent_order_not_cascaded(self):
        # OX1 挂起，但同信的 OX5 保持可处理
        self.assertEqual(self.cases["OX1"].status, "held")
        self.assertEqual(self.cases["OX5"].independent_orders, ())
        self.assertIn("OX5", self.cases["OX1"].independent_orders)

    def test_inconclusive_reply_not_negative_evidence(self):
        case = self.cases["OX3"]
        blocked = {c["card_id"] for c in case.checks if not c["available"]}
        # C2 因遮挡被移出计划而非当作 F20 的否定；C3 是因安全前置锁定
        planned = {c["card_id"] for c in case.checks}
        self.assertNotIn("C2", planned)
        self.assertIn("C3", blocked)
        self.assertTrue(reply_is_inconclusive("外面被桌子挡住，没看出来"))

    def test_new_evidence_narrows_without_negating_unresolved_reading(self):
        # 后到的工程师读数加入 C17，但未明确否定 B17：两者并存，
        # 换件必须同时满足小口 B17 与大口 C17，R48 单用因此被禁
        records = copy.deepcopy(self.records)
        records["replies"].append({
            "id": "A8", "order": "OX2", "card": "C1", "received": "2026-11-30",
            "text": "师傅上门看到标签后贴，底盘开口是大的，铭牌 CY-C17。",
        })
        case = analyze_order(records, "OX2")
        self.assertEqual(set(case.current_plates), {"CY-B17", "CY-C17"})
        self.assertEqual(case.historical_plates, ())
        deliverable = {"+".join(o.parts) for o in case.options if o.deliverable}
        self.assertNotIn("R48", deliverable)
        # 只有在两种开口下都成立的组合保留：R48+J03+F20
        self.assertEqual(deliverable, {"R48+F20+J03"})

    def test_explicit_correction_moves_old_plate_to_history(self):
        # “其实是 D18”这类明确纠正措辞把旧抄牌移入历史档
        case = self.cases["OX1"]
        self.assertEqual(case.current_plates, ("CY-D18",))
        self.assertEqual(case.historical_plates, ("CY-C17",))


class CheckSafetyTest(unittest.TestCase):
    def setUp(self):
        self.records = load_records(DATA)

    def test_c3_locked_until_looseness_excluded(self):
        c3 = next(c for c in self.records["inspection_cards"] if c["id"] == "C3")
        locked = card_safety(c3, looseness_ruled_out=False)
        self.assertFalse(locked.available)
        unlocked = card_safety(c3, looseness_ruled_out=True)
        self.assertTrue(unlocked.available)

    def test_c1_safe_always_and_ranked_first_for_ambiguous_batch(self):
        plan = plan_checks(self.records, candidate_plates=("CY-B17", "CY-C17"),
                           needs_wobble_source=True, looseness_ruled_out=False)
        available = [c for c in plan if c.available]
        self.assertEqual(available[0].card_id, "C1")
        # 到店检查排在远程之后
        self.assertGreater([c.card_id for c in plan].index("C4"), 1)

    def test_c3_unlocks_with_new_evidence(self):
        # 模拟新回复明确排除松脱后，后续来电看到的计划应允许 C3
        records = load_records(DATA)
        records["replies"].append({
            "id": "A9", "order": "OX3", "card": "C3", "received": "2026-11-30",
            "text": "底盘和脚架连接处已检查，没有松动，椅身直立。",
        })
        case = analyze_order(records, "OX3")
        c3 = next(c for c in case.checks if c["card_id"] == "C3")
        self.assertTrue(c3["available"])


if __name__ == "__main__":
    unittest.main()
