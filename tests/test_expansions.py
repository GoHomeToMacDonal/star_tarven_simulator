"""拓展包（source）选择与卡池过滤测试。

覆盖：
1. 常量与数据一致性（拓展包名均出现在卡牌 source 中）。
2. validate_selection 的合法 / 非法路径（数量上限、独占规则、未知包）。
3. random_expansions 始终返回合法选择，且独占包只会单独出现。
4. filter_cards / build_game 的过滤语义（默认仅核心+基础内容，开包后并入）。
"""

from __future__ import annotations

import random

import pytest

from star_tarven_simulator import expansions as exp
from star_tarven_simulator.loader import build_game, load_cards


@pytest.fixture(scope="module")
def loaded():
    cards, _ = load_cards()
    return cards


def test_expansion_constants_present_in_data(loaded):
    """数据里出现的每个拓展包，都应在 EXPANSION_PACKS 常量里登记。"""
    data_sources = set()
    for card in loaded:
        data_sources.update(card.source)
    known = (
        set(exp.CORE_SOURCES)
        | set(exp.BASE_EXTRA_SOURCES)
        | set(exp.EXPANSION_PACKS)
        # 提取器派生的「不进卡池」标签与其它来源并存，只影响能否被抽到
        | {exp.NO_POOL_SOURCE}
    )
    # 数据中的每个来源都应被常量覆盖（否则说明有新来源未登记）
    assert data_sources <= known, f"未登记的来源: {data_sources - known}"
    # 8 个拓展包都真实存在于数据中
    for pack in exp.EXPANSION_PACKS:
        assert any(pack in c.source for c in loaded), f"{pack} 在数据中缺失"


def test_validate_empty_and_none():
    assert exp.validate_selection(None) == []
    assert exp.validate_selection([]) == []


def test_validate_dedup_preserves_order():
    assert exp.validate_selection(["作战计划", "作战计划", "比特狂潮"]) == [
        "作战计划",
        "比特狂潮",
    ]


def test_validate_single_and_pair_of_normal_packs():
    assert exp.validate_selection(["作战计划"]) == ["作战计划"]
    assert exp.validate_selection(["作战计划", "比特狂潮"]) == ["作战计划", "比特狂潮"]


def test_validate_rejects_unknown_pack():
    with pytest.raises(ValueError):
        exp.validate_selection(["不存在的包"])
    # 核心种族不是拓展包，不能作为选择传入
    with pytest.raises(ValueError):
        exp.validate_selection(["核心人族"])


def test_validate_rejects_too_many():
    with pytest.raises(ValueError):
        exp.validate_selection(["作战计划", "比特狂潮", "重装上阵"])


def test_exclusive_pack_alone_is_ok():
    assert exp.validate_selection(["时不我待"]) == ["时不我待"]
    assert exp.validate_selection(["中世纪集市"]) == ["中世纪集市"]


def test_exclusive_pack_cannot_combine():
    # 独占包 + 普通包 非法
    with pytest.raises(ValueError):
        exp.validate_selection(["时不我待", "作战计划"])
    # 两个独占包同时选 非法
    with pytest.raises(ValueError):
        exp.validate_selection(["时不我待", "中世纪集市"])


def test_all_valid_selections_obey_rules():
    combos = exp.all_valid_selections(1, exp.MAX_EXPANSIONS)
    assert combos, "应存在合法组合"
    for combo in combos:
        assert 1 <= len(combo) <= exp.MAX_EXPANSIONS
        # validate 不应抛错
        assert exp.validate_selection(combo) == list(combo)
        exclusive = [p for p in combo if p in exp.EXCLUSIVE_PACKS]
        if exclusive:
            assert len(combo) == 1
    # 独占包 + 普通包 的组合不应出现
    normal = set(exp.EXPANSION_PACKS) - set(exp.EXCLUSIVE_PACKS)
    assert ["时不我待", next(iter(normal))] not in combos


def test_random_expansions_always_valid():
    rng = random.Random(1234)
    for _ in range(300):
        pick = exp.random_expansions(rng=rng)
        assert 1 <= len(pick) <= exp.MAX_EXPANSIONS
        # 独占规则：若含独占包则必须单独出现
        if any(p in exp.EXCLUSIVE_PACKS for p in pick):
            assert len(pick) == 1
        # 始终合法
        assert exp.validate_selection(pick) == pick


def test_random_expansions_count():
    rng = random.Random(7)
    assert exp.random_expansions(count=0, rng=rng) == []
    for _ in range(50):
        one = exp.random_expansions(count=1, rng=rng)
        assert len(one) == 1
        two = exp.random_expansions(count=2, rng=rng)
        assert len(two) == 2
        # 两个包时不可能出现独占包
        assert not (set(two) & set(exp.EXCLUSIVE_PACKS))


def test_random_expansions_can_produce_exclusive():
    rng = random.Random(0)
    seen_exclusive = False
    for _ in range(500):
        pick = exp.random_expansions(rng=rng)
        if len(pick) == 1 and pick[0] in exp.EXCLUSIVE_PACKS:
            seen_exclusive = True
            break
    assert seen_exclusive, "随机挑选应能产出独占包"


def test_enabled_sources_default_is_core_plus_base():
    sources = exp.enabled_sources(None)
    assert set(exp.CORE_SOURCES) <= sources
    assert set(exp.BASE_EXTRA_SOURCES) <= sources
    # 默认不含任何拓展包
    assert not (sources & set(exp.EXPANSION_PACKS))


def test_enabled_sources_includes_selected_pack():
    sources = exp.enabled_sources(["作战计划"])
    assert "作战计划" in sources
    assert "比特狂潮" not in sources


def test_is_card_enabled_empty_source_always_kept():
    enabled = exp.enabled_sources(None)
    assert exp.is_card_enabled([], enabled) is True
    assert exp.is_card_enabled(None, enabled) is True


def test_filter_default_excludes_expansions(loaded):
    filtered = exp.filter_cards(loaded, None)
    filtered_names = {c.name for c in filtered}
    # 任何纯拓展包卡牌都应被排除
    for card in loaded:
        only_expansion = card.source and set(card.source) <= set(exp.EXPANSION_PACKS)
        if only_expansion:
            assert card.name not in filtered_names, card.name
    # 核心 / 辅助卡 / 特殊 / 无来源 的卡牌应保留
    assert len(filtered) < len(loaded)


def test_filter_includes_selected_expansion(loaded):
    base = {c.name for c in exp.filter_cards(loaded, None)}
    with_pack = {c.name for c in exp.filter_cards(loaded, ["时不我待"])}
    added = with_pack - base
    assert added, "开启拓展包后应新增卡牌"
    for name in added:
        card = next(c for c in loaded if c.name == name)
        assert "时不我待" in card.source


def test_filter_core_card_with_expansion_source_kept_by_default(loaded):
    """既属于核心又属于拓展包的卡牌（如 核心人族+重装上阵），默认应保留。"""
    dual = [
        c
        for c in loaded
        if (set(c.source) & set(exp.CORE_SOURCES))
        and (set(c.source) & set(exp.EXPANSION_PACKS))
    ]
    if not dual:
        pytest.skip("数据中无 核心+拓展 双来源卡牌")
    base_names = {c.name for c in exp.filter_cards(loaded, None)}
    for card in dual:
        assert card.name in base_names, card.name


def test_build_game_records_enabled_expansions(loaded):
    game = build_game(loaded, user_count=1, expansions=["作战计划"])
    assert game.enabled_expansions == ["作战计划"]
    # 卡池仅含启用来源的卡牌
    enabled = exp.enabled_sources(["作战计划"])
    for card in game.pool.cards:
        assert exp.is_card_enabled(card.source, enabled)


def test_build_game_default_pool_smaller_than_full(loaded):
    default_game = build_game(loaded, user_count=1)
    assert default_game.enabled_expansions == []
    assert len(default_game.pool.cards) < len(loaded)


def test_build_game_random_pick(loaded):
    game = build_game(loaded, user_count=1, random_pick=True)
    assert exp.validate_selection(game.enabled_expansions) == game.enabled_expansions
    assert 1 <= len(game.enabled_expansions) <= exp.MAX_EXPANSIONS


def test_build_game_rejects_invalid_selection(loaded):
    with pytest.raises(ValueError):
        build_game(loaded, user_count=1, expansions=["时不我待", "作战计划"])


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
