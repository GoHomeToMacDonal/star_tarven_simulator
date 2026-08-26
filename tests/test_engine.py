"""引擎与解析的端到端冒烟测试。

目标：
1. 全部 155 张卡牌都能加载、解析并达到既定覆盖率。
2. 把每张卡牌（含金色版本）放入酒馆并触发所有事件，确保没有 handler 崩溃。
3. 针对若干机制做定点断言（反应堆生产、任务奖励、注卵、黑暗值、三连合成换金色 handler）。
"""

from __future__ import annotations

import random

import pytest

from star_tarven_simulator.loader import build_game, load_cards
from star_tarven_simulator.simulator.card_engine import CardEngine
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


def _fresh_tarven(cards) -> Tarven:
    game = build_game(cards, user_count=1)
    return game.tarvens[0]


def _place(tarven: Tarven, card, idx: int) -> Slot:
    tarven.slots[idx] = Slot(idx, tarven)
    tarven.card_engine.assign_card_to_slot(card, tarven.slots[idx])
    return tarven.slots[idx]


def test_all_cards_load(loaded):
    cards, _ = loaded
    assert len(cards) == 155


def test_coverage_threshold(loaded):
    _, coverage = loaded
    # 参数化 + 注册表当前覆盖率应稳定在 99% 以上
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


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
