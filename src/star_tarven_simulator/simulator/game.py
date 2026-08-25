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
    HeroChoiceAction,
    HeroPowerAction,
)
from star_tarven_simulator.simulator.slot import Slot

# 出售时不触发常规出售特效、只发现卡牌的特殊描述
_SELL_DISCOVER_DESCRIPTIONS = {
    "出售时,发现1张其他1星卡牌,不获得出售晶体矿且不触发其他出售特效",
    "出售时,发现2张其他1星卡牌,不获得出售晶体矿且不触发其他出售特效",
}

_SIGNAL_TOWER_EXTRA_GATHERING_TAG = "在场时,信号塔还能触发2次集结效果"


class Tarven:
    def __init__(self, pool: CardPool, game: "Game", card_engine: AbstractCardEngine, hero_name: str = "default"):
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
        self.rng = pool.rng
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

        # 规范化双向额外相邻边（按位置保存）；物理相邻始终由 Slot.neighbors 合并。
        self.extra_neighbors: Dict[int, set[int]] = {}
        self.egg_hatches_mechanical = False
        self.egg_hatches_any_race = False

        from star_tarven_simulator.simulator.hero import HeroController
        self.hero_controller = HeroController(self, hero_name)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def store_card_to_cache(self, card_type) -> bool:
        for i, cache in enumerate(self.cache):
            if cache is None:
                self.cache[i] = card_type
                return True
        return False

    def can_receive_reward(self, *, direct_only: bool = False) -> bool:
        """Whether a hero reward can be accepted without discarding another card."""
        has_slot = any(slot.card_type is None for slot in self.slots)
        return has_slot if direct_only else has_slot or any(item is None for item in self.cache)

    def enter_card_direct(self, card: Card, *, slot_idx: int | None = None) -> bool:
        """Put ``card`` in an empty slot and broadcast a normal entering event."""
        if slot_idx is None:
            slot = next((candidate for candidate in self.slots if candidate.card_type is None), None)
        elif 0 <= slot_idx < len(self.slots) and self.slots[slot_idx].card_type is None:
            slot = self.slots[slot_idx]
        else:
            slot = None
        if slot is None:
            return False
        self.card_engine.assign_card_to_slot(card, slot)
        self.trigger_entering(slot)
        return True

    def grant_reward_card(self, card: Card, *, direct_only: bool = False) -> bool:
        """Grant a hero card reward using the contract's cache/overflow policy."""
        if direct_only:
            return self.enter_card_direct(card)
        if self.store_card_to_cache(card):
            return True
        return self.enter_card_direct(card)

    def resize_shop_for_level(self) -> None:
        """让商店容量与当前酒馆等级一致，并归还缩容时移出的卡。"""
        target = TARVEN_SHOP_CARD_NUMBER[self.level]
        if len(self.shop) < target:
            self.shop.extend([None] * (target - len(self.shop)))
        elif len(self.shop) > target:
            self.pool.place_back([card for card in self.shop[target:] if card is not None])
            self.shop = self.shop[:target]

    def card_price(self, shop_idx: int) -> int:
        card = self.shop[shop_idx] if 0 <= shop_idx < len(self.shop) else None
        return self.hero_controller.card_price(card, 3, shop_idx=shop_idx)

    def _draw_shop_cards(self, count: int) -> List[Card]:
        excluded = self.hero_controller.state.get("excluded_race")
        if not excluded:
            return self.pool.draw(count, self.level)
        allowed = [r for r in ("terran", "protoss", "zerg", "neutral") if r != excluded]
        cards = []
        for _ in range(count):
            uuid = self.pool._sample(levels=list(range(1, self.level + 1)), tags=allowed)
            if uuid is None:
                break
            cards.append(self.pool.card_map[uuid])
        return cards

    def reload_shop(self) -> None:
        if (
            self.hero_controller.hero_name == "解放者（防卫模式）"
            and self.hero_controller.state.get("shop_initialized")
        ):
            self.lock = False
            return
        if not self.lock:
            self.pool.place_back([c for c in self.shop if c is not None])
            self.shop = self._draw_shop_cards(len(self.shop))
        else:
            for i in range(len(self.shop)):
                if self.shop[i] is None:
                    drawn = self._draw_shop_cards(1)
                    self.shop[i] = drawn[0] if drawn else None
        self.hero_controller.state["shop_initialized"] = True
        self.lock = False

    def refresh(self) -> bool:
        """Execute the single policy-aware refresh path used by every source.

        A successful refresh always returns the old shop to the pool, ignores a
        lock, draws a fresh shop, clears the lock, and broadcasts one refresh
        event.  Liberator defensive mode rejects the operation before mutation.
        """
        if self.hero_controller.hero_name == "解放者（防卫模式）":
            return False
        self.pool.place_back([c for c in self.shop if c is not None])
        self.shop = self._draw_shop_cards(len(self.shop))
        self.lock = False
        self.trigger_refresh()
        return True

    @property
    def psi_level_max(self) -> int:
        return max((slot.psi_level for slot in self.slots), default=0)

    def total_power(self) -> float:
        """场上战力；干扰者额外计入暂存区非衍生静态卡价值。"""
        total = sum(slot.price() for slot in self.slots)
        if self.hero_controller.hero_name == "干扰者":
            for item in self.cache:
                card = self.pool.card_type_map.get(item) if isinstance(item, str) else item
                if isinstance(card, Card) and not card.derived:
                    total += card.price
        return total

    def add_extra_neighbor(self, left: int, right: int) -> None:
        if left == right or not (0 <= left < len(self.slots) and 0 <= right < len(self.slots)):
            return
        self.extra_neighbors.setdefault(left, set()).add(right)
        self.extra_neighbors.setdefault(right, set()).add(left)

    def clear_extra_neighbors(self, index: int | None = None) -> None:
        if index is None:
            self.extra_neighbors.clear()
            return
        for other in self.extra_neighbors.pop(index, set()):
            self.extra_neighbors.get(other, set()).discard(index)

    def are_neighbors(self, left: Slot, right: Slot) -> bool:
        return right in left.neighbors

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
        self.hero_controller.round_start_before_income()
        self.level_up_cost = max(0, self.level_up_cost - 1)
        self.mineral_max = min(self.round + 2, 10)
        self.mineral = self.mineral_max
        if self.hero_controller.is_kerrigan_zerg:
            self.gas_max = 0
            self.gas = 0
        else:
            self.gas_max = 6
            self.gas = min(self.gas + 1, self.gas_max)
        self.hero_controller.round_start_after_income()
        self.reload_shop()
        self.hero_controller.round_start_after_shop()

        # 延迟卡到期属于正常进场：优先进入最左空槽并广播进场事件。
        # 航母购买另有配对记录及双满丢弃规则；普通延迟奖励场满时暂存，
        # 双满则归还其真实卡池实体。
        delayed_cards = self.delay_enter_card.pop(self.round, [])
        for delay_index, card in enumerate(delayed_cards):
            if self.hero_controller.receive_delayed_card(card, delay_index=delay_index):
                continue
            if self.enter_card_direct(card):
                continue
            if not self.store_card_to_cache(card):
                self.pool.place_back(card)
        self.hero_controller.after_delayed_cards()

        # 扎加拉等高优先级英雄必须先于虫卵/卡牌回合开始效果。
        self.hero_controller.round_start_before_cards()
        for slot in list(self.slots):
            if slot.card_type is not None:
                slot.trigger([RoundStartEvent(self)])

    def round_end(self) -> None:
        for slot in list(self.slots):
            if slot.card_type is not None:
                slot.trigger([RoundEndEvent(self)])
                if slot.count("高级科技实验室") > 0:
                    slot.trigger([QuickProduceEvent(self, slot)])
        self.hero_controller.round_end_after_cards()

    def trigger_any_card_event(self, event) -> None:
        for slot in self.slots:
            if slot.card_type is not None:
                slot.trigger([event])

    # ------------------------------------------------------------------
    # 通用效果
    # ------------------------------------------------------------------
    def discover(self, level=None, tags=None) -> bool:
        cards = []
        for _ in range(3):
            uuid = self.pool._sample(levels=level, tags=tags)
            if uuid is not None:
                cards.append(self.pool.card_map[uuid])
        if not cards:
            return False
        self.force_action.append(ChooseCardAction(cards=cards))
        return True

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
        self.hero_controller.on_sell(trigger_slot)

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

        # 出售在触发效果前就释放槽位。这样出售效果中的“注卵”会把虫卵
        # 放入包括出售槽在内的最左侧空位，而不是落到更右侧的空位。
        # ``trigger_slot`` 仍保留完整卡牌数据，作为 SellingEvent / sold_slot
        # 的事件载荷，因此出售相关效果仍可读取被出售卡牌的单位和标签。
        self.slots[trigger_slot.index] = Slot(trigger_slot.index, self)

        # 出售卡牌自身的效果需要显式触发：它已不在 self.slots 中，不能再
        # 依赖下面的全场遍历发现。
        trigger_slot.trigger([SellingEvent(self, trigger_slot)])

        # 虚空水晶塔转移到左侧
        cnt = trigger_slot.count("虚空水晶塔")
        left = trigger_slot.left
        if left is not None and left.card_type is not None and cnt > 0:
            left.add_unit("虚空水晶塔", cnt)
            self.trigger_any_card_event(AnyCardGainVoidCrystalTowerEvent(self, left))

        for slot in self.slots:
            if slot.card_type is None:
                continue
            slot.trigger(events)
            # 相邻卡牌获得黑暗值
            if self.are_neighbors(slot, trigger_slot):
                self.gain_darkness(slot, 1)

        self.mineral += 1

    # ------------------------------------------------------------------
    # 进场
    # ------------------------------------------------------------------
    def trigger_entering(self, trigger_slot: Slot) -> None:
        self.entering = trigger_slot
        self.hero_controller.on_enter(trigger_slot)
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
        self.hero_controller.on_enter_after(trigger_slot)

    def trigger_upgrade(self, trigger_slot: Slot, upgrade_name: str) -> None:
        trigger_slot.upgrade(upgrade_name)
        for slot in self.slots:
            if slot.card_type is not None:
                slot.trigger([UpgradeEvent(self, trigger_slot, upgrade_name)])

    def trigger_level_up(self, level_up_cost: int, old_level: int | None = None) -> None:
        from star_tarven_simulator.simulator.event import LevelUpEvent

        self.hero_controller.on_level_up(self.level - 1 if old_level is None else old_level, level_up_cost)
        for slot in self.slots:
            if slot.card_type is not None:
                slot.trigger([LevelUpEvent(self, level_up_cost)])

    def trigger_refresh(self, *, automatic: bool = False) -> None:
        self.hero_controller.on_refresh(automatic=automatic)
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
        if self.force_action:
            return self._handle_force_action(action)

        if isinstance(action, HeroPowerAction):
            return self.hero_controller.power(action)

        if not self.hero_controller.permits_action(action):
            return False

        if isinstance(action, UpgradeTarvenAction):
            actual_cost = self.hero_controller.level_up_cost(self.level_up_cost)
            if self.mineral < actual_cost:
                return False
            old_level = self.level
            self.mineral -= actual_cost
            cost = actual_cost
            self.level += 1
            self.resize_shop_for_level()
            self.level_up_cost = TARVEN_UPGRADE_COST.get(self.level + 1, 0)
            self.trigger_level_up(cost, old_level)
            return True

        if isinstance(action, (BuyAction, CacheEnterAction)):
            card = None
            if isinstance(action, BuyAction) and 0 <= action.shop_idx < len(self.shop):
                card = self.shop[action.shop_idx]
            result = self._handle_place(action)
            if result and isinstance(action, BuyAction) and isinstance(card, Card):
                self.hero_controller.on_buy(card)
            return result

        if isinstance(action, SellAction):
            if not (0 <= action.slot_idx < len(self.slots)):
                return False
            slot = self.slots[action.slot_idx]
            if slot.card_type is None:
                return False
            self.trigger_selling(slot)
            return True

        if isinstance(action, DeployAction):
            return self._handle_deploy(action)

        if isinstance(action, UpgradeAction):
            if self.gas < 2 or not (0 <= action.slot_idx < len(self.slots)):
                return False
            slot = self.slots[action.slot_idx]
            if slot.card_type is None or len(slot.upgrades) >= slot.upgrades_limit:
                return False
            self.gas -= 2
            names = (
                list(__import__("star_tarven_simulator.simulator.hero", fromlist=["ZERG_RESEARCH_UPGRADES"]).ZERG_RESEARCH_UPGRADES)
                if self.hero_controller.hero_name == "汉森博士（异虫形态）"
                else ["聚能器"] * 4
            )
            self.force_action.append(ChooseUpgradeAction(slot.index, names))
            return True

        if isinstance(action, RefreshAction):
            if self.hero_controller.hero_name == "解放者（防卫模式）":
                return False
            if self.free_refresh > 0:
                self.free_refresh -= 1
            elif self.mineral > 0:
                self.mineral -= 1
            else:
                return False
            return self.refresh()

        if isinstance(action, LockAction):
            self.lock = True
            self.hero_controller.on_lock()
            return True

        if isinstance(action, SynthesisAction):
            return self._handle_synthesis(action)

        return False

    # ------------------------------------------------------------------
    def _handle_force_action(self, action) -> bool:
        if action is not self.force_action[0]:
            return False

        if isinstance(action, HeroChoiceAction):
            if not action.integrity_valid():
                self.pool.place_back(action.original_pool_candidates())
                self.force_action.remove(action)
                return False
            result = self.hero_controller.handle_choice(action)
            if result or action.payload.get("_terminal_failure"):
                self.force_action.remove(action)
            return result

        if isinstance(action, ChooseCardAction):
            if action.selected_card not in action.cards:
                return False
            unselected = list(action.cards)
            unselected.remove(action.selected_card)
            if action.delay > 0:
                self.delay_enter_card.setdefault(self.round + action.delay, []).append(action.selected_card)
                self.pool.place_back(unselected)
                self.force_action.remove(action)
                return True
            if not self.store_card_to_cache(action.selected_card):
                self.pool.place_back(action.cards)
                self.force_action.remove(action)
                return False
            self.pool.place_back(unselected)
            self.force_action.remove(action)
            return True

        if isinstance(action, ChooseUpgradeAction):
            if action.selected_upgrade_name not in action.upgrade_names and action.selected_upgrade_name is not None:
                return False
            self.trigger_upgrade(
                self.slots[action.slot_idx], action.selected_upgrade_name or action.upgrade_names[0]
            )
            if self.hero_controller.hero_name == "汉森博士（异虫形态）":
                self.gas = min(self.gas_max, self.gas + 1)
            self.force_action.remove(action)
            return True

        if isinstance(action, ChooseSynthesisAction):
            if action.selected not in action.options:
                return False
            selected_card = action.selected if isinstance(action.selected, Card) else None

            # 先完成合并释放右槽，再按通用奖励策略发放发现卡。这样暂存区满时
            # 奖励可直入刚释放的槽位，避免已扣费/消耗第三张卡却未完成三连。
            self.card_engine.merge_slots(
                self.slots[action.left_slot_idx], self.slots[action.right_slot_idx]
            )
            if selected_card is not None:
                granted = self.grant_reward_card(selected_card)
                if not granted:  # 合并必然释放一个槽位，仅作不变量保护。
                    raise RuntimeError("三连后没有可接收奖励的位置")
            else:
                self.trigger_upgrade(self.slots[action.left_slot_idx], "聚能器")
            remaining_options = list(action.options)
            remaining_options.remove(action.selected)
            self.pool.place_back([c for c in remaining_options if isinstance(c, Card)])
            self.hero_controller.on_synthesis(self.slots[action.left_slot_idx])
            self.force_action.remove(action)
            return True

        return False

    def available_placement_slots(self, card: Union[Card, str, None]) -> List[int]:
        """返回卡牌当前可以进场的槽位。

        普通卡只能放入从左往右的首个空槽；带 ``能够定点部署`` 标签的卡只要
        场上仍有空槽，就可以指定任意位置。后者指定已占用的位置时由
        :meth:`_make_room` 负责腾位。
        """
        if isinstance(card, str):
            card = self.pool.card_type_map.get(card)
        if not isinstance(card, Card):
            return []
        if any(handler.event_name == DeploymentEvent.event_name for handler in card.event_handlers):
            return []

        empty_slots = [slot.index for slot in self.slots if slot.card_type is None]
        if not empty_slots:
            return []
        if "能够定点部署" in card.tags or self.hero_controller.placement_anywhere(card):
            return list(range(len(self.slots)))
        return [empty_slots[0]]

    def _handle_place(self, action) -> bool:
        if isinstance(action, BuyAction):
            if not (0 <= action.shop_idx < len(self.shop)):
                return False
            card = self.shop[action.shop_idx]
            price = self.card_price(action.shop_idx)
            if card is None or price > self.mineral:
                return False
            delay = self.hero_controller.purchase_delay()
            if delay:
                self.mineral -= price
                self.shop[action.shop_idx] = None
                self.delay_enter_card.setdefault(self.round + delay, []).append(card)
                self.hero_controller.schedule_delayed_purchase(card, delay)
                return True
            if action.slot_idx is None:
                if not self.store_card_to_cache(card):
                    return False
                self.mineral -= self.card_price(action.shop_idx)
                self.shop[action.shop_idx] = None
                return True
        else:  # CacheEnterAction
            if not (0 <= action.cache_idx < len(self.cache)):
                return False
            card = self.cache[action.cache_idx]
            if isinstance(card, str):
                card = self.pool.card_type_map.get(card)
            if card is None:
                return False

        slot_idx = action.slot_idx
        if slot_idx not in self.available_placement_slots(card):
            return False

        # 仅定点部署卡能选中已占用槽；右移优先，右侧无空位时左移。
        if self.slots[slot_idx].card_type is not None:
            if not self._make_room(slot_idx):
                return False

        if isinstance(action, BuyAction):
            self.mineral -= self.card_price(action.shop_idx)
            self.shop[action.shop_idx] = None
        else:
            self.cache[action.cache_idx] = None

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
        from_shop = isinstance(action.shop_idx, int)
        if from_shop:
            if not 0 <= action.shop_idx < len(self.shop):
                return False
            card = self.shop[action.shop_idx]
            price = self.hero_controller.shop_synthesis_price(
                card, shop_idx=action.shop_idx
            )
            if card is None or self.mineral < price:
                return False
        else:
            if not isinstance(action.cache_idx, int) or not 0 <= action.cache_idx < len(self.cache):
                return False
            card = self.cache[action.cache_idx]
            price = 0
        card_name = card.name if isinstance(card, Card) else card
        locs = [
            s.index
            for s in self.slots
            if (
                s.card_type == card_name
                and not s.tags.has("金色")
                and not s.tags.has("无法三连")
            )
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

        if from_shop:
            self.mineral -= price
            self.shop[action.shop_idx] = None
        else:
            self.cache[action.cache_idx] = None

        self.force_action.append(ChooseSynthesisAction(locs[0], locs[1], cards))
        if from_shop and isinstance(card, Card):
            self.hero_controller.on_shop_synthesis_purchase(card)
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
        heroes: List[str] | None = None,
    ):
        self.pool = pool
        self.card_engine = card_engine
        from star_tarven_simulator.simulator.hero import validate_hero_assignment

        hero_names = validate_hero_assignment(heroes, user_count)
        self.tarvens = [
            Tarven(pool, self, card_engine, hero_names[i]) for i in range(user_count)
        ]
        self.max_round = max_round
        self.round = 0
        self._current_opponents: Dict[int, int] = {}

    def set_current_opponent(self, player_idx: int, opponent_idx: int) -> None:
        """为当前回合登记受控对手；飞蛇只能读取由此接口指定的真实玩家场面。"""
        if (
            not 0 <= player_idx < len(self.tarvens)
            or not 0 <= opponent_idx < len(self.tarvens)
            or player_idx == opponent_idx
        ):
            raise ValueError("对手索引无效")
        self._current_opponents[player_idx] = opponent_idx

    def current_opponent_highest_card(self, tarven: Tarven) -> Card | None:
        try:
            player_idx = self.tarvens.index(tarven)
        except ValueError:
            return None
        opponent_idx = self._current_opponents.get(player_idx)
        if opponent_idx is None:
            return None
        candidates = [
            slot
            for slot in self.tarvens[opponent_idx].slots
            if slot.card_type is not None and isinstance(slot.source_card, Card)
        ]
        if not candidates:
            return None
        highest = max(slot.level for slot in candidates)
        return next(slot.source_card for slot in candidates if slot.level == highest)

    def round_start(self) -> None:
        self.round += 1
        self._current_opponents.clear()
        for tarven in self.tarvens:
            tarven.round_start()

    def round_end(self) -> None:
        for tarven in self.tarvens:
            tarven.round_end()
