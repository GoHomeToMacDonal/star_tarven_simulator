"""事件系统。

事件是卡牌效果的驱动核心。每个 ``*Event`` 携带触发上下文，``EventHandler`` 监听
某个 :class:`Event` 并在匹配时调用对应的 ``action_handler(slot, event)``。

相比旧版，这里统一了各事件的载荷属性命名，并修正了构造签名不一致的问题：

* 所有"触发方槽位"都同时暴露一个通用别名 :attr:`BaseEvent.slot`，方便 handler 取用，
  同时保留语义化名称（如 ``sold_slot`` / ``entered_slot`` / ``larva_slot``）。
* :class:`GainDarknessEvent` 统一为 ``(tarven, slot, amount)``。
* :class:`AnyCardHatchEvent` 统一为 ``(tarven, slot, units)``。
"""

from enum import Enum


class Event(Enum):
    """事件枚举（value 与描述中的触发时机一一对应）。"""

    ROUND_START = "round_start"
    ROUND_END = "round_end"
    ROUND_WIN = "round_win"

    SELLING = "selling"
    ENTERING = "entering"
    ANY_CARD_SOLD = "any_card_sold"
    ANY_CARD_ENTERED = "any_card_entered"
    ANY_CARD_ENTERED_OR_SOLD = "any_card_entered_or_sold"
    GAIN_DARKNESS = "gain_darkness"

    UPGRADE = "upgrade"
    REFRESH = "refresh"
    LEVEL_UP = "level_up"

    # 人族事件
    QUICK_PRODUCE = "quick_produce"
    ANY_CARD_ADDON_CHANGED = "any_card_addon_changed"
    ANY_TASK_FINISHED = "any_task_finished"

    # 虫族事件
    ANY_CARD_LARVA = "any_card_larva"
    ANY_CARD_HATCH = "any_card_hatch"

    # 神族事件
    ANY_CARD_TELEPORT = "any_card_teleport"
    ANY_CARD_GAIN_VOID_CRYSTAL_TOWER = "any_card_gain_void_crystal_tower"

    # 辅助卡
    DEPLOYMENT = "deployment"

    # 跨玩家（休眠事件，需多人战斗/出局驱动）
    OTHER_PLAYER_SOLD_HERO_CARD = "other_player_sold_hero_card"


class BaseEvent:
    """所有事件的基类。

    Attributes:
        tarven: 触发事件的酒馆（全局状态）。
        slot: 触发方槽位的通用别名（若适用），否则为 ``None``。
    """

    event_name: Event

    def __init__(self, tarven, slot=None):
        self.tarven = tarven
        self.slot = slot


# ---------------------------------------------------------------------------
# 回合开始 / 结束
# ---------------------------------------------------------------------------
class RoundStartEvent(BaseEvent):
    event_name = Event.ROUND_START


class RoundEndEvent(BaseEvent):
    event_name = Event.ROUND_END


class RoundWinEvent(BaseEvent):
    """回合胜利时（战斗阶段获胜）。

    本模拟器只建模酒馆经济、不建模战斗，因此该事件默认不会由引擎主动触发
    （与 :class:`DeploymentEvent` 一样属于"已实现但需外部驱动"的休眠事件）。
    上层若接入战斗结果，可调用 :meth:`Tarven.trigger_round_win` 派发。
    """

    event_name = Event.ROUND_WIN


# ---------------------------------------------------------------------------
# 购买 / 出售 / 进场
# ---------------------------------------------------------------------------
class SellingEvent(BaseEvent):
    """自身被出售时。"""

    event_name = Event.SELLING

    def __init__(self, tarven, slot):
        super().__init__(tarven, slot)
        self.selling_slot = slot


class EnteringEvent(BaseEvent):
    """自身进场时。"""

    event_name = Event.ENTERING

    def __init__(self, tarven, slot):
        super().__init__(tarven, slot)
        self.entering_slot = slot


class AnyCardSoldEvent(BaseEvent):
    """任意卡牌出售时（对全场派发）。``sold_slot`` 为被出售的槽位。"""

    event_name = Event.ANY_CARD_SOLD

    def __init__(self, tarven, sold_slot):
        super().__init__(tarven, sold_slot)
        self.sold_slot = sold_slot


class AnyCardEnteredEvent(BaseEvent):
    """任意卡牌进场时。``entered_slot`` 为进场的槽位。"""

    event_name = Event.ANY_CARD_ENTERED

    def __init__(self, tarven, entered_slot):
        super().__init__(tarven, entered_slot)
        self.entered_slot = entered_slot


class AnyCardEnteredOrSoldEvent(BaseEvent):
    """任意卡牌进场或出售时。"""

    event_name = Event.ANY_CARD_ENTERED_OR_SOLD

    def __init__(self, tarven, slot):
        super().__init__(tarven, slot)
        self.entered_or_sold_slot = slot


class GainDarknessEvent(BaseEvent):
    """获得黑暗值时。``slot`` 为获得黑暗值的槽位，``amount`` 为数量。

    黑暗值的累加由 :meth:`Tarven.gain_darkness` 负责，事件本身只用于触发效果。
    """

    event_name = Event.GAIN_DARKNESS

    def __init__(self, tarven, slot, amount=1):
        super().__init__(tarven, slot)
        self.gain_darkness_slot = slot
        self.amount = amount


# ---------------------------------------------------------------------------
# 其它
# ---------------------------------------------------------------------------
class LevelUpEvent(BaseEvent):
    """提升酒馆等级时。"""

    event_name = Event.LEVEL_UP

    def __init__(self, tarven, level_up_cost):
        super().__init__(tarven)
        self.level_up_cost = level_up_cost


class UpgradeEvent(BaseEvent):
    """卡牌被加升级时。"""

    event_name = Event.UPGRADE

    def __init__(self, tarven, slot, upgrade_name):
        super().__init__(tarven, slot)
        self.upgrade_slot = slot
        self.upgrade_name = upgrade_name


class RefreshEvent(BaseEvent):
    """刷新商店时。"""

    event_name = Event.REFRESH


class QuickProduceEvent(BaseEvent):
    """人族快速生产。"""

    event_name = Event.QUICK_PRODUCE

    def __init__(self, tarven, slot):
        super().__init__(tarven, slot)
        self.quick_produce_slot = slot


class AnyCardAddonChangedEvent(BaseEvent):
    """任意卡牌挂件变更时。``addon_changed_slot`` 为变更的槽位。"""

    event_name = Event.ANY_CARD_ADDON_CHANGED

    def __init__(self, tarven, slot):
        super().__init__(tarven, slot)
        self.addon_changed_slot = slot


class AnyTaskFinishedEvent(BaseEvent):
    """任意任务完成时。``finished_slot`` 为完成任务的槽位。"""

    event_name = Event.ANY_TASK_FINISHED

    def __init__(self, tarven, slot):
        super().__init__(tarven, slot)
        self.finished_slot = slot


# ---------------------------------------------------------------------------
# 虫族
# ---------------------------------------------------------------------------
class AnyCardLarvaEvent(BaseEvent):
    """任意卡牌注卵时。``larva_slot`` 为被注卵的虫卵槽位。"""

    event_name = Event.ANY_CARD_LARVA

    def __init__(self, tarven, slot):
        super().__init__(tarven, slot)
        self.larva_slot = slot


class AnyCardHatchEvent(BaseEvent):
    """任意卡牌孵化时。``hatch_slot`` 为孵化来源，``units`` 为孵化出的单位 dict。"""

    event_name = Event.ANY_CARD_HATCH

    def __init__(self, tarven, slot, units=None):
        super().__init__(tarven, slot)
        self.hatch_slot = slot
        self.units = units or {}


# ---------------------------------------------------------------------------
# 神族
# ---------------------------------------------------------------------------
class AnyCardTeleportEvent(BaseEvent):
    """任意卡牌折跃时。``source`` 为折跃来源槽位。"""

    event_name = Event.ANY_CARD_TELEPORT

    def __init__(self, tarven, source):
        super().__init__(tarven, source)
        self.source = source


class AnyCardGainVoidCrystalTowerEvent(BaseEvent):
    """任意卡牌获得虚空水晶塔时。``slot`` 为获得的槽位。"""

    event_name = Event.ANY_CARD_GAIN_VOID_CRYSTAL_TOWER

    def __init__(self, tarven, slot):
        super().__init__(tarven, slot)


# ---------------------------------------------------------------------------
# 辅助卡
# ---------------------------------------------------------------------------
class DeploymentEvent(BaseEvent):
    """辅助卡定点部署时。``deployment_slot`` 为部署目标。"""

    event_name = Event.DEPLOYMENT

    def __init__(self, tarven, slot):
        super().__init__(tarven, slot)
        self.deployment_slot = slot


class OtherPlayerSoldHeroCardEvent(BaseEvent):
    """任意其他玩家出局或出售具有英雄单位的卡牌时（跨玩家休眠事件）。

    ``source`` 为那位玩家被出售/出局的卡槽（携带英雄单位）。本模拟器只建模单人
    酒馆经济，玩家间的出局/战斗联动需上层驱动，默认不触发（见
    :meth:`Tarven.trigger_other_player_hero_card`）。
    """

    event_name = Event.OTHER_PLAYER_SOLD_HERO_CARD

    def __init__(self, tarven, source):
        super().__init__(tarven, source)
        self.source = source
