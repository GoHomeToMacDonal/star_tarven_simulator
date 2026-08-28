"""固定英雄系统契约测试。

测试按 ``docs/hero-implementation-plan.md`` 的 Task 1–15 组织。英雄文案不是
运行时数据源；这里显式列出计划内支持、暂缓和仅可通过变身进入的英雄，避免测试
反过来依赖 ``data/heros.json``。
"""

from __future__ import annotations

import random

import pytest

from star_tarven_simulator.loader import build_game, load_cards
from star_tarven_simulator.simulator.action import (
    Action,
    BuyAction,
    CacheEnterAction,
    ChooseSynthesisAction,
    HeroChoiceAction,
    HeroPowerAction,
    LockAction,
    RefreshAction,
    SynthesisAction,
    UpgradeAction,
    UpgradeTarvenAction,
)
from star_tarven_simulator.simulator.card import Card, CardPool
from star_tarven_simulator.simulator.card_engine import CardEngine
from star_tarven_simulator.simulator.game import Game
from star_tarven_simulator.simulator.event import (
    AnyCardEnteredEvent,
    EnteringEvent,
    RefreshEvent,
    RoundEndEvent,
    RoundStartEvent,
)
from star_tarven_simulator.simulator.event_handler import EventHandler
from star_tarven_simulator.simulator import hero as hero_module
from star_tarven_simulator.simulator.slot import Slot


# Task 2–14 中明确列为支持的全部 47 名可初选英雄。
HEROES_BY_TASK = {
    2: ("陆战队员",),
    3: ("工蜂", "副官", "矿骡", "米拉", "德拉肯钻机"),
    4: ("追猎者", "使徒", "阿尔达瑞斯", "飞蛇"),
    5: ("收割者", "解放者（防卫模式）", "眼虫"),
    6: ("航母", "诺娃", "异龙", "响尾蛇", "休伯利安点唱机", "大力神", "飓风"),
    7: ("阿巴瑟", "雷诺", "雷神", "机械哨兵", "德哈卡", "干扰者"),
    8: ("感染虫", "SCV", "探机", "斯旺", "蒙斯克", "医疗兵", "爆虫"),
    9: ("执政官", "阿塔尼斯", "母舰核心"),
    10: ("分裂池", "混合体", "进化腔", "扎加拉"),
    11: ("星港", "凯瑞甘"),
    12: ("汉森博士",),
    13: ("科学球", "火蝠", "战列巡航舰"),
    14: ("坑道虫",),
}
SUPPORTED_HEROES = tuple(name for names in HEROES_BY_TASK.values() for name in names)
DEFERRED_HEROES = ("泰凯斯", "界徐盛", "埃蒙", "亚顿之矛", "纳鲁德博士")
TRANSFORM_ONLY_HEROES = (
    "凯瑞甘（异虫形态）",
    "汉森博士（异虫形态）",
    "解放者（战机模式）",
)


@pytest.fixture(scope="module")
def cards():
    loaded, _ = load_cards()
    return loaded


def _game(cards, hero: str = "default", *, user_count: int = 1):
    return build_game(cards, user_count=user_count, heroes=[hero] * user_count)


def _tarven(cards, hero: str = "default"):
    return _game(cards, hero).tarvens[0]


def _hero_name(tarven) -> str:
    """HeroController 对外暴露当前固定英雄名。"""
    return tarven.hero_controller.hero_name


def _place(tarven, card: Card, idx: int) -> Slot:
    tarven.slots[idx] = Slot(idx, tarven)
    tarven.card_engine.assign_card_to_slot(card, tarven.slots[idx])
    return tarven.slots[idx]


def _pool_size(pool: CardPool) -> int:
    return pool.total_size()


def _public_inventory_size(tarven) -> int:
    visible = [*tarven.shop, *tarven.cache]
    visible += [card for delayed in tarven.delay_enter_card.values() for card in delayed]
    visible += [slot.source_card for slot in tarven.slots]
    return _pool_size(tarven.pool) + sum(
        isinstance(card, Card) and not card.derived for card in visible
    )


def _cache_has(tarven, card: Card) -> bool:
    return any(item is card or item == card.name for item in tarven.cache)


# ---------------------------------------------------------------------------
# Task 1：固定注册表、默认分配、人数和唯一性
# ---------------------------------------------------------------------------


def test_build_game_defaults_every_player_to_default(cards):
    game = build_game(cards, user_count=3)
    assert [_hero_name(tarven) for tarven in game.tarvens] == ["default"] * 3
    assert len({id(tarven.hero_controller) for tarven in game.tarvens}) == 3


def test_build_game_accepts_multiple_unique_heroes(cards):
    heroes = ["工蜂", "陆战队员", "凯瑞甘"]
    game = build_game(cards, user_count=len(heroes), heroes=heroes)
    assert [_hero_name(tarven) for tarven in game.tarvens] == heroes


def test_default_hero_is_the_only_repeatable_hero(cards):
    game = build_game(cards, user_count=2, heroes=["default", "default"])
    assert [_hero_name(tarven) for tarven in game.tarvens] == ["default", "default"]


@pytest.mark.parametrize(
    "heroes",
    [
        pytest.param(["工蜂"], id="hero-count-must-match-player-count"),
        pytest.param(["工蜂", "工蜂"], id="non-default-hero-must-be-unique"),
        pytest.param(["不存在的英雄", "default"], id="unknown-hero"),
    ],
)
def test_build_game_rejects_invalid_hero_assignment(cards, heroes):
    with pytest.raises(ValueError):
        build_game(cards, user_count=2, heroes=heroes)


@pytest.mark.parametrize("hero", DEFERRED_HEROES)
def test_deferred_heroes_cannot_be_selected(cards, hero):
    with pytest.raises(ValueError, match=hero):
        build_game(cards, user_count=1, heroes=[hero])


@pytest.mark.parametrize("hero", TRANSFORM_ONLY_HEROES)
def test_transform_forms_cannot_be_selected_initially(cards, hero):
    with pytest.raises(ValueError, match=hero):
        build_game(cards, user_count=1, heroes=[hero])


# ---------------------------------------------------------------------------
# Task 2：统一动作、可注入 RNG、发现归池和暂存区溢出
# ---------------------------------------------------------------------------


def test_hero_actions_are_public_actions():
    assert issubclass(HeroPowerAction, Action)
    assert issubclass(HeroChoiceAction, Action)


def test_card_pool_rng_is_reproducible(cards):
    first = CardPool(cards, rng=random.Random(2025))
    second = CardPool(cards, rng=random.Random(2025))
    assert [card.uuid for card in first.draw(20, 6)] == [
        card.uuid for card in second.draw(20, 6)
    ]


def test_marine_discovery_returns_unchosen_candidates_to_pool(cards):
    tarven = _tarven(cards, "陆战队员")
    tarven.level = 3
    tarven.mineral = 2
    before = _pool_size(tarven.pool)

    assert tarven.action(HeroPowerAction())
    assert tarven.mineral == 0
    assert len(tarven.force_action) == 1
    choice = tarven.force_action[0]
    assert isinstance(choice, HeroChoiceAction)
    assert len(choice.options) == 3
    assert all(card.level == 2 for card in choice.options)
    assert _pool_size(tarven.pool) == before - 3

    selected = choice.options[0]
    choice.selected = selected
    assert tarven.action(choice)
    assert _cache_has(tarven, selected)
    assert _pool_size(tarven.pool) == before - 1


def test_discovery_overflow_enters_leftmost_empty_slot(cards):
    tarven = _tarven(cards, "陆战队员")
    tarven.level = 2
    tarven.mineral = 2
    tarven.cache[:] = ["占位卡"] * len(tarven.cache)
    before = _pool_size(tarven.pool)

    assert tarven.action(HeroPowerAction())
    choice = tarven.force_action[0]
    selected = choice.options[0]
    choice.selected = selected
    assert tarven.action(choice)
    assert tarven.slots[0].source_card is selected
    assert _pool_size(tarven.pool) == before - 1


# ---------------------------------------------------------------------------
# Task 3–13：注册表和生命周期总冒烟；关键英雄另作行为断言
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("task", "hero"),
    [(task, hero) for task, heroes in HEROES_BY_TASK.items() for hero in heroes],
)
def test_every_supported_hero_registers_and_survives_lifecycle(cards, task, hero):
    """Task 15：每名计划内支持英雄至少经过构建和一轮生命周期。"""
    assert 2 <= task <= 14
    tarven = _tarven(cards, hero)
    assert _hero_name(tarven) == hero
    tarven.round_start()
    tarven.round_end()


def test_drone_odd_and_even_round_income_hooks(cards):
    """Task 3：回合开始钩子在基础收入结算后追加工蜂收入。"""
    tarven = _tarven(cards, "工蜂")
    tarven.round_start()
    assert (tarven.round, tarven.mineral, tarven.gas) == (1, 3, 2)
    tarven.round_end()
    tarven.round_start()
    assert (tarven.round, tarven.mineral, tarven.gas) == (2, 5, 3)


def test_kerrigan_level_up_and_refresh_hooks_grant_one_free_refresh(cards):
    """Task 3/4/11：升级和刷新钩子通过正常玩家动作串联。"""
    tarven = _tarven(cards, "凯瑞甘")
    tarven.mineral = tarven.level_up_cost
    assert tarven.action(UpgradeTarvenAction())
    assert tarven.free_refresh == 1

    mineral_before = tarven.mineral
    assert tarven.action(RefreshAction())
    assert tarven.mineral == mineral_before
    assert tarven.free_refresh == 0


def test_mira_entering_and_level_up_hooks_reset_recorded_level(cards):
    """Task 3：进场递增星级加矿，升级后公开行为可从 1 星重新计数。"""
    tarven = _tarven(cards, "米拉")
    by_level = {
        level: next(card for card in cards if card.level == level)
        for level in (1, 2)
    }

    mineral_before = tarven.mineral
    tarven.trigger_entering(_place(tarven, by_level[2], 0))
    assert tarven.mineral == mineral_before

    tarven.mineral = tarven.level_up_cost
    assert tarven.action(UpgradeTarvenAction())
    mineral_before = tarven.mineral
    tarven.trigger_entering(_place(tarven, by_level[1], 1))
    assert tarven.mineral == mineral_before + 1


def test_eye_cannot_buy_before_round_four(cards):
    """Task 5：眼虫前三回合禁购，第 4 回合公开状态已升到 3 级并强制选择种族。"""
    tarven = _tarven(cards, "眼虫")
    for _ in range(3):
        tarven.round_start()
        tarven.mineral = 99
        assert not tarven.action(BuyAction(shop_idx=0))
        tarven.round_end()

    tarven.round_start()
    assert tarven.level == 3
    assert len(tarven.shop) == 4
    assert len(tarven.force_action) == 1
    choice = tarven.force_action[0]
    assert isinstance(choice, HeroChoiceAction)
    assert choice.kind == "eye-race"
    race = choice.options[0]
    choice.selected = race
    assert tarven.action(choice)
    rewards = [item for item in tarven.cache if isinstance(item, Card)]
    assert len(rewards) == 2
    assert rewards[0].uuid == rewards[1].uuid
    assert rewards[0].level == 3
    assert rewards[0].race == race


def test_liberator_switches_mode_at_most_once_each_round(cards):
    """Task 5/7：解放者是固定双模式切换，不生成第二个控制器。"""
    tarven = _tarven(cards, "解放者（防卫模式）")
    controller_id = id(tarven.hero_controller)

    assert tarven.action(HeroPowerAction())
    assert _hero_name(tarven) == "解放者（战机模式）"
    assert not tarven.action(HeroPowerAction())

    tarven.round_start()
    assert tarven.action(HeroPowerAction())
    assert _hero_name(tarven) == "解放者（防卫模式）"
    assert id(tarven.hero_controller) == controller_id


def test_kerrigan_fifth_purchase_transforms_and_permanently_disables_gas(cards):
    """Task 11：第五次真实购买后立即单向变身。"""
    tarven = _tarven(cards, "凯瑞甘")
    tarven.mineral = 100
    for purchase_number in range(1, 6):
        tarven.shop[0] = tarven.pool.draw(1, 1)[0]
        assert tarven.action(BuyAction(shop_idx=0))
        expected = "凯瑞甘" if purchase_number < 5 else "凯瑞甘（异虫形态）"
        assert _hero_name(tarven) == expected

    assert tarven.gas == 0
    assert tarven.gas_max == 0
    tarven.round_start()
    assert (tarven.gas, tarven.gas_max) == (0, 0)


# ---------------------------------------------------------------------------
# Task 14：统一额外相邻关系与战力
# ---------------------------------------------------------------------------


def test_nydus_extra_neighbor_is_bidirectional_and_keeps_physical_neighbors(cards):
    tarven = _tarven(cards, "坑道虫")
    card = next(card for card in cards if card.name == "好兄弟")
    remote = _place(tarven, card, 0)
    physical = _place(tarven, card, 1)
    anchor = _place(tarven, card, 3)  # 文案中的 4 号位

    assert tarven.action(HeroPowerAction(slot_idx=0))
    assert physical in remote.neighbors
    assert anchor in remote.neighbors
    assert remote in anchor.neighbors


@pytest.mark.parametrize("forbidden_slot", [2, 3, 4])
def test_nydus_rejects_three_middle_positions(cards, forbidden_slot):
    tarven = _tarven(cards, "坑道虫")
    assert not tarven.action(HeroPowerAction(slot_idx=forbidden_slot))


def test_disruptor_counts_non_derived_cache_card_static_power(cards):
    tarven = _tarven(cards, "干扰者")
    card = next(card for card in cards if card.level > 0 and card.price > 0)
    tarven.cache[0] = card
    assert tarven.total_power() == pytest.approx(card.price)


# ---------------------------------------------------------------------------
# Task 15：集合完整性回归
# ---------------------------------------------------------------------------


def test_fixed_hero_sets_are_complete_and_disjoint():
    assert len(SUPPORTED_HEROES) == 47
    assert len(set(SUPPORTED_HEROES)) == len(SUPPORTED_HEROES)
    assert set(SUPPORTED_HEROES).isdisjoint(DEFERRED_HEROES)
    assert set(SUPPORTED_HEROES).isdisjoint(TRANSFORM_ONLY_HEROES)
    assert set(DEFERRED_HEROES).isdisjoint(TRANSFORM_ONLY_HEROES)



# ---------------------------------------------------------------------------
# 额外回归：审计中发现的卡池、实例 handler 与位置一致性边界
# ---------------------------------------------------------------------------


def test_eye_full_cache_keeps_pool_conserved_on_round_four(cards):
    tarven = _tarven(cards, "眼虫")
    for _ in range(3):
        tarven.round_start()
        tarven.round_end()
    tarven.cache[:] = ["占位卡"] * len(tarven.cache)
    before = _public_inventory_size(tarven)
    tarven.round_start()
    choice = tarven.force_action[0]
    choice.selected = choice.options[0]
    assert not tarven.action(choice)
    assert choice not in tarven.force_action
    assert _public_inventory_size(tarven) == before


def test_hercules_swap_preserves_slot_index_invariant(cards):
    tarven = _tarven(cards, "大力神")
    for choice in list(tarven.force_action):
        choice.selected = choice.options[0]
        assert tarven.action(choice)
    card = next(card for card in cards if card.name == "好兄弟")
    source = _place(tarven, card, 0)
    target = _place(tarven, card, 2)

    assert tarven.action(HeroPowerAction(slot_idx=0, target_idx=2))
    assert tarven.slots[0] is target
    assert tarven.slots[2] is source
    assert all(slot.index == index for index, slot in enumerate(tarven.slots))


def test_make_gold_preserves_instance_added_handler(cards):
    tarven = _tarven(cards, "雷诺")
    card = next(card for card in cards if card.name == "好兄弟")
    slot = _place(tarven, card, 0)
    marker = EventHandler(tarven, slot, "实例部署效果", lambda _s, _e: None, "round_end")
    slot.event_handlers.append(marker)

    assert tarven.action(HeroPowerAction(slot_idx=0))
    assert any(handler.description == "实例部署效果" for handler in slot.event_handlers)


def test_zagara_destroys_two_distinct_cards_when_values_tie(cards):
    tarven = _tarven(cards, "扎加拉")
    card = next(card for card in cards if card.name == "好兄弟")
    for index in range(6):
        _place(tarven, card, index)

    tarven.round_start()
    assert sum(slot.card_type is not None for slot in tarven.slots) == 4
    assert tarven.mineral == tarven.mineral_max + 11



# ---------------------------------------------------------------------------
# 固定英雄修复回归：单位分类、事务、动态定义与逐英雄核心行为
# ---------------------------------------------------------------------------


def _blank_card(
    name: str,
    *,
    level: int = 1,
    race: str = "neutral",
    units: dict[str, int] | None = None,
    handlers: list[EventHandler] | None = None,
) -> Card:
    return Card(
        uuid=-(abs(hash((name, level, race))) % 1_000_000 + 1),
        name=name,
        level=level,
        race=race,
        description=[],
        gold_description=[],
        units=dict(units or {}),
        tags=[race],
        gold_tags=[race, "金色"],
        event_handlers=list(handlers or []),
        gold_event_handlers=list(handlers or []),
        derived=True,
    )


def _choose_first(tarven, *, kind: str | None = None):
    choice = next(
        action
        for action in tarven.force_action
        if isinstance(action, HeroChoiceAction) and (kind is None or action.kind == kind)
    )
    choice.selected = choice.options[0]
    assert tarven.action(choice)
    return choice.selected


def test_fixed_unit_classifications_cover_basics_and_exclude_hero_mechs():
    assert {"陆战队员", "狂热者"} <= set(hero_module.BIOLOGICAL_UNITS)
    assert "攻城坦克" in hero_module.MECHANICAL_UNITS
    assert "仲裁者" in hero_module.MECHANICAL_UNITS
    assert "仲裁者" in hero_module.HERO_UNITS
    assert {"陆战队员", "狂热者", "攻城坦克"} <= set(hero_module.GROUND_UNITS)
    assert "维京战机" in hero_module.AIR_UNITS
    assert set(hero_module.GROUND_UNITS).isdisjoint(hero_module.AIR_UNITS)


def test_infestor_egg_hatches_only_nonhero_mechanical_units(cards):
    tarven = _tarven(cards, "感染虫")
    left = _place(tarven, _blank_card("左虫", race="zerg"), 0)
    egg = tarven.slots[1]
    tarven.card_engine.assign_card_to_slot("虫卵", egg)
    right = _place(tarven, _blank_card("右虫", race="zerg"), 2)
    for unit in ("攻城坦克", "陆战队员", "狂热者", "仲裁者"):
        egg.add_unit(unit, 1)

    egg.trigger([RoundStartEvent(tarven)])
    for target in (left, right):
        assert target.count("攻城坦克") == 1
        assert target.count("陆战队员") == 1
        assert target.count("狂热者") == 1
        assert target.count("仲裁者") == 0


def test_swann_recycles_only_mechanical_units(cards):
    tarven = _tarven(cards, "斯旺")
    slot = _place(
        tarven,
        _blank_card(
            "混编部队",
            race="terran",
            units={"攻城坦克": 2, "陆战队员": 3, "狂热者": 1, "仲裁者": 1},
        ),
        0,
    )
    assert tarven.action(HeroPowerAction(slot_idx=0))
    assert slot.count("攻城坦克") == 0
    assert slot.count("陆战队员") == 3
    assert slot.count("狂热者") == 1
    assert slot.count("仲裁者") == 0
    factory = next(candidate for candidate in tarven.slots if candidate.card_type == "机械工厂")
    assert factory.count("零件") == 3
    assert factory.level == 0 and factory.tags.has("neutral")


def test_medic_distributes_only_biological_units(cards):
    tarven = _tarven(cards, "医疗兵")
    source = _place(
        tarven,
        _blank_card(
            "伤员",
            units={"陆战队员": 2, "狂热者": 2, "攻城坦克": 1, "仲裁者": 1},
        ),
        0,
    )
    first = _place(tarven, _blank_card("接收一"), 1)
    second = _place(tarven, _blank_card("接收二"), 2)
    assert tarven.action(HeroPowerAction(slot_idx=0))
    assert source.units == {}
    assert first.count("陆战队员") + second.count("陆战队员") == 2
    assert first.count("狂热者") + second.count("狂热者") == 2
    assert first.count("攻城坦克") + second.count("攻城坦克") == 0
    assert first.count("仲裁者") + second.count("仲裁者") == 0


def test_carrier_intercepts_cache_and_direct_slot_purchases_then_enters(cards):
    tarven = _tarven(cards, "航母")
    assert len(tarven.shop) == 4
    tarven.mineral = 20
    first, second = tarven.pool.draw(2, 3)
    tarven.shop[0] = first
    tarven.shop[1] = second

    assert tarven.action(BuyAction(shop_idx=0))
    assert tarven.action(BuyAction(shop_idx=1, slot_idx=0))
    assert not any(item is first or item is second for item in tarven.cache)
    assert all(slot.card_type is None for slot in tarven.slots)
    assert tarven.delay_enter_card[1] == [first]
    assert tarven.delay_enter_card[2] == [second]

    tarven.round_start()
    assert tarven.slots[0].source_card is first
    tarven.mineral = 10
    third = tarven.shop[0]
    assert tarven.action(BuyAction(shop_idx=0))
    tarven.round_end()
    tarven.round_start()
    assert any(slot.source_card is second for slot in tarven.slots)
    assert any(slot.source_card is third for slot in tarven.slots)
    combo = next(action for action in tarven.force_action if action.kind == "carrier-combo")
    assert len(combo.options) == 2
    assert all(card.level == third.level and card.race == second.race for card in combo.options)


def test_carrier_full_board_and_cache_discards_delayed_cards(cards):
    tarven = _tarven(cards, "航母")
    tarven.mineral = 20
    drawn = tarven.pool.draw(20, 2)
    first = drawn.pop(0)
    second_index = next(
        index for index, card in enumerate(drawn) if card.uuid != first.uuid
    )
    second = drawn.pop(second_index)
    tarven.pool.place_back(drawn)
    tarven.shop[:2] = [first, second]
    assert tarven.action(BuyAction(shop_idx=0))
    assert tarven.action(BuyAction(shop_idx=1, slot_idx=0))
    filler = _blank_card("占位")
    for index in range(7):
        _place(tarven, filler, index)
    tarven.cache[:] = ["占位卡"] * len(tarven.cache)

    def inventory_count(card):
        visible = [*tarven.shop, *tarven.cache]
        visible += [item for delayed in tarven.delay_enter_card.values() for item in delayed]
        visible += [slot.source_card for slot in tarven.slots]
        return tarven.pool.count(card) + sum(
            isinstance(item, Card) and item.uuid == card.uuid for item in visible
        )

    first_before = inventory_count(first)
    second_before = inventory_count(second)
    tarven.round_start()
    assert inventory_count(first) == first_before - 1
    tarven.round_end()
    tarven.round_start()
    assert inventory_count(second) == second_before - 1
    assert not tarven.hero_controller.state.get("carrier_entries")
    assert all(slot.source_card is not first and slot.source_card is not second for slot in tarven.slots)
    assert not any(item is first or item is second for item in tarven.cache)


def test_hansen_field_study_grants_stetmann_next_round_with_overflow(cards):
    tarven = _tarven(cards, "汉森博士")
    action = HeroChoiceAction(options=["战地勘察"], selected="战地勘察", kind="hansen-study", pool_owned=False)
    tarven.force_action.append(action)
    assert tarven.action(action)
    tarven.cache[:] = ["占位卡"] * len(tarven.cache)
    tarven.round_start()
    assert tarven.slots[0].card_type == "斯台特曼"


def test_hansen_sample_log_reduces_locked_upgrade_cost(cards):
    tarven = _tarven(cards, "汉森博士")
    tarven.hero_controller.state["studies"] = ["样本日志"]
    tarven.level_up_cost = 8
    tarven.lock = True
    tarven.round_end()
    assert tarven.level_up_cost in {6, 7}


def test_hansen_serum_transforms_clears_studies_and_keeps_zerg_upgrade_refund(cards):
    tarven = _tarven(cards, "汉森博士")
    _place(tarven, _blank_card("全虫场", race="zerg"), 0)
    tarven.hero_controller.state["studies"] = ["感染研究"]
    action = HeroChoiceAction(options=["新式血清"], selected="新式血清", kind="hansen-study", pool_owned=False)
    tarven.force_action.append(action)
    assert tarven.action(action)
    assert _hero_name(tarven) == "汉森博士（异虫形态）"
    assert tarven.hero_controller.state["studies"] == []

    tarven.gas = 2
    assert tarven.action(UpgradeAction(slot_idx=0))
    upgrade = tarven.force_action[0]
    assert set(upgrade.upgrade_names) == set(hero_module.ZERG_RESEARCH_UPGRADES)
    upgrade.selected_upgrade_name = upgrade.upgrade_names[0]
    assert tarven.action(upgrade)
    assert tarven.gas == 1


def test_artanis_tenth_entry_fuses_full_dynamic_definition(cards):
    tarven = _tarven(cards, "阿塔尼斯")
    filler = next(card for card in cards if card.name == "好兄弟")
    for _ in range(9):
        temp = Slot(6, tarven)
        tarven.card_engine.assign_card_to_slot(filler, temp)
        tarven.trigger_entering(temp)
    target = _place(tarven, filler, 0)
    target.upgrades.append("聚能器")
    marker = EventHandler(tarven, target, "保留处理器", lambda _s, _e: None, "round_end")
    target.event_handlers.append(marker)
    before_units = dict(target.units)

    tarven.trigger_entering(target)
    assert target.derived and target.tags.has("无法融合")
    assert target.source_card is None
    assert target.card_type == "好兄弟+阿塔尼斯"
    assert target.count("阿塔尼斯") == 1
    assert all(target.count(unit) >= count for unit, count in before_units.items())
    assert "聚能器" in target.upgrades
    assert any(handler.description == "保留处理器" for handler in target.event_handlers)


def test_mothership_core_second_synthesis_renames_only(cards):
    tarven = _tarven(cards, "母舰核心")
    core_card = next(card for card in cards if card.name == "母舰核心")
    core = _place(tarven, core_card, 0)
    tarven.hero_controller.state["core_ref"] = core
    core.upgrades.append("聚能器")
    core.event_handlers.append(EventHandler(tarven, core, "核心实例效果", lambda _s, _e: None, "round_end"))
    before_units = dict(core.units)

    tarven.hero_controller.on_synthesis(core)
    tarven.hero_controller.on_synthesis(core)
    assert core.card_type == "母舰"
    assert core.source_card is core_card
    assert not core.derived
    assert core.count("虚空辉光舰(精英)") == tarven.level * 2
    assert all(core.count(unit) == count for unit, count in before_units.items())
    assert "聚能器" in core.upgrades
    assert any(handler.description == "核心实例效果" for handler in core.event_handlers)


def test_jukebox_takes_exact_named_pool_copy_and_rejects_forged_cards(cards):
    tarven = _tarven(cards, "休伯利安点唱机")
    tarven.level = 3
    target = next(card for card in tarven.pool.cards if card.level == 2 and card.uuid not in tarven.pool.no_draw_uuids)
    forged = _blank_card(target.name, level=target.level, race=target.race)
    before = _pool_size(tarven.pool)
    assert not tarven.action(HeroPowerAction(card=forged))
    assert _pool_size(tarven.pool) == before
    assert tarven.action(HeroPowerAction(card=target.name))
    assert _pool_size(tarven.pool) == before
    assert any(isinstance(item, Card) and item.name == target.name for item in tarven.cache)


def test_jukebox_full_cache_enters_without_consuming_pool(cards):
    tarven = _tarven(cards, "休伯利安点唱机")
    tarven.level = 3
    tarven.cache[:] = ["占位卡"] * len(tarven.cache)
    target = next(card for card in tarven.pool.cards if card.level == 2 and card.uuid not in tarven.pool.no_draw_uuids)
    before = _pool_size(tarven.pool)
    assert tarven.action(HeroPowerAction(card=target.name))
    assert tarven.slots[0].card_type == target.name
    assert _pool_size(tarven.pool) == before
    assert tarven.hero_controller.state["uses"] == 1


def test_viper_only_copies_current_opponents_highest_level_without_pool_cost(cards):
    game = build_game(cards, user_count=2, heroes=["飞蛇", "default"])
    viper, opponent = game.tarvens
    low = next(card for card in cards if card.level == 2)
    high = next(card for card in cards if card.level == 5)
    _place(opponent, low, 0)
    _place(opponent, high, 1)
    game.set_current_opponent(0, 1)
    before = _pool_size(viper.pool)

    assert not viper.action(HeroPowerAction(card=_blank_card("伪造", level=6)))
    assert viper.action(HeroPowerAction())
    copied = next(item for item in viper.cache if isinstance(item, Card))
    assert copied.name == high.name and copied.level == 5 and copied.derived
    assert _pool_size(viper.pool) == before


@pytest.mark.parametrize("hero", ["机械哨兵", "阿巴瑟", "感染虫", "执政官"])
def test_each_round_active_heroes_have_success_only_cooldown(cards, hero):
    tarven = _tarven(cards, hero)
    tarven.mineral = 20
    if hero == "机械哨兵":
        slot = _place(tarven, next(card for card in cards if card.level < 5), 0)
        action = HeroPowerAction(slot_idx=0)
    elif hero == "阿巴瑟":
        slot = _place(tarven, next(card for card in cards if card.level == 1), 0)
        action = HeroPowerAction(slot_idx=0)
    elif hero == "感染虫":
        slot = _place(tarven, next(card for card in cards if card.race == "terran"), 0)
        action = HeroPowerAction(slot_idx=0)
    else:
        left_card = next(card for card in cards if card.race == "terran")
        right_card = next(card for card in cards if card.race == "protoss")
        left = _place(tarven, left_card, 0)
        right = _place(tarven, right_card, 1)
        left.tags.add("金色")
        right.tags.add("金色")
        action = HeroPowerAction(slot_idx=0)
    assert tarven.action(action)
    assert not tarven.action(action)


def test_failed_each_round_power_does_not_consume_cooldown(cards):
    tarven = _tarven(cards, "感染虫")
    invalid = _place(tarven, next(card for card in cards if card.race != "terran"), 0)
    assert not tarven.action(HeroPowerAction(slot_idx=0))
    valid = _place(tarven, next(card for card in cards if card.race == "terran"), 0)
    assert tarven.action(HeroPowerAction(slot_idx=0))
    assert valid.temporary_description


def test_abathur_validates_discovery_before_destroying_or_paying(cards):
    tarven = _tarven(cards, "阿巴瑟")
    slot = _place(tarven, next(card for card in cards if card.level == 1), 0)
    tarven.mineral = 2
    saved = tarven.pool.bucket_uuids(2)
    tarven.pool.clear_bucket(2)
    assert not tarven.action(HeroPowerAction(slot_idx=0))
    assert tarven.slots[0] is slot and slot.card_type is not None
    assert tarven.mineral == 2
    tarven.pool.set_bucket(2, saved)


def test_starport_transforms_ground_but_never_existing_air(cards):
    tarven = _tarven(cards, "星港")
    tarven.level = 4
    slot = _place(
        tarven,
        _blank_card(
            "海陆混编",
            units={"陆战队员": 1, "狂热者": 1, "攻城坦克": 1, "维京战机": 1},
        ),
        0,
    )
    tarven.hero_controller.state["air_mode"] = "怨灵战机"
    tarven.hero_controller.round_start_before_cards()
    assert slot.count("维京战机") == 1
    assert slot.count("怨灵战机") == 3
    assert slot.count("陆战队员") == slot.count("狂热者") == slot.count("攻城坦克") == 0


def test_eye_race_choice_rolls_back_exact_pair_when_only_one_cache_space(cards):
    tarven = _tarven(cards, "眼虫")
    for _ in range(4):
        tarven.round_start()
        if tarven.round < 4:
            tarven.round_end()
    tarven.cache[:] = ["占位卡"] * (len(tarven.cache) - 1) + [None]
    choice = tarven.force_action[0]
    before = _pool_size(tarven.pool)
    choice.selected = choice.options[0]
    assert not tarven.action(choice)
    assert _pool_size(tarven.pool) == before
    assert choice not in tarven.force_action


def test_lieutenant_and_kerrigan_free_refreshes_do_not_accumulate(cards):
    lieutenant = _tarven(cards, "副官")
    lieutenant.round_start()
    lieutenant.round_end()
    lieutenant.round_start()
    assert lieutenant.free_refresh == 1

    kerrigan = _tarven(cards, "凯瑞甘")
    kerrigan.mineral = kerrigan.level_up_cost
    assert kerrigan.action(UpgradeTarvenAction())
    assert kerrigan.free_refresh == 1
    kerrigan.round_start()
    assert kerrigan.free_refresh == 0


def test_viper_purchase_discount_resets_each_round(cards):
    tarven = _tarven(cards, "飞蛇")
    tarven.round_start()
    first = tarven.shop[0]
    assert tarven.card_price(0) == 2
    tarven.mineral = 10
    assert tarven.action(BuyAction(shop_idx=0))
    tarven.shop[0] = next(card for card in cards if card.name != first.name)
    assert tarven.card_price(0) == 3
    tarven.round_end()
    tarven.round_start()
    tarven.shop[0] = next(card for card in cards if card.name not in tarven.hero_controller.state["bought_names"])
    assert tarven.card_price(0) == 2


def test_aldaris_lock_discount_begins_next_round_only(cards):
    tarven = _tarven(cards, "阿尔达瑞斯")
    tarven.round_start()
    card = tarven.shop[0]
    assert tarven.action(LockAction())
    assert tarven.card_price(0) == 3
    tarven.round_end()
    tarven.round_start()
    assert tarven.shop[0] is card
    assert tarven.card_price(0) == 2


def test_firebat_discovers_sequentially_without_temporarily_exhausting_pool(cards):
    tarven = _tarven(cards, "火蝠")
    _place(tarven, _blank_card("高战力", units={"菲尼克斯": 5}), 0)
    candidate = next(card for card in cards if card.level == 5 and card.uuid not in tarven.pool.no_draw_uuids)
    tarven.pool.set_bucket(5, [candidate.uuid] * 3)

    assert tarven.action(HeroPowerAction())
    assert tarven.hero_controller.state["uses"] == 1
    assert len(tarven.force_action) == 1

    first = tarven.force_action[0]
    assert first.kind == "firebat" and len(first.options) == 3
    first.selected = first.options[0]
    assert tarven.action(first)
    assert len(tarven.force_action) == 1

    second = tarven.force_action[0]
    assert second.kind == "firebat" and len(second.options) == 2
    second.selected = second.options[0]
    assert tarven.action(second)
    assert tarven.force_action == []
    assert sum(item is candidate for item in tarven.cache) == 2


def test_firebat_full_cache_stops_remaining_chain_and_returns_candidates(cards):
    tarven = _tarven(cards, "火蝠")
    _place(tarven, _blank_card("高战力", units={"菲尼克斯": 5}), 0)
    candidate = next(card for card in cards if card.level == 5 and card.uuid not in tarven.pool.no_draw_uuids)
    tarven.pool.set_bucket(5, [candidate.uuid] * 3)
    tarven.cache[:-1] = ["占位卡"] * (len(tarven.cache) - 1)

    assert tarven.action(HeroPowerAction())
    first = tarven.force_action[0]
    first.selected = first.options[0]
    assert tarven.action(first)
    assert tarven.cache[-1] is candidate

    second = tarven.force_action[0]
    second.selected = second.options[0]
    assert tarven.action(second)
    assert tarven.force_action == []
    assert any(slot.source_card is candidate for slot in tarven.slots)


def test_force_action_queue_is_strict_fifo(cards):
    tarven = _tarven(cards)
    first = HeroChoiceAction(options=["先"], selected="先", kind="evolution", pool_owned=False)
    second = HeroChoiceAction(options=["后"], selected="后", kind="evolution", pool_owned=False)
    tarven.force_action.extend([first, second])

    assert not tarven.action(second)
    assert tarven.force_action == [first, second]
    assert tarven.action(first)
    assert tarven.force_action == [second]
    assert tarven.action(second)
    assert tarven.force_action == []


def test_pool_owned_unknown_choice_failure_returns_exact_candidates(cards):
    tarven = _tarven(cards)
    options = tarven.pool.draw(3, 2)
    before = _pool_size(tarven.pool)
    action = HeroChoiceAction(options=options, selected=options[0], kind="unknown-pool-choice", pool_owned=True)
    tarven.force_action.append(action)
    assert not tarven.action(action)
    assert _pool_size(tarven.pool) == before + 3
    assert action not in tarven.force_action


def test_hurricane_synthesis_reward_is_same_race(cards):
    """飓风直接限制标准三连奖励的种族，不再额外生成第二次发现。"""
    tarven = _tarven(cards, "飓风")
    card = next(card for card in cards if card.level == 1 and card.race == "terran")
    _place(tarven, card, 0)
    _place(tarven, card, 1)
    tarven.cache[0] = card
    assert tarven.action(SynthesisAction(cache_idx=0))

    synthesis = tarven.force_action[0]
    reward_cards = [option for option in synthesis.options if isinstance(option, Card)]
    assert len(reward_cards) == 3
    assert {option.level for option in reward_cards} == {2}
    assert len({option.name for option in reward_cards}) == 3
    assert all(option.race == "terran" for option in reward_cards)

    synthesis.selected = reward_cards[0]
    assert tarven.action(synthesis)
    assert not any(isinstance(action, HeroChoiceAction) for action in tarven.force_action)


def test_hercules_upgrade_to_three_and_five_costs_one_more(cards):
    tarven = _tarven(cards, "大力神")
    while tarven.force_action:
        _choose_first(tarven)
    tarven.level = 2
    tarven.level_up_cost = 7
    tarven.mineral = 7
    assert not tarven.action(UpgradeTarvenAction())
    tarven.mineral = 8
    assert tarven.action(UpgradeTarvenAction())
    tarven.level = 4
    tarven.level_up_cost = 9
    tarven.mineral = 9
    assert not tarven.action(UpgradeTarvenAction())
    tarven.mineral = 10
    assert tarven.action(UpgradeTarvenAction())


def test_probe_adds_overload_and_void_towers_even_without_price_entry(cards):
    tarven = _tarven(cards, "探机")
    slot = _place(tarven, next(card for card in cards if card.level == 1), 0)
    tarven.mineral = 1
    assert tarven.action(HeroPowerAction(slot_idx=0))
    assert slot.count("过载水晶塔") == 1
    assert slot.count("虚空水晶塔") == 2


def test_infestor_destroys_target_overload_tower(cards):
    tarven = _tarven(cards, "感染虫")
    slot = _place(tarven, next(card for card in cards if card.race == "terran"), 0)
    slot.add_unit("过载水晶塔", 2)
    assert tarven.action(HeroPowerAction(slot_idx=0))
    assert slot.count("过载水晶塔") == 0


# The remaining smoke-only heroes each get one direct core-behavior assertion.
def test_mule_and_drill_core_economy(cards):
    mule = _tarven(cards, "矿骡")
    mule.mineral_max = 5
    assert mule.action(HeroPowerAction())
    assert (mule.mineral, mule.mineral_max) == (5, 5)
    mule.round_start()
    assert (mule.mineral, mule.mineral_max) == (2, 3)
    assert not mule.action(HeroPowerAction())
    mule.round_end()
    mule.round_start()
    assert mule.action(HeroPowerAction())

    drill = _tarven(cards, "德拉肯钻机")
    drill.level = 2
    drill.mineral = 5
    assert drill.action(HeroPowerAction(amount=4))
    assert drill.hero_controller.state["drill_points"] == 4
    drill.round_start()
    assert drill.mineral == drill.mineral_max + 1


def test_stalker_adept_and_reaper_purchase_rules(cards):
    stalker = _tarven(cards, "追猎者")
    stalker.round_start()
    stalker.mineral = 10
    assert stalker.action(BuyAction(shop_idx=0))
    assert stalker.hero_controller.state["refreshes_this_round"] == 1

    adept = _tarven(cards, "使徒")
    adept.hero_controller.state["purchases_this_round"] = 2
    adept.shop[0] = next(card for card in cards if card.level == 1)
    assert adept.card_price(0) == 1

    reaper = _tarven(cards, "收割者")
    deployable = next(card for card in cards if "能够定点部署" in card.tags)
    reaper.shop[0] = deployable
    assert reaper.card_price(0) == 2
    assert len(reaper.available_placement_slots(deployable)) == 7


def test_nova_mutalisk_and_rattlesnake_discovery_triggers(cards):
    nova = _tarven(cards, "诺娃")
    nova.round_start()
    nova_action = next(a for a in nova.force_action if a.kind == "nova")
    assert nova_action is not None
    # 候选仅来自 7 张可发现辅助卡；冷钱包 / 矿簇为专属获得，不进入通用发现
    assert {c.name for c in nova_action.options} <= set(hero_module.AUXILIARY_CARD_NAMES)
    assert {"冷钱包", "矿簇"}.isdisjoint(c.name for c in nova_action.options)

    mutalisk = _tarven(cards, "异龙")
    mutalisk.level = 3
    mutalisk.mineral = 2
    assert mutalisk.action(HeroPowerAction())
    assert all(card.race == "zerg" for card in mutalisk.force_action[0].options)

    rattlesnake = _tarven(cards, "响尾蛇")
    rattlesnake.level = 3
    for _ in range(3):
        rattlesnake.trigger_refresh()
    assert any(action.kind == "rattlesnake" for action in rattlesnake.force_action)


def test_thor_scv_mengsk_and_baneling_core_mutations(cards):
    thor = _tarven(cards, "雷神")
    thor.level = 3
    thor_slot = _place(thor, next(card for card in cards if card.level == 1 and card.race == "terran"), 0)
    assert thor.action(HeroPowerAction(slot_idx=0))
    assert thor.force_action[0].kind == "thor-description"

    scv = _tarven(cards, "SCV")
    scv.hero_controller.state["charges"] = 1
    scv_slot = _place(scv, _blank_card("挂件", race="terran", units={"反应堆": 1}), 0)
    assert scv.action(HeroPowerAction(slot_idx=0))
    assert scv_slot.count("科技实验室") == 1

    mengsk = _tarven(cards, "蒙斯克")
    mengsk.mineral = 1
    mengsk_slot = _place(mengsk, _blank_card("帝国军", units={"攻城坦克": 1}), 0)
    assert mengsk.action(HeroPowerAction())
    assert mengsk_slot.count("皇家攻城坦克") == 1

    baneling = _tarven(cards, "爆虫")
    baneling_slot = _place(baneling, _blank_card("扩军", units={"跳虫": 4}), 0)
    assert baneling.action(HeroPowerAction())
    assert baneling_slot.count("跳虫") == 6


def test_dehaka_split_pool_hybrid_and_evolution_core_behaviors(cards):
    dehaka = _tarven(cards, "德哈卡")
    dehaka.hero_controller.state["essence"] = 6
    _place(dehaka, _blank_card("猎物", race="zerg", units={"跳虫": 1}), 0)
    assert dehaka.action(HeroPowerAction(slot_idx=0))
    assert dehaka.slots[0].card_type == "原始刺蛇"

    split = _tarven(cards, "分裂池")
    fired = []
    hatch = _blank_card(
        "孵化目标",
        race="zerg",
        handlers=[EventHandler(None, None, "孵化", lambda _s, _e: fired.append(True), "round_start")],
    )
    _place(split, hatch, 0)
    assert split.action(HeroPowerAction(slot_idx=0))
    assert fired == [True]

    hybrid = _tarven(cards, "混合体")
    protoss = _place(hybrid, _blank_card("神族", level=2, race="protoss"), 0)
    _place(hybrid, _blank_card("虫族", level=2, race="zerg"), 1)
    hybrid.round_end()
    assert protoss.count("混合体天罚者") == 1

    evolution = _tarven(cards, "进化腔")
    evolution.round_start()
    assert evolution.hero_controller.state["mutation_refreshes"] == 3
    assert evolution.force_action[0].kind == "evolution"


def test_science_ball_and_battlecruiser_core_abilities(cards):
    science = _tarven(cards, "科学球")
    observed = _place(science, _blank_card("样本", units={"陆战队员": 1, "雷神": 1}), 0)
    science.trigger_entering(observed)
    assert science.action(HeroPowerAction())
    sample = next(slot for slot in science.slots if slot.card_type == "观察样本")
    assert sample.units == {"雷神": 1}

    cruiser = _tarven(cards, "战列巡航舰")
    cruiser.level = 4
    assert cruiser.action(HeroPowerAction())
    assert cruiser.level == 1
    assert not cruiser.action(HeroPowerAction())


def test_all_47_supported_heroes_have_named_core_behavior_coverage():
    covered = {
        "陆战队员", "工蜂", "副官", "矿骡", "米拉", "德拉肯钻机",
        "追猎者", "使徒", "阿尔达瑞斯", "飞蛇", "收割者", "解放者（防卫模式）", "眼虫",
        "航母", "诺娃", "异龙", "响尾蛇", "休伯利安点唱机", "大力神", "飓风",
        "阿巴瑟", "雷诺", "雷神", "机械哨兵", "德哈卡", "干扰者",
        "感染虫", "SCV", "探机", "斯旺", "蒙斯克", "医疗兵", "爆虫",
        "执政官", "阿塔尼斯", "母舰核心", "分裂池", "混合体", "进化腔", "扎加拉",
        "星港", "凯瑞甘", "汉森博士", "科学球", "火蝠", "战列巡航舰", "坑道虫",
    }
    assert covered == set(SUPPORTED_HEROES)



def test_generic_discover_with_empty_pool_does_not_enqueue_dead_action(cards):
    tarven = _tarven(cards)
    saved = [tarven.pool.bucket_uuids(level) for level in range(7)]
    for level in range(7):
        tarven.pool.clear_bucket(level)
    tarven.discover(level=[6])
    assert tarven.force_action == []
    for level, values in enumerate(saved):
        tarven.pool.set_bucket(level, values)


def test_archon_fusion_keeps_right_slot_and_caps_upgrades(cards):
    tarven = _tarven(cards, "执政官")
    left = _place(tarven, next(card for card in cards if card.race == "terran"), 0)
    right = _place(tarven, next(card for card in cards if card.race == "protoss"), 1)
    left.tags.add("金色")
    right.tags.add("金色")
    left.upgrades = [f"左{i}" for i in range(5)]
    right.upgrades = [f"右{i}" for i in range(3)]
    left.level, right.level = 2, 4
    assert tarven.action(HeroPowerAction(slot_idx=0))
    assert tarven.slots[0].card_type is None
    assert tarven.slots[1] is right
    assert right.upgrades == [f"左{i}" for i in range(5)]
    assert right.upgrades_limit == 5 and right.level == 4
    assert right.tags.has("neutral")
    assert not right.tags.has("terran") and not right.tags.has("protoss")


def test_hercules_full_cache_directly_enters_due_reserved_reward(cards):
    tarven = _tarven(cards, "大力神")
    selected = {}
    for action in list(tarven.force_action):
        action.selected = action.options[0]
        selected[action.payload["level"]] = action.selected
        assert tarven.action(action)
    reward = selected[3]
    before = tarven.pool.count(reward)
    tarven.cache[:] = ["占位卡"] * len(tarven.cache)
    tarven.level = 2
    tarven.level_up_cost = 7
    tarven.mineral = 8
    assert tarven.action(UpgradeTarvenAction())
    assert tarven.pool.count(reward) == before
    assert tarven.slots[0].source_card is reward
    assert 3 not in tarven.hero_controller.state["pending_rewards"]


def test_royal_thor_is_fixed_ground_unit():
    assert "皇家雷神" in hero_module.GROUND_UNITS



def test_carrier_pairs_each_purchase_round_history_only_once(cards):
    tarven = _tarven(cards, "航母")
    tarven.mineral = 100
    # 第 1 回合买两张。
    tarven.round_start()
    tarven.mineral = 100
    tarven.shop[0], tarven.shop[1] = tarven.pool.draw(2, 2)
    assert tarven.action(BuyAction(0))
    assert tarven.action(BuyAction(1))
    tarven.round_end()
    # 第 2 回合再买两张；第 3 回合会同时到达“旧第2张”和“新第1张”。
    tarven.round_start()
    tarven.mineral = 100
    tarven.shop[0], tarven.shop[1] = tarven.pool.draw(2, 2)
    assert tarven.action(BuyAction(0))
    assert tarven.action(BuyAction(1))
    tarven.round_end()
    tarven.round_start()
    combos = [action for action in tarven.force_action if getattr(action, "kind", None) == "carrier-combo"]
    assert len(combos) == 1



def test_splitting_pool_failure_does_not_consume_round_cooldown(cards):
    tarven = _tarven(cards, "分裂池")
    _place(tarven, _blank_card("无孵化效果", race="zerg"), 0)
    assert not tarven.action(HeroPowerAction(slot_idx=0))
    fired = []
    hatch = _blank_card(
        "有孵化效果",
        race="zerg",
        handlers=[EventHandler(None, None, "孵化", lambda _s, _e: fired.append(True), "round_start")],
    )
    _place(tarven, hatch, 0)
    assert tarven.action(HeroPowerAction(slot_idx=0))
    assert fired == [True]



def test_all_storm_generated_heroes_are_classified_as_hero_units():
    assert {"马拉什", "阿拉纳克", "利维坦", "虚空构造体", "科罗拉里昂"} <= set(hero_module.HERO_UNITS)


def test_hybrid_rewards_left_member_when_zerg_is_left(cards):
    tarven = _tarven(cards, "混合体")
    left = _place(tarven, _blank_card("左虫族", level=2, race="zerg"), 0)
    right = _place(tarven, _blank_card("右神族", level=2, race="protoss"), 1)
    tarven.round_end()
    assert left.count("混合体天罚者") == 1
    assert right.count("混合体天罚者") == 0



# ---------------------------------------------------------------------------
# 最终修复回归
# ---------------------------------------------------------------------------


def test_elite_and_royal_variants_inherit_base_unit_classifications():
    assert "陆战队员(精英)" in hero_module.BIOLOGICAL_UNITS
    assert "陆战队员(精英)" in hero_module.GROUND_UNITS
    assert "跳虫(精英)" in hero_module.BIOLOGICAL_UNITS
    assert "跳虫(精英)" in hero_module.GROUND_UNITS
    assert "维京战机(精英)" in hero_module.MECHANICAL_UNITS
    assert "维京战机(精英)" in hero_module.AIR_UNITS
    assert "幽灵(皇家卫队)" in hero_module.BIOLOGICAL_UNITS
    assert "幽灵(皇家卫队)" in hero_module.GROUND_UNITS


def test_generated_hero_units_follow_each_conversion_contract(cards):
    heroes = {"扎加拉", "斯旺", "大力神", "沃拉尊", "仲裁者", "阿塔尼斯"}
    assert heroes <= set(hero_module.HERO_UNITS)

    swann = _tarven(cards, "斯旺")
    swann_slot = _place(swann, _blank_card("英雄机械", race="terran", units={"斯旺": 1, "攻城坦克": 1}), 0)
    assert swann.action(HeroPowerAction(slot_idx=0))
    assert swann_slot.count("斯旺") == 1
    assert swann_slot.count("攻城坦克") == 0

    medic = _tarven(cards, "医疗兵")
    source = _place(medic, _blank_card("英雄生物", units={"扎加拉": 1, "陆战队员": 1}), 0)
    recipient = _place(medic, _blank_card("接收者"), 1)
    assert medic.action(HeroPowerAction(slot_idx=0))
    assert recipient.count("扎加拉") == 0
    assert recipient.count("陆战队员") == 1

    starport = _tarven(cards, "星港")
    starport.level = 6
    hero_slot = _place(starport, _blank_card("地面英雄", units={"沃拉尊": 1, "陆战队员": 1}), 0)
    starport.hero_controller.round_start_before_cards()
    assert hero_slot.count("沃拉尊") == 0
    assert hero_slot.count("战列巡航舰") == 1
    assert hero_slot.count("陆战队员") == 0


def test_carrier_does_not_intercept_unrelated_delayed_card(cards):
    tarven = _tarven(cards, "航母")
    ordinary = next(card for card in cards if card.level == 3)
    tarven.delay_enter_card[3] = [ordinary]
    tarven.round = 2

    tarven.round_start()

    assert tarven.slots[0].source_card is ordinary
    assert not _cache_has(tarven, ordinary)
    assert tarven.hero_controller.state["carrier_entries"] == []


def test_disruptor_counts_and_rerolls_static_names_and_cards_but_not_derived(cards):
    tarven = _tarven(cards, "干扰者")
    first = next(card for card in cards if card.level > 0 and card.price > 0)
    second = next(card for card in cards if card.level == first.level and card is not first and card.price > 0)
    derived = _blank_card("衍生卡", level=first.level, units={"菲尼克斯": 1})
    tarven.cache[:3] = [first.name, second, derived]

    assert tarven.total_power() == pytest.approx(first.price + second.price)
    assert tarven.action(HeroPowerAction())
    assert all(isinstance(item, Card) for item in tarven.cache[:2])
    assert tarven.cache[2] is derived
    assert tarven.hero_controller.state["charges"] == 1


def test_disruptor_failed_reroll_does_not_spend_charge(cards):
    tarven = _tarven(cards, "干扰者")
    tarven.cache[0] = _blank_card("不可重随机的衍生卡", level=3)
    before = tarven.hero_controller.state.get("charges", 2)

    assert not tarven.action(HeroPowerAction())
    assert tarven.hero_controller.state.get("charges", 2) == before


def test_mengsk_uses_explicit_royal_mapping_for_every_source_unit(cards):
    mapping = {
        "战列巡航舰": "皇家战列巡航舰",
        "雷神": "皇家雷神",
        "攻城坦克": "皇家攻城坦克",
        "维京战机": "皇家维京战机",
        "幽灵": "皇家幽灵",
        "劫掠者": "帝盾卫兵",
    }
    tarven = _tarven(cards, "蒙斯克")
    tarven.mineral = 1
    slots = [
        _place(tarven, _blank_card(f"来源-{source}", units={source: 1}), index)
        for index, source in enumerate(mapping)
    ]

    assert tarven.action(HeroPowerAction())
    for slot, (source, royal) in zip(slots, mapping.items()):
        assert slot.count(source) == 0
        assert slot.count(royal) == 1


def test_mengsk_preserves_conversion_priority_within_each_slot(cards):
    tarven = _tarven(cards, "蒙斯克")
    tarven.mineral = 1
    slot = _place(
        tarven,
        _blank_card("混合帝国军", units={"雷神": 1, "维京战机": 1, "幽灵": 1}),
        0,
    )

    assert tarven.action(HeroPowerAction())
    assert slot.count("皇家雷神") == 1
    assert slot.count("维京战机") == 1
    assert slot.count("幽灵") == 1


def test_artanis_fuses_only_after_entering_and_any_entered_broadcasts(cards):
    tarven = _tarven(cards, "阿塔尼斯")
    filler = next(card for card in cards if card.name == "好兄弟")
    for _ in range(9):
        temporary = Slot(6, tarven)
        tarven.card_engine.assign_card_to_slot(filler, temporary)
        tarven.trigger_entering(temporary)

    observations = []
    target = _place(tarven, filler, 0)
    target.event_handlers.append(
        EventHandler(tarven, target, "观察自身进场", lambda slot, _event: observations.append(("self", slot.card_type)), EnteringEvent.event_name)
    )
    observer = _place(tarven, _blank_card("观察者"), 1)
    observer.event_handlers.append(
        EventHandler(tarven, observer, "观察任意进场", lambda _slot, event: observations.append(("other", event.entered_slot.card_type)), AnyCardEnteredEvent.event_name)
    )

    tarven.trigger_entering(target)

    assert observations == [("self", filler.name), ("other", filler.name)]
    assert target.card_type == "好兄弟+阿塔尼斯"


def test_dynamic_cards_always_use_negative_uuid():
    for index in range(20):
        assert hero_module._dynamic_card(f"动态卡-{index}", level=index % 7).uuid < 0



def test_carrier_matches_delayed_purchase_by_queue_occurrence_when_cards_share_definition(cards):
    tarven = _tarven(cards, "航母")
    shared = next(card for card in cards if card.level == 3)
    tarven.round = 3
    tarven.delay_enter_card[3] = [shared, shared]
    tarven.hero_controller.state["carrier_pending"][3] = [(shared, 2, 1, 1)]

    assert not tarven.hero_controller.receive_delayed_card(shared, delay_index=0)
    assert tarven.hero_controller.receive_delayed_card(shared, delay_index=1)


def test_carrier_delayed_arrival_third_copy_forces_synthesis(cards):
    """航母延迟购买到货形成第 3 张同名卡时必须强制三连，不能普通进场绕开。"""
    tarven = _tarven(cards, "航母")
    pair = next(card for card in cards if card.name == "好兄弟")
    _place(tarven, pair, 0)
    _place(tarven, pair, 1)
    tarven.mineral = 100
    tarven.shop[0] = pair

    # 航母第 1 次购买 -> 延迟 1 回合到货
    assert tarven.action(BuyAction(shop_idx=0))
    assert tarven.delay_enter_card[1] == [pair]
    assert len([s for s in tarven.slots if s.card_type == "好兄弟"]) == 2

    tarven.round_start()

    # 到货强制三连：不普通进场，进入 ChooseSynthesisAction
    assert len(tarven.force_action) == 1
    assert isinstance(tarven.force_action[0], ChooseSynthesisAction)
    assert len([s for s in tarven.slots if s.card_type == "好兄弟"]) == 2
    # 航母簿记保留（到货已记录，组合奖励依赖该簿记）
    assert len(tarven.hero_controller.state["carrier_entries"]) == 1
    assert tarven.hero_controller.state["carrier_history"].get(1) == {1: pair}

    # 完成三连：左槽变金色，右槽清空
    choice = tarven.force_action[0]
    choice.selected = choice.options[0]
    assert tarven.action(choice) is True
    assert tarven.slots[0].tags.has("金色")
    assert tarven.slots[1].card_type is None


def test_carrier_delayed_arrival_third_copy_forces_synthesis_on_full_board(cards):
    """7 槽满场 + 两张同名非金色 + 航母延迟第 3 张到货：仍必须强制三连，不能转存暂存区。

    回归验收：receive_delayed_card 曾以 ``if empty is not None and enter_card_direct``
    短路，场满时完全不调用统一入口，导致第 3 张同名卡被转存而非强制三连。
    """
    tarven = _tarven(cards, "航母")
    pair = next(card for card in cards if card.name == "好兄弟")
    _place(tarven, pair, 0)
    _place(tarven, pair, 1)
    # 填满其余 5 个槽位，场上无空位
    filler = _blank_card("占位")
    for index in range(2, 7):
        _place(tarven, filler, index)
    assert all(slot.card_type is not None for slot in tarven.slots)

    tarven.mineral = 100
    tarven.shop[0] = pair

    # 航母第 1 次购买 -> 延迟 1 回合到货
    assert tarven.action(BuyAction(shop_idx=0))
    assert tarven.delay_enter_card[1] == [pair]
    assert len([s for s in tarven.slots if s.card_type == "好兄弟"]) == 2

    tarven.round_start()

    # 场满也不转存：到货第 3 张强制三连，进入 ChooseSynthesisAction
    assert len(tarven.force_action) == 1
    assert isinstance(tarven.force_action[0], ChooseSynthesisAction)
    assert len([s for s in tarven.slots if s.card_type == "好兄弟"]) == 2
    assert not any(item is pair or item == pair.name for item in tarven.cache)
    # 航母簿记保留（到货已记录，组合奖励依赖该簿记）
    assert len(tarven.hero_controller.state["carrier_entries"]) == 1
    assert tarven.hero_controller.state["carrier_history"].get(1) == {1: pair}

    # 完成三连：左槽变金色，右槽清空
    choice = tarven.force_action[0]
    choice.selected = choice.options[0]
    assert tarven.action(choice) is True
    assert tarven.slots[0].tags.has("金色")
    assert tarven.slots[1].card_type is None


def test_carrier_delayed_egg_does_not_create_second_egg(cards):
    """航母延迟购买到货是虫卵且场上已有虫卵时，不产生第二张，转存暂存区。"""
    tarven = _tarven(cards, "航母")
    egg = _blank_card("虫卵", race="zerg", units={"跳虫": 1})
    tarven.larva({"跳虫": 1})
    tarven.round = 3
    tarven.delay_enter_card[3] = [egg]
    tarven.hero_controller.state["carrier_pending"][3] = [(egg, 1, 1, 0)]

    assert tarven.hero_controller.receive_delayed_card(egg, delay_index=0)
    # 不产生第二张虫卵，被拒绝进场后转存暂存区
    assert len([s for s in tarven.slots if s.card_type == "虫卵"]) == 1
    assert _cache_has(tarven, egg)
    assert tarven.hero_controller.state["carrier_entries"] == []



# ---------------------------------------------------------------------------
# docs/hero-system.md mismatch regressions
# ---------------------------------------------------------------------------


def test_all_refresh_sources_use_policy_and_broadcast(cards):
    defender = _tarven(cards, "解放者（防卫模式）")
    card = next(card for card in cards if card.level == 1)
    slot = _place(defender, card, 0)
    events = []
    slot.event_handlers.append(
        EventHandler(defender, slot, "刷新观察", lambda _s, _e: events.append(True), RefreshEvent.event_name)
    )
    original_shop = list(defender.shop)
    assert not defender.refresh()
    assert defender.shop == original_shop and events == []

    assert defender.action(HeroPowerAction())
    assert defender.refresh()
    assert events == [True]
    assert defender.hero_controller.state["refreshes_this_round"] == 1


def test_stalker_upgrades_immediately_on_fourth_refresh(cards):
    tarven = _tarven(cards, "追猎者")
    for _ in range(4):
        assert tarven.refresh()
    assert tarven.hero_controller.state["stalker_upgraded"]
    before = tarven.hero_controller.state["refreshes_this_round"]
    tarven.mineral = 10
    assert tarven.action(BuyAction(0))
    assert tarven.hero_controller.state["refreshes_this_round"] == before + 1


def test_sentinel_copies_initial_card_and_overflows_directly(cards):
    tarven = _tarven(cards, "机械哨兵")
    definition = next(card for card in cards if 1 <= card.level <= 4)
    source = _place(tarven, definition, 0)
    source.add_unit("陆战队员", 9)
    source.upgrades.append("聚能器")
    source.tags.add("金色")
    tarven.cache[:] = ["占位卡"] * len(tarven.cache)
    tarven.mineral = 3

    assert tarven.action(HeroPowerAction(slot_idx=0))
    copied = tarven.slots[1]
    assert copied.card_type == definition.name
    assert copied.units == definition.units
    assert copied.upgrades == [] and not copied.tags.has("金色")


def test_adept_synthesis_charges_stack_and_shop_synthesis_is_not_purchase(cards):
    tarven = _tarven(cards, "使徒")
    card = next(card for card in cards if card.level == 1)
    _place(tarven, card, 0)
    _place(tarven, card, 1)
    tarven.shop[0] = card
    tarven.mineral = 3
    tarven.hero_controller.state["adept_one_cost_charges"] = 1
    assert tarven.action(SynthesisAction(shop_idx=0))
    assert tarven.hero_controller.state["purchases_this_round"] == 0
    assert tarven.hero_controller.state["adept_one_cost_charges"] == 1
    choice = tarven.force_action[0]
    choice.selected = choice.options[0]
    assert tarven.action(choice)
    assert tarven.hero_controller.state["adept_one_cost_charges"] == 2

    tarven.mineral = 2
    tarven.shop[0] = next(candidate for candidate in cards if candidate.level == 1 and candidate is not card)
    tarven.shop[1] = next(candidate for candidate in cards if candidate.level == 1 and candidate.name != tarven.shop[0].name)
    assert tarven.card_price(0) == 1 and tarven.action(BuyAction(0))
    assert tarven.card_price(1) == 1 and tarven.action(BuyAction(1))
    assert tarven.hero_controller.state["adept_one_cost_charges"] == 0


def test_shop_synthesis_updates_kerrigan_and_viper_purchase_history(cards):
    card = next(card for card in cards if card.level == 1)
    kerrigan = _tarven(cards, "凯瑞甘")
    _place(kerrigan, card, 0)
    _place(kerrigan, card, 1)
    kerrigan.shop[0] = card
    kerrigan.mineral = 3
    assert kerrigan.action(SynthesisAction(shop_idx=0))
    assert kerrigan.hero_controller.state["purchases_this_round"] == 1

    viper = _tarven(cards, "飞蛇")
    _place(viper, card, 0)
    _place(viper, card, 1)
    viper.shop[0] = card
    viper.mineral = 3
    assert viper.action(SynthesisAction(shop_idx=0))
    assert card.name in viper.hero_controller.state["bought_names"]


def test_abathur_level_six_discovers_six_and_uses_destroyed_slot_overflow(cards):
    tarven = _tarven(cards, "阿巴瑟")
    target = next(card for card in cards if card.level == 6)
    _place(tarven, target, 0)
    tarven.cache[:] = ["占位卡"] * len(tarven.cache)
    tarven.mineral = 2
    tarven.level_up_cost = 7
    assert tarven.action(HeroPowerAction(slot_idx=0))
    choice = tarven.force_action[0]
    assert all(card.level == 6 for card in choice.options)
    selected = choice.options[0]
    choice.selected = selected
    assert tarven.action(choice)
    assert tarven.slots[0].source_card is selected
    assert tarven.mineral == 0 and tarven.level_up_cost == 3


def test_infestor_larva_uses_current_unit_types_including_heroes(cards):
    tarven = _tarven(cards, "感染虫")
    source = _place(tarven, _blank_card("感染目标", race="terran", units={"仲裁者": 1}), 0)
    assert tarven.action(HeroPowerAction(slot_idx=0))
    source.trigger([RoundEndEvent(tarven)])
    egg = next(slot for slot in tarven.slots if slot.card_type == "虫卵")
    assert egg.units == {"仲裁者": 1}


def test_swann_factory_creation_is_atomic_when_board_is_full(cards):
    tarven = _tarven(cards, "斯旺")
    source = _place(tarven, _blank_card("机械", units={"攻城坦克": 2}), 0)
    for index in range(1, 7):
        _place(tarven, _blank_card(f"占位{index}"), index)
    assert not tarven.action(HeroPowerAction(slot_idx=0))
    assert source.count("攻城坦克") == 2
    assert all(slot.card_type != "机械工厂" for slot in tarven.slots)


def test_medic_clears_target_even_without_recipients(cards):
    tarven = _tarven(cards, "医疗兵")
    source = _place(tarven, _blank_card("孤立伤员", units={"陆战队员": 2, "攻城坦克": 1}), 0)
    assert tarven.action(HeroPowerAction(slot_idx=0))
    assert source.units == {}


def test_raynor_uses_glitter_upgrade_and_requires_capacity(cards):
    tarven = _tarven(cards, "雷诺")
    card = next(card for card in cards if card.level < 6)
    slot = _place(tarven, card, 0)
    assert tarven.action(HeroPowerAction(slot_idx=0))
    assert slot.tags.has("金色") and slot.upgrades == ["金光闪闪"]


def test_dehaka_static_transform_preserves_payload_and_mapped_choice(cards):
    tarven = _tarven(cards, "德哈卡")
    slot = _place(tarven, _blank_card("猎物", level=5, race="terran", units={"原始蟑螂": 2}), 0)
    slot.upgrades = ["聚能器"]
    tarven.hero_controller.state["essence"] = 7
    assert tarven.action(HeroPowerAction(slot_idx=0))
    assert slot.card_type == "原始刺蛇" and slot.level == 2
    assert slot.units == {"原始蟑螂": 2} and slot.upgrades == ["聚能器"]

    assert tarven.action(HeroPowerAction(slot_idx=0))
    choice = tarven.force_action[0]
    assert choice.options == ["原始点火虫"]
    choice.selected = choice.options[0]
    assert tarven.action(choice)
    assert slot.count("原始蟑螂") == 1 and slot.count("原始点火虫") == 1


def test_liberator_fighter_refreshes_each_round_and_nydus_is_once_per_round(cards):
    liberator = _tarven(cards, "解放者（防卫模式）")
    assert liberator.action(HeroPowerAction())
    liberator.free_refresh = 0
    liberator.round_start()
    assert liberator.free_refresh == 1
    liberator.level = 4
    liberator.round_end()
    liberator.round_start()
    assert liberator.free_refresh == 2

    nydus = _tarven(cards, "坑道虫")
    assert nydus.action(HeroPowerAction(slot_idx=0))
    assert not nydus.action(HeroPowerAction(slot_idx=1))
    nydus.round_start()
    assert nydus.action(HeroPowerAction(slot_idx=1))


def test_battlecruiser_requires_level_two_and_emits_refresh(cards):
    tarven = _tarven(cards, "战列巡航舰")
    card = next(card for card in cards if card.level == 1)
    slot = _place(tarven, card, 0)
    events = []
    slot.event_handlers.append(
        EventHandler(tarven, slot, "刷新观察", lambda _s, _e: events.append(True), RefreshEvent.event_name)
    )
    assert not tarven.action(HeroPowerAction())
    tarven.level = 2
    assert tarven.action(HeroPowerAction())
    assert tarven.level == 1 and events == [True]


def test_hansen_research_cost_and_artifact_studies(cards):
    researcher = _tarven(cards, "汉森博士")
    researcher.hero_controller.state["studies"] = ["科研成本"]
    researcher.mineral = researcher.level_up_cost + 3
    assert researcher.action(UpgradeTarvenAction())
    assert researcher.mineral == 0
    discovery = next(action for action in researcher.force_action if action.kind == "card")
    assert all(card.level <= 3 for card in discovery.options)

    collector = _tarven(cards, "汉森博士")
    collector.hero_controller.state["studies"] = ["采集神器"]
    _place(collector, _blank_card("左邻"), 0)
    sold = _place(collector, _blank_card("神器", race="protoss"), 1)
    _place(collector, _blank_card("右邻"), 2)
    collector.trigger_selling(sold)
    for index in (0, 2):
        assert collector.slots[index].count("劫掠者") == 1 or collector.slots[index].count("陆战队员") == 2



def test_evolution_options_follow_contract_domains(cards):
    tarven = _tarven(cards, "进化腔")
    options = tarven.hero_controller._mutation_options()
    basic = {"跳虫", "爆虫", "蟑螂", "刺蛇", "破坏者", "异龙", "雷兽"}
    first_level = {}
    for card in tarven.pool.cards:
        for unit in card.units:
            first_level[unit] = min(first_level.get(unit, 7), card.level)
    assert len(options) == 3
    for old, new, amount in options:
        assert old in basic and old != new
        assert new in first_level
        assert not new.endswith(("(精英)", "(英雄)", "(皇家卫队)"))
        assert 1 <= amount <= 13 - 2 * first_level[new]


def test_artanis_self_tenth_entry_keeps_single_definition(cards):
    tarven = _tarven(cards, "阿塔尼斯")
    filler = next(card for card in cards if card.name == "好兄弟")
    for _ in range(9):
        temporary = Slot(6, tarven)
        tarven.card_engine.assign_card_to_slot(filler, temporary)
        tarven.trigger_entering(temporary)
    definition = next(card for card in cards if card.name == "阿塔尼斯")
    slot = _place(tarven, definition, 0)
    handler_count = len(slot.event_handlers)
    initial_units = dict(slot.units)
    tarven.trigger_entering(slot)
    assert slot.card_type == "阿塔尼斯" and slot.tags.has("protoss") and slot.tags.has("金色")
    assert len(slot.event_handlers) == handler_count
    assert all(slot.count(unit) == count * 2 for unit, count in initial_units.items())


def test_scv_rejects_no_addon_without_spending_charge(cards):
    tarven = _tarven(cards, "SCV")
    _place(tarven, _blank_card("无人族挂件", race="terran", units={"陆战队员": 1}), 0)
    before = tarven.hero_controller.state["charges"]
    assert not tarven.action(HeroPowerAction(slot_idx=0))
    assert tarven.hero_controller.state["charges"] == before


def test_raynor_and_sentinel_reject_special_cards(cards):
    special = next(card for card in cards if card.name == "母舰核心")
    raynor = _tarven(cards, "雷诺")
    _place(raynor, special, 0)
    assert special.uuid in raynor.pool.no_draw_uuids
    assert not raynor.action(HeroPowerAction(slot_idx=0))

    sentinel = _tarven(cards, "机械哨兵")
    _place(sentinel, special, 0)
    sentinel.mineral = 3
    assert not sentinel.action(HeroPowerAction(slot_idx=0))


def test_hurricane_exclusion_does_not_refresh_current_shop(cards):
    tarven = _tarven(cards, "飓风")
    tarven.mineral = 1
    before = list(tarven.shop)
    assert tarven.action(HeroPowerAction(race="zerg"))
    assert tarven.shop == before
    assert tarven.hero_controller.state["refreshes_this_round"] == 0


def test_starport_banshee_is_air_and_nonhero(cards):
    assert "女妖" in hero_module.AIR_UNITS
    assert "女妖" in hero_module.MECHANICAL_UNITS
    assert "女妖" not in hero_module.HERO_UNITS


def test_starport_power_is_once_per_round(cards):
    """星港主动技每回合至多一次：设置 air_mode 后同回合再次调用返回 False，
    避免重复设置同一值形成“可无限重复且状态不再变化”的静默 no-op。"""
    tarven = _tarven(cards, "星港")
    assert tarven.action(HeroPowerAction(unit="怨灵战机"))
    assert tarven.hero_controller.state["last_power_round"] == tarven.round
    assert tarven.hero_controller.state["air_mode"] == "怨灵战机"
    # 同回合换另一个单位也应拒绝（一次性）
    assert not tarven.action(HeroPowerAction(unit="维京战机"))
    # 下一回合可再用
    tarven.round_end()
    tarven.round_start()
    assert tarven.action(HeroPowerAction(unit="维京战机"))
    assert tarven.hero_controller.state["air_mode"] == "维京战机"
    # 非法单位名恒拒绝
    assert not tarven.action(HeroPowerAction(unit="不存在的单位"))


def test_real_card_unit_classifications_cover_hero_contract_examples(cards):
    assert {"歌利亚", "原始穿刺者", "塔里斯", "拟态雏虫", "驯养雷兽"} <= set(hero_module.GROUND_UNITS)
    assert "原始守卫" in hero_module.AIR_UNITS
    assert "奥丁" in hero_module.MECHANICAL_UNITS
    assert {"原始穿刺者", "拟态雏虫", "驯养雷兽"} <= set(hero_module.BIOLOGICAL_UNITS)
    assert {"原始穿刺者", "原始守卫", "拟态雏虫", "驯养雷兽"} <= set(hero_module.ZERG_UNITS)
    assert "塔里斯" in hero_module.HERO_UNITS


def test_aldaris_does_not_discount_same_name_copy_replenished_after_lock(cards):
    tarven = _tarven(cards, "阿尔达瑞斯")
    card = next(card for card in cards if card.level == 1)
    tarven.shop[0] = card
    assert tarven.action(LockAction())
    tarven.mineral = 3
    assert tarven.action(BuyAction(shop_idx=0))
    # A distinct pool copy shares the static UUID but was not retained by the lock.
    tarven.shop[0] = None
    tarven.round_start()
    tarven.shop[0] = card
    assert tarven.card_price(0) == 3


def test_splitting_pool_can_immediately_hatch_native_egg(cards):
    tarven = _tarven(cards, "分裂池")
    left = _place(tarven, _blank_card("左", race="zerg"), 0)
    tarven.card_engine.assign_card_to_slot("虫卵", tarven.slots[1])
    tarven.slots[1].add_unit("跳虫", 2)
    right = _place(tarven, _blank_card("右", race="zerg"), 2)
    assert tarven.action(HeroPowerAction(slot_idx=1))
    assert tarven.slots[1].card_type is None
    assert left.count("跳虫") == right.count("跳虫") == 2


def test_infected_cannot_triple_tag_blocks_synthesis(cards):
    tarven = _tarven(cards, "感染虫")
    card = next(card for card in cards if card.level == 1 and card.race == "terran")
    _place(tarven, card, 0)
    _place(tarven, card, 1)
    assert tarven.action(HeroPowerAction(slot_idx=0))
    tarven.cache[0] = card
    assert not tarven.action(SynthesisAction(cache_idx=0))


def test_viper_known_purchase_does_not_consume_new_name_discount(cards):
    tarven = _tarven(cards, "飞蛇")
    known = next(card for card in cards if card.level == 1)
    fresh = next(card for card in cards if card.level == 1 and card.name != known.name)
    tarven.hero_controller.state["bought_names"] = {known.name}
    tarven.hero_controller.state["viper_discount_used"] = False
    tarven.shop[0] = known
    tarven.shop[1] = fresh
    tarven.mineral = 6
    assert tarven.card_price(0) == 3
    assert tarven.action(BuyAction(0))
    assert not tarven.hero_controller.state["viper_discount_used"]
    assert tarven.card_price(1) == 2


def test_eye_purchase_ban_includes_shop_synthesis(cards):
    tarven = _tarven(cards, "眼虫")
    card = next(card for card in cards if card.level == 1)
    _place(tarven, card, 0)
    _place(tarven, card, 1)
    tarven.shop[0] = card
    tarven.mineral = 3
    assert not tarven.action(SynthesisAction(shop_idx=0))
    assert tarven.shop[0] is card and tarven.mineral == 3


def test_firebat_destroys_before_starting_discovery(cards):
    tarven = _tarven(cards, "火蝠")
    _place(tarven, _blank_card("高战力", units={"菲尼克斯": 2}), 0)
    observed = []

    def observe_discovery(**_kwargs):
        observed.append(all(slot.card_type is None for slot in tarven.slots))
        return False

    tarven.hero_controller.discover = observe_discovery
    assert tarven.action(HeroPowerAction())
    assert observed == [True]
    assert tarven.hero_controller.state["uses"] == 1



def test_kerrigan_same_round_level_ups_cap_free_refresh_at_one(cards):
    tarven = _tarven(cards, "凯瑞甘")
    tarven.hero_controller.on_level_up(1, 5)
    tarven.hero_controller.on_level_up(2, 7)
    assert tarven.free_refresh == 1


def test_disruptor_level_six_immediately_caps_remaining_charges(cards):
    tarven = _tarven(cards, "干扰者")
    tarven.hero_controller.state["charges"] = 2
    tarven.level = 6
    tarven.hero_controller.on_level_up(5, 10)
    assert tarven.hero_controller.state["charges"] == 1


def test_reaper_auxiliary_cards_still_require_deploy_action(cards):
    tarven = _tarven(cards, "收割者")
    auxiliary = next(card for card in cards if any(handler.event_name.value == "deployment" for handler in card.event_handlers))
    tarven.cache[0] = auxiliary
    assert tarven.available_placement_slots(auxiliary) == []
    assert not tarven.action(CacheEnterAction(cache_idx=0, slot_idx=0))



@pytest.mark.parametrize("hero", ["米拉", "探机", "阿塔尼斯"])
def test_ordinary_delayed_arrival_is_a_normal_entry(cards, hero):
    tarven = _tarven(cards, hero)
    delayed = _blank_card(f"{hero}延迟卡", level=2, race="terran")
    tarven.delay_enter_card[1] = [delayed]

    if hero == "米拉":
        tarven.hero_controller.state["last_enter_level"] = 0
    elif hero == "阿塔尼斯":
        tarven.hero_controller.state["entered"] = 9

    tarven.round_start()

    slot = tarven.slots[0]
    if hero == "米拉":
        assert slot.source_card is delayed
        assert tarven.mineral == tarven.mineral_max + 1
        assert tarven.hero_controller.state["last_enter_level"] == 2
    elif hero == "探机":
        assert slot.source_card is delayed
        assert slot.count("水晶塔") == 1
    else:
        assert slot.card_type == f"{delayed.name}+阿塔尼斯"
        assert slot.tags.has("金色") and slot.tags.has("protoss")



def test_shop_synthesis_full_cache_completes_and_overflows_reward(cards):
    tarven = _tarven(cards, "使徒")
    card = next(card for card in cards if card.level == 1)
    left = _place(tarven, card, 0)
    _place(tarven, card, 1)
    tarven.cache[:] = ["占位卡"] * len(tarven.cache)
    tarven.shop[0] = card
    tarven.mineral = 3

    assert tarven.action(SynthesisAction(shop_idx=0))
    choice = tarven.force_action[0]
    reward = next(option for option in choice.options if isinstance(option, Card))
    choice.selected = reward
    assert tarven.action(choice)

    assert left.tags.has("金色")
    assert tarven.slots[1].source_card is reward
    assert tarven.hero_controller.state["syntheses"] == 1
    assert tarven.hero_controller.state["adept_one_cost_charges"] == 1
    assert tarven.hero_controller.state["purchases_this_round"] == 0
    assert tarven.mineral == 0



def test_card_refresh_handler_uses_controlled_refresh_entry(cards):
    refresh_card = next(card for card in cards if card.name == "小捞油水")

    normal = _tarven(cards)
    normal.round_start()
    source = _place(normal, refresh_card, 0)
    observer = _place(normal, _blank_card("刷新观察者"), 1)
    events = []
    observer.event_handlers.append(
        EventHandler(normal, observer, "刷新广播", lambda _s, _e: events.append(True), RefreshEvent.event_name)
    )
    normal.lock = True
    normal.trigger_selling(source)
    assert normal.hero_controller.state["refreshes_this_round"] == 1
    assert events == [True] and not normal.lock

    defender = _tarven(cards, "解放者（防卫模式）")
    defender.round_start()
    source = _place(defender, refresh_card, 0)
    original_shop = list(defender.shop)
    defender.lock = True
    defender.trigger_selling(source)
    assert defender.shop == original_shop
    assert defender.hero_controller.state["refreshes_this_round"] == 0
    assert defender.lock


def test_liberator_defense_keeps_shop_holes_and_modes_fix_prices(cards):
    tarven = _tarven(cards, "解放者（防卫模式）")
    tarven.round_start()
    bought = tarven.shop[0]
    tarven.mineral = 2
    assert tarven.card_price(0) == 2
    assert tarven.action(BuyAction(0))
    assert _cache_has(tarven, bought) and tarven.shop[0] is None

    tarven.round_end()
    tarven.round_start()
    assert tarven.shop[0] is None
    assert tarven.action(HeroPowerAction())
    tarven.shop[0] = next(card for card in cards if card.level == 1)
    assert tarven.card_price(0) == 4


def test_hansen_level_milestones_offer_unresearched_fifo_studies(cards):
    tarven = _tarven(cards, "汉森博士")

    tarven.mineral = 100
    assert tarven.action(UpgradeTarvenAction())
    first = tarven.force_action[0]
    assert first.kind == "hansen-study" and set(first.options) == set(hero_module.HANSEN_STUDIES)
    first.selected = "样本日志"
    assert tarven.action(first)

    tarven.mineral = 100
    assert tarven.action(UpgradeTarvenAction())  # 3 本不产生课题。
    assert tarven.force_action == []
    assert tarven.action(UpgradeTarvenAction())
    second = tarven.force_action[0]
    assert second.kind == "hansen-study" and "样本日志" not in second.options
    second.selected = "采集神器"
    assert tarven.action(second)

    tarven.mineral = 100
    assert tarven.action(UpgradeTarvenAction())  # 5 本不产生课题。
    assert tarven.force_action == []
    assert tarven.action(UpgradeTarvenAction())
    third = tarven.force_action[0]
    assert third.kind == "hansen-study"
    assert {"样本日志", "采集神器"}.isdisjoint(third.options)
    third.selected = "新式血清"
    assert tarven.action(third)
    assert _hero_name(tarven) == "汉森博士（异虫形态）"
    assert tarven.hero_controller.state["studies"] == []



def test_thor_accepts_core_cards_with_additional_expansion_source(cards):
    tarven = _tarven(cards, "雷神")
    target = _place(tarven, _blank_card("四星人族目标", level=4, race="terran"), 0)
    assert target.source_card is not None

    assert tarven.action(HeroPowerAction(slot_idx=0))
    choice = tarven.force_action[0]
    assert choice.kind == "thor-description"
    assert any(
        option.name == "快速生产"
        and "核心人族" in option.source
        and "重装上阵" in option.source
        for option in choice.options
    )



def test_swann_recounts_mechanical_units_added_during_factory_entry(cards):
    tarven = _tarven(cards, "斯旺")
    add_tank = EventHandler(
        None,
        None,
        "机械工厂进场时补充机械单位",
        lambda slot, _event: slot.add_unit("攻城坦克", 1),
        "any_card_entered",
    )
    source = _place(
        tarven,
        _blank_card(
            "动态回收目标",
            race="terran",
            units={"攻城坦克": 1},
            handlers=[add_tank],
        ),
        0,
    )

    assert tarven.action(HeroPowerAction(slot_idx=0))
    factory = next(slot for slot in tarven.slots if slot.card_type == "机械工厂")
    assert source.count("攻城坦克") == 0
    assert factory.count("零件") == 2


def test_medic_distributes_all_nonhero_hybrid_biological_units(cards):
    tarven = _tarven(cards, "医疗兵")
    source = _place(
        tarven,
        _blank_card(
            "混合体伤员",
            units={"混合体天罚者": 1, "混合体巨兽": 1},
        ),
        0,
    )
    recipient = _place(tarven, _blank_card("混合体接收者"), 1)

    assert tarven.action(HeroPowerAction(slot_idx=0))
    assert source.units == {}
    assert recipient.count("混合体天罚者") == 1
    assert recipient.count("混合体巨兽") == 1


def test_viper_uses_current_slot_level_and_leftmost_tiebreak(cards):
    game = build_game(cards, user_count=2, heroes=["飞蛇", "default"])
    tarven, opponent = game.tarvens
    static_high = next(card for card in cards if card.level == 4)
    current_high = next(card for card in cards if card.level == 2 and card.name != static_high.name)
    downgraded = _place(opponent, static_high, 0)
    promoted = _place(opponent, current_high, 1)
    downgraded.level = 1
    promoted.level = 3
    game.set_current_opponent(0, 1)

    assert tarven.action(HeroPowerAction())
    copied = next(item for item in tarven.cache if isinstance(item, Card))
    assert copied.name == current_high.name



def test_adept_shop_synthesis_uses_full_price_without_consuming_discounts(cards):
    tarven = _tarven(cards, "使徒")
    card = next(card for card in cards if card.level == 1)
    _place(tarven, card, 0)
    _place(tarven, card, 1)
    tarven.shop[0] = card
    tarven.mineral = 3
    tarven.hero_controller.state["purchases_this_round"] = 2
    tarven.hero_controller.state["adept_one_cost_charges"] = 1

    assert tarven.action(SynthesisAction(shop_idx=0))
    assert tarven.mineral == 0
    assert tarven.hero_controller.state["purchases_this_round"] == 2
    assert tarven.hero_controller.state["adept_one_cost_charges"] == 1


def test_tampered_hero_choice_is_rejected_and_restores_pool(cards):
    tarven = _tarven(cards, "陆战队员")
    tarven.level = 2
    tarven.mineral = 2
    before = _pool_size(tarven.pool)
    assert tarven.action(HeroPowerAction())
    choice = tarven.force_action[0]
    forged = _blank_card("伪造奖励", level=6)
    choice.options[:] = [forged]
    choice.selected = forged

    assert not tarven.action(choice)
    assert choice not in tarven.force_action
    assert not _cache_has(tarven, forged)
    assert _pool_size(tarven.pool) == before


@pytest.mark.parametrize(
    "heroes",
    [
        ["工蜂", "工蜂"],
        ["泰凯斯", "default"],
        ["凯瑞甘（异虫形态）", "default"],
    ],
)
def test_direct_game_constructor_enforces_hero_assignment(cards, heroes):
    pool = CardPool(cards)
    engine = CardEngine(cards)
    with pytest.raises(ValueError):
        Game(pool, engine, user_count=2, heroes=heroes)
