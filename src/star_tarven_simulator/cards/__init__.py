"""卡牌效果解析入口。

对外暴露 :func:`resolve`（描述文本 -> handler）与 :func:`is_passive`（被动 tag 声明判定）。
解析优先级：注册表（不规则效果）> 参数化解析器（规整机制族）。
"""

from __future__ import annotations

from typing import Callable, List, Optional, Tuple, Union

from star_tarven_simulator.constants.card_tag import CARD_TAGS
from star_tarven_simulator.cards.parametric import resolve_parametric
from star_tarven_simulator.cards.registry import ACTION_HANDLERS

# 导入以填充注册表（不规则效果的 override）
from star_tarven_simulator.cards import overrides as _overrides  # noqa: F401

Resolved = Optional[Tuple[Union[str, List[str]], Callable]]

# 被动声明（不生成 handler，只影响 tags）的正文片段
_PASSIVE_KEYWORDS = (
    "具有黑暗容器",
    "能够定点部署",
    "具有虚空投影",
    "属于原始虫群",
    "属于UED",
    "无法三连",
    "你的折跃效果总是添加到这张牌上",
    "作为极低概率出现的卡牌",
    "只用于沙盒模式测试",
    "本体的生命值只有1点",
)

# 风味 / 战斗阶段文本：本模拟器只建模酒馆经济，不建模战斗，故这些不生成 handler。
_FLAVOR_MARKERS = (
    "寿仙杯",
    "冠军:",
    "感谢",
    "纪念",
    "蒸蒸日上",
    "努力付出",
    "战斗中",
    "战斗阶段",
    "战斗失败时",
    "向对方阵地",
    "发射一枚核弹",
    "释放聚变打击",
    "空降到敌人后方",
    "冲锋",
    "三连后还原",
    "出售免费且不触发任何效果",
)


def is_passive(text: str) -> bool:
    """判断某条归一化描述是否只是被动 tag 声明 / 风味文本（不应生成 handler）。"""
    if text in CARD_TAGS:
        return True
    for marker in _FLAVOR_MARKERS:
        if marker in text:
            return True
    for kw in _PASSIVE_KEYWORDS:
        if kw in text and len(text) <= len(kw) + 4:
            return True
    return False


def resolve(text: str, colors=None) -> Resolved:
    """把一条归一化描述解析为 ``(event_name, handler)``；无法解析返回 ``None``。"""
    if text in ACTION_HANDLERS:
        return ACTION_HANDLERS[text]
    return resolve_parametric(text, colors or [])


__all__ = ["resolve", "is_passive", "ACTION_HANDLERS"]
