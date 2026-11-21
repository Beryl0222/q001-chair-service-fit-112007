"""远程检查卡：安全前置与候选区分。

原则：
- 查看外露铭牌（C1）对所有候选椅都安全，优先级最高；
- 要求顾客坐、转动、翻动座椅的检查，必须先排除底盘/脚架松脱；
- 无法完成（被遮挡、看不清）记为 inconclusive，绝不视为否定证据；
- 远程无法区分候选时，明确指出到店检查要确认什么。
"""

from __future__ import annotations

from dataclasses import dataclass

# 检查卡的固定安全语义，依据 instruction 原句建模，不随数据漂移。
CARD_SEMANTICS = {
    "C1": {
        "channel": "远程",
        "safe_always": True,
        "moves_chair": False,
        "precondition": None,
        "distinguishes": "铭牌批次（CY-B17/CY-C17/CY-D18），直接定开口",
    },
    "C2": {
        "channel": "远程",
        "safe_always": True,
        "moves_chair": False,
        "precondition": None,
        "distinguishes": "脚架银环（F19/F20）；外侧被遮挡时只能记无法辨认",
    },
    "C3": {
        "channel": "远程",
        "safe_always": False,
        "moves_chair": True,
        "precondition": "已经排除底盘与脚架松脱并确认椅身直立",
        "distinguishes": "晃动来源（杆/转接件/底盘），区分换杆能否解决投诉",
    },
    "C4": {
        "channel": "到店",
        "safe_always": True,
        "moves_chair": True,
        "precondition": "由维修员固定椅身后进行",
        "distinguishes": "底盘开口实物与转接座刻印，远程区分不了的最终依据",
    },
}


@dataclass(frozen=True)
class CheckOption:
    card_id: str
    channel: str
    instruction: str
    effort: int
    safe_always: bool
    precondition: str | None
    distinguishes: str
    available: bool
    unavailable_reason: str | None


def card_safety(card: dict, looseness_ruled_out: bool) -> CheckOption:
    """评估一张检查卡当前能否安全发起。"""
    semantic = CARD_SEMANTICS[card["id"]]
    available = True
    reason = None
    if not semantic["safe_always"] and not looseness_ruled_out:
        available = False
        reason = f"前置条件未满足：{semantic['precondition']}，不能要求顾客坐或翻动座椅"
    return CheckOption(
        card_id=card["id"], channel=card["channel"], instruction=card["instruction"],
        effort=card["effort"], safe_always=semantic["safe_always"],
        precondition=semantic["precondition"], distinguishes=semantic["distinguishes"],
        available=available, unavailable_reason=reason,
    )


def reply_is_inconclusive(reply_text: str) -> bool:
    """答不上来/被遮挡属于未决，不得当作否定候选的证据。"""
    markers = ("没看出来", "无法辨认", "看不清", "挡住", "看不到", "没法看")
    return any(marker in reply_text for marker in markers)


def plan_checks(records: dict, candidate_plates, needs_wobble_source: bool,
                looseness_ruled_out: bool, blocked_cards=()) -> list[CheckOption]:
    """给出当前已知情况下可安全继续的检查顺序。

    优先不动椅、顾客一次回复能说明白的办法；远程全部区分不了才落到 C4。
    blocked_cards 是已被答复证明走不通（如外侧被桌挡住）的检查。
    """
    options = [
        card_safety(card, looseness_ruled_out)
        for card in records["inspection_cards"]
    ]
    options = [opt for opt in options if opt.card_id not in blocked_cards]
    multiple_plates = len(set(candidate_plates)) > 1

    def rank(opt: CheckOption) -> tuple:
        if not opt.available:
            return (3, 0, opt.effort)
        if opt.channel == "到店":
            return (2, 0, opt.effort)
        # 多批次未定时 C1 区分力最强；单一批次时 C1 仅做复核
        if opt.card_id == "C1" and multiple_plates:
            return (0, 0, opt.effort)
        if opt.card_id == "C2":
            return (0, 1, opt.effort)
        if opt.card_id == "C1":
            return (1, 0, opt.effort)
        if opt.card_id == "C3" and needs_wobble_source:
            return (1, 1, opt.effort)
        return (1, 2, opt.effort)

    return sorted(options, key=rank)
