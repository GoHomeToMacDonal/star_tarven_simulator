"""卡牌静态数据模型：:class:`Tags` / :class:`Card` / :class:`CardPool`。

相比旧版的调整：

* :class:`Tags` 不再在未知标签上抛断言错误（旧版 ``assert tag in CARD_TAGS`` 极易崩溃）。
  未知标签会被静默接受，便于 handler 动态添加派生标签（如"虚空投影""金色"）。
* :meth:`Card.from_json` 直接消费新版 ``v260822`` 结构（``units`` 为 dict、``description`` /
  ``gold_description`` 为字符串列表、``tags`` / ``gold_tags`` 显式给出）。
* :class:`CardPool` 用标准库 ``random`` 做按等级加权采样，去除 numpy 依赖。
* 加载后的 :class:`Card` 是**运行时不可变**的（16.10 裁决）：加载器在
  ``parse_card`` 填完 handler 模板后调用 :meth:`Card.seal`，把列表字段转 tuple、
  ``units`` 转 ``types.MappingProxyType``，并禁止任何字段赋值（抛
  :class:`AttributeError`）。需要"改一张卡"时用 :func:`dataclasses.replace`
  生成新卡（新卡默认未 seal）。动态派生的卡照常由英雄代码构造。

:class:`CardPool` 的采样性能（设计决策，改动前请先跑 ``tests/test_card_pool.py``）：

* 桶里存**密集下标**而非 uuid，并额外维护 ``_counts``（等级 × 卡种的剩余份数）与
  标签合格性位串 ``bytearray``。无标签采样因此是 O(等级数)、带标签采样是
  O(候选卡种数) + 单桶一次定位扫描，取代了原先"遍历全部 1500+ 份拷贝并逐份建
  ``set(card.tags)``"的做法。
* 桶仍**按位置保序**、移除仍用 swap-remove，且每次采样只消耗**一个**
  ``rng.randrange``，映射规则与旧实现一致 —— 所以同一 seed 下抽卡结果、卡池演化与
  随机数序列**位级不变**（RL 侧的 "bit identical" 契约依赖这一点）。
* 不引入 numpy：热路径优化后只剩几次整数运算，numpy 的调用开销反而更大；密集下标
  恒 < 256 会命中 CPython 小整数缓存，``list[int]`` 扫描比 ``array('i')`` 更快
  （后者每次取元素都要装箱）。基准见 ``benchmarks/bench_card_pool.py``。
* :class:`Card` / :class:`CardPool` 自定义 ``__deepcopy__``：卡牌定义运行时只读，
  克隆整局（``mud_agent`` 的 rollout）只复制桶 / 计数表 / RNG。
"""

from __future__ import annotations

import copy
import random
import types
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from star_tarven_simulator.constants.card_tag import (
    CARD_TAGS,
    CARD_PACKAGE_INVERTED_INDEX,
)
from star_tarven_simulator.constants.unit_prices import UNIT_PRICES

# 游戏中可用的种族列表
RACES = ["protoss", "terran", "zerg", "neutral"]

# 每个等级卡池中每种卡牌的基础份数（实际份数由 card_copies 派生）
CARD_POOL_NUMBER = {1: 18, 2: 15, 3: 13, 4: 11, 5: 9, 6: 6}

# "极低概率"同时命中被动标签"作为极低概率出现的卡牌"与风味"…极低概率特典卡"。
LOW_PROBABILITY_MARKER = "极低概率"

# 极低概率卡牌的同等级权重 = 普通卡牌的 1/LOW_PROBABILITY_SCALE。
LOW_PROBABILITY_SCALE = 10


def is_low_probability_card(card: Card) -> bool:
    """该卡是否为"极低概率出现"卡牌（普通/金色描述文本任一含标记）。"""
    return any(
        LOW_PROBABILITY_MARKER in desc
        for desc in (*card.description, *card.gold_description)
    )


def card_copies(card: Card) -> int:
    """该卡在卡池中的初始份数。

    普通卡牌 = ``CARD_POOL_NUMBER[level] * LOW_PROBABILITY_SCALE``；
    极低概率卡牌 = ``CARD_POOL_NUMBER[level]``（即普通卡的 1/10，整数比例精确成立）。
    """
    base = CARD_POOL_NUMBER[card.level]
    return base if is_low_probability_card(card) else base * LOW_PROBABILITY_SCALE


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
    """一张卡牌的静态定义（由 JSON 反序列化得到，加载后运行时不可变）。

    生命周期：:meth:`Card.from_json` 构造（未 seal，可写）→
    :func:`parsing.parser.parse_card` 就地填充 ``event_handlers`` /
    ``gold_event_handlers`` 模板 → :meth:`Card.seal` 固化。密封后：

    * 所有列表字段转为 ``tuple``（``append`` / ``remove`` / 下标赋值都会抛错）；
    * ``units`` 转为 :class:`types.MappingProxyType`（原地写入抛 ``TypeError``）；
    * 任何字段重新赋值抛 :class:`AttributeError`。

    需要"改一张卡"时用 :func:`dataclasses.replace` 从静态定义生成新卡：新卡
    默认**未** seal、字段可自由赋值，且原卡不受影响（英雄代码的动态派生卡
    走这条路径，见 ``simulator/hero.py`` 的 ``_initial_copy``）。

    * :func:`dataclasses.replace` 不依赖 :func:`copy.copy`：CPython 3.12 的
      replace 直接 ``Card(**changes)`` 走类构造器建新实例；``_sealed`` 是
      ``init=False`` 字段，构造器不接收它，且 :meth:`__post_init__` 总会把它
      复位为 ``False``，所以新卡自然未 seal。
    * 默认 :func:`copy.copy` 则**保留**封印：浅拷贝逐字段复制 ``__dict__``
      （含 ``_sealed``），sealed 卡的副本仍 sealed、依旧不可写——不会借
      浅拷贝绕过运行时不可变保护。
    """

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
    # 密封标志（init=False，见 seal()）；不在 repr/eq 中参与比较。
    # __post_init__ 保证任何构造路径都以未密封状态起步。
    _sealed: bool = field(default=False, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """任何构造路径（``from_json`` / 直接构造 / 英雄动态卡）都以未密封起步。"""
        self._sealed = False

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

    # ------------------------------------------------------------------
    # 运行时不可变（seal）契约
    # ------------------------------------------------------------------
    def seal(self) -> None:
        """把加载期确定的字段固化为运行时不可变（幂等，可重复调用）。

        依次做三件事：

        1. 全部列表字段（``description`` / ``gold_description`` / ``tags`` /
           ``gold_tags`` / ``source`` / ``event_handlers`` /
           ``gold_event_handlers``）转为 ``tuple``，杜绝 ``append`` /
           ``remove`` / 下标赋值等原地修改；
        2. ``units`` 转为 :class:`types.MappingProxyType`（先 ``dict()`` 拷贝再
           包装，代理与任何外部引用解耦），原地写入抛 :class:`TypeError`；
        3. 置 ``_sealed`` 标志，此后任何字段重新赋值抛 :class:`AttributeError`
           （见 :meth:`__setattr__`）。

        加载器在 ``parse_card`` 填完 handler 模板后调用本方法；此后整卡在
        运行时只读，``CardPool`` 跨局克隆（``__deepcopy__`` 按引用共享）才能
        安全成立。已密封的卡再次调用是空操作。
        """
        if getattr(self, "_sealed", False):
            return
        self.description = tuple(self.description)
        self.gold_description = tuple(self.gold_description)
        self.tags = tuple(self.tags)
        self.gold_tags = tuple(self.gold_tags)
        self.source = tuple(self.source)
        self.event_handlers = tuple(self.event_handlers)
        self.gold_event_handlers = tuple(self.gold_event_handlers)
        self.units = types.MappingProxyType(dict(self.units))
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value) -> None:
        """密封后禁止任何字段赋值（运行时不可变契约）。

        未 seal（构造期 / ``from_json`` / ``dataclasses.replace`` 生成的新卡）
        时行为与普通 dataclass 完全一致；已 seal 后一切赋值抛
        :class:`AttributeError`，提示改用 :func:`dataclasses.replace`。
        """
        if getattr(self, "_sealed", False):
            raise AttributeError(
                f"Card {getattr(self, 'name', '?')!r} 已 seal，禁止修改字段 "
                f"{name!r}；如需变更请用 dataclasses.replace 生成新卡"
            )
        object.__setattr__(self, name, value)

    def __str__(self) -> str:
        return f"{self.name}({self.level})"

    def __deepcopy__(self, memo):
        """卡牌是加载期确定的静态定义，运行时只读，因此深拷贝按值共享。

        加载后每张卡都已 :meth:`seal`（字段赋值抛错、嵌套容器不可原地修改），
        "改一张卡"一律走 ``dataclasses.replace`` 造新对象，因此共享实例不会让
        克隆局与原局互相影响。收益：``mud_agent`` 的 ``clone_game``
        （``copy.deepcopy(game)``）不再复制上百个 Card 及其 handler 模板。
        """
        return self


# 等级桶的合法下标范围（0 号桶恒空，仅为让 level 直接当下标用）
_LEVEL_BUCKETS = 7

# sample(levels=None) 的默认等级集合
_ALL_LEVELS = (1, 2, 3, 4, 5, 6)


class CardPool:
    """卡池：按等级加权随机抽卡，支持标签过滤（用于"发现"）。

    ``no_draw_uuids`` 中的卡牌仍会进入 :attr:`cards` / :attr:`card_map` /
    :attr:`card_type_map`（可查询、可被引擎使用），但**不会**进入可抽取的
    等级桶——即"不出现在卡池、但仍可使用"。用于辅助卡 / 特殊卡：
    它们通过定点部署等途径使用，不应被商店随机抽到。

    内部表示（性能相关，见 ``benchmarks/bench_card_pool.py``）：

    * 卡牌按出现顺序编号为**密集下标** ``dense``（``_uuids`` / ``_dense`` / ``_cards``），
      桶里存的是密集下标而非 uuid。密集下标恒 < 256，因此列表元素全部命中 CPython
      小整数缓存，扫描时不产生装箱开销；这也让计数表与标签掩码可以用扁平数组/字节串
      按下标 O(1) 访问（``array('i')`` 反而会在每次取元素时装箱，故不采用）。
    * ``_buckets[level]`` 仍是**按位置保序**的可重复列表，移除沿用"与末位交换后弹出"
      （swap-remove）语义。这保证同一 seed 下抽卡结果、卡池演化与随机数消耗与旧实现
      **位级一致**（见 ``tests/test_card_pool.py`` 的对拍测试）。

    桶的内部结构不对外暴露，请用 :meth:`bucket_uuids` / :meth:`bucket_size` /
    :meth:`total_size` / :meth:`set_bucket` 访问。
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

        # uuid <-> 密集下标
        self._uuids: List[int] = []
        self._dense: Dict[int, int] = {}
        self._cards: List[Card] = []
        for card in cards:
            if card.uuid in self._dense:
                continue
            self._dense[card.uuid] = len(self._uuids)
            self._uuids.append(card.uuid)
            self._cards.append(card)

        # _buckets[level]：该等级的密集下标可重复列表（不含 no_draw_uuids 中的卡）
        # _counts[level][dense]：该等级该卡的剩余份数，与 _buckets 同步维护，
        #   让"按等级/标签求权重"从 O(拷贝数) 降到 O(卡种数)。
        self._ncards = len(self._uuids)
        self._buckets: List[List[int]] = [[] for _ in range(_LEVEL_BUCKETS)]
        self._counts: List[List[int]] = [
            [0] * self._ncards for _ in range(_LEVEL_BUCKETS)
        ]
        for card in cards:
            if card.uuid in self.no_draw_uuids:
                continue
            if 1 <= card.level <= 6:
                dense = self._dense[card.uuid]
                number = card_copies(card)
                self._buckets[card.level] += [dense] * number
                self._counts[card.level][dense] += number

        # 标签过滤用的合格性位串：dense -> 0/1，按标签组合缓存
        self._all_mask = bytearray(b"\x01" * self._ncards)
        self._all_members: List[int] = list(range(self._ncards))
        self._mask_cache: Dict[frozenset, tuple] = {}

    # ------------------------------------------------------------------
    # 桶访问器
    # ------------------------------------------------------------------
    def bucket_size(self, level: int) -> int:
        """该等级剩余的总份数。"""
        if not (0 <= level < _LEVEL_BUCKETS):
            return 0
        return len(self._buckets[level])

    def total_size(self) -> int:
        """全部等级剩余的总份数。"""
        return sum(len(bucket) for bucket in self._buckets)

    def bucket_uuids(self, level: int) -> List[int]:
        """该等级桶内的 uuid 序列（含重复，顺序即内部顺序）。"""
        if not (0 <= level < _LEVEL_BUCKETS):
            return []
        uuids = self._uuids
        return [uuids[dense] for dense in self._buckets[level]]

    def is_pool_entity(self, card) -> bool:
        """该 Card 是否代表一份真实的公共池实体（可抽取、非衍生、存在于池中）。

        判断依据与 :meth:`place_back` 的忽略条件互补：``derived`` / ``no_draw`` /
        未知 uuid 的卡不属于公共池实体，出售/摧毁时不应被归池，否则会凭空膨胀卡池。
        免费生成的静态定义（如英雄直接发放的卡池内卡牌）不是实体，调用方必须通过
        显式 ``origin=[]`` 覆盖本判断（见 ``Tarven.grant_reward_card`` / ``enter_card_direct``）。
        """
        return (
            isinstance(card, Card)
            and not card.derived
            and card.uuid in self._dense
            and card.uuid not in self.no_draw_uuids
        )

    def set_bucket(self, level: int, uuids: List[int]) -> None:
        """整桶替换（测试 / 场景构造用）。未知 uuid 会被忽略。"""
        if not (0 <= level < _LEVEL_BUCKETS):
            return
        dense_of = self._dense
        bucket = [dense_of[u] for u in uuids if u in dense_of]
        self._buckets[level] = bucket
        counts = [0] * self._ncards
        for dense in bucket:
            counts[dense] += 1
        self._counts[level] = counts

    def clear_bucket(self, level: int) -> None:
        """清空某个等级的桶。"""
        self.set_bucket(level, [])

    def assert_consistent(self) -> None:
        """校验计数表与桶内容同步（测试用；正常路径不调用，避免开销）。"""
        for level in range(_LEVEL_BUCKETS):
            counts = self._counts[level]
            assert len(counts) == self._ncards
            expected = [0] * self._ncards
            for dense in self._buckets[level]:
                expected[dense] += 1
            assert counts == expected, f"等级 {level} 的计数表与桶不一致"
            assert sum(counts) == len(self._buckets[level])

    # ------------------------------------------------------------------
    # 克隆
    # ------------------------------------------------------------------
    #: 深拷贝时按引用共享的静态字段（加载期确定、运行时只读）
    _SHARED_ON_COPY = (
        "cards",
        "card_map",
        "card_type_map",
        "no_draw_uuids",
        "_uuids",
        "_dense",
        "_cards",
        "_ncards",
        "_all_mask",
        "_all_members",
        "_mask_cache",
    )

    def __deepcopy__(self, memo) -> "CardPool":
        """只复制可变状态（桶 / 计数表 / RNG），静态数据共享。

        ``mud_agent`` 每次 rollout 都 ``copy.deepcopy`` 整局，默认行为会连带复制卡牌
        定义与各种索引字典。这里只复制真正会变的部分：

        * ``_buckets`` / ``_counts``：用 ``list()`` 逐级浅拷（元素是小整数）；
        * ``rng``：交给 ``deepcopy`` 并带上 ``memo``，因为 ``Tarven.rng`` 与
          ``MudAgent.rng`` 都是同一个 ``Random`` 实例的别名，必须保持克隆后仍然别名同一份；
        * ``_mask_cache``：内容由静态标签推导，且取用时不会原地修改，可安全共享。
        """
        cls = self.__class__
        new = cls.__new__(cls)
        memo[id(self)] = new
        for attr in self._SHARED_ON_COPY:
            setattr(new, attr, getattr(self, attr))
        new._buckets = [list(bucket) for bucket in self._buckets]
        new._counts = [list(counts) for counts in self._counts]
        new.rng = copy.deepcopy(self.rng, memo)
        return new

    # ------------------------------------------------------------------
    # 抽取 / 归还
    # ------------------------------------------------------------------
    def draw(self, count: int, max_level: int) -> List[Card]:
        cards: List[Card] = []
        levels = list(range(1, max_level + 1))
        for _ in range(count):
            uuid = self.sample(levels=levels)
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
                dense = self._dense[card.uuid]
                self._buckets[card.level].append(dense)
                self._counts[card.level][dense] += 1

    def take(self, card: Card) -> Optional[Card]:
        """从公共池精确取出 ``card`` 的一份；不存在时不修改卡池。"""
        if not isinstance(card, Card) or card.derived or card.uuid in self.no_draw_uuids:
            return None
        if not (1 <= card.level < _LEVEL_BUCKETS):
            return None
        dense = self._dense.get(card.uuid)
        if dense is None:
            return None
        if not self._counts[card.level][dense]:
            return None
        bucket = self._buckets[card.level]
        index = bucket.index(dense)
        bucket[index] = bucket[-1]
        bucket.pop()
        self._counts[card.level][dense] -= 1
        return self.card_map[card.uuid]

    def take_by_name(self, name: str) -> Optional[Card]:
        """按当前卡池中的静态名称取一份，不接受调用方构造的 Card 实例。"""
        card = self.card_type_map.get(name)
        return self.take(card) if card is not None else None

    def count(self, card: Card) -> int:
        if not isinstance(card, Card) or not (1 <= card.level < _LEVEL_BUCKETS):
            return 0
        dense = self._dense.get(card.uuid)
        if dense is None:
            return 0
        return self._counts[card.level][dense]

    def sample(
        self,
        levels=None,
        excepts: Optional[List[int]] = None,
        tags: Optional[List[str]] = None,
    ) -> Optional[int]:
        """随机取出一份卡牌并返回其 uuid；无候选时返回 ``None``。

        :param levels: 允许的等级。``None`` 表示 1~6；也接受单个 int 或任意可迭代。
        :param excepts: 禁止抽到的 uuid 列表。
        :param tags: 标签白名单，命中**任一**标签即合格（用于"发现"）。

        抽取权重 = 各卡在池中的剩余份数，与旧实现（逐拷贝铺平后均匀取一个）等价，
        且随机数消耗完全相同。
        """
        if levels is None:
            levels = _ALL_LEVELS
        elif isinstance(levels, int):
            levels = (levels,)
        elif not isinstance(levels, (list, tuple)):
            levels = tuple(levels)

        if not tags and not excepts:
            return self._sample_unfiltered(levels)
        return self._sample_filtered(levels, excepts, tags)

    # 兼容别名：历史代码直接调用私有方法，保留以免破坏外部使用者。
    _sample = sample

    def _sample_unfiltered(self, levels) -> Optional[int]:
        """无标签/排除时的快路径：O(len(levels))，不做任何逐拷贝扫描。

        与旧实现位级等价：旧实现把候选按"等级优先、桶内下标升序"铺成一维列表后
        ``randrange(len)``，这里用同一个 ``randrange(总份数)`` 再按前缀和还原
        ``(level, position)``，抽到的是同一个位置、消耗同一个随机数。
        """
        buckets = self._buckets
        total = 0
        for level in levels:
            if 0 <= level < _LEVEL_BUCKETS:
                total += len(buckets[level])
        if not total:
            return None

        offset = self.rng.randrange(total)
        for level in levels:
            if not (0 <= level < _LEVEL_BUCKETS):
                continue
            size = len(buckets[level])
            if offset < size:
                return self._remove_at(level, offset)
            offset -= size
        return None  # pragma: no cover - 前缀和必然命中

    def _sample_filtered(self, levels, excepts, tags) -> Optional[int]:
        """带标签 / 排除时的采样：O(候选卡种数) 求权重 + 单桶一次定位扫描。

        同样与旧实现位级等价：权重按"等级优先"累加、桶内按位置升序定位第 k 个合格
        拷贝，因此 ``randrange(合格总份数)`` 映射到的位置与旧实现完全相同。
        """
        eligible, members = self._eligibility(tags, excepts)

        # 各等级的合格份数（与 levels 的迭代顺序一一对应，重复等级也照旧计两次）
        valid_levels: List[int] = []
        level_totals: List[int] = []
        total = 0
        counts_all = self._counts
        for level in levels:
            if not (0 <= level < _LEVEL_BUCKETS):
                continue
            counts = counts_all[level]
            subtotal = 0
            for dense in members:
                subtotal += counts[dense]
            valid_levels.append(level)
            level_totals.append(subtotal)
            total += subtotal
        if not total:
            return None

        offset = self.rng.randrange(total)
        for level, subtotal in zip(valid_levels, level_totals):
            if offset < subtotal:
                return self._locate_and_remove(level, offset, eligible)
            offset -= subtotal
        return None  # pragma: no cover - 前缀和必然命中

    def _locate_and_remove(self, level: int, k: int, eligible) -> Optional[int]:
        """取出 ``level`` 桶内第 ``k``（0 起）个合格拷贝。"""
        for pos, dense in enumerate(self._buckets[level]):
            if eligible[dense]:
                if not k:
                    return self._remove_at(level, pos)
                k -= 1
        return None  # pragma: no cover - 权重保证 k 一定命中

    def _eligibility(self, tags, excepts):
        """返回 ``(eligible 位串, 合格卡的密集下标列表)``。

        标签掩码按标签组合缓存：卡牌标签是加载期静态数据，运行时不会变（衍生卡走
        ``replace`` 造新对象且从不入池），因此缓存无需失效。
        """
        if tags:
            key = frozenset(tags)
            cached = self._mask_cache.get(key)
            if cached is None:
                mask = bytearray(self._ncards)
                members: List[int] = []
                for dense, card in enumerate(self._cards):
                    if key & set(card.tags):
                        mask[dense] = 1
                        members.append(dense)
                cached = (mask, members)
                self._mask_cache[key] = cached
            mask, members = cached
        else:
            mask, members = self._all_mask, self._all_members

        if excepts:
            dense_of = self._dense
            blocked = {dense_of[u] for u in excepts if u in dense_of}
            if blocked:
                mask = bytearray(mask)
                for dense in blocked:
                    mask[dense] = 0
                members = [dense for dense in members if dense not in blocked]
        return mask, members

    def _remove_at(self, level: int, index: int) -> int:
        """从 ``level`` 桶的 ``index`` 位置 swap-remove 一份，返回其 uuid。"""
        bucket = self._buckets[level]
        dense = bucket[index]
        bucket[index] = bucket[-1]
        bucket.pop()
        self._counts[level][dense] -= 1
        return self._uuids[dense]
