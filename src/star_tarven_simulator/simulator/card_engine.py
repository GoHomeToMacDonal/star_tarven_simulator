"""卡牌引擎：负责把卡牌分配到槽位、复制 handler、以及三连合成。

相比旧版的修正：

* :meth:`CardEngine.merge_slots` 使用 ``handler.description``（旧版误用 ``handler.desc`` 会抛
  ``AttributeError``），并真正填充金色 handler（``card.gold_event_handlers``）。
* 孵化（虫卵）修正了左右邻居判断，并按统一签名广播 :class:`AnyCardHatchEvent(tarven, slot, units)`。
* 解析已在加载阶段完成（见 ``parsing`` 包），引擎只负责实例化，不再内嵌解析逻辑。
"""

from __future__ import annotations

from typing import Dict, List, Union

from star_tarven_simulator.constants.unit_type import BIOLOGICAL_UNITS
from star_tarven_simulator.simulator.base import AbstractCardEngine
from star_tarven_simulator.simulator.card import Card, Tags
from star_tarven_simulator.simulator.event import AnyCardHatchEvent
from star_tarven_simulator.simulator.event_handler import EventHandler
from star_tarven_simulator.simulator.slot import Slot


def _larva_action_handler(slot: Slot, event) -> None:
    """虫卵在回合开始时：若左右两侧均为虫族，则把生物单位复制给两侧并清空自身。"""
    if slot.card_type != "虫卵":
        return
    left, right = slot.left, slot.right
    if left is None or right is None:
        return
    if left.card_type is None or right.card_type is None:
        return
    if not left.tags.has("zerg") or not right.tags.has("zerg"):
        return

    hatched: Dict[str, int] = {}
    for unit, count in slot.units.items():
        if unit in BIOLOGICAL_UNITS:
            left.add_unit(unit, count)
            right.add_unit(unit, count)
            hatched[unit] = count

    event.tarven.trigger_any_card_event(
        AnyCardHatchEvent(event.tarven, slot, hatched)
    )
    # 清空虫卵
    event.tarven.slots[slot.index] = Slot(slot.index, event.tarven)


_larva_event_handler = EventHandler(
    None, None, "虫卵", _larva_action_handler, "round_start"
)


def _gold_miner_action_handler(slot: Slot, event) -> None:
    event.tarven.mineral += 1


_gold_miner_event_handler = EventHandler(
    None, None, "黄金矿工", _gold_miner_action_handler, "round_start"
)


class CardEngine(AbstractCardEngine):
    """卡牌引擎。卡牌的 handler 模板由解析器预先填充在 ``card.event_handlers`` /
    ``card.gold_event_handlers`` 上，本引擎只做实例化与合成。"""

    def __init__(self, cards: List[Card]):
        self.cards = cards
        self.card_map = {card.name: card for card in cards}

    # ------------------------------------------------------------------
    def assign_card_to_slot(self, card: Union[Card, str], slot: Slot) -> None:
        if card == "虫卵":
            slot.card_type = "虫卵"
            slot.tags = Tags()
            slot.tags.add("zerg")
            slot.tags.add("金色")
            slot.tags.add("无法三连")
            slot.event_handlers.append(_larva_event_handler.copy(slot.state, slot))
            return

        if card == "黄金矿工":
            slot.event_handlers = [_gold_miner_event_handler.copy(slot.state, slot)]
            return

        assert isinstance(card, Card), f"未知卡牌: {card!r}"

        if slot.card_type is None:
            slot.card_type = card.name
            slot.level = card.level
            for unit, cnt in card.units.items():
                slot.add_unit(unit, cnt)
            slot.tags = Tags(card.tags)
            for handler in card.event_handlers:
                slot.event_handlers.append(handler.copy(slot.state, slot))

    # ------------------------------------------------------------------
    def merge_slots(self, left_slot: Slot, right_slot: Slot) -> None:
        """三连合成：右槽合并进左槽，换成金色 handler，右槽清空。"""
        assert left_slot.card_type == right_slot.card_type
        assert left_slot.level == right_slot.level

        for unit, count in right_slot.units.items():
            left_slot.add_unit(unit, count)
        for tag in right_slot.tags:
            left_slot.tags.add(tag)
        for upgrade in right_slot.upgrades:
            if len(left_slot.upgrades) < left_slot.upgrades_limit:
                left_slot.upgrades.append(upgrade)

        left_slot.tags.add("金色")

        card = self.card_map[left_slot.card_type]

        # 移除普通描述对应的 handler，替换为金色 handler。
        # 注意：card.description 是含颜色标记的原始文本，而 handler.description 是归一化文本，
        # 因此按"卡牌普通 handler 的归一化描述集合"来判定，而不是直接对比 card.description。
        normal_descriptions = {h.description for h in card.event_handlers}
        left_slot.event_handlers = [
            h
            for h in left_slot.event_handlers + right_slot.event_handlers
            if h.description not in normal_descriptions
        ]
        for handler in card.gold_event_handlers:
            left_slot.event_handlers.append(handler.copy(left_slot.state, left_slot))

        # 清空右槽
        left_slot.state.slots[right_slot.index] = Slot(right_slot.index, left_slot.state)
        right_slot.card_type = None
