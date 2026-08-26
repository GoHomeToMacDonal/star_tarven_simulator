"""前向 rollout：clone 一局，用骨架把整局打到第 9 回合末，返回局末标量。

按用户定稿，MC 回报改为"三连 + 本级"而非战力，且整局限制到 9 回合结束。因此 rollout
从当前局面出发，用骨架（``search_enabled=False``）把**剩余所有回合**打到 ``FINAL_ROUND``
末，期间用一份**独立的 Trackers** 统计 rollout 内新产生的三连，最后调用
:func:`terminal_score`。

clone 用 :func:`copy.deepcopy`（已验证引擎可安全深拷贝，且克隆与原局完全独立）。
"""

from __future__ import annotations

import copy

from star_tarven_simulator.agents.mud_agent.reward import (
    FINAL_ROUND,
    RewardWeights,
    terminal_score,
)
from star_tarven_simulator.agents.mud_agent.state import Trackers
from star_tarven_simulator.simulator.game import Game


def clone_game(game: Game) -> Game:
    """深拷贝整局（含 RNG 状态），返回与原局完全独立的克隆。"""
    return copy.deepcopy(game)


def playout_terminal_score(
    game: Game,
    player_idx: int,
    *,
    base_triples: int,
    weights: RewardWeights,
    rng_seed: int | None = None,
    final_round: int = FINAL_ROUND,
) -> float:
    """在（已 clone 的）局面上用骨架把整局打到 ``final_round`` 末，返回局末标量。

    :param game: **已经 clone 的** 局面（会就地推进，勿传真实主局）。
    :param player_idx: 受控玩家索引。
    :param base_triples: 当前主局已累计的三连数（rollout 在其之上继续累加）。
    :param weights: 局末标量权重。
    :param rng_seed: 覆盖克隆局 RNG，让不同采样去相关。
    :param final_round: 打到第几回合末结束（默认 9）。
    """
    from star_tarven_simulator.agents.mud_agent.policy import MudAgent, _run_round

    t = game.tarvens[player_idx]
    if rng_seed is not None:
        t.pool.rng.seed(rng_seed)

    # rollout 用独立 Trackers，起点计入主局已有三连，便于局末标量口径一致。
    trackers = Trackers(triple_count=base_triples)
    agent = MudAgent(
        game,
        player_idx,
        trackers=trackers,
        search_enabled=False,  # rollout 内部只用骨架，避免递归 MC
        weights=weights,
    )

    # 1) 先把"当前回合"的剩余动作打完（当前回合已 round_start 过，勿重复开新回合）。
    _run_round(agent)
    # 2) 继续推进后续回合到 final_round 末。
    while t.round < final_round:
        game.round_end()
        if t.round >= final_round:
            break
        game.round_start()
        agent.new_round()
        _run_round(agent)
    game.round_end()

    return terminal_score(t, trackers.triple_count, weights)
