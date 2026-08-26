"""瓦斯升级定义、发现池、即时效果与等效战力估值。"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from importlib.resources import files
from typing import Iterable

from star_tarven_simulator.constants.unit_type import BIOLOGICAL_UNITS

PUBLIC_GROUP = "公共升级"
RACE_GROUPS = {
    "terran": ("人族升级",),
    "protoss": ("神族升级",),
    "zerg": ("虫族升级",),
}
PRIMAL_TAG = "属于原始虫群"
VOID_PROJECTION_TAG = "具有虚空投影"

# 可重复升级必须明确列入；其余只要描述声明“不可叠加/无法叠加”就不可重复。
STACKABLE_UPGRADES = frozenset({
    "内在潜力", "阳光滋润", "吸血", "顽强生命力", "电磁加速器", "灼热打击",
    "缩小光束", "强化药剂", "合金护甲", "火力压制", "玻璃大炮", "重型装甲",
    "聚能器", "虚空水晶", "吞噬", "深槽脊刺", "几丁质甲壳", "恶臭胆汁",
    "原始甲壳", "原始尖刺", "反甲",
})

# 乘法估值字段。utility 表示难以从单位静态数据精确推导的控制、射程、回复等启发式收益。
POWER_FACTORS: dict[str, dict[str, float]] = {
    "内在潜力": {"damage": 1.50, "survival": 0.85},
    "阳光滋润": {"survival": 1.25},
    "吸血": {"survival": 1.15},
    "顽强生命力": {"health": 1.20},
    "毒质变": {"damage": 1.15},
    "电磁加速器": {"attack_speed": 1.20},
    "灼热打击": {"damage": 1.20},
    "重力炸弹": {"utility": 1.08},
    "折光屏障": {"survival": 1.12},
    "缩小光束": {"utility": 1.10},
    "团结一致": {"survival": 1 / 0.70},
    "强化药剂": {"attack_speed": 1.25, "survival": 1.10},
    "合金护甲": {"health": 1.20, "survival": 1.08},
    "火力压制": {"attack_speed": 1.10, "utility": 1.08},
    "玻璃大炮": {"damage": 1.50, "health": 0.65},
    "重型装甲": {"health": 1.40, "utility": 0.92},
    "星空加速": {"tempo": 1.18},
    "聚能器": {"damage": 1.25},
    "虚空水晶": {"attack_speed": 1.20},
    "护盾充能": {"survival": 1.03},  # 额外按能量强度动态增加
    "吞噬": {"survival": 1.15},
    "狂暴": {"damage": 1.20, "utility": 1.05},
    "深槽脊刺": {"damage": 0.90, "utility": 1.15},
    "几丁质甲壳": {"survival": 1.18},
    "恶臭胆汁": {"damage": 1.12},
    "原始甲壳": {"health": 1.30},
    "原始尖刺": {"damage": 1.25, "utility": 1.04},
    "虚空能量": {"damage": 1.15},
    "反甲": {"damage": 1.15},
    "力大砖飞": {"utility": 1.08},
    "金光闪闪": {},  # 依场上金色卡数量动态计算
    "狙击镜": {"utility": 1.15},
    "重构之壳": {"survival": 1.20},
    "献祭": {"utility": 1.25},
    "轨道空降": {"utility": 1.08},
    "暗影战士": {"survival": 1.25, "utility": 1.08},
}


@lru_cache(maxsize=1)
def definitions() -> tuple[dict, ...]:
    path = files("star_tarven_simulator").joinpath("data/upgrades.json")
    return tuple(json.loads(path.read_text(encoding="utf-8")))


@lru_cache(maxsize=1)
def definition_map() -> dict[str, dict]:
    return {item["name"]: item for item in definitions()}


def is_stackable(name: str) -> bool:
    item = definition_map().get(name)
    if item is None:
        return True  # 兼容动态/历史升级，不擅自改变其语义。
    return name in STACKABLE_UPGRADES


def available_upgrades(slot) -> list[str]:
    """公共池 + 合法专属池；中立专属升级按机制标签而非种族开放。"""
    groups = {PUBLIC_GROUP}
    for race, race_groups in RACE_GROUPS.items():
        if slot.tags.has(race):
            groups.update(race_groups)
    if slot.tags.has(PRIMAL_TAG):
        groups.add("中立原始升级")
    if slot.tags.has(VOID_PROJECTION_TAG):
        groups.add("中立虚影升级")
    return [
        item["name"]
        for item in definitions()
        if item["group"] in groups and item["name"] not in slot.upgrades
    ]


def discover_upgrades(slot, count: int = 3) -> list[str]:
    candidates = available_upgrades(slot)
    if len(candidates) <= count:
        return candidates
    return slot.state.rng.sample(candidates, count)


def apply_instant_effect(slot, name: str) -> None:
    if name == "修理无人机":
        slot.add_unit("修理无人机", slot.state.level + 3)
    elif name == "折跃援军":
        slot.add_unit("高阶圣堂武士", 2)
        slot.add_unit("水晶塔", 3)
    elif name == "原始尖塔":
        slot.add_unit("原始异龙", 2)
        slot.add_unit("精华", 4)


def biological_units(slot) -> dict[str, int]:
    return {unit: count for unit, count in slot.units.items() if unit in BIOLOGICAL_UNITS}


def _dynamic_factor(slot, name: str) -> float:
    if name == "护盾充能":
        return 1.0 + 0.025 * slot.energy
    if name == "金光闪闪":
        gold = sum(
            1 for s in slot.all
            if s.tags.has("金色") and not s.tags.has("无法三连")
        )
        return 1.0 + 0.10 * gold  # 攻防同时成长，按统一战力倍率估值，避免平方重复计价。
    return math.prod(POWER_FACTORS.get(name, {}).values())


def equivalent_power(slot) -> float:
    """基础单位价值乘以所有持续升级的战斗倍率；一次性单位已计入基础价值。"""
    factor = 1.0
    for name in slot.upgrades:
        factor *= _dynamic_factor(slot, name)
    return slot.price() * factor


def equivalent_power_breakdown(slot) -> dict[str, object]:
    factors = [(name, _dynamic_factor(slot, name)) for name in slot.upgrades]
    multiplier = math.prod(value for _, value in factors)
    base = slot.price()
    return {
        "base_power": base,
        "multiplier": multiplier,
        "equivalent_power": base * multiplier,
        "upgrade_factors": factors,
    }
