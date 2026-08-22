"""引擎抽象基类，用于打破 slot / card_engine / game 之间的循环依赖。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Union

from star_tarven_simulator.simulator.card import Card


class AbstractCardEngine(ABC):
    """卡牌引擎抽象基类。"""

    @abstractmethod
    def assign_card_to_slot(self, card: Union["Card", str], slot) -> None:
        ...

    @abstractmethod
    def merge_slots(self, left_slot, right_slot) -> None:
        ...


class AbstractTarven(ABC):
    """酒馆抽象基类（Slot 只需要它暴露的少量接口）。"""

    slots: list
    card_engine: AbstractCardEngine
