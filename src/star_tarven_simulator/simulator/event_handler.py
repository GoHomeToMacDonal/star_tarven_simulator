"""事件处理器（EventHandler）与两个特殊包装器（Task / Gathering）。

相比旧版的修正：

* 统一使用 ``description`` 字段（旧版 ``card_engine.merge_slots`` 误用 ``handler.desc``）。
* 移除了 ``handle()`` 里的调试 ``print``。
* 通用化 ``copy()``：任何带 ``copy`` 方法的包装器都会被深拷贝（隔离计数器），
  普通函数 / ``functools.partial`` 则原样复用。
* 修正 unique 分支：不再在此处偷偷修改黑暗值（改由 :meth:`Tarven.gain_darkness`）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable, Union

from star_tarven_simulator.simulator.event import (
    Event,
    BaseEvent,
    AnyTaskFinishedEvent,
)

if TYPE_CHECKING:  # 避免运行时循环导入
    from star_tarven_simulator.simulator.slot import Slot


class GatheringActionHandler:
    """集结（集结(N)）包装器。

    触发次数 ``times = min(energy // cost, 2) + has_artanis``，
    并作为额外参数传给内部 handler ``handler(slot, event, times)``。
    """

    def __init__(self, handler: Callable, cost: int):
        self.handler = handler
        self.cost = cost

    def copy(self) -> "GatheringActionHandler":
        return GatheringActionHandler(self.handler, self.cost)

    def times(self, slot: "Slot") -> int:
        return min(slot.energy // self.cost, 2) + slot.has_artanis

    def __call__(self, slot: "Slot", event):
        self.handler(slot, event, self.times(slot))


class TaskActionHandler:
    """任务（任务:… 奖励:…）包装器。

    内部维护 ``counter``，达到 ``goal`` 时调用奖励 handler 并广播
    :class:`AnyTaskFinishedEvent`。``copy()`` 会隔离计数器。
    """

    def __init__(self, handler: Callable, goal: int, auto_reset: bool = False):
        self.handler = handler
        self.goal = goal
        self.counter = 0
        self.auto_reset = auto_reset

    def copy(self) -> "TaskActionHandler":
        return TaskActionHandler(self.handler, self.goal, self.auto_reset)

    def is_finished(self) -> bool:
        return self.counter >= self.goal

    def reset(self) -> None:
        self.counter = 0

    def __call__(self, slot: "Slot", event):
        if self.counter < self.goal:
            self.counter += 1

        if self.counter == self.goal:
            self.handler(slot, event)
            event.tarven.trigger_any_card_event(
                AnyTaskFinishedEvent(event.tarven, slot)
            )
            if self.auto_reset:
                self.counter = 0


# 任何"可复制"的包装器类型
_Copyable = Union[GatheringActionHandler, TaskActionHandler]


class EventHandler:
    """把一条卡牌描述绑定到某个槽位与某个事件。"""

    def __init__(
        self,
        tarven,
        slot: "Slot",
        description: str,
        action_handler: Callable,
        event_name: Union[Event, str],
        unique: bool = False,
    ):
        self.tarven = tarven
        self.slot = slot
        self.description = description
        self.action_handler = action_handler
        self.event_name = Event(event_name)
        self.unique = unique

    def copy(self, tarven, slot: "Slot") -> "EventHandler":
        handler = self.action_handler
        if hasattr(handler, "copy"):
            handler = handler.copy()
        return EventHandler(
            tarven,
            slot,
            self.description,
            handler,
            self.event_name,
            self.unique,
        )

    def handle(self, event: BaseEvent) -> None:
        if event.event_name != self.event_name:
            return

        if self.unique:
            # 全场同描述效果只结算一次：若左侧已有同描述 handler，则跳过。
            for s in self.slot.left_all:
                for other in s.event_handlers:
                    if other is not self and other.description == self.description:
                        return

        self.action_handler(self.slot, event)
