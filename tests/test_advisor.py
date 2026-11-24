"""换件决定引擎的行为核对。

这些测试把业务要求固化为可执行断言：
别名歧义、单位归一、证据等级、临时换架的解释、营销不压工程、
候选世界与安全拦截、答复不收窄规则、C3 安全前提、挂起/寄出口径、
联络员档案以及挂起不跨单传播。
"""

import copy
import unittest
from pathlib import Path

from inspect_records import load_records
import advisor


ROOT = Path(__file__).resolve().parents[1]


class AdvisorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.groups = load_records(ROOT / "data" / "records.json")
        cls.result = advisor.analyze(cls.groups)
        cls.decisions = cls.result["orders"]

    # ---- 文本/单位/别名 -------------------------------------------------

    def test_chinese_dimensions_normalize_to_mm(self):
        dims48 = advisor.parse_dimensions("短杆接口四十八毫米")
        self.assertEqual(dims48[0]["value_mm"], 48.0)
        dims52 = advisor.parse_dimensions("五点二厘米杆")
        self.assertEqual(dims52[0]["value_mm"], 52.0)
        self.assertEqual(advisor.cn_number("四十八"), 48)
        self.assertAlmostEqual(advisor.cn_number("五点二"), 5.2)

    def test_missing_unit_measurement_is_flagged_but_matches_r52(self):
        meas = advisor.parse_measurement_claim("杆的肩部要 52，单位没记。")
        self.assertTrue(meas["unit_missing"])
        self.assertIsNone(meas["value_mm"])
        self.assertEqual(meas["spec_match_part"], "R52")
        self.assertEqual(meas["normalized_value_mm"], 52.0)

    def test_series_alias_resolves_to_two_different_interfaces(self):
        hits = advisor.resolve_name("轻帆二代", self.groups)
        self.assertEqual({m["plate"] for m in hits}, {"CY-B17", "CY-C17"})
        self.assertEqual({advisor.model_mouth(m) for m in hits}, {"small", "large"})
        d18 = advisor.resolve_name("轻帆城店款", self.groups)
        self.assertEqual([m["plate"] for m in d18], ["CY-D18"])
        # A2 里的简写 C17 也要能识别
        self.assertEqual(advisor.extract_plates("对着灰牌读的是 C17"), ["CY-C17"])

    def test_marketing_claim_is_kept_out_of_engineering_rules(self):
        parsed = advisor.parse_catalog(self.groups["catalog"])
        sources = {r["source"] for r in parsed["rules"]}
        self.assertNotIn("I5", sources)
        marketing_sources = {m["source"] for m in parsed["marketing"]}
        self.assertIn("I5", marketing_sources)
        codes = {r["kind"] for r in parsed["rules"]}
        self.assertIn("forbidden_pair", codes)
        self.assertIn("forbidden_plate", codes)
        self.assertIn("large_rod_requires", codes)

    # ---- OX2：干净的小口原架，R48 单杆可交付 ----------------------------

    def test_ox2_ready_with_r48_only_and_no_j03(self):
        d = self.decisions["OX2"]
        self.assertEqual(d["status"], "ready_to_deliver")
        self.assertTrue(d["can_deliver"])
        self.assertFalse(d["hold"])
        self.assertEqual(d["recommended_kit"], ["R48"])
        self.assertEqual(d["recommended_kit_verdict"]["overall"], "safe_to_deliver")
        kit_names = set(d["candidate_kit_verdicts"])
        # R48+J03 在 F19 世界必须被 I2 拦截
        self.assertEqual(
            d["candidate_kit_verdicts"]["R48+J03"]["overall"], "blocked_unsafe"
        )
        codes = {
            r["code"]
            for wv in d["candidate_kit_verdicts"]["R48+J03"]["world_verdicts"]
            for r in wv["verdict"]["reasons"]
        }
        self.assertIn("J03_F19_FORBIDDEN", codes)
        self.assertEqual(len(d["worlds"]), 1)

    # ---- OX3：52→R52；blocked 答复不是否定；C3 被门控；C4 兜底 ----------

    def test_ox3_spec_is_r52_not_short_rod_guess(self):
        d = self.decisions["OX3"]
        self.assertEqual(d["recommended_kit"], ["R52"])
        self.assertNotEqual(d["status"], "ready_to_deliver")
        self.assertFalse(d["can_deliver"])

    def test_ox3_blocked_c2_reply_does_not_negate_adapter(self):
        d = self.decisions["OX3"]
        adapters = {w["adapter"] for w in d["worlds"]}
        self.assertEqual(adapters, {True, False})
        c2 = next(c for c in d["questions"]["cards"] if c["id"] == "C2")
        self.assertFalse(c2["applicable_now"])
        self.assertEqual(c2["already_reply"], "A1")

    def test_ox3_c3_blocked_until_looseness_excluded(self):
        d = self.decisions["OX3"]
        c3 = next(c for c in d["questions"]["cards"] if c["id"] == "C3")
        self.assertFalse(c3["applicable_now"])
        self.assertFalse(c3["precondition_met"])
        self.assertTrue(any(h["id"] == "H_chassis_loose" for h in d["symptom_hypotheses"]))
        self.assertIn("C3", d["questions"]["blocked_question_ids"])
        self.assertTrue(d["questions"]["onsite_required_for_discrimination"])
        onsite_text = " ".join(d["questions"]["onsite_confirmations"])
        self.assertIn("松脱", onsite_text)

    def test_incremental_reply_recomputes_next_question(self):
        # 答复到达前：C2 是用时最低的可区分远程问法
        session = advisor.Session.without_replies(self.groups, "OX3")
        before = session.advice()
        self.assertEqual([c["id"] for c in before["questions"]["next_remote_questions"]], ["C2"])
        c3_before = next(c for c in before["questions"]["cards"] if c["id"] == "C3")
        # S4 未提晃，C3 不区分任何决定
        self.assertFalse(c3_before["discriminating_now"])

        # A1 到达后：C2 无观察、底盘晃 → C2/C3 都不可继续，转上门
        after = session.add_reply({
            "id": "A1x", "card": "C2", "received": "2026-11-29",
            "text": "外面被桌子挡住，没看出来，底盘还是会晃。",
        })
        self.assertEqual(after["questions"]["next_remote_questions"], [])
        self.assertEqual(set(after["questions"]["blocked_question_ids"]), {"C2", "C3"})
        self.assertTrue(after["questions"]["onsite_required_for_discrimination"])
        self.assertEqual({w["adapter"] for w in after["worlds"]}, {True, False})

    def test_positive_c2_reply_narrows_adapter(self):
        groups = copy.deepcopy(self.groups)
        groups["replies"].append({
            "id": "A9", "order": "OX3", "card": "C2",
            "text": "拍到了，脚架外侧有银环。",
        })
        d = advisor.advise_order("OX3", groups)
        self.assertEqual({w["adapter"] for w in d["worlds"]}, {True})

    def test_terse_replaced_note_is_not_current_assembly_fact(self):
        # 维修历史只写“已换”时，不能替它补出 F20 现状；F19 世界仍未排除，
        # 因而 J03 组合必须被安全拦截。
        groups = copy.deepcopy(self.groups)
        for row in groups["service"]:
            if row["id"] == "S2":
                row["text"] = "维修回单只写了两个字：已换。"
            if row["id"] == "S7":
                row["text"] = "复测读到同批椅铭牌其实是 CY-D18。"
        d = advisor.advise_order("OX1", groups)
        current_bases = [f["value"] for f in d["facts"] if f["variable"] == "base"]
        self.assertNotIn("F20", current_bases)
        bases_in_worlds = {w["base"] for w in d["worlds"]}
        self.assertEqual(bases_in_worlds, {"F19", "F20"})
        self.assertEqual(d["committed_kit_verdict"]["overall"], "blocked_unsafe")
        self.assertTrue(d["hold"])

    def test_later_looseness_exclusion_unblocks_c3(self):
        session = advisor.Session.without_replies(self.groups, "OX3")
        session.add_reply({
            "id": "A1x", "card": "C2",
            "text": "外面被桌子挡住，没看出来，底盘还是会晃。",
        })
        gated = session.advice()
        c3 = next(c for c in gated["questions"]["cards"] if c["id"] == "C3")
        self.assertFalse(c3["applicable_now"])

        # 新回复证明螺丝已紧固、底盘不晃 → 适用条件被重新满足，
        # 后续来电看到的下一问应重新包含 C3。
        after = session.add_reply({
            "id": "A4", "card": "C2", "received": "2026-11-30",
            "text": "师傅电话指导后把底盘螺丝拧紧了，现在底盘不晃了，椅子也放直立了。",
        })
        self.assertTrue(after["looseness_excluded"])
        c3_after = next(c for c in after["questions"]["cards"] if c["id"] == "C3")
        self.assertTrue(c3_after["precondition_met"])
        self.assertTrue(c3_after["applicable_now"])
        self.assertIn("C3", [c["id"] for c in after["questions"]["next_remote_questions"]])

    def test_chassis_not_moved_is_not_looseness_exclusion(self):
        self.assertFalse(advisor.detects_looseness_excluded("刚才读的是 C17，底盘没有挪过。"))
        self.assertFalse(advisor.detects_looseness_excluded("外面被挡住，底盘还是会晃。"))
        self.assertTrue(advisor.detects_looseness_excluded("螺丝已经拧紧，无松动。"))

    def test_intake_with_only_guess_prioritizes_c1_by_effort(self):
        # 最初只有 S1：灰牌磨没 + 猜“轻帆二代”。C1（effort1）应排在 C2 前，
        # 且客户猜测里的别名解析到 M1/M2 但仍标记为 customer_guess。
        groups = copy.deepcopy(self.groups)
        groups["service"] = [r for r in groups["service"] if r["id"] == "S1"]
        groups["replies"] = []
        d = advisor.advise_order("OX1", groups)
        next_ids = [c["id"] for c in d["questions"]["next_remote_questions"]]
        self.assertEqual(next_ids[0], "C1")
        c1 = next(c for c in d["questions"]["cards"] if c["id"] == "C1")
        self.assertTrue(c1["safe_for_all"])
        mention = next(a for a in d["alias_mentions"] if a["alias"] == "轻帆二代")
        self.assertEqual(set(mention["resolves_to"]), {"M1", "M2"})
        self.assertEqual(mention["provenance"], "customer_guess")

    # ---- OX1：临时换架消歧 + 承诺未寄挂起 + 档案 ------------------------

    def test_a3_temporary_old_base_is_not_current_fact(self):
        d = self.decisions["OX1"]
        base_facts = [f for f in d["facts"] if f["variable"] == "base"]
        self.assertTrue(all(f["value"] == "F20" for f in base_facts))
        self.assertTrue(any(h.get("temporary") for h in d["ambiguous_history"]))
        trial = [f for f in d["facts"] if f["variable"] == "base_trial_removed"]
        self.assertTrue(trial)

    def test_ox1_promised_kit_suspended_after_retest(self):
        d = self.decisions["OX1"]
        self.assertEqual(d["status"], "promised_suspended_review")
        self.assertTrue(d["hold"])
        self.assertFalse(d["can_deliver"])
        self.assertEqual(d["committed_kit"], ["R48", "J03"])
        self.assertEqual(d["committed_kit_verdict"]["overall"], "blocked_unsafe")
        codes = {
            r["code"]
            for r in d["committed_kit_verdict"]["blocking_reasons"]
        }
        self.assertIn("J03_D18_FORBIDDEN", codes)
        self.assertIn("R48_D18_NO_LEGAL_ROUTE", codes)

    def test_ox1_dossier_has_verified_c1_check(self):
        dossiers = {x["order"]: x for x in self.result["delivery_dossiers"]}
        ox1 = dossiers["OX1"]
        cards = [c.get("card") for c in ox1["checks_passed_at_time"]]
        self.assertIn("C1", cards)
        self.assertTrue(any(c.get("reply") == "A2" for c in ox1["checks_passed_at_time"]))

    def test_counterfactual_f19_with_j03_is_unsafe(self):
        # 即使没有 A3/S2，只要 F19 当前在用的世界未排除，J03 组合就必须拦截
        world = advisor.World("M1", "CY-B17", "small", base="F19", adapter=False)
        verdict = advisor.assess_kit(["R48", "J03"], world, advisor.parse_catalog(self.groups["catalog"]))
        self.assertEqual(verdict["verdict"]["status"], "unsafe")
        self.assertTrue(any(r["code"] == "J03_F19_FORBIDDEN" for r in verdict["verdict"]["reasons"]))

    # ---- OX4：已寄出 → 联络员档案 --------------------------------------

    def test_ox4_shipped_escalation_and_liaison_dossier(self):
        d = self.decisions["OX4"]
        self.assertEqual(d["status"], "shipped_liaison_review")
        self.assertTrue(d["liaison_escalation"])
        self.assertEqual(d["shipments"][0]["id"], "D1")
        dossier = advisor.liaison_dossier(self.groups, "OX4")
        then_plates = {f["value"] for f in dossier["confirmed_at_the_time"] if f["variable"] == "plate"}
        self.assertIn("CY-C17", then_plates)
        now_plates = {f["value"] for f in dossier["later_findings"] if f.get("variable") == "plate"}
        self.assertIn("CY-D18", now_plates)
        self.assertTrue(dossier["unsafe_assemblies_that_remain_possible"])
        messages = " ".join(
            p["message"]
            for up in dossier["unsafe_assemblies_that_remain_possible"]
            for p in up["problems"]
        )
        self.assertIn("CY-D18", messages)
        self.assertIn("暂停使用", dossier["recommendation"])
        # 营销页 I5 必须作为“无权威依据”暴露给联络员
        self.assertTrue(any(m["source"] == "I5" for m in dossier["marketing_claim_not_authority"]))

    def test_liaison_index_lists_hold_and_shipped(self):
        liaison = self.result["liaison"]
        self.assertEqual([c["order"] for c in liaison["shipped_cases"]], ["OX4"])
        holds = {h["order"]: h for h in liaison["promised_holds"]}
        self.assertIn("OX1", holds)
        self.assertIn("J03_D18_FORBIDDEN", holds["OX1"]["reason_codes"])

    # ---- OX5：独立事项不被挂起 -----------------------------------------

    def test_ox5_upholstery_independent_not_held(self):
        d = self.decisions["OX5"]
        self.assertEqual(d["status"], "independent_proceed")
        self.assertTrue(d["independent_proceed"])
        self.assertFalse(d["hold"])
        self.assertFalse(d["liaison_escalation"])
        self.assertNotIn("wobble", {s["type"] for s in d["symptoms"]})
        items = advisor.independent_items(self.groups)
        self.assertEqual([i["order"] for i in items], ["OX5"])
        self.assertFalse(items[0]["hold"])

    # ---- 安全评估矩阵抽检 ----------------------------------------------

    def test_r52_large_f20_is_safe(self):
        parsed = advisor.parse_catalog(self.groups["catalog"])
        w = advisor.World("M2", "CY-C17", "large", "F20", False)
        self.assertEqual(advisor.assess_kit(["R52"], w, parsed)["verdict"]["status"], "safe")

    def test_r48_large_requires_j03_and_f20(self):
        parsed = advisor.parse_catalog(self.groups["catalog"])
        w = advisor.World("M2", "CY-C17", "large", "F20", False)
        self.assertEqual(advisor.assess_kit(["R48"], w, parsed)["verdict"]["status"], "unsafe")
        ok = advisor.World("M2", "CY-C17", "large", "F20", True)
        self.assertEqual(advisor.assess_kit(["R48", "J03"], ok, parsed)["verdict"]["status"], "safe")

    def test_d18_has_no_legal_r48_route_but_r52_route(self):
        parsed = advisor.parse_catalog(self.groups["catalog"])
        w = advisor.World("M3", "CY-D18", "large", "F20", False)
        self.assertEqual(advisor.assess_kit(["R48", "J03"], w, parsed)["verdict"]["status"], "unsafe")
        self.assertEqual(advisor.assess_kit(["R52"], w, parsed)["verdict"]["status"], "safe")


if __name__ == "__main__":
    unittest.main()
