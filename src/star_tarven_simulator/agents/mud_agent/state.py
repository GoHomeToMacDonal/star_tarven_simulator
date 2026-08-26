"""对局的只读观测快照与跨回合追踪器。

引擎的 :class:`~star_tarven_simulator.simulator.game.Tarven` 是可变状态机；本模块提供：

* :class:`Observation` —— 从 ``Tarven`` 抽取的只读快照，方便策略/评分读取，不改引擎。
* :class:`Trackers` —— 由 Agent 维护的跨回合计数（三连次数、首次达 5 本的回合），
  因为 Agent 驱动了每个动作，这些量在 Agent 侧统计最可靠、且不需要改引擎。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from star_tarven_simulator.agents.mud_agent.reward import FINAL_ROUND, NEVER
from star_tarven_simulator.simulator.card import Card
from star_tarven_simulator.simulator.game import Tarven


@dataclass
class SlotView:
    """场上单个槽位的只读视图。"""

    index: int
    card_type: Optional[str]
    level: int
    is_gold: bool
    is_no_triple: bool
    tags: tuple
    upgrades: tuple


@dataclass
class Observation:
    """一个 ``Tarven`` 的只读快照。"""

    round: int
    level: int
    level_up_cost: int
    mineral: int
    gas: int
    hero_name: str
    shop: List[Optional[Card]]
    cache: List[object]
    slots: List[SlotView]
    has_force_action: bool

    @classmethod
    def capture(cls, t: Tarven) -> "Observation":
        return cls(
            round=t.round,
            level=t.level,
            level_up_cost=t.level_up_cost,
            mineral=t.mineral,
            gas=t.gas,
            hero_name=t.hero_controller.hero_name,
            shop=list(t.shop),
            cache=list(t.cache),
            slots=[
                SlotView(
                    index=s.index,
                    card_type=s.card_type,
                    level=s.level,
                    is_gold=bool(s.tags.has("金色")),
                    is_no_triple=bool(s.tags.has("无法三连")),
                    tags=tuple(s.tags),
                    upgrades=tuple(s.upgrades),
                )
                for s in t.slots
            ],
            has_force_action=bool(t.force_action),
        )

    # ------------------------------------------------------------------
    def empty_slot_count(self) -> int:
        return sum(1 for s in self.slots if s.card_type is None)

    def occupied_slots(self) -> List[SlotView]:
        return [s for s in self.slots if s.card_type is not None]

    def empty_cache_count(self) -> int:
        return sum(1 for c in self.cache if c is None)


@dataclass
class Trackers:
    """Agent 维护的跨回合统计。"""

    triple_count: int = 0
    round_reached_l5: int = NEVER
    _seen_l5: bool = field(default=False, repr=False)

    def on_triple(self) -> None:
        self.triple_count += 1

    def observe_level(self, level: int, current_round: int) -> None:
        """每回合观察一次等级，记录首次达到 5 本的回合。"""
        if not self._seen_l5 and level >= 5 and current_round <= FINAL_ROUND:
            self.round_reached_l5 = current_round
            self._seen_l5 = True

    def snapshot(self) -> "Trackers":
        return Trackers(
            triple_count=self.triple_count,
            round_reached_l5=self.round_reached_l5,
            _seen_l5=self._seen_l5,
        )
