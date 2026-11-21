"""售后换件判定命令行。

用法（在项目根目录）：
  python3 -I aftersales_cli.py data/records.json overview
  python3 -I aftersales_cli.py data/records.json order OX1
  python3 -I aftersales_cli.py data/records.json checks OX3
  python3 -I aftersales_cli.py data/records.json holds
  python3 -I aftersales_cli.py data/records.json order OX1 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# -I 隔离模式下 sys.path 不含脚本目录，显式补入；仍不加载环境 site 包
sys.path.insert(0, str(Path(__file__).resolve().parent))

from inspect_records import load_records
from aftersales.decisions import analyze, analyze_order, case_to_dict


def _print_case(case) -> None:
    print(f"工单 {case.order}　状态：{case.status}")
    if case.symptom:
        print(f"  投诉症状：{case.symptom}")
    if case.customer_request:
        print(f"  客户点名/请求（非现状）：{'、'.join(case.customer_request)}")
    print(f"  当前铭牌：{'、'.join(case.current_plates) or '无'}")
    if case.historical_plates:
        print(f"  历史抄牌（已留档不作现状）：{'、'.join(case.historical_plates)}")
    print(f"  尚未排除解释：{'、'.join(case.residual_plates) or '无'}")
    print(f"  当前在用部件：{'、'.join(case.current_parts) or '未能从记录确认'}")
    if case.temporary_history:
        print("  临时测试历史（不算装配事实）：")
        for item in case.temporary_history:
            print(f"    - {item}")
    deliverable = [o for o in case.options if o.deliverable]
    blocked = [o for o in case.options if not o.deliverable]
    if deliverable:
        print("  可交付组合（在所有未排除解释下均通过工程规则）：")
        for option in deliverable:
            print(f"    ✓ {'+'.join(option.parts)}　依据 {','.join(option.bases)}")
    if blocked:
        print(f"  不可交付组合 {len(blocked)} 个，典型禁用：")
        shown = set()
        for option in blocked:
            for violation in option.blocking_violations:
                key = violation["rule_id"]
                if key in shown:
                    continue
                shown.add(key)
                print(f"    ✗ {'+'.join(option.parts)}：{violation['reason']}（规则 {key}）")
            if len(shown) >= 4:
                break
    if case.promised:
        print(f"  已承诺未寄：{'+'.join(case.promised['parts'])}（{case.promised['record']}，"
              f"依据检查 {','.join(case.promised['verified']) or '无'}）")
    if case.shipment:
        print(f"  已出库：{'+'.join(case.shipment['parts'])}（{case.shipment['record']}）")
    if case.hold_reason:
        print(f"  挂起原因：{case.hold_reason}")
    if case.liaison:
        print("  联络员交接：")
        print(f"    寄出组合：{'+'.join(case.liaison['shipped_parts'])}")
        seen = set()
        for violation in case.liaison["unsafe_interpretation"]:
            if violation["rule_id"] in seen:
                continue
            seen.add(violation["rule_id"])
            print(f"    仍不安全的组装可能：{violation['reason']}")
        print("    当初确认内容可查：" + "；".join(
            f"{h['record']}({h['phase']})" for h in case.liaison["what_was_confirmed"]))
        print("    可选动作：" + "；".join(case.liaison["action_choices"]))
    if case.independent_orders:
        print(f"  同信独立工单（不连带挂起）：{'、'.join(case.independent_orders)}")
    print("  说明依据与备注：")
    for note in case.notes:
        print(f"    · {note}")


def _print_checks(case) -> None:
    print(f"工单 {case.order} 的可继续检查（按安全与区分力排序）")
    for check in case.checks:
        if check["available"]:
            print(f"  {check['card_id']}（{check['channel']}，用时 {check['effort']}）"
                  f"区分：{check['distinguishes']}")
            print(f"      手册原句：{check['instruction']}")
        else:
            print(f"  {check['card_id']}（{check['channel']}）暂不可发起：{check['unavailable_reason']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="远程售后换件安全判定")
    parser.add_argument("path", type=Path, help="资料文件路径")
    parser.add_argument("command", choices=("overview", "order", "checks", "holds"))
    parser.add_argument("order", nargs="?", help="工单号，如 OX1")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    records = load_records(args.path)
    cases = analyze(records)

    if args.command == "overview":
        payload = {order: case_to_dict(case) for order, case in cases.items()}
        if args.json:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for order, case in cases.items():
                deliverable = [ "+".join(o.parts) for o in case.options if o.deliverable ]
                print(f"{order}　{case.status}　可交付候选：{', '.join(deliverable) or '无'}")
    elif args.command in ("order", "checks"):
        if not args.order:
            parser.error("该命令需要工单号")
        case = analyze_order(records, args.order)
        if args.json:
            print(json.dumps(case_to_dict(case), ensure_ascii=False, indent=2))
        elif args.command == "order":
            _print_case(case)
        else:
            _print_checks(case)
    else:  # holds
        rows = []
        for order, case in cases.items():
            if case.status in ("held", "liaison"):
                rows.append(case_to_dict(case))
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=2))
        else:
            for case in rows:
                print(f"{case['order']}　{case['status']}　{case.get('hold_reason') or '已寄出，待联络员处理'}")
            if not rows:
                print("无挂起或待交接工单")


if __name__ == "__main__":
    main()
