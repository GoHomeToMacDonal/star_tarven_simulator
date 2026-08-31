"""跨卡牌共享的机制原语：折跃 / 孵化 / 供养 / 卵鞘。

这些是 handler 内部会调用的辅助函数，独立出来便于参数化 handler 复用。
"""

from __future__ import annotations

from typing import Dict

from star_tarven_simulator.constants.unit_prices import UNIT_PRICES
from star_tarven_simulator.constants.unit_type import BIOLOGICAL_UNITS, HERO_UNITS
from star_tarven_simulator.simulator.event import (
    AnyCardHatchEvent,
    AnyCardTeleportEvent,
)
from star_tarven_simulator.simulator.slot import Slot

# 卵鞘词条标签：卡牌拥有该标签时，被出售触发 :func:`sheath_hatch`。
SHEATH_TAG = "拥有卵鞘"


def teleport(slot: Slot, event, units: Dict[str, int]) -> None:
    """逐个单位选择折跃落点，并在至少一个单位成功加入后只广播一次事件。

    存在指定折跃目标（见 :attr:`Slot.teleport`）时，所有单位进入该目标；
    否则每个单位独立、带放回地随机选择一张场上的神族卡牌——因此折跃多个单位
    会执行多次随机选择，可能落到不同卡牌，也可能重复落到同一卡牌。场上没有
    合法神族落点时不添加任何单位、也不广播事件。
    """
    fixed_target = slot.teleport
    candidates = slot.protoss if fixed_target is None else []
    if fixed_target is None and not candidates:
        return

    teleported = 0
    for unit, cnt in units.items():
        for _ in range(max(0, cnt)):
            target = fixed_target or event.tarven.rng.choice(candidates)
            before = target.unit_count
            target.add_unit(unit, 1)
            teleported += target.unit_count - before
    if teleported > 0:
        event.tarven.trigger_any_card_event(AnyCardTeleportEvent(event.tarven, slot))


def hatch(slot: Slot, event, units: Dict[str, int]) -> None:
    """把单位孵化给相邻的虫族卡牌，并广播孵化事件。"""
    hatched: Dict[str, int] = {}
    for s in slot.neighbors:
        if s.tags.has("zerg"):
            for unit, cnt in units.items():
                if cnt > 0:
                    s.add_unit(unit, cnt)
                    hatched[unit] = hatched.get(unit, 0) + cnt
    if hatched:
        event.tarven.trigger_any_card_event(
            AnyCardHatchEvent(event.tarven, slot, hatched)
        )


def feed(slot: Slot, event, unit_type: str, price: int) -> None:
    """供养(price)：出售时，按精华数量 // price 把单位给右侧卡牌。

    若右侧属于原始虫群，则把精华也一并转移。
    """
    essence = slot.count("精华")
    right = slot.right
    if right is None or right.card_type is None or price <= 0:
        return
    gained = essence // price
    if gained > 0:
        right.add_unit(unit_type, gained)
    if right.tags.has("属于原始虫群") and essence > 0:
        right.add_unit("精华", essence)


def sheath_hatch(slot: Slot, tarven) -> None:
    """卵鞘词条：被出售时，注卵卡牌内单位价值最高的 min(n, m) 个非英雄生物单位。

    ``n`` = 酒馆等级；``m`` = 该卡牌内非英雄生物单位的个数（含重复份数）。
    单位按 ``UNIT_PRICES``（单位价值）降序选取；同单位价值随机选一个
    （先洗牌、再稳定降序，稳定排序保留洗牌顺序以打破平局）。
    """
    n = tarven.level

    # 展开为「每个单位」的列表（含重复份数）
    individuals = []
    for unit, cnt in slot.units.items():
        if unit in BIOLOGICAL_UNITS and unit not in HERO_UNITS:
            individuals.extend([unit] * cnt)

    if not individuals:
        return

    k = min(n, len(individuals))

    # 同单位价值随机：先洗牌，再按单位价值稳定降序（稳定排序保留洗牌顺序打破平局）
    tarven.rng.shuffle(individuals)
    individuals.sort(key=lambda u: -UNIT_PRICES.get(u, 0.0))

    payload: Dict[str, int] = {}
    for u in individuals[:k]:
        payload[u] = payload.get(u, 0) + 1
    if payload:
        tarven.larva(payload)
