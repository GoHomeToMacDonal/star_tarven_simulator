"""卡牌引擎：负责把卡牌分配到槽位、复制 handler、以及三连合成。

相比旧版的修正：

* :meth:`CardEngine.merge_slots` 使用 ``handler.description``（旧版误用 ``handler.desc`` 会抛
  ``AttributeError``），并真正填充金色 handler（``card.gold_event_handlers``）。
* 孵化（虫卵）修正了左右邻居判断，并按统一签名广播 :class:`AnyCardHatchEvent(tarven, slot, units)`。
* 解析已在加载阶段完成（见 ``parsing`` 包），引擎只负责实例化，不再内嵌解析逻辑。
"""

from __future__ import annotations

from typing import Dict, List, Union

from star_tarven_simulator.constants.unit_type import (
    BIOLOGICAL_UNITS,
    HERO_UNITS,
    MECHANICAL_UNITS,
)
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
    if (
        (not left.tags.has("zerg") or not right.tags.has("zerg"))
        and not getattr(event.tarven, "egg_hatches_any_race", False)
    ):
        return

    allow_mechanical = getattr(event.tarven, "egg_hatches_mechanical", False)
    hatched: Dict[str, int] = {}
    for unit, count in slot.units.items():
        if unit not in HERO_UNITS and (
            unit in BIOLOGICAL_UNITS or (allow_mechanical and unit in MECHANICAL_UNITS)
        ):
            left.add_unit(unit, count)
            right.add_unit(unit, count)
            hatched[unit] = count

    event.tarven.trigger_any_card_event(
        AnyCardHatchEvent(event.tarven, slot, hatched)
    )

    # 孵化所：若它是虫卵的接收侧，则额外获得最后一次注卵调用中的最后一个单位。
    # 普通孵化所额外 2 个，金色额外 3 个；只对本次确实孵化出的单位生效。
    last_unit = getattr(event.tarven, "last_larva_unit", None)
    if last_unit in hatched:
        for target in (left, right):
            if target.card_type == "孵化所":
                target.add_unit(last_unit, 3 if target.tags.has("金色") else 2)

    # 清空虫卵及其最后注卵记录
    event.tarven.last_larva_unit = None
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

    def __deepcopy__(self, memo) -> "CardEngine":
        """引擎无可变状态（只持有静态卡牌定义），深拷贝时直接共享自身。

        这样 ``clone_game`` 不会重建 ``card_map``；引擎方法全部只读 ``self``，
        写操作都作用在传入的 ``slot`` / ``event`` 上。
        """
        return self

    # ------------------------------------------------------------------
    def assign_card_to_slot(self, card: Union[Card, str], slot: Slot) -> None:
        if card == "虫卵":
            slot.card_type = "虫卵"
            slot.source_card = None
            slot.derived = True
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
            slot.source_card = card
            slot.derived = bool(card.derived)
            slot.level = card.level
            for unit, cnt in card.units.items():
                slot.add_unit(unit, cnt)
            slot.tags = Tags(card.tags)
            for handler in card.event_handlers:
                slot.event_handlers.append(handler.copy(slot.state, slot))

    # ------------------------------------------------------------------
    @staticmethod
    def _runtime_handlers(slot: Slot) -> List[EventHandler]:
        """返回不属于静态定义或英雄临时定义的实例附加 handler。"""
        descriptions = set(slot.temporary_handler_descriptions)
        card = slot.source_card
        if isinstance(card, Card):
            descriptions.update(h.description for h in card.event_handlers)
            descriptions.update(h.description for h in card.gold_event_handlers)
        return [h for h in slot.event_handlers if h.description not in descriptions]

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

        card = left_slot.source_card or self.card_map.get(left_slot.card_type)
        if not isinstance(card, Card):
            # 动态卡只做数值合并；没有静态金色描述可切换。
            left_slot.state.slots[right_slot.index] = Slot(right_slot.index, left_slot.state)
            right_slot.card_type = None
            return

        # 三连恢复静态来源的金色描述，同时保留部署/升级等实例附加 handler。
        runtime_handlers = [
            h.copy(left_slot.state, left_slot)
            for h in self._runtime_handlers(left_slot) + self._runtime_handlers(right_slot)
        ]
        left_slot.temporary_description = []
        left_slot.temporary_handler_descriptions = set()
        left_slot.event_handlers = runtime_handlers + [
            h.copy(left_slot.state, left_slot) for h in card.gold_event_handlers
        ]

        # 清空右槽
        left_slot.state.slots[right_slot.index] = Slot(right_slot.index, left_slot.state)
        right_slot.card_type = None



    # ------------------------------------------------------------------
    # 英雄所需的实例级卡牌操作；不修改共享 Card 模板。
    # ------------------------------------------------------------------
    def make_gold(self, slot: Slot) -> None:
        """将单个实例转为金色，并切换到其静态来源的金色 handler。"""
        slot.tags.add("金色")
        card = slot.source_card
        if not isinstance(card, Card):
            return
        runtime_handlers = self._runtime_handlers(slot)
        slot.event_handlers = runtime_handlers + [
            h.copy(slot.state, slot) for h in card.gold_event_handlers
        ]
        slot.temporary_description = []
        slot.temporary_handler_descriptions = set()

    def reload_static_definition(self, slot: Slot, *, gold: bool | None = None) -> None:
        """恢复实例的静态描述；用于雷神临时描述在三连后还原。"""
        card = slot.source_card
        if not isinstance(card, Card):
            return
        use_gold = bool(slot.tags.has("金色")) if gold is None else gold
        templates = card.gold_event_handlers if use_gold else card.event_handlers
        runtime_handlers = self._runtime_handlers(slot)
        slot.event_handlers = runtime_handlers + [h.copy(slot.state, slot) for h in templates]
        slot.temporary_description = []
        slot.temporary_handler_descriptions = set()

    def apply_temporary_definition(self, slot: Slot, definition: Card) -> None:
        runtime_handlers = self._runtime_handlers(slot)
        slot.temporary_description = list(definition.description)
        slot.temporary_handler_descriptions = {
            h.description for h in definition.event_handlers
        }
        slot.event_handlers = runtime_handlers + [
            h.copy(slot.state, slot) for h in definition.event_handlers
        ]

    def copy_slot(self, source: Slot, target: Slot, *, derived: bool = False) -> None:
        """复制在场实例，不共享 Tags/units/handler 绑定。"""
        target.card_type = source.card_type
        target.source_card = source.source_card
        target.derived = derived or source.derived
        target.level = source.level
        target.tags = Tags(list(source.tags))
        if derived:
            target.tags.add("衍生卡")
        for unit, count in source.units.items():
            target.add_unit(unit, count)
        target.upgrades = list(source.upgrades)
        target.temporary_description = list(source.temporary_description)
        target.temporary_handler_descriptions = set(source.temporary_handler_descriptions)
        target.event_handlers = [h.copy(target.state, target) for h in source.event_handlers]

    def transform_slot(self, slot: Slot, card: Card) -> None:
        """在原位置重载为固定/动态定义，保留槽位对象以维持外部引用。"""
        slot.card_type = None
        slot.source_card = None
        slot.level = -1
        slot.tags = Tags()
        slot.units = {}
        slot.unit_count = 0
        slot.upgrades = []
        slot.event_handlers = []
        slot.temporary_description = []
        slot.temporary_handler_descriptions = set()
        slot.derived = bool(card.derived)
        self.assign_card_to_slot(card, slot)

    def fuse_card_definition(self, slot: Slot, definition: Card) -> None:
        """把一张静态/动态定义完整融合进现有槽，不产生进场或出售事件。"""
        original_name = slot.card_type
        for unit, count in definition.units.items():
            slot.add_unit(unit, count)
        for tag in definition.tags:
            slot.tags.add(tag)
        for race in ("terran", "zerg", "neutral"):
            slot.tags.remove(race)
        slot.tags.add("protoss")
        slot.event_handlers.extend(
            handler.copy(slot.state, slot) for handler in definition.event_handlers
        )
        slot.card_type = f"{original_name}+{definition.name}"
        slot.source_card = None
        slot.derived = True
        slot.tags.add("衍生卡")
        slot.tags.add("无法融合")
        slot.tags.add("无法三连")
        slot.tags.add("金色")

    def reload_dynamic_definition(self, slot: Slot, definition: Card) -> None:
        """完整装载动态定义，同时保留实例已有单位、升级和 handlers。"""
        existing_handlers = list(slot.event_handlers)
        for unit, count in definition.units.items():
            slot.add_unit(unit, count)
        for tag in definition.tags:
            slot.tags.add(tag)
        slot.card_type = definition.name
        slot.source_card = definition
        slot.derived = True
        slot.level = definition.level
        slot.event_handlers = existing_handlers + [
            handler.copy(slot.state, slot) for handler in definition.event_handlers
        ]
        slot.temporary_description = []
        slot.temporary_handler_descriptions = set()

    def replace_definition_preserving_payload(self, slot: Slot, definition: Card) -> None:
        """Replace card identity/handlers while retaining current units and upgrades."""
        use_gold = bool(slot.tags.has("金色"))
        slot.card_type = definition.name
        slot.source_card = definition
        slot.derived = False
        slot.level = definition.level
        slot.upgrades_limit = max(5, len(slot.upgrades))
        slot.tags = Tags(definition.gold_tags if use_gold else definition.tags)
        slot.tags.add("属于原始虫群")
        if use_gold:
            slot.tags.add("金色")
        templates = definition.gold_event_handlers if use_gold else definition.event_handlers
        slot.event_handlers = [handler.copy(slot.state, slot) for handler in templates]
        slot.temporary_description = []
        slot.temporary_handler_descriptions = set()

    def fuse_slots(self, left: Slot, right: Slot) -> None:
        """Fuse Archon targets into a dynamic result retained in the right slot."""
        left_name, right_name = left.card_type, right.card_type
        left_races = {race for race in ("terran", "protoss", "zerg") if left.tags.has(race)}
        right_races = {race for race in ("terran", "protoss", "zerg") if right.tags.has(race)}
        combined_non_neutral = left_races | right_races
        result_race = next(iter(combined_non_neutral)) if len(combined_non_neutral) == 1 else "neutral"

        for unit, count in left.units.items():
            right.add_unit(unit, count)
        merged_upgrades = list(left.upgrades) + list(right.upgrades)
        right.upgrades_limit = 5
        right.upgrades = merged_upgrades[:5]
        right.level = max(left.level, right.level)
        for tag in left.tags:
            right.tags.add(tag)
        for race in ("terran", "protoss", "zerg", "neutral"):
            right.tags.remove(race)
        right.tags.add(result_race)
        right.event_handlers = [
            *(handler.copy(right.state, right) for handler in left.event_handlers),
            *right.event_handlers,
        ]
        right.card_type = f"{left_name}+{right_name}"
        right.source_card = None
        right.derived = True
        right.tags.add("衍生卡")
        right.tags.add("无法融合")
        right.tags.add("无法三连")
        right.tags.add("金色")
        left.state.slots[left.index] = Slot(left.index, left.state)
        left.card_type = None
