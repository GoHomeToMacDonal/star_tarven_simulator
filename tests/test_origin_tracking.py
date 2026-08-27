"""出售/摧毁/融合后的公共池来源（origin_cards）可靠追踪测试。

规则：
1. 出售与摧毁都放回构成该实例的原始公共池 Card（每份 = 一份池实体）。
2. 三连金卡代表 3 份原卡：左右槽 origin_cards + 被消费第 3 张的来源随
   ``ChooseSynthesisAction`` 暂存并在结算时合并；第 3 张的单位不并入。
3. 执政官 ``fuse_slots`` 合并两张金卡的双方全部原卡；阿塔尼斯
   ``fuse_card_definition`` 不丢失底牌原卡，也不凭空归还阿塔尼斯。
4. ``Slot.level`` 被降低（放弃智力）后仍按 source Card 的原始等级桶归池。
5. derived / 免费复制 / unknown uuid / no_draw 卡不归池。
6. 暂存区来源元数据（cache_origin）在进场、部署、三连、干扰者替换、
   焦土销毁中不泄漏、不重复。
7. ``enter_card_direct`` 拒绝含 deployment handler 的辅助卡直接常驻进场；
   海盗商人溢出时把原 Card 放回卡池。

说明：测试用 ``set_bucket`` 收窄桶以保证抽取确定性；所有卡池数量断言都是
相对 ``before`` 的增量断言（take -1 / sample -1 / place_back +1），因此与
桶的初始份数无关。
"""

from __future__ import annotations

import pytest

from star_tarven_simulator.cards.registry import ACTION_HANDLERS
from star_tarven_simulator.loader import build_game, load_cards
from star_tarven_simulator.parsing.text import normalize
from star_tarven_simulator.simulator.action import (
    BuyAction,
    CacheEnterAction,
    ChooseSynthesisAction,
    DeployAction,
    HeroPowerAction,
    SellAction,
)
from star_tarven_simulator.simulator.card import Card
from star_tarven_simulator.simulator.event import (
    DeploymentEvent,
    EnteringEvent,
    RoundWinEvent,
    SellingEvent,
)
from star_tarven_simulator.simulator.event_handler import EventHandler
from star_tarven_simulator.simulator.game import Tarven
from star_tarven_simulator.simulator.slot import Slot


@pytest.fixture(scope="module")
def cards():
    return load_cards()[0]


def _tarven(cards, hero: str = "default") -> Tarven:
    return build_game(cards, user_count=1, heroes=[hero]).tarvens[0]


def _card_map(cards):
    return {c.name: c for c in cards}


def _drawable_1star(tarven: Tarven) -> Card:
    """取一张可抽取的 1 星卡（无进场副作用，适合做池算术）。"""
    return next(
        c
        for c in tarven.pool.cards
        if c.level == 1 and c.uuid not in tarven.pool.no_draw_uuids
    )


def _narrow_bucket(tarven: Tarven, cards, copies: int = 4) -> None:
    """把 1 星桶收窄到给定卡牌（各 copies 份），保证抽取确定性。"""
    tarven.pool.set_bucket(1, [c.uuid for c in cards for _ in range(copies)])


def _shop_draw(tarven: Tarven, card: Card, idx: int = 0) -> None:
    """模拟商店抽到该卡：从公共池取出一份放入商店（真实刷新路径）。"""
    assert tarven.pool.take(card) is not None
    tarven.shop[idx] = card


def _place(tarven: Tarven, card: Card, idx: int) -> Slot:
    tarven.slots[idx] = Slot(idx, tarven)
    tarven.card_engine.assign_card_to_slot(card, tarven.slots[idx])
    return tarven.slots[idx]


def _blank_card(
    name: str, *, level: int = 1, race: str = "neutral", units=None, derived: bool = True
) -> Card:
    """构造与公共池无关的卡（负 uuid / derived，place_back 会自动忽略）。"""
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
        source=[],
        derived=derived,
    )


# ---------------------------------------------------------------------------
# 1) 出售 / 摧毁 / 重复摧毁
# ---------------------------------------------------------------------------
def test_sell_returns_pool_copy_and_destroy_restores_exactly_once(cards):
    t = _tarven(cards)
    card = _drawable_1star(t)
    _narrow_bucket(t, [card])
    before = t.pool.count(card)

    # 买一张真实池卡并放置 -> 池少一份，槽位记录一份来源
    _shop_draw(t, card)
    t.mineral = 10
    assert t.action(BuyAction(shop_idx=0, slot_idx=0))
    assert t.pool.count(card) == before - 1
    assert t.slots[0].origin_cards == [card]

    # 出售 -> 份数恢复
    assert t.action(SellAction(slot_idx=0))
    assert t.pool.count(card) == before
    assert t.slots[0].card_type is None

    # 再买一张 -> 摧毁恢复；重复摧毁不重复归还
    _shop_draw(t, card)
    t.mineral = 10
    assert t.action(BuyAction(shop_idx=0, slot_idx=0))
    assert t.pool.count(card) == before - 1
    t.destroy(t.slots[0])
    assert t.pool.count(card) == before
    t.destroy(t.slots[0])
    assert t.pool.count(card) == before


def test_special_discover_sell_path_returns_origin(cards):
    """出售特殊提前返回路径（只发现、不触发其他出售特效）同样归还来源。"""
    t = _tarven(cards)
    card = next(c for c in cards if c.name == "拾荒猎人")
    _narrow_bucket(t, [card], copies=4)
    before = t.pool.count(card)

    _shop_draw(t, card)
    t.mineral = 10
    assert t.action(BuyAction(shop_idx=0, slot_idx=0))
    assert t.slots[0].origin_cards == [card]

    assert t.action(SellAction(slot_idx=0))
    # 归还 1 份，但特殊路径的发现又抽走 3 份：净变化 = -3
    assert t.pool.count(card) == before - 3
    assert t.slots[0].card_type is None
    assert len(t.force_action) == 1  # 发现动作照常入队


def test_scavenger_hunter_selling_handler_present(cards):
    """拾荒猎人（普通/金色）的"只发现、不触发其他出售特效"必须解析为 selling handler。

    回归：这两条效果文本曾被误列入 constants.card_tag.CARD_TAGS，被 cards.is_passive
    当作被动声明直接跳过，导致 event_handlers 为空、Tarven.trigger_selling 的特殊分支
    （按 handler.description 命中 _SELL_DISCOVER_DESCRIPTIONS）永远无法进入。
    """
    from star_tarven_simulator.simulator.game import _SELL_DISCOVER_DESCRIPTIONS

    card = next(c for c in cards if c.name == "拾荒猎人")

    assert card.event_handlers, "拾荒猎人普通版本应有 selling handler"
    assert card.gold_event_handlers, "拾荒猎人金色版本应有 selling handler"

    for handlers, raw in (
        (card.event_handlers, card.description[0]),
        (card.gold_event_handlers, card.gold_description[0]),
    ):
        expected = normalize(raw)
        selling = [h for h in handlers if h.event_name == SellingEvent.event_name]
        assert selling, f"{expected!r} 应解析为 selling handler"
        assert any(
            h.description == expected and h.description in _SELL_DISCOVER_DESCRIPTIONS
            for h in selling
        ), f"{expected!r} 应命中 trigger_selling 的特殊分支"


def test_seize_destroys_source_and_returns_its_origin(cards):
    """seize 走 destroy 后自然归还被夺取卡的真实来源。"""
    t = _tarven(cards)
    card = _drawable_1star(t)
    _narrow_bucket(t, [card])
    before = t.pool.count(card)
    assert t.pool.take(card) is not None
    source = _place(t, card, 0)
    target = _place(t, _blank_card("夺取者"), 1)

    t.seize(source, target)
    assert t.slots[0].card_type is None
    assert t.pool.count(card) == before
    assert target.units == dict(card.units)  # 单位转移不受来源归还影响


# ---------------------------------------------------------------------------
# 2) 三连：消费 3 份，金卡出售恢复 3 份；第 3 张单位不并入
# ---------------------------------------------------------------------------
def test_triple_golden_sell_returns_all_three_copies(cards):
    t = _tarven(cards)
    card = _drawable_1star(t)
    _narrow_bucket(t, [card])
    before = t.pool.count(card)

    # 两张上场
    _shop_draw(t, card)
    t.mineral = 20
    assert t.action(BuyAction(shop_idx=0, slot_idx=0))
    _shop_draw(t, card)
    assert t.action(BuyAction(shop_idx=0, slot_idx=1))
    assert t.pool.count(card) == before - 2

    # 第 3 张（商店）强制三连：来源随动作暂存
    _shop_draw(t, card)
    assert t.action(BuyAction(shop_idx=0, slot_idx=2))
    assert t.pool.count(card) == before - 3
    fa = t.force_action[0]
    assert isinstance(fa, ChooseSynthesisAction)
    assert list(fa.consumed_origin) == [card]

    # 结算：左槽变金，来源 = 3 份；第 3 张的单位未并入（仍是两张的单位之和）
    fa.selected = fa.options[0]
    assert t.action(fa)
    golds = [s for s in t.slots if s.card_type == card.name and s.tags.has("金色")]
    assert len(golds) == 1
    assert golds[0].origin_cards == [card, card, card]
    assert golds[0].units == {u: c * 2 for u, c in card.units.items()}

    # 出售金卡 -> 3 份全部恢复
    assert t.action(SellAction(slot_idx=golds[0].index))
    assert t.pool.count(card) == before


def test_cache_synthesis_third_copy_origin_transfers(cards):
    """暂存区第 3 张三连：来源从 cache_origin 转移，不留副本。"""
    t = _tarven(cards)
    card = _drawable_1star(t)
    _narrow_bucket(t, [card])
    before = t.pool.count(card)
    for _ in range(3):
        assert t.pool.take(card) is not None
    _place(t, card, 0)
    _place(t, card, 1)
    assert t.store_card_to_cache(card)  # 来源 [card]
    idx = t.cache.index(card)
    assert t.cache_origin[idx] == [card]

    assert t.action(CacheEnterAction(cache_idx=idx, slot_idx=2))
    fa = t.force_action[0]
    assert isinstance(fa, ChooseSynthesisAction)
    assert list(fa.consumed_origin) == [card]
    assert t.cache[idx] is None and t.cache_origin[idx] is None  # 元数据不留副本

    fa.selected = fa.options[0]
    assert t.action(fa)
    gold = next(s for s in t.slots if s.tags.has("金色"))
    assert gold.origin_cards == [card, card, card]
    assert t.pool.count(card) == before - 3


# ---------------------------------------------------------------------------
# 3) 执政官 fuse_slots / 阿塔尼斯 fuse_card_definition
# ---------------------------------------------------------------------------
def test_archon_fuse_slots_sell_returns_both_origin_sets(cards):
    t = _tarven(cards, "执政官")
    left_card = next(
        c for c in cards
        if c.level == 1 and c.race == "terran" and c.uuid not in t.pool.no_draw_uuids
    )
    right_card = next(
        c for c in cards
        if c.level == 1 and c.race == "protoss" and c.uuid not in t.pool.no_draw_uuids
    )
    _narrow_bucket(t, [left_card, right_card])
    before_left = t.pool.count(left_card)
    before_right = t.pool.count(right_card)
    for _ in range(3):
        assert t.pool.take(left_card) is not None
        assert t.pool.take(right_card) is not None

    left = _place(t, left_card, 0)
    right = _place(t, right_card, 1)
    left.origin_cards = [left_card] * 3
    right.origin_cards = [right_card] * 3
    left.tags.add("金色")
    right.tags.add("金色")

    assert t.action(HeroPowerAction(slot_idx=0))
    result = t.slots[1]
    assert result.card_type == f"{left_card.name}+{right_card.name}"
    assert result.origin_cards == [right_card] * 3 + [left_card] * 3
    assert t.slots[0].card_type is None

    assert t.action(SellAction(slot_idx=1))
    assert t.pool.count(left_card) == before_left
    assert t.pool.count(right_card) == before_right


def test_artanis_fuse_sell_returns_only_base_origin(cards):
    """阿塔尼斯融合：保留底牌原卡，不凭空归还阿塔尼斯定义。"""
    t = _tarven(cards, "阿塔尼斯")
    cm = _card_map(cards)
    base = cm["好兄弟"]
    artanis = cm["阿塔尼斯"]
    before_base = t.pool.count(base)
    before_artanis = t.pool.count(artanis)
    assert t.pool.take(base) is not None

    t.hero_controller.state["entered"] = 9
    target = _place(t, base, 0)
    t.trigger_entering(target)
    assert target.card_type == f"{base.name}+阿塔尼斯"
    assert target.origin_cards == [base]

    assert t.action(SellAction(slot_idx=0))
    assert t.pool.count(base) == before_base  # 底牌归还
    assert t.pool.count(artanis) == before_artanis  # 阿塔尼斯不归池


# ---------------------------------------------------------------------------
# 4) 星级被降低后仍按 source Card 原始等级归池
# ---------------------------------------------------------------------------
def test_sold_after_level_reduction_returns_original_level_bucket(cards):
    t = _tarven(cards)
    card = next(
        c
        for c in cards
        if c.level == 2
        and c.uuid not in t.pool.no_draw_uuids
        and not any(h.event_name == EnteringEvent.event_name for h in c.event_handlers)
    )
    t.pool.set_bucket(2, [card.uuid] * 3)
    t.pool.set_bucket(1, [])  # 若误按降级后的 slot.level 归还会失败
    before = t.pool.count(card)

    _shop_draw(t, card)
    t.mineral = 10
    assert t.action(BuyAction(shop_idx=0, slot_idx=0))
    slot = t.slots[0]
    assert slot.level == 2
    slot.level = 1  # “放弃智力”等降低星级效果

    assert t.action(SellAction(slot_idx=0))
    assert t.pool.count(card) == before  # 仍归还到 2 星桶
    assert card.uuid in t.pool.bucket_uuids(2)


# ---------------------------------------------------------------------------
# 5) derived / 免费复制 / unknown uuid / no_draw 不归池
# ---------------------------------------------------------------------------
def test_derived_free_and_unknown_uuids_never_return_to_pool(cards):
    t = _tarven(cards)
    star = _drawable_1star(t)
    _narrow_bucket(t, [star])
    before = t.pool.count(star)

    free = _blank_card("免费复制", level=1, units={"跳虫": 1}, derived=False)
    slot = _place(t, free, 0)
    assert slot.origin_cards == []  # 负 uuid：不构成公共池来源
    assert t.action(SellAction(slot_idx=0))
    assert t.pool.count(star) == before

    derived = _blank_card("衍生卡", level=1, units={"跳虫": 1}, derived=True)
    s2 = _place(t, derived, 0)
    assert s2.origin_cards == []
    t.destroy(s2)
    assert t.pool.count(star) == before

    aux = next(c for c in cards if c.uuid in t.pool.no_draw_uuids)
    s3 = _place(t, aux, 0)
    assert s3.origin_cards == []  # no_draw：不是可抽取实体
    t.destroy(s3)
    assert t.pool.count(star) == before


# ---------------------------------------------------------------------------
# 6) 暂存区来源元数据：进场 / 部署 / 干扰者替换 / 焦土销毁
# ---------------------------------------------------------------------------
def test_cache_enter_transfers_origin_and_sell_returns_once(cards):
    t = _tarven(cards)
    card = _drawable_1star(t)
    _narrow_bucket(t, [card])
    before = t.pool.count(card)
    assert t.pool.take(card) is not None

    assert t.store_card_to_cache(card)
    idx = t.cache.index(card)
    assert t.cache_origin[idx] == [card]
    assert t.pool.count(card) == before - 1

    assert t.action(CacheEnterAction(cache_idx=idx, slot_idx=0))
    assert t.cache[idx] is None and t.cache_origin[idx] is None  # 元数据随卡转移
    assert t.slots[0].origin_cards == [card]
    assert t.pool.count(card) == before - 1

    assert t.action(SellAction(slot_idx=0))
    assert t.pool.count(card) == before  # 只归还一次


def test_deploy_clears_cache_origin_metadata(cards):
    t = _tarven(cards)
    aux = next(
        c for c in cards
        if any(h.event_name == DeploymentEvent.event_name for h in c.event_handlers)
    )
    _place(t, _drawable_1star(t), 3)
    assert t.store_card_to_cache(aux)
    idx = t.cache.index(aux)
    assert t.cache_origin[idx] == []  # 辅助卡 no_draw：来源为空

    assert t.action(DeployAction(slot_idx=3, cache_idx=idx))
    assert t.cache[idx] is None and t.cache_origin[idx] is None


def test_disruptor_reroll_keeps_origin_metadata_consistent(cards):
    t = _tarven(cards, "干扰者")
    first = _drawable_1star(t)
    _narrow_bucket(t, [first])
    before = t.pool.count(first)
    assert t.pool.take(first) is not None
    free = _blank_card("免费条目", level=1, units={"菲尼克斯": 1})

    assert t.store_card_to_cache(first)      # 真实实体：来源 1 份
    assert t.store_card_to_cache(free)        # 免费卡：来源为空
    assert t.store_card_to_cache("矿簇")      # 名字静态定义：来源为空
    assert t.cache_origin[0] == [first]
    assert t.cache_origin[1] == []
    assert t.cache_origin[2] == []

    assert t.action(HeroPowerAction())        # 干扰者替换
    # 第一格被新池实体替换并记录新来源；免费/名字条目原样保留
    assert isinstance(t.cache[0], Card) and t.cache_origin[0] == [t.cache[0]]
    assert t.cache[1] is free and t.cache_origin[1] == []
    assert t.cache[2] == "矿簇" and t.cache_origin[2] == []
    # 旧来源归还（take 一份 + 重掷抽一份 + 归还旧一份 = 净 -1）
    assert t.pool.count(first) == before - 1


def test_scorched_destroys_cache_origins_but_keeps_free_definitions(cards):
    t = _tarven(cards)
    key = "出售时,可摧毁暂存区所有非衍生牌,每张返还3晶体矿"
    event_name, handler = ACTION_HANDLERS[normalize(key)]
    pool_card = _drawable_1star(t)
    _narrow_bucket(t, [pool_card])
    before = t.pool.count(pool_card)
    assert t.pool.take(pool_card) is not None
    assert t.store_card_to_cache(pool_card)  # 真实实体：来源 1 份
    assert t.store_card_to_cache("矿簇")      # 免费静态定义：来源为空

    scorched = _blank_card("焦土策略", level=1, units={})
    scorched.event_handlers = [
        EventHandler(t, None, key, handler, event_name)
    ]
    _place(t, scorched, 0)

    assert t.action(SellAction(slot_idx=0))
    assert t.pool.count(pool_card) == before  # 摧毁归还（take 一份 + 归还一份）
    assert all(item is None for item in t.cache)
    assert all(origin is None for origin in t.cache_origin)


# ---------------------------------------------------------------------------
# 7) enter_card_direct 拒绝部署卡；海盗商人溢出归池
# ---------------------------------------------------------------------------
def test_enter_card_direct_rejects_deployment_auxiliary_cards(cards):
    t = _tarven(cards)
    aux = next(
        c for c in cards
        if any(h.event_name == DeploymentEvent.event_name for h in c.event_handlers)
    )
    # 直接常驻进场被拒绝，缓存里仍可保留（不进场、不归池）
    assert not t.enter_card_direct(aux)
    assert not any(s.card_type is not None for s in t.slots)
    assert t.store_card_to_cache(aux)
    idx = t.cache.index(aux)
    assert t.cache_origin[idx] == []
    assert t.cache[idx] is aux


def test_pirate_merchant_reward_uses_grant_and_overflow_returns_original(cards):
    t = _tarven(cards)
    star = _drawable_1star(t)
    _narrow_bucket(t, [star])
    key = "任务:回合胜利 奖励:随机获得1张一星卡牌"
    event_name, handler = ACTION_HANDLERS[normalize(key)]

    # 满场 + 满缓存：发放失败 -> 原 Card 实体放回卡池
    t.cache[:] = ["占位"] * 6
    for idx in range(7):
        s = Slot(idx, t)
        s.card_type = "占位"
        t.slots[idx] = s
    before = t.pool.count(star)
    handler(Slot(0, t), RoundWinEvent(t))
    assert t.pool.count(star) == before

    # 缓存优先：存入 Card 实体并记录来源
    t.cache[:] = [None] * 6
    t.slots = [Slot(idx, t) for idx in range(7)]
    handler(Slot(0, t), RoundWinEvent(t))
    assert any(item is star for item in t.cache)
    assert t.pool.count(star) == before - 1
    assert t.cache_origin[t.cache.index(star)] == [star]
