"""玩家动作定义。"""

from copy import deepcopy
from dataclasses import dataclass, field
from typing import List, Optional, Union

from star_tarven_simulator.simulator.card import Card


class Action:
    """动作基类。"""


@dataclass
class UpgradeTarvenAction(Action):
    """升级酒馆等级。"""


@dataclass
class BuyAction(Action):
    """购买卡牌。

    ``slot_idx`` 为空表示进暂存区。直接进场时，普通卡只能选择首个空槽；带
    ``能够定点部署`` 标签的卡在场上有空槽时可以选择任意位置。
    """

    shop_idx: int
    slot_idx: Optional[int] = None


@dataclass
class CacheEnterAction(Action):
    """把暂存区的卡放到场上，槽位规则与 :class:`BuyAction` 相同。"""

    cache_idx: int
    slot_idx: Optional[int] = None


@dataclass
class SynthesisAction(Action):
    """三连合成。"""

    shop_idx: Optional[int] = None
    cache_idx: Optional[int] = None


@dataclass
class SellAction(Action):
    """出售。"""

    slot_idx: int


@dataclass
class DeployAction(Action):
    """定点部署一张辅助卡到指定卡牌槽位。

    辅助卡（``辅助卡`` tag，无单位，带 ``部署时`` 效果）不进场为常驻卡，而是"部署"到某张
    已在场的卡牌上，对其结算部署效果后被消耗。

    Attributes:
        slot_idx: 部署目标槽位（"指定卡牌"），**必须已有卡牌**，否则动作非法。
        cache_idx: 辅助卡来自暂存区的下标（与 ``shop_idx`` 二选一）。
        shop_idx: 辅助卡来自商店的下标（与 ``cache_idx`` 二选一，需花费晶体矿）。
    """

    slot_idx: int
    cache_idx: Optional[int] = None
    shop_idx: Optional[int] = None


@dataclass
class UpgradeAction(Action):
    """给卡牌加升级（花 gas）。"""

    slot_idx: int


@dataclass
class RefreshAction(Action):
    """刷新商店。"""


@dataclass
class LockAction(Action):
    """锁定商店。"""


@dataclass
class ChooseCardAction(Action):
    """强制动作：从若干候选卡中选择（发现）。"""

    cards: List[Card] = field(default_factory=list)
    selected_card: Optional[Card] = None
    delay: int = 0


@dataclass
class ChooseUpgradeAction(Action):
    """强制动作：选择升级。"""

    slot_idx: int
    upgrade_names: List[str] = field(default_factory=list)
    selected_upgrade_name: Optional[str] = None


@dataclass
class ChooseSynthesisAction(Action):
    """强制动作：三连合成后的选择（3 张随机卡 + 可能的聚能器）。

    ``consumed_origin`` 保存被三连消耗的"第 3 张卡"的真实公共池来源（商店 /
    暂存区 / 直接进场路径在 :meth:`~star_tarven_simulator.simulator.game.Tarven._begin_synthesis`
    时随动作暂存），结算时合并进金卡槽，保证金卡出售/摧毁时能归还全部 3 份原卡。
    """

    left_slot_idx: int
    right_slot_idx: int
    options: List[Union[Card, str]] = field(default_factory=list)
    selected: Optional[Union[Card, str]] = None
    consumed_origin: List[Card] = field(default_factory=list)



@dataclass
class HeroPowerAction(Action):
    """通用英雄主动能力。

    字段刻意保持宽泛；固定英雄实现只读取自己需要的参数，因而无需为每个英雄
    新增一种动作类型。
    """

    slot_idx: Optional[int] = None
    target_idx: Optional[int] = None
    cache_idx: Optional[int] = None
    shop_idx: Optional[int] = None
    choice: object = None
    mode: Optional[str] = None
    card: object = None
    race: Optional[str] = None
    amount: Optional[int] = None
    unit: Optional[str] = None


@dataclass
class HeroChoiceAction(Action):
    """英雄能力产生的统一选择动作。

    ``pool_owned`` 表示候选是从卡池实际抽出的；完成选择或失败时控制器据此
    精确归还候选，保证卡池数量守恒。创建时保存服务端快照，提交时只允许修改
    ``selected``，防止调用方篡改候选、类型或所有权来伪造奖励。
    """

    options: List[object] = field(default_factory=list)
    selected: object = None
    kind: str = "card"
    payload: dict = field(default_factory=dict)
    pool_owned: bool = True
    _original_options: tuple = field(init=False, repr=False)
    _original_kind: str = field(init=False, repr=False)
    _original_payload: dict = field(init=False, repr=False)
    _original_pool_owned: bool = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._original_options = tuple(self.options)
        self._original_kind = self.kind
        self._original_payload = deepcopy(self.payload)
        self._original_pool_owned = self.pool_owned

    def integrity_valid(self) -> bool:
        same_options = len(self.options) == len(self._original_options) and all(
            current is original
            if isinstance(original, Card)
            else current == original
            for current, original in zip(self.options, self._original_options)
        )
        return (
            same_options
            and self.kind == self._original_kind
            and self.payload == self._original_payload
            and self.pool_owned == self._original_pool_owned
        )

    def original_pool_candidates(self) -> list[Card]:
        if not self._original_pool_owned:
            return []
        return [item for item in self._original_options if isinstance(item, Card)]
