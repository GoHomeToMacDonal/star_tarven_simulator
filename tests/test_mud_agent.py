"""泥巴流 Agent 冒烟测试。

多数用例跑**骨架**（``search_enabled=False``）以保证速度；另有一个小采样蒙特卡洛用例
验证搜索路径能跑通。断言聚焦"跑得完、能凑三连、能上本、回报可复现"，不锁定具体数值
（卡池随机，具体走向依种子而变）。
"""

from __future__ import annotations

import random

import pytest

from star_tarven_simulator.agents.mud_agent.policy import MudAgent, play_game
from star_tarven_simulator.agents.mud_agent.reward import FINAL_ROUND, RewardWeights
from star_tarven_simulator.agents.mud_agent.rollout import (
    clone_game,
    playout_terminal_score,
)
from star_tarven_simulator.loader import build_game, load_cards

HEROES = ["陆战队员", "工蜂", "副官"]


@pytest.fixture(scope="module")
def cards():
    loaded, _ = load_cards()
    return loaded


def _play(cards, hero, seed, *, search=False, samples=8):
    game = build_game(cards, user_count=1, heroes=[hero], rng=random.Random(seed))
    trackers = play_game(
        game, 0, search_enabled=search, samples=samples, final_round=FINAL_ROUND
    )
    return game, trackers


@pytest.mark.parametrize("hero", HEROES)
def test_skeleton_runs_to_end(cards, hero):
    """骨架能跑完到局末（第 9 回合），且资源/等级处于合法范围。"""
    game, trackers = _play(cards, hero, seed=0)
    t = game.tarvens[0]
    assert t.round == FINAL_ROUND
    assert 1 <= t.level <= 6
    assert t.mineral >= 0 and t.gas >= 0
    assert trackers.triple_count >= 0
    assert not t.force_action  # 结束时不应残留未解决的强制动作


@pytest.mark.parametrize("hero", HEROES)
def test_skeleton_makes_progress(cards, hero):
    """多种子平均下，骨架应能凑出三连并升到 ≥3 本。"""
    triples, levels = [], []
    for seed in range(6):
        game, trackers = _play(cards, hero, seed=seed)
        triples.append(trackers.triple_count)
        levels.append(game.tarvens[0].level)
    assert max(levels) >= 3, f"{hero} 应至少有一局升到 3 本: {levels}"
    assert sum(triples) >= 3, f"{hero} 六局累计三连过少: {triples}"


def test_reward_reproducible(cards):
    """相同种子两次运行，回报统计完全一致（确定性可复现）。"""
    _, t1 = _play(cards, "陆战队员", seed=3)
    _, t2 = _play(cards, "陆战队员", seed=3)
    assert t1.triple_count == t2.triple_count
    assert t1.round_reached_l5 == t2.round_reached_l5


def test_clone_is_independent(cards):
    """clone 后修改克隆不影响原局。"""
    game = build_game(cards, user_count=1, heroes=["陆战队员"], rng=random.Random(0))
    game.round_start()
    t = game.tarvens[0]
    before = t.mineral
    clone = clone_game(game)
    clone.tarvens[0].mineral = 999
    clone.round_start()
    assert t.mineral == before
    assert t.round == 1


def test_playout_returns_terminal_score(cards):
    """整局 rollout 返回一个有限的局末标量（三连+本级+对子进度加权）。"""
    game = build_game(cards, user_count=1, heroes=["陆战队员"], rng=random.Random(1))
    game.round_start()
    score = playout_terminal_score(
        clone_game(game), 0, base_triples=0, weights=RewardWeights(), rng_seed=42
    )
    # 至少含 level_weight * level（level>=1），故为正且有限。
    assert score > 0.0
    import math

    assert math.isfinite(score)


def test_search_path_runs(cards):
    """小采样蒙特卡洛路径能跑完一局（验证搜索接线正确，不锁数值）。"""
    game, trackers = _play(cards, "陆战队员", seed=0, search=True, samples=4)
    t = game.tarvens[0]
    assert t.round == FINAL_ROUND
    assert 1 <= t.level <= 6
    assert trackers.triple_count >= 0


def test_no_leftover_force_action_each_hero(cards):
    """三个英雄跑完后都不应残留强制动作（说明所有分支都被正确解决）。"""
    for hero in HEROES:
        game, _ = _play(cards, hero, seed=1)
        assert not game.tarvens[0].force_action, f"{hero} 结束残留强制动作"
