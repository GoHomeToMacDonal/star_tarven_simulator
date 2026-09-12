from __future__ import annotations

import pytest

from star_tarven_simulator.loader import build_game, load_cards
from star_tarven_simulator.recipes.catalog import generate_recipe_catalog
from star_tarven_simulator.recipes.generator import validate_recipe
from star_tarven_simulator.recipes.graph import build_fact_graph
from star_tarven_simulator.recipes.strategy import derive_strategy_guides


@pytest.fixture(scope="module")
def strategy_state():
    cards, _ = load_cards()
    graph = build_fact_graph(cards, dataset_id="v20260826", strict=False).activate()
    catalog = generate_recipe_catalog(graph, max_recipes=100, limit_per_template=10_000)
    return cards, graph, catalog


def test_psi_and_darkness_semantics_are_typed(strategy_state):
    _, graph, _ = strategy_state

    construct = next(
        effect for effect in graph.effects_for("虚空构造体", "normal")
        if effect.normalized_text.startswith("灵能:")
    )
    assert construct.mechanism == "psi"
    assert any(condition.kind == "psi_below_max" for condition in construct.conditions)
    assert any(action.kind == "transform_units" for action in construct.actions)

    omen = next(
        effect for effect in graph.effects_for("黑暗预兆", "normal")
        if effect.mechanism == "race_diversity"
    )
    assert omen.extraction == "automatic"
    assert any(
        condition.kind == "race_diversity_threshold" and condition.value == 4
        for condition in omen.conditions
    )

    broadcast = next(
        effect for effect in graph.effects_for("死亡舰队", "normal")
        if effect.mechanism == "darkness_broadcast"
    )
    assert broadcast.extraction == "override"
    assert any(
        action.target_scope == "other_darkness_containers"
        and action.emits_event == "gain_darkness"
        for action in broadcast.actions
    )


def test_requested_psi_lineup_is_in_catalog(strategy_state):
    _, graph, catalog = strategy_state
    expected = ["步兵连队", "势不可挡", "黑暗预兆", "虚空构造体"]
    recipes = [recipe for recipe in catalog.recipes if recipe.template_id == "psi-ascension-engine"]
    match = next(
        recipe for recipe in recipes
        if [slot.card.card.name for slot in sorted(recipe.slots, key=lambda slot: slot.index)] == expected
    )

    assert validate_recipe(graph, match) == (True, ())
    assert dict(match.derived_metrics)["required_tower_payload"] == "10"
    assert [slot.index for slot in match.slots] == [0, 1, 2, 3]
    assert "round_end_order:psi_producers_before_board_elite" in match.satisfied_factors


def test_requested_darkness_carousel_is_in_catalog(strategy_state):
    _, graph, catalog = strategy_state
    recipes = [recipe for recipe in catalog.recipes if recipe.template_id == "darkness-carousel"]
    match = next(recipe for recipe in recipes if all(
        slot.card.card.name == "死亡舰队"
        for slot in recipe.slots if "darkness_broadcaster" in slot.roles
    ))

    assert validate_recipe(graph, match) == (True, ())
    assert dict(match.derived_metrics)["cycle_slot"] == "1"
    assert {slot.index for slot in match.slots if "darkness_broadcaster" in slot.roles} == {0, 2}
    assert 1 not in {slot.index for slot in match.slots}
    assert len([slot for slot in match.slots if "darkness_unit_listener" in slot.roles]) == 4

    guides = derive_strategy_guides(graph, catalog, limit=2, template_id="darkness-carousel")
    assert guides and "买入—出售循环位" in "".join(guides[0].priorities)


def test_psi_lineup_runtime_round_end_order(strategy_state):
    cards, _, _ = strategy_state
    card_map = {card.name: card for card in cards}
    tarven = build_game(cards).tarvens[0]
    for index, name in enumerate(("步兵连队", "势不可挡", "黑暗预兆", "虚空构造体")):
        tarven.card_engine.assign_card_to_slot(card_map[name], tarven.slots[index], origin=[])

    # 势不可挡初始有1塔；补至终局要求的10塔。
    tarven.slots[1].add_unit("水晶塔", 9)
    tarven.round_end()

    assert tarven.slots[0].count("幽灵") == 2  # 幽灵不在当前 ELITE_UNITS 映射中
    assert tarven.slots[0].count("陆战队员") == 0
    assert tarven.slots[0].count("陆战队员(精英)") == 6
    assert tarven.slots[0].count("劫掠者(精英)") == 5
    assert tarven.slots[1].count("执政官") == 0
    assert tarven.slots[1].count("执政官(精英)") >= 7
    assert tarven.slots[2].count("混合体巨兽") == 0  # 5星最高灵能锚点不触发自身灵能


def test_darkness_carousel_one_sale_runtime(strategy_state):
    cards, _, _ = strategy_state
    card_map = {card.name: card for card in cards}
    tarven = build_game(cards).tarvens[0]
    placements = {
        0: "死亡舰队", 1: "死神火车", 2: "死亡舰队",
        3: "不死队", 4: "不死队", 5: "不死队", 6: "不死队",
    }
    for index, name in placements.items():
        tarven.card_engine.assign_card_to_slot(card_map[name], tarven.slots[index], origin=[])

    tarven.trigger_selling(tarven.slots[1])

    assert tarven.slots[1].card_type is None
    assert tarven.slots[0].darkness == 1
    assert tarven.slots[2].darkness == 2  # 左舰队传播一次 + 右邻位直接获得一次
    assert [tarven.slots[index].darkness for index in (3, 4, 5, 6)] == [1, 1, 1, 1]
    assert tarven.slots[0].count("毁灭者") == 2
    assert tarven.slots[2].count("毁灭者") == 3
    assert [tarven.slots[index].count("不死队") for index in (3, 4, 5, 6)] == [5, 5, 5, 5]
