"""卡槽（Slot）：一张在场卡牌的载体，也是卡牌效果的执行主体（"此卡牌"）。"""

from __future__ import annotations

from typing import List, Optional

from star_tarven_simulator.constants.unit_prices import UNIT_PRICES
from star_tarven_simulator.simulator.base import AbstractTarven
from star_tarven_simulator.simulator.card import Tags
from star_tarven_simulator.simulator.event import (
    AnyCardAddonChangedEvent,
    QuickProduceEvent,
)

# 折跃目标标签：拥有该标签的槽位会成为全场折跃的落点
TELEPORT_TARGET_TAG = "你的折跃效果总是添加到这张牌上"

# 单位总量硬上限
UNIT_COUNT_LIMIT = 200


class Slot:
    def __init__(self, idx: int, state: AbstractTarven):
        self.index: int = idx
        self.card_type: Optional[str] = None

        self.state: AbstractTarven = state
        self.tags: Tags = Tags()
        self.level: int = -1
        self.units: dict = {}
        self.upgrades: List[str] = []
        self.upgrades_limit: int = 5
        self.event_handlers: List = []

        # 当前实例的静态来源与英雄临时改写。Card 模板始终保持只读。
        self.source_card = None
        self.temporary_description: List[str] = []
        self.temporary_handler_descriptions: set[str] = set()
        self.derived: bool = False

        # 特殊数值
        self.darkness: int = 0

        # handler 私有的持久计数器（跨回合）
        self.task_vars: dict = {}

        # 单位总数（用于 200 上限）
        self.unit_count: int = 0

    # ------------------------------------------------------------------
    # 位置 / 集合（只读 property）
    # ------------------------------------------------------------------
    @property
    def all(self) -> List["Slot"]:
        return [s for s in self.state.slots if s.card_type is not None]

    @property
    def left_all(self) -> List["Slot"]:
        return [s for s in self.state.slots[: self.index] if s.card_type is not None]

    @property
    def right_all(self) -> List["Slot"]:
        return [s for s in self.state.slots[self.index + 1 :] if s.card_type is not None]

    @property
    def neighbors(self) -> List["Slot"]:
        indexes = set()
        i = self.index
        if i > 0:
            indexes.add(i - 1)
        if i + 1 < len(self.state.slots):
            indexes.add(i + 1)
        indexes.update(getattr(self.state, "extra_neighbors", {}).get(i, set()))
        return [
            self.state.slots[idx]
            for idx in sorted(indexes)
            if 0 <= idx < len(self.state.slots)
            and self.state.slots[idx].card_type is not None
        ]

    @property
    def left(self) -> Optional["Slot"]:
        return self.state.slots[self.index - 1] if self.index > 0 else None

    @property
    def right(self) -> Optional["Slot"]:
        if self.index + 1 < len(self.state.slots):
            return self.state.slots[self.index + 1]
        return None

    @property
    def random(self) -> Optional["Slot"]:
        slots = self.all
        return self.state.rng.choice(slots) if slots else None

    # 按种族过滤（返回全场对应 tag 的非空槽）
    @property
    def zerg(self) -> List["Slot"]:
        return [s for s in self.all if s.tags.has("zerg")]

    @property
    def terran(self) -> List["Slot"]:
        return [s for s in self.all if s.tags.has("terran")]

    @property
    def protoss(self) -> List["Slot"]:
        return [s for s in self.all if s.tags.has("protoss")]

    @property
    def neutral(self) -> List["Slot"]:
        return [s for s in self.all if s.tags.has("neutral")]

    @property
    def teleport(self) -> "Slot":
        """折跃落点。

        若全场存在带 :data:`TELEPORT_TARGET_TAG` 标签的槽位，则折跃到该槽；
        否则折跃到自身。（旧版此处为未实现的 ``None`` 占位。）
        """
        for s in self.all:
            if s.tags.has(TELEPORT_TARGET_TAG):
                return s
        return self

    # ------------------------------------------------------------------
    # 派生量
    # ------------------------------------------------------------------
    def count(self, unit_type: str) -> int:
        return self.units.get(unit_type, 0)

    @property
    def psi_level(self) -> int:
        return self.level if self.tags.has("灵能") else 0

    @property
    def energy(self) -> int:
        total = 0
        bonus = 1
        for s in self.all:
            for handler in s.event_handlers:
                desc = handler.description
                if desc == "唯一:所有虚空水晶塔提供3点能量强度":
                    bonus = 3
                    break
                if desc == "唯一:所有虚空水晶塔提供2点能量强度":
                    bonus = max(bonus, 2)
        for s in self.neighbors + [self]:
            total += s.count("水晶塔") + s.count("虚空水晶塔") * bonus
        return total

    def price(self) -> float:
        return sum(UNIT_PRICES.get(u, 0.0) * n for u, n in self.units.items())

    @property
    def has_artanis(self) -> int:
        return int(any(s.count("阿塔尼斯") > 0 for s in self.all))

    @property
    def has_narud(self) -> int:
        return int(any(s.count("纳鲁德") > 0 for s in self.all))

    # ------------------------------------------------------------------
    # 单位增删改
    # ------------------------------------------------------------------
    def add_unit(self, unit_type: str, cnt: int) -> None:
        if cnt <= 0:
            return
        cnt = min(cnt, UNIT_COUNT_LIMIT - self.unit_count)
        if cnt <= 0:
            return
        self.units[unit_type] = self.units.get(unit_type, 0) + cnt
        self.unit_count += cnt

    def remove_unit(self, unit_type: str, cnt: int) -> None:
        old = self.units.get(unit_type, 0)
        cnt = min(cnt, old)
        if cnt <= 0:
            return
        self.units[unit_type] = old - cnt
        self.unit_count -= cnt
        if self.units[unit_type] == 0:
            del self.units[unit_type]

    def replace_unit(self, unit_type: str, max_cnt: int, new_unit_type: str, new_cnt: int) -> None:
        """数量 ≥ max_cnt 时整组替换一次。"""
        if self.count(unit_type) >= max_cnt:
            self.remove_unit(unit_type, max_cnt)
            self.add_unit(new_unit_type, new_cnt)

    def replace_all_units(self, unit_type: str, old_cnt: int, new_unit_type: str, new_cnt: int) -> None:
        """按 old_cnt 为一组，成组替换全部。"""
        if old_cnt <= 0:
            return
        repeats = self.count(unit_type) // old_cnt
        if repeats > 0:
            self.remove_unit(unit_type, old_cnt * repeats)
            self.add_unit(new_unit_type, new_cnt * repeats)

    # ------------------------------------------------------------------
    # 挂件 / 升级
    # ------------------------------------------------------------------
    def change_add_on(self, add_on_name: Optional[str] = None) -> None:
        reactor_cnt = self.count("反应堆")
        tech_cnt = self.count("科技实验室")

        if add_on_name is None:
            # 反应堆 <-> 科技实验室 互换
            if reactor_cnt > 0:
                self.remove_unit("反应堆", 1)
                self.add_unit("科技实验室", 1)
            elif tech_cnt > 0:
                self.remove_unit("科技实验室", 1)
                self.add_unit("反应堆", 1)
            else:
                return
        elif add_on_name == "高级科技实验室":
            if reactor_cnt > 0:
                self.remove_unit("反应堆", reactor_cnt)
                self.add_unit("高级科技实验室", reactor_cnt)
            elif tech_cnt > 0:
                self.remove_unit("科技实验室", tech_cnt)
                self.add_unit("高级科技实验室", tech_cnt)
            else:
                return
        else:
            return

        self.trigger([QuickProduceEvent(self.state, self)])
        self.state.trigger_any_card_event(AnyCardAddonChangedEvent(self.state, self))

    def upgrade(self, upgrade_name: str) -> bool:
        from star_tarven_simulator.upgrades import apply_instant_effect, is_stackable

        if len(self.upgrades) >= self.upgrades_limit:
            return False
        if upgrade_name in self.upgrades and not is_stackable(upgrade_name):
            return False

        self.upgrades.append(upgrade_name)
        apply_instant_effect(self, upgrade_name)
        if upgrade_name == "黄金矿工":
            self.state.card_engine.assign_card_to_slot("黄金矿工", self)
        return True

    def equivalent_power(self) -> float:
        """包含瓦斯升级战斗收益的启发式等效战力。"""
        from star_tarven_simulator.upgrades import equivalent_power

        return equivalent_power(self)

    # ------------------------------------------------------------------
    # 事件派发（对自身 handler）
    # ------------------------------------------------------------------
    def trigger(self, events) -> None:
        for event in events:
            for handler in list(self.event_handlers):
                if handler.event_name == event.event_name:
                    handler.handle(event)

    def __str__(self) -> str:
        return f"{self.card_type}({self.price()})"
