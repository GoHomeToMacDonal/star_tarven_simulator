"""玩家动作定义。"""

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
    """强制动作：三连合成后的选择（3 张随机卡 + 可能的聚能器）。"""

    left_slot_idx: int
    right_slot_idx: int
    options: List[Union[Card, str]] = field(default_factory=list)
    selected: Optional[Union[Card, str]] = None
