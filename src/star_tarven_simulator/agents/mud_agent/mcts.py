"""关键分叉的蒙特卡洛前向采样评估器（全程 MC 版）。

按用户定稿：**全程开 MC（从 round 1）**，回报改为"三连 + 本级"（不再用战力），整局
限制到第 9 回合末。对每个候选卡牌：clone 局面 → 施加候选 → 用骨架把**整局打到 9 回合末**
→ 用局末标量（:func:`~star_tarven_simulator.agents.mud_agent.reward.terminal_score`）做回报；
每候选采样 ``N`` 次取均值后 ``argmax``。

**升本决策不在 MC 范围**：实证发现"现在升本 vs 暂缓"的 MC 对比有收敛缺陷（暂缓分支里
骨架自己会升，导致 MC 系统性选暂缓、反而拖垮升本）。因此升本由 :mod:`.policy` 的骨架规则
驱动，MC 只做选卡/发现。整局 rollout 内部也走同一套骨架规则，终局标量的 ``level_weight``
通过选卡间接引导升本友好的局面。
"""

from __future__ import annotations

from typing import List, Optional, TYPE_CHECKING

from star_tarven_simulator.agents.mud_agent.rollout import (
    clone_game,
    playout_terminal_score,
)
from star_tarven_simulator.simulator.action import (
    ChooseSynthesisAction,
    HeroChoiceAction,
)
from star_tarven_simulator.simulator.card import Card

if TYPE_CHECKING:
    from star_tarven_simulator.agents.mud_agent.policy import MudAgent


def _mean_terminal_score(agent: "MudAgent", apply_fn, *, samples: int) -> float:
    """对 ``apply_fn(clone_tarven)`` 后的局面整局 rollout，返回平均局末标量。

    ``apply_fn`` 接收克隆局受控 ``Tarven``，就地施加候选动作；返回 ``False`` 表示
    该候选在克隆上不合法（记为极低分）。
    """
    total = 0.0
    n = 0
    base_seed = agent.rng.randrange(1 << 30)
    base_triples = agent.trackers.triple_count
    for i in range(samples):
        clone = clone_game(agent.game)
        ct = clone.tarvens[agent.player_idx]
        if apply_fn(ct) is False:
            return float("-inf")
        total += playout_terminal_score(
            clone,
            agent.player_idx,
            base_triples=base_triples,
            weights=agent.weights,
            rng_seed=base_seed + i,
        )
        n += 1
    return total / n if n else float("-inf")


def evaluate_card_choices(agent: "MudAgent", cards: List[Card]) -> Optional[Card]:
    """选"整局打到 9 回合末后局末标量最高"的候选卡。禁用搜索时返回 None（用启发式）。"""
    if not agent.search_enabled:
        return None
    t = agent.t
    if not t.force_action:
        return None

    def make_apply(target_name: str):
        def apply(ct):
            if not ct.force_action:
                return False
            cfa = ct.force_action[0]
            options = list(getattr(cfa, "options", []) or getattr(cfa, "cards", []))
            match = next(
                (o for o in options if isinstance(o, Card) and o.name == target_name),
                None,
            )
            if match is None:
                return False
            if isinstance(cfa, (HeroChoiceAction, ChooseSynthesisAction)):
                cfa.selected = match
            else:  # ChooseCardAction
                cfa.selected_card = match
            return ct.action(cfa)

        return apply

    best_card, best_val = None, float("-inf")
    seen = set()
    for c in cards:
        if c.name in seen:
            continue
        seen.add(c.name)
        val = _mean_terminal_score(agent, make_apply(c.name), samples=agent.samples)
        if val > best_val:
            best_card, best_val = c, val
    return best_card
