"""引擎与解析的端到端冒烟测试。

目标：
1. 全部 154 张卡牌都能加载、解析并达到既定覆盖率。
2. 把每张卡牌（含金色版本）放入酒馆并触发所有事件，确保没有 handler 崩溃。
3. 针对若干机制做定点断言（反应堆生产、任务奖励、注卵、黑暗值、三连合成换金色 handler）。
"""

from __future__ import annotations

import random

import pytest

from star_tarven_simulator.cards import resolve
from star_tarven_simulator.cards.registry import (
    ACTION_HANDLERS,
    register,
    register_value,
)
from star_tarven_simulator.constants.tarven import (
    TARVEN_MAX_LEVEL,
    TARVEN_UPGRADE_COST,
)
from star_tarven_simulator.loader import build_game, load_cards
from star_tarven_simulator.simulator.action import (
    ChooseSynthesisAction,
    ChooseUpgradeAction,
    DeployAction,
    UpgradeAction,
    UpgradeTarvenAction,
)
from star_tarven_simulator.simulator.card_engine import CardEngine
from star_tarven_simulator.simulator.event_handler import TaskActionHandler
from star_tarven_simulator.upgrades import available_upgrades, definition_map
from star_tarven_simulator.simulator.event import (
    AnyCardAddonChangedEvent,
    AnyCardEnteredEvent,
    AnyCardGainVoidCrystalTowerEvent,
    AnyCardHatchEvent,
    AnyCardLarvaEvent,
    AnyCardSoldEvent,
    AnyCardTeleportEvent,
    AnyTaskFinishedEvent,
    DeploymentEvent,
    EnteringEvent,
    GainDarknessEvent,
    LevelUpEvent,
    OtherPlayerSoldHeroCardEvent,
    QuickProduceEvent,
    RefreshEvent,
    RoundEndEvent,
    RoundStartEvent,
    RoundWinEvent,
    SellingEvent,
    UpgradeEvent,
)
from star_tarven_simulator.simulator.game import Game, Tarven
from star_tarven_simulator.simulator.slot import Slot


@pytest.fixture(scope="module")
def loaded():
    cards, coverage = load_cards()
    return cards, coverage


@pytest.fixture(scope="module")
def cards(loaded):
    return loaded[0]


def _fresh_tarven(cards) -> Tarven:
    game = build_game(cards, user_count=1)
    return game.tarvens[0]


def _place(tarven: Tarven, card, idx: int) -> Slot:
    tarven.slots[idx] = Slot(idx, tarven)
    tarven.card_engine.assign_card_to_slot(card, tarven.slots[idx])
    return tarven.slots[idx]


def test_all_cards_load(loaded):
    cards, _ = loaded
    assert len(cards) == 154


def test_coverage_threshold(loaded):
    _, coverage = loaded
    # 覆盖率门槛为 95%（与下方断言一致；勿声称高于实际断言值）
    assert coverage.rate >= 0.95, coverage.summary()


def _events_for(tarven: Tarven, slot: Slot):
    """构造一批覆盖所有事件类型的事件，触发方指向 slot。"""
    return [
        RoundStartEvent(tarven),
        RoundEndEvent(tarven),
        RoundWinEvent(tarven),
        EnteringEvent(tarven, slot),
        SellingEvent(tarven, slot),
        AnyCardEnteredEvent(tarven, slot),
        AnyCardSoldEvent(tarven, slot),
        GainDarknessEvent(tarven, slot, 3),
        UpgradeEvent(tarven, slot, "聚能器"),
        RefreshEvent(tarven),
        LevelUpEvent(tarven, 5),
        QuickProduceEvent(tarven, slot),
        AnyCardAddonChangedEvent(tarven, slot),
        AnyTaskFinishedEvent(tarven, slot),
        AnyCardLarvaEvent(tarven, slot),
        AnyCardHatchEvent(tarven, slot, {"跳虫": 2}),
        AnyCardTeleportEvent(tarven, slot),
        AnyCardGainVoidCrystalTowerEvent(tarven, slot),
        DeploymentEvent(tarven, slot),
        OtherPlayerSoldHeroCardEvent(tarven, slot),
    ]


def test_every_card_handlers_do_not_crash(loaded):
    """把每张卡牌（普通 + 金色）放入酒馆并触发全部事件，确保无异常。"""
    cards, _ = loaded
    random.seed(0)

    for card in cards:
        for gold in (False, True):
            tarven = _fresh_tarven(cards)
            # 在中间槽放置目标卡，两侧各放一张同种族/异种族卡，制造相邻关系
            slot = _place(tarven, card, 3)
            if gold:
                slot.tags.add("金色")
                # 用金色 handler 替换普通 handler
                slot.event_handlers = [
                    h.copy(tarven, slot) for h in card.gold_event_handlers
                ]
            neighbor = cards[(card.uuid + 1) % len(cards)]
            _place(tarven, neighbor, 2)
            _place(tarven, neighbor, 4)
            tarven.mineral = 5
            tarven.gas = 5
            slot.darkness = 6

            for event in _events_for(tarven, slot):
                # 只对拥有对应 handler 的槽派发（模拟引擎行为）
                slot.trigger([event])
                tarven.trigger_any_card_event(event)


def test_reactor_production():
    """好兄弟：反应堆生产陆战队员 -> 回合结束 +1 陆战队员。"""
    cards, _ = load_cards()
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    slot = _place(tarven, card_map["好兄弟"], 3)
    before = slot.count("陆战队员")
    slot.trigger([RoundEndEvent(tarven)])
    assert slot.count("陆战队员") == before + 1


def test_task_reward_mineral():
    """死神火车：任务 让1张卡牌进场 -> 奖励 获得1晶体矿。"""
    cards, _ = load_cards()
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    slot = _place(tarven, card_map["死神火车"], 3)
    tarven.mineral = 0
    # 进场一张其它卡以推进任务
    other = _place(tarven, card_map["好兄弟"], 4)
    tarven.trigger_entering(other)
    assert tarven.mineral == 1


def test_entering_effect_resolves_before_other_cards_observe_entry(loaded):
    """艾尔之刃先给相邻卡加水晶塔，发电站随后应能将新塔转为虚空塔。"""
    cards, _ = loaded
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    power_station = _place(tarven, card_map["发电站"], 0)
    target = _place(tarven, card_map["万叉奔腾"], 1)

    for slot in (power_station, target):
        pylons = slot.count("水晶塔")
        slot.remove_unit("水晶塔", pylons)
        slot.add_unit("虚空水晶塔", pylons)

    entering = _place(tarven, card_map["艾尔之刃"], 2)
    tarven.trigger_entering(entering)

    assert target.count("水晶塔") == 0
    assert target.count("虚空水晶塔") == 2


def test_swarm_condition():
    """虫群先锋：集群(1) 获得跳虫（自身即虫族，满足 >=1）。"""
    cards, _ = load_cards()
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    slot = _place(tarven, card_map["虫群先锋"], 3)
    before = slot.count("跳虫")
    slot.trigger([RoundEndEvent(tarven)])
    assert slot.count("跳虫") == before + 2


def test_gold_handlers_populated(loaded):
    """金色描述应被解析为 gold_event_handlers（旧版从未填充）。"""
    cards, _ = loaded
    card_map = {c.name: c for c in cards}
    # 好兄弟金色描述含"反应堆生产陆战队员"，应有金色 handler
    assert len(card_map["好兄弟"].gold_event_handlers) > 0


def test_merge_swaps_to_gold():
    """三连合成后使用金色 handler：好兄弟金色反应堆生产 +2 陆战队员。"""
    cards, _ = load_cards()
    card_map = {c.name: c for c in cards}
    engine: CardEngine = CardEngine(cards)
    game = Game(build_game(cards, 1).pool, engine, user_count=1)
    tarven = game.tarvens[0]

    left = _place(tarven, card_map["好兄弟"], 3)
    right = _place(tarven, card_map["好兄弟"], 4)
    engine.merge_slots(left, right)

    assert left.tags.has("金色")
    before = left.count("陆战队员")
    left.trigger([RoundEndEvent(tarven)])
    # 金色：1 + has("金色") = 2
    assert left.count("陆战队员") == before + 2


def test_larva_creates_egg():
    """注卵：Tarven.larva(dict) 生成虫卵并注入单位。"""
    cards, _ = load_cards()
    tarven = _fresh_tarven(cards)
    tarven.larva({"蟑螂": 2})
    eggs = [s for s in tarven.slots if s.card_type == "虫卵"]
    assert len(eggs) == 1
    assert eggs[0].count("蟑螂") == 2


def test_hatchery_copies_last_larva_unit(loaded):
    """孵化所额外孵化最后注入虫卵的单位：普通 +2，金色 +3。"""
    cards, _ = loaded
    card_map = {c.name: c for c in cards}

    for gold, bonus in ((False, 2), (True, 3)):
        tarven = _fresh_tarven(cards)
        hatchery = _place(tarven, card_map["孵化所"], 1)
        if gold:
            tarven.card_engine.make_gold(hatchery)
        tarven.card_engine.assign_card_to_slot("虫卵", tarven.slots[2])
        right = _place(tarven, card_map["注卵虫后"], 3)

        # 同一次注卵调用中刺蛇最后；孵化所应在基础复制外再获得 bonus 个刺蛇。
        tarven.larva({"蟑螂": 1, "刺蛇": 1})
        before_roach = hatchery.count("蟑螂")
        before_hydra = hatchery.count("刺蛇")
        tarven.slots[2].trigger([RoundStartEvent(tarven)])

        assert hatchery.count("蟑螂") == before_roach + 1
        assert hatchery.count("刺蛇") == before_hydra + 1 + bonus
        assert right.count("蟑螂") >= 1
        assert right.count("刺蛇") >= 1
        assert tarven.last_larva_unit is None


def test_hatchery_tracks_last_larva_call(loaded):
    """多次注卵时，以最后一次调用的最后一个单位作为孵化所额外产物。"""
    cards, _ = loaded
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    hatchery = _place(tarven, card_map["孵化所"], 1)
    tarven.card_engine.assign_card_to_slot("虫卵", tarven.slots[2])
    _place(tarven, card_map["注卵虫后"], 3)

    tarven.larva({"雷兽": 1})
    tarven.larva({"蟑螂": 1, "刺蛇": 1})
    before = dict(hatchery.units)
    tarven.slots[2].trigger([RoundStartEvent(tarven)])

    assert hatchery.count("雷兽") == before.get("雷兽", 0) + 1
    assert hatchery.count("蟑螂") == before.get("蟑螂", 0) + 1
    assert hatchery.count("刺蛇") == before.get("刺蛇", 0) + 3


def test_same_addon_counts_whole_board(loaded):
    cards, _ = loaded
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    owner = _place(tarven, card_map["恶火小队"], 0)
    for idx in range(1, 5):
        slot = _place(tarven, card_map["好兄弟"], idx)
        slot.units = {"反应堆": 1}
        slot.unit_count = 1
    before = owner.count("恶蝠游骑兵")
    owner.trigger([RoundEndEvent(tarven)])
    assert owner.count("恶蝠游骑兵") == before + 1


def test_infested_conversion_affects_every_card(loaded):
    cards, _ = loaded
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    owner = _place(tarven, card_map["感染深渊"], 0)
    other = _place(tarven, card_map["好兄弟"], 1)
    other.add_unit("被感染的陆战队员", 2)
    owner.trigger([RoundStartEvent(tarven)])
    assert other.count("被感染的陆战队员") == 1
    assert other.count("畸变体") == 1


def test_swarm_seize_moves_upgrades(loaded):
    cards, _ = loaded
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    owner = _place(tarven, card_map["弱肉强食"], 0)
    for idx in range(1, 7):
        zerg = _place(tarven, card_map["虫群先锋"], idx)
        if idx == 1:
            zerg.upgrades.append("测试升级")
    owner.trigger([RoundEndEvent(tarven)])
    assert "测试升级" in owner.upgrades


def test_void_projection_uses_static_tag(loaded):
    cards, _ = loaded
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    owner = _place(tarven, card_map["聚铁成兵"], 0)
    before = owner.count("零件")
    owner.trigger([RoundEndEvent(tarven)])
    assert owner.count("零件") == before + 1


def test_dehaka_clone_uses_canonical_unit_name(loaded):
    cards, _ = loaded
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    owner = _place(tarven, card_map["德哈卡"], 0)
    sold = _place(tarven, card_map["好兄弟"], 1)
    sold.add_unit("精华", 3)
    owner.trigger([AnyCardSoldEvent(tarven, sold)])
    assert owner.count("德哈卡分身") == 5
    assert owner.count("德哈卡的分身") == 0


def test_void_tower_bonus_disappears_with_card(loaded):
    cards, _ = loaded
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    owner = _place(tarven, card_map["一鼓作气"], 0)
    target = _place(tarven, card_map["好兄弟"], 1)
    target.add_unit("虚空水晶塔", 1)
    assert target.energy >= 2
    tarven.destroy(owner)
    assert target.energy == target.count("水晶塔") + target.count("虚空水晶塔")


def test_upgrade_catalog_and_race_specific_discovery(loaded):
    cards, _ = loaded
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    protoss = _place(tarven, card_map["万叉奔腾"], 0)
    terran = _place(tarven, card_map["好兄弟"], 1)
    plain_neutral = _place(tarven, card_map["酒馆后勤处"], 2)
    primal = _place(tarven, card_map["原始蟑螂"], 3)
    void_projection = _place(tarven, card_map["虚空大军"], 4)

    assert len(definition_map()) == 40
    assert "聚能器" in available_upgrades(protoss)
    assert "火力压制" not in available_upgrades(protoss)
    assert "火力压制" in available_upgrades(terran)
    assert "聚能器" not in available_upgrades(terran)
    assert "原始甲壳" not in available_upgrades(plain_neutral)
    assert "虚空能量" not in available_upgrades(plain_neutral)
    assert "原始甲壳" in available_upgrades(primal)
    assert "虚空能量" not in available_upgrades(primal)
    assert "虚空能量" in available_upgrades(void_projection)
    assert "原始甲壳" not in available_upgrades(void_projection)


def test_spending_gas_discovers_three_unowned_upgrades(loaded):
    cards, _ = loaded
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    target = _place(tarven, card_map["万叉奔腾"], 0)
    target.upgrades.append("聚能器")
    tarven.gas = 4

    assert tarven.action(UpgradeAction(slot_idx=0))
    assert tarven.gas == 2
    choice = tarven.force_action[0]
    assert isinstance(choice, ChooseUpgradeAction)
    assert len(choice.upgrade_names) == 3
    assert "聚能器" not in choice.upgrade_names
    choice.selected_upgrade_name = choice.upgrade_names[0]
    assert tarven.action(choice)
    assert choice.selected_upgrade_name in target.upgrades


def test_equivalent_power_applies_multiplicative_upgrade_factors(loaded):
    cards, _ = loaded
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    target = _place(tarven, card_map["万叉奔腾"], 0)
    base = target.price()

    assert tarven.trigger_upgrade(target, "灼热打击")
    assert tarven.trigger_upgrade(target, "电磁加速器")
    assert target.equivalent_power() == pytest.approx(base * 1.2 * 1.2)
    assert tarven.total_equivalent_power() == pytest.approx(target.equivalent_power())
    assert tarven.total_power() == base


def test_instant_upgrade_effects_add_real_units(loaded):
    cards, _ = loaded
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    target = _place(tarven, card_map["好兄弟"], 0)
    before = target.count("修理无人机")
    tarven.level = 4

    assert tarven.trigger_upgrade(target, "修理无人机")
    assert target.count("修理无人机") == before + 7


def test_warp_reinforcements_grants_units_and_cannot_repeat(loaded):
    cards, _ = loaded
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    target = _place(tarven, card_map["万叉奔腾"], 0)

    before_pylons = target.count("水晶塔")
    before_templars = target.count("高阶圣堂武士")
    assert tarven.trigger_upgrade(target, "折跃援军")
    assert target.upgrades.count("折跃援军") == 1
    assert target.count("水晶塔") == before_pylons + 3
    assert target.count("高阶圣堂武士") == before_templars + 2

    assert not tarven.trigger_upgrade(target, "折跃援军")
    assert target.upgrades.count("折跃援军") == 1
    assert target.count("水晶塔") == before_pylons + 3
    assert target.count("高阶圣堂武士") == before_templars + 2


def test_selling_warp_reinforcements_spreads_to_random_eligible_protoss(loaded):
    cards, _ = loaded
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    source = _place(tarven, card_map["万叉奔腾"], 0)
    ineligible = _place(tarven, card_map["万叉奔腾"], 1)
    target = _place(tarven, card_map["万叉奔腾"], 2)
    _place(tarven, card_map["好兄弟"], 3)
    tarven.trigger_upgrade(source, "折跃援军")
    tarven.trigger_upgrade(ineligible, "折跃援军")
    before_pylons = target.count("水晶塔")
    before_templars = target.count("高阶圣堂武士")
    tarven.gas = 2

    tarven.trigger_selling(source)

    assert tarven.gas == 1
    assert "折跃援军" in target.upgrades
    assert target.count("水晶塔") == before_pylons + 3
    assert target.count("高阶圣堂武士") >= before_templars + 2
    # JSON 最新规则还会复制出售卡中的生物单位。
    assert target.count("高阶圣堂武士") >= source.count("高阶圣堂武士")


def test_selling_warp_reinforcements_without_target_does_not_spend_gas(loaded):
    cards, _ = loaded
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    source = _place(tarven, card_map["万叉奔腾"], 0)
    existing = _place(tarven, card_map["万叉奔腾"], 1)
    _place(tarven, card_map["好兄弟"], 2)
    tarven.trigger_upgrade(source, "折跃援军")
    tarven.trigger_upgrade(existing, "折跃援军")
    tarven.gas = 2

    tarven.trigger_selling(source)

    assert tarven.gas == 2
    assert existing.upgrades.count("折跃援军") == 1


def test_selling_void_towers_prefers_protoss_card_on_left(loaded):
    cards, _ = loaded
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    left = _place(tarven, card_map["万叉奔腾"], 1)
    sold = _place(tarven, card_map["好兄弟"], 2)
    right = _place(tarven, card_map["发电站"], 3)
    sold.add_unit("虚空水晶塔", 3)
    left_before = left.count("虚空水晶塔")
    right_before = right.count("虚空水晶塔")

    tarven.trigger_selling(sold)

    assert left.count("虚空水晶塔") == left_before + 3
    assert right.count("虚空水晶塔") == right_before


def test_selling_void_towers_falls_back_to_protoss_card_on_right(loaded):
    cards, _ = loaded
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    left = _place(tarven, card_map["好兄弟"], 1)
    sold = _place(tarven, card_map["死神火车"], 2)
    right = _place(tarven, card_map["万叉奔腾"], 3)
    sold.add_unit("虚空水晶塔", 4)
    left_before = left.count("虚空水晶塔")
    right_before = right.count("虚空水晶塔")

    tarven.trigger_selling(sold)

    assert left.count("虚空水晶塔") == left_before
    assert right.count("虚空水晶塔") == right_before + 4


def test_selling_void_towers_has_no_target_without_adjacent_protoss(loaded):
    cards, _ = loaded
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    left = _place(tarven, card_map["好兄弟"], 1)
    sold = _place(tarven, card_map["死神火车"], 2)
    right = _place(tarven, card_map["虫群先锋"], 3)
    sold.add_unit("虚空水晶塔", 2)
    left_before = left.count("虚空水晶塔")
    right_before = right.count("虚空水晶塔")

    tarven.trigger_selling(sold)

    assert left.count("虚空水晶塔") == left_before
    assert right.count("虚空水晶塔") == right_before


def test_valhalla_copies_hero(loaded):
    cards, _ = loaded
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    owner = _place(tarven, card_map["英灵殿"], 0)
    source = Slot(0, tarven)
    source.card_type = "来源"
    source.add_unit("阿拉纳克", 1)
    owner.trigger([OtherPlayerSoldHeroCardEvent(tarven, source)])
    assert owner.count("阿拉纳克") == 1
    assert source.count("阿拉纳克") == 1


# ---------------------------------------------------------------------------
# 已裁决修复的回归测试
# ---------------------------------------------------------------------------
def test_task_single_reward_then_idle_until_reset(cards):
    """一次性任务：达成 goal 只奖励一次，后续触发不再奖励；reset 后恢复可计数。"""
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    slot = _place(tarven, card_map["死神火车"], 3)
    tarven.mineral = 0

    other = _place(tarven, card_map["好兄弟"], 4)
    tarven.trigger_entering(other)
    assert tarven.mineral == 1

    # 再次触发不再奖励（旧实现会重复奖励）
    other2 = _place(tarven, card_map["好兄弟"], 5)
    tarven.trigger_entering(other2)
    assert tarven.mineral == 1

    task = next(
        h.action_handler
        for h in slot.event_handlers
        if isinstance(h.action_handler, TaskActionHandler)
    )
    assert task.is_finished()

    # reset 后恢复可计数
    task.reset()
    other3 = _place(tarven, card_map["好兄弟"], 6)
    tarven.trigger_entering(other3)
    assert tarven.mineral == 2


def test_task_auto_reset_keeps_looping(cards):
    """auto_reset 任务：每 goal 次触发奖励一次并归零，可持续循环。"""
    tarven = _fresh_tarven(cards)
    calls = []

    def reward(slot, event):
        calls.append(1)

    handler = TaskActionHandler(reward, 2, auto_reset=True)
    slot = Slot(0, tarven)
    event = RoundEndEvent(tarven)

    handler(slot, event)  # 1/2
    handler(slot, event)  # 2/2 -> 奖励并归零
    assert len(calls) == 1
    assert handler.counter == 0
    assert not handler.is_finished()

    handler(slot, event)  # 1/2
    handler(slot, event)  # 2/2 -> 再次奖励
    assert len(calls) == 2
    assert handler.counter == 0
    assert not handler.is_finished()


def test_task_handler_copy_is_fresh_uncompleted(cards):
    """TaskActionHandler.copy() 创建全新未完成实例（计数器 / 完成态均隔离）。"""
    tarven = _fresh_tarven(cards)
    calls = []

    def reward(slot, event):
        calls.append(1)

    handler = TaskActionHandler(reward, 2)
    slot = Slot(0, tarven)
    event = RoundEndEvent(tarven)
    handler(slot, event)
    handler(slot, event)
    assert len(calls) == 1
    assert handler.is_finished()

    clone = handler.copy()
    assert clone is not handler
    assert clone.counter == 0
    assert not clone.is_finished()
    clone(slot, event)  # 1/2
    clone(slot, event)  # 2/2 -> 副本独立完成并奖励
    assert len(calls) == 2
    assert clone.is_finished()
    assert handler.is_finished()  # 原实例不受副本影响


def test_upgrade_tarven_rejected_at_max_level(cards):
    """六本时升级动作被拒绝：不扣矿、不改状态；5->6 仍正常（回归）。"""
    tarven = _fresh_tarven(cards)
    tarven.level = TARVEN_MAX_LEVEL
    tarven.level_up_cost = 0
    tarven.mineral = 100
    assert not tarven.action(UpgradeTarvenAction())
    assert tarven.level == TARVEN_MAX_LEVEL
    assert tarven.mineral == 100

    tarven.level = TARVEN_MAX_LEVEL - 1
    tarven.level_up_cost = TARVEN_UPGRADE_COST[TARVEN_MAX_LEVEL]
    tarven.mineral = TARVEN_UPGRADE_COST[TARVEN_MAX_LEVEL]
    assert tarven.action(UpgradeTarvenAction())
    assert tarven.level == TARVEN_MAX_LEVEL
    assert tarven.mineral == 0


def test_deploy_requires_exactly_one_source(cards):
    """定点部署来源严格二选一（XOR）：双来源或空来源均被拒绝且不消耗。"""
    tarven = _fresh_tarven(cards)
    card_map = {c.name: c for c in cards}
    _place(tarven, card_map["好兄弟"], 3)
    aux = card_map["矿簇"]  # "部署时,获得1晶体矿" 的辅助卡
    assert any(h.event_name == DeploymentEvent.event_name for h in aux.event_handlers)

    tarven.cache[0] = aux
    tarven.shop[0] = aux
    tarven.mineral = 5
    price = tarven.card_price(0)

    # 两个来源都给出 -> 拒绝，且不消耗任何一边
    assert not tarven.action(DeployAction(slot_idx=3, cache_idx=0, shop_idx=0))
    assert tarven.cache[0] is aux
    assert tarven.shop[0] is aux
    assert tarven.mineral == 5

    # 两个来源都缺失 -> 拒绝
    assert not tarven.action(DeployAction(slot_idx=3))
    assert tarven.cache[0] is aux
    assert tarven.shop[0] is aux

    # 仅暂存区来源 -> 成功并消耗暂存区（部署效果 +1 晶体矿）
    assert tarven.action(DeployAction(slot_idx=3, cache_idx=0))
    assert tarven.cache[0] is None
    assert tarven.shop[0] is aux
    assert tarven.mineral == 6

    # 仅商店来源 -> 成功并扣费、清商店位
    tarven.cache[0] = aux
    tarven.mineral = 5
    assert tarven.action(DeployAction(slot_idx=3, shop_idx=0))
    assert tarven.shop[0] is None
    assert tarven.cache[0] is aux
    assert tarven.mineral == 5 - price + 1


def test_larva_full_field_without_egg_keeps_last_larva_unit(cards):
    """场满且无虫卵时注卵无效：不新增虫卵，也不更新 last_larva_unit。"""
    tarven = _fresh_tarven(cards)
    card_map = {c.name: c for c in cards}
    for idx in range(7):
        _place(tarven, card_map["好兄弟"], idx)
    tarven.last_larva_unit = "刺蛇"  # 此前注卵留下的记录
    tarven.larva({"蟑螂": 1})
    assert tarven.last_larva_unit == "刺蛇"
    assert not any(s.card_type == "虫卵" for s in tarven.slots)


def test_larva_updates_last_larva_unit_on_success(cards):
    """注卵真正生效（创建虫卵 / 复用虫卵）时才更新 last_larva_unit。"""
    tarven = _fresh_tarven(cards)
    tarven.larva({"蟑螂": 1, "刺蛇": 2})
    assert tarven.last_larva_unit == "刺蛇"
    eggs = [s for s in tarven.slots if s.card_type == "虫卵"]
    assert len(eggs) == 1
    assert eggs[0].count("蟑螂") == 1
    assert eggs[0].count("刺蛇") == 2

    # 复用虫卵：最后一次调用覆盖记录
    tarven.larva({"雷兽": 1})
    assert tarven.last_larva_unit == "雷兽"


def _one_star_reward_task():
    """构造参数化 '任务:让1张卡牌进场 奖励:随机获得1张一星卡牌' 的 TaskActionHandler。"""
    resolved = resolve("任务:让1张卡牌进场 奖励:随机获得1张一星卡牌")
    assert resolved is not None, "参数化应解析出随机一星奖励任务"
    event_name, handler = resolved
    assert event_name == "any_card_entered"
    assert isinstance(handler, TaskActionHandler)
    return handler


def _single_star_card(tarven: Tarven):
    """从可抽取的 1 星卡中取一张，并把 1 星桶收窄到只剩它（测试确定性）。"""
    star = next(
        c
        for c in tarven.pool.cards
        if c.level == 1 and c.uuid not in tarven.pool.no_draw_uuids
    )
    tarven.pool.set_bucket(1, [star.uuid])
    return star


def test_random_one_star_reward_cache_priority(cards):
    """随机一星奖励：缓存有空位时优先入缓存，且保存 Card 实体而非 name。"""
    tarven = _fresh_tarven(cards)
    star = _single_star_card(tarven)
    before = tarven.pool.count(star)  # set_bucket 收窄后初始份数
    handler = _one_star_reward_task()
    handler(Slot(0, tarven), RoundEndEvent(tarven))

    assert any(c is star for c in tarven.cache), "奖励卡应作为实体存入缓存"
    assert tarven.pool.count(star) == before - 1  # 抽走 1 份


def test_random_one_star_reward_forced_entry_when_cache_full(cards):
    """随机一星奖励：缓存满时强制进场到空槽。"""
    tarven = _fresh_tarven(cards)
    star = _single_star_card(tarven)
    before = tarven.pool.count(star)  # set_bucket 收窄后初始份数
    tarven.cache[:] = ["占位"] * 6
    for idx in (0, 1, 2, 4, 5, 6):
        s = Slot(idx, tarven)
        s.card_type = "占位"
        tarven.slots[idx] = s
    assert tarven.slots[3].card_type is None

    handler = _one_star_reward_task()
    handler(Slot(0, tarven), RoundEndEvent(tarven))

    assert tarven.slots[3].card_type == star.name
    assert all(tarven.cache[i] == "占位" for i in range(6))
    assert tarven.pool.count(star) == before - 1  # 抽走 1 份


def test_random_one_star_reward_forces_triple(cards):
    """随机一星奖励：缓存满且场上有同名对子时走强制三连。"""
    tarven = _fresh_tarven(cards)
    star = _single_star_card(tarven)
    before = tarven.pool.count(star)  # set_bucket 收窄后初始份数
    tarven.cache[:] = ["占位"] * 6
    _place(tarven, star, 0)
    _place(tarven, star, 1)

    handler = _one_star_reward_task()
    handler(Slot(0, tarven), RoundEndEvent(tarven))

    assert len(tarven.force_action) == 1
    assert isinstance(tarven.force_action[0], ChooseSynthesisAction)
    assert tarven.pool.count(star) == before - 1  # 抽走 1 份


def test_random_one_star_reward_returns_to_pool_when_full(cards):
    """随机一星奖励：缓存满、场满且无法三连时，把原 Card 放回卡池。"""
    tarven = _fresh_tarven(cards)
    star = _single_star_card(tarven)
    before = tarven.pool.count(star)  # set_bucket 收窄后初始份数
    tarven.cache[:] = ["占位"] * 6
    _place(tarven, star, 0)  # 场上只有 1 张，无法三连
    for idx in range(1, 7):
        s = Slot(idx, tarven)
        s.card_type = "占位"
        tarven.slots[idx] = s

    handler = _one_star_reward_task()
    handler(Slot(0, tarven), RoundEndEvent(tarven))

    assert tarven.pool.count(star) == before  # 抽走一份后又归还
    assert all(tarven.cache[i] == "占位" for i in range(6))
    assert len([s for s in tarven.slots if s.card_type == star.name]) == 1


def test_registry_rejects_duplicate_keys():
    """ACTION_HANDLERS 重复 key 必须抛 ValueError（信息含 description），禁止静默覆盖。"""
    key = "测试:重复注册key"
    assert key not in ACTION_HANDLERS

    def _noop(slot, event):
        return None

    try:
        register_value(key, "round_end", _noop)
        assert key in ACTION_HANDLERS

        with pytest.raises(ValueError) as exc:
            register_value(key, "round_end", _noop)
        assert key in str(exc.value)

        with pytest.raises(ValueError) as exc:
            register(key, "round_end")(_noop)
        assert key in str(exc.value)

        # 原注册项未被覆盖
        assert ACTION_HANDLERS[key] == ("round_end", _noop)
    finally:
        ACTION_HANDLERS.pop(key, None)
    assert key not in ACTION_HANDLERS


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
