"""泥巴流回报函数（全程 MC 版）。

引擎是单人经济沙盒（无真实战斗/血量）。按用户定稿，蒙特卡洛**不再用战力**做回报，
而是直接优化"三连"与"升本"两个泥巴流核心目标：

* 整局**限制到最多 9 回合结束**（``FINAL_ROUND = 9``）。
* **全程开 MC（从 round 1）**：不再有"三连前只启发式"的阶段分界；每个关键分叉
  （发现选牌 / 买牌 / 是否升本）都可以 clone 局面 → 施加候选 → 用骨架把**整局剩余回合
  打到 9 回合末** → 用局末标量做回报，取样均值后 ``argmax``。
* **局末标量**（:func:`terminal_score`）::

      score = w_triple * 累计三连数
            + w_level  * 局末本级
            + w_pair   * 局末"在建对子进度"（弱 shaping，避免同名信息在均值里被抹平）

  在建对子进度 = Σ_name min(2, 场上+暂存可三连同名张数)/2，鼓励把同名卡攒到 3 张的路上。

``RewardWeights`` 现同时服务于 MC 局末标量与 CLI 汇总统计口径。
"""

from __future__ import annotations

from dataclasses import dataclass

# 整局最多推进到第 9 回合结束（用户要求：泥巴流理论 8-9 回合应已 5 本 3 三连）。
FINAL_ROUND = 9

# 达成 5 本回合的哨兵：终局仍未到 5 本。
NEVER = FINAL_ROUND + 1


@dataclass(frozen=True)
class RewardWeights:
    """MC 局末标量与骨架排序共用的权重。

    默认权重的量纲设计：一个三连(150) > 升一本(40) ≫ 一个满进度在建对子(15)，
    使 MC 首选"多做三连"，其次"连着升本"，最后才在无三连收益时用在建对子破均值平局。
    """

    # 每个累计三连的权重（MC 局末标量的主项）。
    triple_weight: float = 150.0
    # 每一本级的权重。经验扫描（15 局/英雄）显示 40→120 对工蜂/副官达成 5 本率有 +6% 的
    # Pareto 提升（三连数不变），对陆战队员三连损失很小，故默认取 120。
    level_weight: float = 120.0
    # 在建对子进度的弱 shaping 权重（0~7 张对子，进度 0~3.5）。
    pair_progress_weight: float = 15.0
    # 三连前用于骨架早期升本偏好的小项（保留旧字段名，供 policy 引用）。
    early_level_bonus: float = 1.0


def _pair_progress(tarven) -> float:
    """局末"在建对子进度"：Σ_name min(2, 可三连同名张数)/2。

    统计场上非金、非'无法三连'的同名卡张数（暂存区同名也计入），每种名字进度上限 1.0
    （2 张即视为满进度，第 3 张会直接三连、进度归零并计入 triple）。
    """
    from collections import Counter

    from star_tarven_simulator.agents.mud_agent import heuristics as H

    counts: Counter = Counter()
    for s in tarven.slots:
        if s.card_type is None:
            continue
        if s.tags.has("金色") or s.tags.has("无法三连"):
            continue
        counts[s.card_type] += 1
    for item in tarven.cache:
        name = H.card_name(item)
        if name is not None:
            counts[name] += 1
    return sum(min(2, c) / 2.0 for c in counts.values())


def terminal_score(tarven, triple_count: int, weights: RewardWeights) -> float:
    """局末（第 9 回合结束）标量，MC 用它比较候选动作。"""
    return (
        weights.triple_weight * float(triple_count)
        + weights.level_weight * float(tarven.level)
        + weights.pair_progress_weight * _pair_progress(tarven)
    )
