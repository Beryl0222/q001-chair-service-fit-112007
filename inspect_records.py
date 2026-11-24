"""读入资料包并列出可供检查的记录。"""

import argparse
import json
from pathlib import Path

# 决策引擎 advisor.py 中的常用名称也允许从本模块惰性导入，例如
# ``from inspect_records import advise_order``。惰性加载避免循环导入。
_ADVISOR_NAMES = {
    "advise_order",
    "analyze",
    "parse_catalog",
    "parse_dimensions",
    "resolve_name",
    "recommend_cards",
    "liaison_dossier",
    "liaison_index",
    "independent_items",
    "build_worlds",
    "assess_kit",
    "Session",
}


def __getattr__(name):
    if name in _ADVISOR_NAMES:
        import advisor
        return getattr(advisor, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def load_records(path):
    """读取 UTF-8 资料并检查记录编号。"""
    with Path(path).open(encoding="utf-8") as stream:
        groups = json.load(stream)
    if not isinstance(groups, dict) or not groups:
        raise ValueError("资料应包含非空的记录类别")
    for kind, records in groups.items():
        if not isinstance(records, list):
            raise ValueError(f"{kind} 应为记录数组")
        ids = set()
        for row in records:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                raise ValueError(f"{kind} 中存在没有编号的记录")
            if row["id"] in ids:
                raise ValueError(f"{kind} 中编号重复 {row['id']}")
            ids.add(row["id"])
    return groups


def main():
    parser = argparse.ArgumentParser(description="浏览合成原始资料")
    parser.add_argument("path", type=Path, help="资料文件路径")
    parser.add_argument("--list", action="store_true", help="列出记录类别与数量")
    args = parser.parse_args()
    records = load_records(args.path)
    if args.list:
        for kind, group in records.items():
            print(f"{kind}　{len(group)} 条")
    else:
        print(json.dumps(records, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
