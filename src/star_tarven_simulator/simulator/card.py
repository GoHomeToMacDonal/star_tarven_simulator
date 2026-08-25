"""卡牌静态数据模型：:class:`Tags` / :class:`Card` / :class:`CardPool`。

相比旧版的调整：

* :class:`Tags` 不再在未知标签上抛断言错误（旧版 ``assert tag in CARD_TAGS`` 极易崩溃）。
  未知标签会被静默接受，便于 handler 动态添加派生标签（如"虚空投影""金色"）。
* :meth:`Card.from_json` 直接消费新版 ``v260822`` 结构（``units`` 为 dict、``description`` /
  ``gold_description`` 为字符串列表、``tags`` / ``gold_tags`` 显式给出）。
* :class:`CardPool` 用标准库 ``random`` 做按等级加权采样，去除 numpy 依赖。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from star_tarven_simulator.constants.card_tag import (
    CARD_TAGS,
    CARD_PACKAGE_INVERTED_INDEX,
)
from star_tarven_simulator.constants.unit_prices import UNIT_PRICES

# 游戏中可用的种族列表
RACES = ["protoss", "terran", "zerg", "neutral"]

# 每个等级卡池中每种卡牌的份数
CARD_POOL_NUMBER = {1: 18, 2: 15, 3: 13, 4: 11, 5: 9, 6: 6}


class Tags:
    """卡牌标签集合。

    ``has`` / ``add`` / ``remove`` 均不对未知标签报错，只在标签明显拼错时打印一次告警。
    """

    def __init__(self, tags: Optional[List[str]] = None):
        self.tags: List[str] = list(tags) if tags else []

    def has(self, tag: str) -> int:
        return int(tag in self.tags)

    def add(self, tag: str) -> None:
        if tag not in self.tags:
            self.tags.append(tag)

    def remove(self, tag: str) -> None:
        if tag in self.tags:
            self.tags.remove(tag)

    def clear(self) -> None:
        self.tags = []

    def __iter__(self):
        return iter(self.tags)

    def __repr__(self) -> str:
        return f"Tags({self.tags!r})"


@dataclass
class Card:
    """一张卡牌的静态定义（由 JSON 反序列化得到）。"""

    uuid: int
    name: str
    level: int
    race: str
    description: List[str]
    gold_description: List[str]
    units: Dict[str, int]
    tags: List[str]
    gold_tags: List[str]
    package: Optional[str] = None
    # 卡牌来源标注（核心种族 / 拓展包 / 辅助卡 / 特殊）；用于按拓展包过滤卡池
    source: List[str] = field(default_factory=list)
    # 由解析器填充：描述行 -> EventHandler 模板
    event_handlers: List = field(default_factory=list)
    gold_event_handlers: List = field(default_factory=list)
    # 由英雄能力动态生成的卡牌不会归还普通卡池，也不计入干扰者静态战力。
    derived: bool = False

    @property
    def price(self) -> float:
        """按单位价值计算的卡牌总价。"""
        return sum(UNIT_PRICES.get(unit, 0.0) * num for unit, num in self.units.items())

    @staticmethod
    def from_json(card_json: dict) -> "Card":
        package = CARD_PACKAGE_INVERTED_INDEX.get(card_json["name"], None)

        card = Card(
            uuid=int(card_json["uuid"]),
            name=card_json["name"],
            level=int(card_json["level"]),
            race=card_json["race"],
            description=list(card_json.get("description", [])),
            gold_description=list(card_json.get("gold_description", [])),
            units={str(k): int(v) for k, v in card_json.get("units", {}).items()},
            tags=list(card_json.get("tags", [])),
            gold_tags=list(card_json.get("gold_tags", [])),
            package=package,
            source=list(card_json.get("source", [])),
        )

        # race 自动补进 tags / gold_tags
        if card.race not in card.tags:
            card.tags.append(card.race)
        if card.race not in card.gold_tags:
            card.gold_tags.append(card.race)

        # 未在白名单中的标签只告警，不阻断加载
        for tag in set(card.tags) | set(card.gold_tags):
            if tag not in CARD_TAGS:
                print(f"[warn] 未知标签 {tag!r}（卡牌 {card.name}），已保留但不在 CARD_TAGS 中")

        return card

    def __str__(self) -> str:
        return f"{self.name}({self.level})"


class CardPool:
    """卡池：按等级加权随机抽卡，支持标签过滤（用于"发现"）。

    ``no_draw_uuids`` 中的卡牌仍会进入 :attr:`cards` / :attr:`card_map` /
    :attr:`card_type_map`（可查询、可被引擎使用），但**不会**进入可抽取的
    :attr:`pool` 等级桶——即"不出现在卡池、但仍可使用"。用于辅助卡 / 特殊卡：
    它们通过定点部署等途径使用，不应被商店随机抽到。
    """

    def __init__(
        self,
        cards: List[Card],
        no_draw_uuids: Optional[set] = None,
        rng: Optional[random.Random] = None,
    ):
        self.cards = cards
        self.rng = rng if rng is not None else random.Random()
        self.card_map = {card.uuid: card for card in cards}
        self.card_type_map = {card.name: card for card in cards}
        self.no_draw_uuids = set(no_draw_uuids) if no_draw_uuids else set()

        # pool[level] 是该等级 uuid 的可重复列表（不含 no_draw_uuids 中的卡）
        self.pool: List[List[int]] = [[] for _ in range(7)]
        for card in cards:
            if card.uuid in self.no_draw_uuids:
                continue
            if 1 <= card.level <= 6:
                self.pool[card.level] += [card.uuid] * CARD_POOL_NUMBER[card.level]

    def draw(self, count: int, max_level: int) -> List[Card]:
        cards: List[Card] = []
        levels = list(range(1, max_level + 1))
        for _ in range(count):
            uuid = self._sample(levels=levels)
            if uuid is None:
                break
            cards.append(self.card_map[uuid])
        return cards

    def place_back(self, cards) -> None:
        """把卡牌放回卡池。接受单张 Card 或 Card 列表。"""
        if isinstance(cards, Card):
            cards = [cards]
        for card in cards:
            if isinstance(card, Card) and 1 <= card.level <= 6:
                if card.derived or card.uuid in self.no_draw_uuids or card.uuid not in self.card_map:
                    continue
                self.pool[card.level].append(card.uuid)

    def take(self, card: Card) -> Optional[Card]:
        """从公共池精确取出 ``card`` 的一份；不存在时不修改卡池。"""
        if not isinstance(card, Card) or card.derived or card.uuid in self.no_draw_uuids:
            return None
        if not (1 <= card.level < len(self.pool)):
            return None
        bucket = self.pool[card.level]
        try:
            index = bucket.index(card.uuid)
        except ValueError:
            return None
        bucket[index] = bucket[-1]
        bucket.pop()
        return self.card_map[card.uuid]

    def take_by_name(self, name: str) -> Optional[Card]:
        """按当前卡池中的静态名称取一份，不接受调用方构造的 Card 实例。"""
        card = self.card_type_map.get(name)
        return self.take(card) if card is not None else None

    def count(self, card: Card) -> int:
        if not isinstance(card, Card) or not (1 <= card.level < len(self.pool)):
            return 0
        return self.pool[card.level].count(card.uuid)

    def _sample(
        self,
        levels: Optional[List[int]] = None,
        excepts: Optional[List[int]] = None,
        tags: Optional[List[str]] = None,
        max_sample_times: int = 100,
    ) -> Optional[int]:
        if levels is None:
            levels = [1, 2, 3, 4, 5, 6]

        eligible = []
        wanted_tags = set(tags or [])
        blocked = set(excepts or [])
        for level in levels:
            if not (0 <= level < len(self.pool)):
                continue
            for idx, uuid in enumerate(self.pool[level]):
                card = self.card_map[uuid]
                if uuid in blocked:
                    continue
                if wanted_tags and not (wanted_tags & set(card.tags)):
                    continue
                eligible.append((level, idx, uuid))
        if not eligible:
            return None

        level, idx, uuid = eligible[self.rng.randrange(len(eligible))]
        pool = self.pool[level]
        pool[idx] = pool[-1]
        pool.pop()
        return uuid
