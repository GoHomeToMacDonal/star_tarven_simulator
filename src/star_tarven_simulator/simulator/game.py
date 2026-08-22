"""酒馆状态机（Tarven）与对局（Game）。

相比旧版的修正：

* :meth:`Tarven.larva` 统一接收 dict。
* 新增 :meth:`Tarven.gain_darkness`，集中处理黑暗值累加 + 事件广播（旧版在
  ``EventHandler.handle`` 里偷偷加、且 ``GainDarknessEvent`` 构造参数不一致）。
* 出售相邻卡牌时通过 :meth:`gain_darkness` 给邻居 +1 黑暗值。
* 触发进场/出售时同步 ``self.entering`` / ``self.sold``，兼容引用它们的效果。
"""

from __future__ import annotations

from typing import Dict, List, Union

from star_tarven_simulator.constants.tarven import (
    TARVEN_UPGRADE_COST,
    TARVEN_SHOP_CARD_NUMBER,
)
from star_tarven_simulator.simulator.base import AbstractCardEngine
from star_tarven_simulator.simulator.card import Card, CardPool
from star_tarven_simulator.simulator.event_handler import GatheringActionHandler
from star_tarven_simulator.simulator.event import (
    RoundStartEvent,
    RoundEndEvent,
    AnyCardTeleportEvent,
    AnyTaskFinishedEvent,
    SellingEvent,
    EnteringEvent,
    AnyCardSoldEvent,
    AnyCardEnteredEvent,
    AnyCardEnteredOrSoldEvent,
    GainDarknessEvent,
    UpgradeEvent,
    RefreshEvent,
    QuickProduceEvent,
    AnyCardGainVoidCrystalTowerEvent,
    AnyCardLarvaEvent,
    DeploymentEvent,
)
from star_tarven_simulator.simulator.action import (
    Action,
    BuyAction,
    CacheEnterAction,
    DeployAction,
    SynthesisAction,
    SellAction,
    UpgradeAction,
    RefreshAction,
    LockAction,
    ChooseCardAction,
    ChooseUpgradeAction,
    UpgradeTarvenAction,
    ChooseSynthesisAction,
)
from star_tarven_simulator.simulator.slot import Slot

# 出售时不触发常规出售特效、只发现卡牌的特殊描述
_SELL_DISCOVER_DESCRIPTIONS = {
    "出售时,发现1张其他1星卡牌,不获得出售晶体矿且不触发其他出售特效",
    "出售时,发现2张其他1星卡牌,不获得出售晶体矿且不触发其他出售特效",
}

_SIGNAL_TOWER_EXTRA_GATHERING_TAG = "在场时,信号塔还能触发2次集结效果"


class Tarven:
    def __init__(self, pool: CardPool, game: "Game", card_engine: AbstractCardEngine):
        self.game = game
        self.card_engine = card_engine

        # 最近一次进场 / 出售 / 挂件变更的槽位（部分效果会引用）
        self.entering: Union[Slot, None] = None
        self.sold: Union[Slot, None] = None
        self.addon_changed: Union[Slot, None] = None

        # 等级
        self.level = 1
        self.level_up_cost = TARVEN_UPGRADE_COST[self.level + 1]

        # 资源
        self.gas = 0
        self.gas_max = 6
        self.mineral = 0
        self.mineral_max = 3
        self.free_refresh = 0

        self.health = 100
        self.round = 0

        # 神族"虚空水晶塔"提供的能量强度（默认 1，"一鼓作气"可改为 2/3）
        self.void_tower_energy = 1

        # 卡片
        self.pool: CardPool = pool
        self.shop: List[Card] = [None] * TARVEN_SHOP_CARD_NUMBER[self.level]
        self.cache: List[Union[str, Card, None]] = [None] * 6
        self.slots: List[Slot] = [Slot(idx, self) for idx in range(7)]

        self.lock = False

        # 强制动作
        self.force_action: List[
            Union[ChooseCardAction, ChooseUpgradeAction, ChooseSynthesisAction]
        ] = []

        # 延迟进场卡牌 {到达回合: [Card, ...]}
        self.delay_enter_card: Dict[int, List[Card]] = {}

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def store_card_to_cache(self, card_type) -> bool:
        for i, cache in enumerate(self.cache):
            if cache is None:
                self.cache[i] = card_type
                return True
        return False

    def card_price(self, shop_idx: int) -> int:
        return 3

    def reload_shop(self) -> None:
        if not self.lock:
            self.pool.place_back([c for c in self.shop if c is not None])
            self.shop = self.pool.draw(len(self.shop), self.level)
        else:
            for i in range(len(self.shop)):
                if self.shop[i] is None:
                    drawn = self.pool.draw(1, self.level)
                    self.shop[i] = drawn[0] if drawn else None
        self.lock = False

    def refresh(self) -> None:
        self.pool.place_back([c for c in self.shop if c is not None])
        self.shop = self.pool.draw(len(self.shop), self.level)
        self.lock = False

    @property
    def psi_level_max(self) -> int:
        return max((slot.psi_level for slot in self.slots), default=0)

    def larva(self, units: Dict[str, int]) -> None:
        """注卵：找到现有虫卵或空槽生成虫卵，注入单位并广播 any_card_larva。"""
        empty_idx = None
        for i, slot in enumerate(self.slots):
            if slot.card_type == "虫卵":
                for unit_type, cnt in units.items():
                    slot.add_unit(unit_type, cnt)
                self.trigger_any_card_larva(slot)
                return
            if slot.card_type is None and empty_idx is None:
                empty_idx = i

        if empty_idx is not None:
            self.slots[empty_idx] = Slot(empty_idx, self)
            self.card_engine.assign_card_to_slot("虫卵", self.slots[empty_idx])
            for unit_type, cnt in units.items():
                self.slots[empty_idx].add_unit(unit_type, cnt)
            self.trigger_any_card_larva(self.slots[empty_idx])

    def gain_darkness(self, slot: Slot, amount: int = 1) -> None:
        """给某槽位增加黑暗值并触发其 gain_darkness 效果。"""
        if slot is None or slot.card_type is None or amount <= 0:
            return
        slot.darkness += amount
        slot.trigger([GainDarknessEvent(self, slot, amount)])

    # ------------------------------------------------------------------
    # 回合
    # ------------------------------------------------------------------
    def round_start(self) -> None:
        self.round += 1
        self.level_up_cost = max(0, self.level_up_cost - 1)
        self.mineral_max = min(self.round + 2, 10)
        self.mineral = self.mineral_max
        self.gas_max = 6
        self.gas = min(self.gas + 1, self.gas_max)
        self.reload_shop()

        # 延迟进场
        for card in self.delay_enter_card.pop(self.round, []):
            self.store_card_to_cache(card.name if isinstance(card, Card) else card)

        for slot in self.slots:
            if slot.card_type is not None:
                slot.trigger([RoundStartEvent(self)])

    def round_end(self) -> None:
        for slot in self.slots:
            if slot.card_type is not None:
                slot.trigger([RoundEndEvent(self)])
                if slot.count("高级科技实验室") > 0:
                    slot.trigger([QuickProduceEvent(self, slot)])

    def trigger_any_card_event(self, event) -> None:
        for slot in self.slots:
            if slot.card_type is not None:
                slot.trigger([event])

    # ------------------------------------------------------------------
    # 通用效果
    # ------------------------------------------------------------------
    def discover(self, level=None, tags=None) -> None:
        cards = []
        for _ in range(3):
            uuid = self.pool._sample(levels=level, tags=tags)
            if uuid is not None:
                cards.append(self.pool.card_map[uuid])
        self.force_action.append(ChooseCardAction(cards=cards))

    def seize(self, source: Slot, target: Slot) -> None:
        for unit_type, cnt in list(source.units.items()):
            target.add_unit(unit_type, cnt)
        for upgrade_name in source.upgrades:
            if upgrade_name != "黄金矿工" and len(target.upgrades) < target.upgrades_limit:
                target.upgrades.append(upgrade_name)
        self.destroy(source)

    def destroy(self, slot: Slot) -> None:
        if slot.card_type is not None:
            self.slots[slot.index] = Slot(slot.index, self)

    # 便捷广播
    def trigger_any_card_teleport(self, source: Slot) -> None:
        self.trigger_any_card_event(AnyCardTeleportEvent(self, source))

    def trigger_round_win(self) -> None:
        """回合胜利时派发（休眠事件，需上层战斗结果驱动）。"""
        from star_tarven_simulator.simulator.event import RoundWinEvent

        for slot in self.slots:
            if slot.card_type is not None:
                slot.trigger([RoundWinEvent(self)])

    def trigger_other_player_hero_card(self, source: Slot) -> None:
        """其他玩家出售/出局一张具有英雄单位的卡牌时，对本酒馆派发（休眠事件）。

        ``source`` 为那位玩家的卡槽。需上层多人驱动：当玩家 X 出售/出局英雄卡时，
        对每个 Y != X 调用 ``Y.trigger_other_player_hero_card(source)``。
        """
        from star_tarven_simulator.simulator.event import (
            OtherPlayerSoldHeroCardEvent,
        )

        for slot in self.slots:
            if slot.card_type is not None:
                slot.trigger([OtherPlayerSoldHeroCardEvent(self, source)])

    def trigger_deployment(self, card, target_slot: Slot) -> None:
        """把一张辅助卡的 ``部署时`` 效果结算到 ``target_slot`` 上。

        ``card`` 为辅助卡（其 ``event_handlers`` 里含 ``deployment`` handler 模板）。
        部署效果通过 ``event.deployment_slot`` 作用于目标卡牌。
        """
        event = DeploymentEvent(self, target_slot)
        for handler in card.event_handlers:
            if handler.event_name == DeploymentEvent.event_name:
                handler.action_handler(target_slot, event)

    def trigger_any_card_larva(self, trigger_slot: Slot) -> None:
        self.trigger_any_card_event(AnyCardLarvaEvent(self, trigger_slot))

    def trigger_any_task_finished(self, trigger_slot: Slot) -> None:
        self.trigger_any_card_event(AnyTaskFinishedEvent(self, trigger_slot))

    # ------------------------------------------------------------------
    # 出售
    # ------------------------------------------------------------------
    def trigger_selling(self, trigger_slot: Slot) -> None:
        self.sold = trigger_slot

        # 特殊："只发现、不触发其他出售特效"
        for handler in trigger_slot.event_handlers:
            if handler.description in _SELL_DISCOVER_DESCRIPTIONS:
                n = 2 if "2张" in handler.description else 1
                for _ in range(n):
                    self.discover(level=[1])
                self.slots[trigger_slot.index] = Slot(trigger_slot.index, self)
                return

        events = [
            AnyCardSoldEvent(self, trigger_slot),
            AnyCardEnteredOrSoldEvent(self, trigger_slot),
        ]

        for slot in self.slots:
            if slot.card_type is None:
                continue
            if slot.index == trigger_slot.index:
                slot.trigger([SellingEvent(self, trigger_slot)])
                # 虚空水晶塔转移到左侧
                cnt = trigger_slot.count("虚空水晶塔")
                left = trigger_slot.left
                if left is not None and left.card_type is not None and cnt > 0:
                    left.add_unit("虚空水晶塔", cnt)
                    self.trigger_any_card_event(
                        AnyCardGainVoidCrystalTowerEvent(self, left)
                    )
            else:
                slot.trigger(events)
                # 相邻卡牌获得黑暗值
                if abs(slot.index - trigger_slot.index) == 1:
                    self.gain_darkness(slot, 1)

        self.slots[trigger_slot.index] = Slot(trigger_slot.index, self)
        self.mineral += 1

    # ------------------------------------------------------------------
    # 进场
    # ------------------------------------------------------------------
    def trigger_entering(self, trigger_slot: Slot) -> None:
        self.entering = trigger_slot
        events = [
            AnyCardEnteredEvent(self, trigger_slot),
            AnyCardEnteredOrSoldEvent(self, trigger_slot),
        ]
        for slot in self.slots:
            if slot.card_type is None:
                continue
            if slot.index == trigger_slot.index:
                slot.trigger([EnteringEvent(self, trigger_slot)])
            else:
                slot.trigger(events)

    def trigger_upgrade(self, trigger_slot: Slot, upgrade_name: str) -> None:
        trigger_slot.upgrade(upgrade_name)
        for slot in self.slots:
            if slot.card_type is not None:
                slot.trigger([UpgradeEvent(self, trigger_slot, upgrade_name)])

    def trigger_level_up(self, level_up_cost: int) -> None:
        from star_tarven_simulator.simulator.event import LevelUpEvent

        for slot in self.slots:
            if slot.card_type is not None:
                slot.trigger([LevelUpEvent(self, level_up_cost)])

    def trigger_refresh(self) -> None:
        has_special = any(
            s.card_type is not None and s.tags.has(_SIGNAL_TOWER_EXTRA_GATHERING_TAG)
            for s in self.slots
        )
        for slot in self.slots:
            if slot.card_type is None:
                continue
            slot.trigger([RefreshEvent(self)])
            if slot.count("信号塔") > 0:
                slot.trigger([QuickProduceEvent(self, slot)] * 2)
                if has_special:
                    for handler in slot.event_handlers:
                        if isinstance(handler.action_handler, GatheringActionHandler):
                            handler.action_handler.handler(slot, RefreshEvent(self), 2)

    # ------------------------------------------------------------------
    # 动作分发
    # ------------------------------------------------------------------
    def action(self, action: Action) -> bool:
        if len(self.force_action) > 0:
            return self._handle_force_action(action)

        if isinstance(action, UpgradeTarvenAction):
            if self.mineral < self.level_up_cost:
                return False
            self.mineral -= self.level_up_cost
            cost = self.level_up_cost
            self.level += 1
            self.level_up_cost = TARVEN_UPGRADE_COST.get(self.level + 1, 0)
            self.trigger_level_up(cost)
            return True

        if isinstance(action, (BuyAction, CacheEnterAction)):
            return self._handle_place(action)

        if isinstance(action, SellAction):
            slot = self.slots[action.slot_idx]
            if slot.card_type is None:
                return False
            self.trigger_selling(slot)
            return True

        if isinstance(action, DeployAction):
            return self._handle_deploy(action)

        if isinstance(action, UpgradeAction):
            if self.gas < 2:
                return False
            slot = self.slots[action.slot_idx]
            if slot.card_type is None or len(slot.upgrades) >= slot.upgrades_limit:
                return False
            self.gas -= 2
            self.force_action = [
                ChooseUpgradeAction(slot.index, ["聚能器"] * 4)
            ]
            return True

        if isinstance(action, RefreshAction):
            if self.free_refresh > 0:
                self.free_refresh -= 1
            elif self.mineral > 0:
                self.mineral -= 1
            else:
                return False
            self.refresh()
            self.trigger_refresh()
            return True

        if isinstance(action, LockAction):
            self.lock = True
            return True

        if isinstance(action, SynthesisAction):
            return self._handle_synthesis(action)

        return False

    # ------------------------------------------------------------------
    def _handle_force_action(self, action) -> bool:
        if action not in self.force_action:
            return False
        self.force_action = []

        if isinstance(action, ChooseCardAction):
            if action.delay > 0:
                self.delay_enter_card.setdefault(self.round + action.delay, []).append(
                    action.selected_card
                )
                return True
            self.store_card_to_cache(action.selected_card)
            return True

        if isinstance(action, ChooseUpgradeAction):
            self.trigger_upgrade(
                self.slots[action.slot_idx], action.selected_upgrade_name or "聚能器"
            )
            return True

        if isinstance(action, ChooseSynthesisAction):
            if action.selected not in action.options:
                return False
            self.card_engine.merge_slots(
                self.slots[action.left_slot_idx], self.slots[action.right_slot_idx]
            )
            if isinstance(action.selected, str):
                self.trigger_upgrade(self.slots[action.left_slot_idx], "聚能器")
            else:
                self.store_card_to_cache(action.selected.name)
            return True

        return True

    def _handle_place(self, action) -> bool:
        if isinstance(action, BuyAction):
            if self.card_price(action.shop_idx) > self.mineral:
                return False
            card = self.shop[action.shop_idx]
            if card is None:
                return False
            if action.slot_idx is None:
                if not self.store_card_to_cache(card.name):
                    return False
                self.mineral -= self.card_price(action.shop_idx)
                self.shop[action.shop_idx] = None
                return True
        else:  # CacheEnterAction
            if action.slot_idx is None:
                return False

        slot_idx = action.slot_idx

        # 腾位（右移优先，否则左移）
        if self.slots[slot_idx].card_type is not None:
            if not self._make_room(slot_idx):
                return False

        if isinstance(action, BuyAction):
            self.mineral -= self.card_price(action.shop_idx)
            card = self.shop[action.shop_idx]
            self.shop[action.shop_idx] = None
        else:
            card = self.cache[action.cache_idx]
            self.cache[action.cache_idx] = None
            if isinstance(card, str):
                card = self.pool.card_type_map.get(card, card)

        self.slots[slot_idx] = Slot(slot_idx, self)
        self.card_engine.assign_card_to_slot(card, self.slots[slot_idx])
        self.trigger_entering(self.slots[slot_idx])
        return True

    def _handle_deploy(self, action) -> bool:
        # 目标槽位必须已有卡牌
        if not (0 <= action.slot_idx < len(self.slots)):
            return False
        target = self.slots[action.slot_idx]
        if target.card_type is None:
            return False

        # 取出辅助卡（来自暂存区或商店，二选一）
        if action.cache_idx is not None:
            if not (0 <= action.cache_idx < len(self.cache)):
                return False
            card = self.cache[action.cache_idx]
            from_cache = True
        elif action.shop_idx is not None:
            if not (0 <= action.shop_idx < len(self.shop)):
                return False
            card = self.shop[action.shop_idx]
            if card is not None and self.card_price(action.shop_idx) > self.mineral:
                return False
            from_cache = False
        else:
            return False

        if card is None:
            return False
        if isinstance(card, str):
            card = self.pool.card_type_map.get(card, card)
        if not isinstance(card, Card):
            return False

        # 仅允许"带部署效果的辅助卡"定点部署（辅助卡的 辅助卡 标记有的在 tag、有的只在描述里，
        # 因此以"是否含 deployment handler"为准，更稳健）。
        has_deploy = any(
            handler.event_name == DeploymentEvent.event_name
            for handler in card.event_handlers
        )
        if not has_deploy:
            return False

        # 结算部署效果，然后消耗辅助卡 / 扣费
        self.trigger_deployment(card, target)
        if from_cache:
            self.cache[action.cache_idx] = None
        else:
            self.mineral -= self.card_price(action.shop_idx)
            self.shop[action.shop_idx] = None
        return True

    def _make_room(self, slot_idx: int) -> bool:
        # 右移
        for i in range(slot_idx + 1, len(self.slots)):
            if self.slots[i].card_type is None:
                for j in range(i, slot_idx, -1):
                    self.slots[j] = self.slots[j - 1]
                    self.slots[j].index = j
                self.slots[slot_idx] = Slot(slot_idx, self)
                return True
        # 左移
        for i in range(slot_idx - 1, -1, -1):
            if self.slots[i].card_type is None:
                for j in range(i, slot_idx):
                    self.slots[j] = self.slots[j + 1]
                    self.slots[j].index = j
                self.slots[slot_idx] = Slot(slot_idx, self)
                return True
        return False

    def _handle_synthesis(self, action) -> bool:
        card = (
            self.shop[action.shop_idx]
            if isinstance(action.shop_idx, int)
            else self.cache[action.cache_idx]
        )
        card_name = card.name if isinstance(card, Card) else card
        locs = [
            s.index
            for s in self.slots
            if s.card_type == card_name and not s.tags.has("金色")
        ]
        if len(locs) != 2:
            return False

        cards: List[Union[Card, str]] = []
        for _ in range(3):
            uuid = self.pool._sample()
            if uuid is not None:
                cards.append(self.pool.card_map[uuid])
        if len(self.slots[locs[0]].upgrades) + len(self.slots[locs[1]].upgrades) <= 4:
            cards.append("聚能器")

        if isinstance(action.shop_idx, int):
            self.shop[action.shop_idx] = None
        else:
            self.cache[action.cache_idx] = None

        self.force_action = [ChooseSynthesisAction(locs[0], locs[1], cards)]
        return True

    def __str__(self) -> str:
        return (
            f"level={self.level};gas={self.gas},mineral={self.mineral},"
            f"health={self.health};slots={','.join(str(s) for s in self.slots)}"
        )


class Game:
    def __init__(
        self,
        pool: CardPool,
        card_engine: AbstractCardEngine,
        user_count: int = 8,
        max_round: int = 20,
    ):
        self.pool = pool
        self.card_engine = card_engine
        self.tarvens = [Tarven(pool, self, card_engine) for _ in range(user_count)]
        self.max_round = max_round
        self.round = 0

    def round_start(self) -> None:
        self.round += 1
        for tarven in self.tarvens:
            tarven.round_start()

    def round_end(self) -> None:
        for tarven in self.tarvens:
            tarven.round_end()
