"""拓展包（source）管理：按卡牌 ``source`` 字段过滤卡池。

设计约定
--------
* **核心种族**（:data:`CORE_SOURCES`）与**基础内容**（:data:`BASE_EXTRA_SOURCES`：辅助卡 / 特殊）
  始终启用；无 ``source`` 标注的卡牌视为基础内容一并启用。这些不是"拓展包"，无法单独关闭。
* **拓展包**（:data:`EXPANSION_PACKS`，共 8 个）为可选内容，默认全部关闭，一局至多开启
  :data:`MAX_EXPANSIONS` (= 2) 个。
* **独占拓展包**（:data:`EXCLUSIVE_PACKS`：时不我待 / 中世纪集市）互斥：一旦选中其中之一，
  就不能再搭配任何其它拓展包（该局只能开这一个）。

对外主要接口
------------
* :func:`validate_selection` —— 校验并归一化一组拓展包选择。
* :func:`random_expansions` —— 随机挑选一组合法拓展包（遵守独占规则）。
* :func:`enabled_sources` —— 给定拓展包选择，算出启用的 ``source`` 集合。
* :func:`filter_cards` —— 按拓展包选择过滤卡牌列表（供卡池 / 对局使用）。
"""

from __future__ import annotations

import itertools
import random as _random
from typing import Iterable, List, Optional, Sequence, Set

# 核心种族来源：始终启用
CORE_SOURCES = ("核心人族", "核心神族", "核心虫族", "核心中立")

# 非拓展包的基础内容来源：辅助卡（定点部署所需）与特殊卡，随核心一并常驻启用
BASE_EXTRA_SOURCES = ("辅助卡", "特殊")

# 可选拓展包（默认关闭）
EXPANSION_PACKS = (
    "作战计划",
    "时不我待",
    "重装上阵",
    "穷兵黩武",
    "一念之差",
    "身经百战",
    "比特狂潮",
    "中世纪集市",
)

# 独占拓展包：选中其一后不能再选其它任何拓展包
EXCLUSIVE_PACKS = ("时不我待", "中世纪集市")

# 一局最多可开启的拓展包数量
MAX_EXPANSIONS = 2

_EXPANSION_SET = frozenset(EXPANSION_PACKS)
_EXCLUSIVE_SET = frozenset(EXCLUSIVE_PACKS)


def _selection_ok(packs: Sequence[str]) -> bool:
    """判断一组（已知均为合法拓展包名、无重复的）选择是否满足数量 / 独占约束。"""
    if len(packs) > MAX_EXPANSIONS:
        return False
    has_exclusive = any(p in _EXCLUSIVE_SET for p in packs)
    if has_exclusive and len(packs) > 1:
        return False
    return True


def validate_selection(expansions: Optional[Iterable[str]]) -> List[str]:
    """校验并归一化拓展包选择，返回去重后的列表（保持输入顺序）。

    规则：
      * 每个名称必须属于 :data:`EXPANSION_PACKS`；
      * 去重后数量不得超过 :data:`MAX_EXPANSIONS`；
      * 若含独占拓展包（:data:`EXCLUSIVE_PACKS`），则必须是唯一选择。

    非法选择抛出 :class:`ValueError`。``None`` / 空 视为"只开核心"。
    """
    if expansions is None:
        return []

    seen: List[str] = []
    for pack in expansions:
        if pack not in _EXPANSION_SET:
            raise ValueError(
                f"未知拓展包 {pack!r}，可选：{', '.join(EXPANSION_PACKS)}"
            )
        if pack not in seen:
            seen.append(pack)

    if len(seen) > MAX_EXPANSIONS:
        raise ValueError(
            f"一局最多开启 {MAX_EXPANSIONS} 个拓展包，收到 {len(seen)} 个：{seen}"
        )

    exclusive = [p for p in seen if p in _EXCLUSIVE_SET]
    if exclusive and len(seen) > 1:
        raise ValueError(
            f"独占拓展包 {exclusive} 不能与其它拓展包同时开启：{seen}"
        )

    return seen


def all_valid_selections(
    min_size: int = 1, max_size: int = MAX_EXPANSIONS
) -> List[List[str]]:
    """枚举所有满足约束的拓展包组合（大小落在 ``[min_size, max_size]`` 内）。"""
    combos: List[List[str]] = []
    for size in range(max(0, min_size), max_size + 1):
        for combo in itertools.combinations(EXPANSION_PACKS, size):
            if _selection_ok(combo):
                combos.append(list(combo))
    return combos


def random_expansions(
    count: Optional[int] = None,
    rng: Optional[_random.Random] = None,
) -> List[str]:
    """随机挑选一组合法拓展包，遵守数量与独占约束。

    :param count: 期望的拓展包数量（1 或 2）。为 ``None`` 时在所有合法的
        1~:data:`MAX_EXPANSIONS` 个组合中等概率随机挑选。
    :param rng: 可选的随机源（便于测试复现）。
    """
    picker = rng or _random
    if count is None:
        combos = all_valid_selections(1, MAX_EXPANSIONS)
    else:
        if count < 0 or count > MAX_EXPANSIONS:
            raise ValueError(
                f"count 必须在 0..{MAX_EXPANSIONS} 之间，收到 {count}"
            )
        if count == 0:
            return []
        combos = all_valid_selections(count, count)
    return list(picker.choice(combos))


def enabled_sources(
    expansions: Optional[Iterable[str]] = None,
    *,
    include_base_extra: bool = True,
) -> Set[str]:
    """给定拓展包选择，返回启用的 ``source`` 集合（含核心与基础内容）。"""
    packs = validate_selection(expansions)
    sources: Set[str] = set(CORE_SOURCES)
    if include_base_extra:
        sources |= set(BASE_EXTRA_SOURCES)
    sources |= set(packs)
    return sources


def is_card_enabled(source: Optional[Iterable[str]], enabled: Set[str]) -> bool:
    """判断某卡牌（其 ``source`` 列表）在给定启用集合下是否可用。

    无来源标注（空 ``source``）的卡牌视为基础内容，始终可用。
    """
    src = list(source) if source else []
    if not src:
        return True
    return bool(set(src) & enabled)


def filter_cards(
    cards: Iterable,
    expansions: Optional[Iterable[str]] = None,
    *,
    include_base_extra: bool = True,
) -> List:
    """按拓展包选择过滤卡牌列表。

    仅保留 ``source`` 与启用集合有交集的卡牌；核心种族、基础内容（辅助卡 / 特殊）、
    以及无来源标注的卡牌默认保留。非法的拓展包选择会抛 :class:`ValueError`。
    """
    enabled = enabled_sources(expansions, include_base_extra=include_base_extra)
    return [c for c in cards if is_card_enabled(getattr(c, "source", None), enabled)]
