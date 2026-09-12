"""Declarative graph-only overrides for effects that cannot be safely inferred.

Keys use the same normalized description text as the execution registry.  The
small predicate layer keeps normal/gold values data-driven while avoiding any
reflection of executable closures.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .effect_ir import Action, Condition

Override = Callable[[Any, str, str], tuple[tuple[Condition, ...], tuple[Action, ...], str | None] | None]


def _void_energy(card: Any, text: str, variant: str):
    """Model the runtime's board-wide MAX void-pylon energy modifier."""
    if "所有虚空水晶塔提供" not in text or "能量强度" not in text:
        return None
    value = 3 if "提供3点" in text else 2 if "提供2点" in text else None
    if value is None:
        return None
    return (), (Action.make(
        "modify_global", target_scope="board", object="void_pylon_energy_value", quantity=value,
        details={"aggregation": "max", "attribute": "local_energy", "unit": "虚空水晶塔"},
    ),), "global_modifier"


def _artanis_gathering_bonus(card: Any, text: str, variant: str):
    """Declare the otherwise handler-less global extra gathering trigger."""
    if card.name != "阿塔尼斯" or "所有的集结卡牌每回合无条件额外集结一次" not in text:
        return None
    return (), (Action.make(
        "modify_global", target_scope="board", object="gathering_trigger_bonus", quantity=1,
        details={"aggregation": "presence", "mechanism": "gathering", "trigger": "round_end"},
    ),), "global_modifier"


def _purifier_gathering(card: Any, text: str, variant: str):
    if card.name != "净化者军团" or not text.removeprefix("唯一:").startswith("集结("):
        return None
    return (), (Action.make("move_units", target_scope="board", object="elite_protoss_units",
                            quantity="all", details={"exclude_self": "true", "target": "self"}),), "gathering"


def _power_station(card: Any, text: str, variant: str):
    if card.name != "发电站" or "水晶塔变为虚空水晶塔" not in text:
        return None
    quantity = 4 if "4" in text else 2
    return (), (Action.make("convert_units", target_scope="board", object="水晶塔", quantity=quantity,
                            random=True, consumes_input=True,
                            emits_event="any_card_gain_void_crystal_tower",
                            details={"new_unit": "虚空水晶塔"}),), "energy_supply"


def _void_fleet(card: Any, text: str, variant: str):
    if card.name != "虚空舰队" or "获得1虚空水晶塔" not in text:
        return None
    return (
        (Condition.make("value_compare", operator=">", value="void_ray_count > energy", scope="self"),),
        (Action.make("produce", object="虚空水晶塔", quantity=1,
                     emits_event="any_card_gain_void_crystal_tower"),),
        "energy_supply",
    )


def _darkness_share(card: Any, text: str, variant: str):
    if card.name != "死亡舰队" or "其他卡牌获得" not in text or "黑暗值" not in text:
        return None
    multiplier = 2 if "双倍黑暗值" in text else 1
    return (), (Action.make(
        "modify_attribute", target_scope="other_darkness_containers",
        object="darkness", quantity=f"event.amount*{multiplier}",
        emits_event="gain_darkness",
        details={
            "filter_tag": "具有黑暗容器", "exclude_self": "true",
            "aggregation": "per_gain_event", "multiplier": str(multiplier),
        },
    ),), "darkness_broadcast"


# Exact-text overrides can be registered here as data changes require them.
GRAPH_EFFECT_OVERRIDES: dict[str, Override] = {}
_PREDICATE_OVERRIDES: tuple[Override, ...] = (
    _void_energy, _artanis_gathering_bonus, _purifier_gathering, _power_station,
    _void_fleet, _darkness_share,
)


def resolve_override(card: Any, variant: str, normalized_text: str):
    """Return semantic facts for a controlled special effect, if one is known."""
    exact = GRAPH_EFFECT_OVERRIDES.get(normalized_text)
    if exact is not None:
        return exact(card, normalized_text, variant)
    for resolver in _PREDICATE_OVERRIDES:
        result = resolver(card, normalized_text, variant)
        if result is not None:
            return result
    return None


__all__ = ["GRAPH_EFFECT_OVERRIDES", "resolve_override"]
