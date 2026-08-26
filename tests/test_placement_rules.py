"""进场规则回归测试：虫卵在场唯一 + 第 3 张同名卡强制三连。

覆盖（两个 bug 的两种来源：商店购买进场 / 暂存区进场，以及直接进场路径）：

1. ``虫卵`` 在场上最多只能出现一张：
   - 购买直接进场被拒绝（``BuyAction(slot_idx=...)``）；
   - 暂存区进场被拒绝（``CacheEnterAction``）；
   - ``enter_card_direct`` 直接进场被拒绝（延迟进场 / 直接奖励路径）；
   - 买进暂存区仍合法（暂存区不是场）；
   - ``larva`` 重复注卵复用同一张虫卵。

2. 第 3 张同名、非金色、同等级卡进场必须强制三连：
   - 商店购买直接进场触发合成选择，而不是普通放置；
   - 暂存区进场触发合成选择，而不是普通放置；
   - 买进暂存区不触发（囤货合法）；
   - 场上只有 1 张 / 对子含金色 / 对子带 ``无法三连`` / 两张等级不同时，
     仍走普通进场（不满足三连条件）；
   - 显式 ``SynthesisAction`` 行为不变（回归）。
"""

from __future__ import annotations

import pytest

from star_tarven_simulator.loader import build_game, load_cards
from star_tarven_simulator.simulator.action import (
    BuyAction,
    CacheEnterAction,
    ChooseSynthesisAction,
    SynthesisAction,
)
from star_tarven_simulator.simulator.card import Card
from star_tarven_simulator.simulator.game import Tarven
from star_tarven_simulator.simulator.slot import Slot


@pytest.fixture(scope="module")
def cards():
    return load_cards()[0]


def _tarven(cards) -> Tarven:
    return build_game(cards, user_count=1).tarvens[0]


def _card_map(cards):
    return {c.name: c for c in cards}


def _place(tarven: Tarven, card, idx: int) -> Slot:
    tarven.slots[idx] = Slot(idx, tarven)
    tarven.card_engine.assign_card_to_slot(card, tarven.slots[idx])
    return tarven.slots[idx]


def _egg_card() -> Card:
    """构造一张可购买的"虫卵"卡（当前卡池数据中没有虫卵卡，测试直接构造）。"""
    return Card(
        uuid=990001,
        name="虫卵",
        level=1,
        race="zerg",
        description=[],
        gold_description=[],
        units={"跳虫": 1},
        tags=["zerg"],
        gold_tags=["zerg"],
        source=[],
        derived=True,
    )


def _field_eggs(tarven: Tarven) -> list:
    return [s for s in tarven.slots if s.card_type == "虫卵"]


def _resolve_synthesis(tarven: Tarven) -> None:
    """结算当前的 ChooseSynthesisAction：选第一个候选（一张随机卡）。"""
    fa = tarven.force_action[0]
    assert isinstance(fa, ChooseSynthesisAction)
    fa.selected = fa.options[0]
    assert tarven.action(fa) is True


# ---------------------------------------------------------------------------
# Bug 1：虫卵在场最多一张
# ---------------------------------------------------------------------------
def test_egg_limit_buy_entry_rejected_when_egg_on_field(cards):
    """场上已有虫卵时，从商店购买虫卵直接进场被拒绝，场上的虫卵数不变。"""
    t = _tarven(cards)
    t.larva({"跳虫": 1})  # 注卵生成衍生虫卵
    assert len(_field_eggs(t)) == 1

    egg = _egg_card()
    t.shop[0] = egg
    t.mineral = 3
    assert t.available_placement_slots(egg) == []
    assert t.action(BuyAction(shop_idx=0, slot_idx=1)) is False
    assert len(_field_eggs(t)) == 1
    # 拒绝发生在扣费/清商店之前，商店位仍保留
    assert t.shop[0] is egg
    assert t.mineral == 3


def test_egg_limit_cache_enter_rejected_when_egg_on_field(cards):
    """场上已有虫卵时，暂存区的虫卵进场被拒绝，暂存区条目保留。"""
    t = _tarven(cards)
    t.larva({"跳虫": 1})
    egg = _egg_card()
    assert t.store_card_to_cache(egg)
    cache_idx = t.cache.index(egg)
    assert t.action(CacheEnterAction(cache_idx=cache_idx, slot_idx=1)) is False
    assert len(_field_eggs(t)) == 1
    assert t.cache[cache_idx] is egg


def test_egg_limit_enter_card_direct_rejected_when_egg_on_field(cards):
    """直接进场路径（延迟进场 / 直接奖励）同样遵守虫卵在场唯一。"""
    t = _tarven(cards)
    t.larva({"跳虫": 1})
    assert t.enter_card_direct(_egg_card()) is False
    assert len(_field_eggs(t)) == 1


def test_egg_buy_allowed_when_no_egg_on_field(cards):
    """场上没有虫卵时，购买虫卵可以正常进场。"""
    t = _tarven(cards)
    egg = _egg_card()
    t.shop[0] = egg
    t.mineral = 3
    assert t.available_placement_slots(egg) == [0]
    assert t.action(BuyAction(shop_idx=0, slot_idx=0)) is True
    assert len(_field_eggs(t)) == 1


def test_egg_limit_buy_to_cache_allowed_when_egg_on_field(cards):
    """场上已有虫卵时，买第 2 张进暂存区仍合法（暂存区不是场）。"""
    t = _tarven(cards)
    t.larva({"跳虫": 1})
    egg = _egg_card()
    t.shop[0] = egg
    t.mineral = 3
    assert t.action(BuyAction(shop_idx=0, slot_idx=None)) is True
    assert any(c is egg for c in t.cache)
    assert len(_field_eggs(t)) == 1


def test_larva_reuses_existing_egg(cards):
    """重复注卵只会注入同一张虫卵，不会生成第二张。"""
    t = _tarven(cards)
    t.larva({"蟑螂": 2})
    t.larva({"刺蛇": 1})
    eggs = _field_eggs(t)
    assert len(eggs) == 1
    assert eggs[0].count("蟑螂") == 2
    assert eggs[0].count("刺蛇") == 1


# ---------------------------------------------------------------------------
# Bug 2：第 3 张同名非金色同等级卡进场强制三连
# ---------------------------------------------------------------------------
def test_shop_buy_third_copy_forces_triple(cards):
    """商店购买第 3 张同名卡直接进场 -> 强制三连，而不是普通放置。"""
    t = _tarven(cards)
    cm = _card_map(cards)
    _place(t, cm["好兄弟"], 0)
    _place(t, cm["好兄弟"], 1)
    t.shop[0] = cm["好兄弟"]
    t.mineral = 3

    assert t.action(BuyAction(shop_idx=0, slot_idx=2)) is True
    # 进入合成选择；来源卡被消耗（清商店位 + 扣矿），场上没有被放置第 3 张
    assert len(t.force_action) == 1
    assert isinstance(t.force_action[0], ChooseSynthesisAction)
    assert t.shop[0] is None
    assert t.mineral == 0
    assert len([s for s in t.slots if s.card_type == "好兄弟"]) == 2

    # 结算选择 -> 左槽变金色
    _resolve_synthesis(t)
    golds = [s for s in t.slots if s.card_type == "好兄弟" and s.tags.has("金色")]
    assert len(golds) == 1


def test_cache_enter_third_copy_forces_triple(cards):
    """暂存区第 3 张同名卡进场 -> 强制三连，而不是普通放置。"""
    t = _tarven(cards)
    cm = _card_map(cards)
    _place(t, cm["好兄弟"], 0)
    _place(t, cm["好兄弟"], 1)
    assert t.store_card_to_cache(cm["好兄弟"])

    assert t.action(CacheEnterAction(cache_idx=0, slot_idx=2)) is True
    assert len(t.force_action) == 1
    assert isinstance(t.force_action[0], ChooseSynthesisAction)
    assert t.cache[0] is None
    assert len([s for s in t.slots if s.card_type == "好兄弟"]) == 2

    _resolve_synthesis(t)
    golds = [s for s in t.slots if s.card_type == "好兄弟" and s.tags.has("金色")]
    assert len(golds) == 1


def test_third_copy_buy_to_cache_does_not_triple(cards):
    """第 3 张买进暂存区不触发三连（可以囤着，不进场）。"""
    t = _tarven(cards)
    cm = _card_map(cards)
    _place(t, cm["好兄弟"], 0)
    _place(t, cm["好兄弟"], 1)
    t.shop[0] = cm["好兄弟"]
    t.mineral = 3

    assert t.action(BuyAction(shop_idx=0, slot_idx=None)) is True
    assert len(t.force_action) == 0
    assert any(c is cm["好兄弟"] for c in t.cache)
    assert len([s for s in t.slots if s.card_type == "好兄弟"]) == 2


def test_third_copy_normal_placement_when_only_one_on_field(cards):
    """场上只有 1 张时买第 2 张 -> 普通进场，不触发三连。"""
    t = _tarven(cards)
    cm = _card_map(cards)
    _place(t, cm["好兄弟"], 0)
    t.shop[0] = cm["好兄弟"]
    t.mineral = 3

    assert t.action(BuyAction(shop_idx=0, slot_idx=1)) is True
    assert len(t.force_action) == 0
    assert t.slots[1].card_type == "好兄弟"


def test_third_copy_normal_placement_when_pair_is_golden(cards):
    """对子含金色卡时（金色不参与三连），第 3 张仍普通进场。"""
    t = _tarven(cards)
    cm = _card_map(cards)
    _place(t, cm["好兄弟"], 0)
    golden = _place(t, cm["好兄弟"], 1)
    golden.tags.add("金色")
    t.shop[0] = cm["好兄弟"]
    t.mineral = 3

    assert t.action(BuyAction(shop_idx=0, slot_idx=2)) is True
    assert len(t.force_action) == 0
    assert t.slots[2].card_type == "好兄弟"


def test_third_copy_normal_placement_when_pair_is_no_triple_tag(cards):
    """对子带 ``无法三连`` tag 时，第 3 张仍普通进场。"""
    t = _tarven(cards)
    cm = _card_map(cards)
    _place(t, cm["好兄弟"], 0)
    no_triple = _place(t, cm["好兄弟"], 1)
    no_triple.tags.add("无法三连")
    t.shop[0] = cm["好兄弟"]
    t.mineral = 3

    assert t.action(BuyAction(shop_idx=0, slot_idx=2)) is True
    assert len(t.force_action) == 0
    assert t.slots[2].card_type == "好兄弟"


def test_third_copy_normal_placement_when_levels_differ(cards):
    """两张同名卡等级不同时不满足三连，第 3 张普通进场（不再触发 merge 断言）。"""
    t = _tarven(cards)
    cm = _card_map(cards)
    _place(t, cm["好兄弟"], 0)
    other = _place(t, cm["好兄弟"], 1)
    other.level = cm["好兄弟"].level + 1
    t.shop[0] = cm["好兄弟"]
    t.mineral = 3

    assert t.action(BuyAction(shop_idx=0, slot_idx=2)) is True
    assert len(t.force_action) == 0
    assert t.slots[2].card_type == "好兄弟"


def test_explicit_synthesis_still_works(cards):
    """显式 SynthesisAction（商店来源）回归：行为不变。"""
    t = _tarven(cards)
    cm = _card_map(cards)
    _place(t, cm["好兄弟"], 0)
    _place(t, cm["好兄弟"], 1)
    t.shop[0] = cm["好兄弟"]
    t.mineral = 3

    assert t.action(SynthesisAction(shop_idx=0)) is True
    assert len(t.force_action) == 1
    assert isinstance(t.force_action[0], ChooseSynthesisAction)
    assert t.shop[0] is None
    assert t.mineral == 0


def test_explicit_synthesis_from_cache_still_works(cards):
    """显式 SynthesisAction（暂存区来源）回归：行为不变。"""
    t = _tarven(cards)
    cm = _card_map(cards)
    _place(t, cm["好兄弟"], 0)
    _place(t, cm["好兄弟"], 1)
    assert t.store_card_to_cache(cm["好兄弟"])

    assert t.action(SynthesisAction(cache_idx=0)) is True
    assert len(t.force_action) == 1
    assert isinstance(t.force_action[0], ChooseSynthesisAction)
    assert t.cache[0] is None


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
