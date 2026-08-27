"""卡池采样的行为契约测试（对拍 + 不变量）。

本文件是 ``CardPool`` 性能重构的**硬门槛**：:class:`LegacyPool` 是重构前
``CardPool`` 的逐行副本（保留 uuid 可重复列表 + 逐拷贝扫描的 ``_sample``），
测试用同一 seed 分别驱动真实实现与参考实现，逐步比对

* 每次采样/取卡返回的 uuid；
* 每个等级桶的完整内容（顺序敏感，因为 ``take`` 与采样都用 swap-remove）；
* ``rng.getstate()``（即随机数消耗必须完全一致）。

只要这套对拍通过，就说明重构后的实现与旧实现**位级等价**：同一 seed 下抽到的
卡、卡池演化、RNG 序列都不变。
"""

from __future__ import annotations

import random
from typing import Dict, List, Optional

import pytest

from star_tarven_simulator.expansions import BASE_EXTRA_SOURCES
from star_tarven_simulator.loader import load_cards
from star_tarven_simulator.simulator.card import CARD_POOL_NUMBER, Card, CardPool


# ----------------------------------------------------------------------
# 参考实现（重构前的 CardPool，逐行冻结，勿优化）
# ----------------------------------------------------------------------
class LegacyPool:
    """重构前 ``CardPool`` 的采样相关部分，作为对拍 oracle。"""

    def __init__(
        self,
        cards: List[Card],
        no_draw_uuids: Optional[set] = None,
        rng: Optional[random.Random] = None,
    ):
        self.cards = cards
        self.rng = rng if rng is not None else random.Random()
        self.card_map: Dict[int, Card] = {card.uuid: card for card in cards}
        self.card_type_map = {card.name: card for card in cards}
        self.no_draw_uuids = set(no_draw_uuids) if no_draw_uuids else set()

        self.pool: List[List[int]] = [[] for _ in range(7)]
        for card in cards:
            if card.uuid in self.no_draw_uuids:
                continue
            if 1 <= card.level <= 6:
                self.pool[card.level] += [card.uuid] * CARD_POOL_NUMBER[card.level]

    def draw(self, count: int, max_level: int) -> List[Card]:
        cards: List[Card] = []
        levels = list(range(1, max_level + 1))
        for _ in range(count):
            uuid = self._sample(levels=levels)
            if uuid is None:
                break
            cards.append(self.card_map[uuid])
        return cards

    def place_back(self, cards) -> None:
        if isinstance(cards, Card):
            cards = [cards]
        for card in cards:
            if isinstance(card, Card) and 1 <= card.level <= 6:
                if (
                    card.derived
                    or card.uuid in self.no_draw_uuids
                    or card.uuid not in self.card_map
                ):
                    continue
                self.pool[card.level].append(card.uuid)

    def take(self, card: Card) -> Optional[Card]:
        if not isinstance(card, Card) or card.derived or card.uuid in self.no_draw_uuids:
            return None
        if not (1 <= card.level < len(self.pool)):
            return None
        bucket = self.pool[card.level]
        try:
            index = bucket.index(card.uuid)
        except ValueError:
            return None
        bucket[index] = bucket[-1]
        bucket.pop()
        return self.card_map[card.uuid]

    def take_by_name(self, name: str) -> Optional[Card]:
        card = self.card_type_map.get(name)
        return self.take(card) if card is not None else None

    def count(self, card: Card) -> int:
        if not isinstance(card, Card) or not (1 <= card.level < len(self.pool)):
            return 0
        return self.pool[card.level].count(card.uuid)

    def _sample(
        self,
        levels: Optional[List[int]] = None,
        excepts: Optional[List[int]] = None,
        tags: Optional[List[str]] = None,
    ) -> Optional[int]:
        if levels is None:
            levels = [1, 2, 3, 4, 5, 6]

        eligible = []
        wanted_tags = set(tags or [])
        blocked = set(excepts or [])
        for level in levels:
            if not (0 <= level < len(self.pool)):
                continue
            for idx, uuid in enumerate(self.pool[level]):
                card = self.card_map[uuid]
                if uuid in blocked:
                    continue
                if wanted_tags and not (wanted_tags & set(card.tags)):
                    continue
                eligible.append((level, idx, uuid))
        if not eligible:
            return None

        level, idx, uuid = eligible[self.rng.randrange(len(eligible))]
        pool = self.pool[level]
        pool[idx] = pool[-1]
        pool.pop()
        return uuid


# ----------------------------------------------------------------------
# 兼容读写辅助（重构过程中真实实现的内部表示会变，测试只走公开访问器）
# ----------------------------------------------------------------------
def snapshot(pool) -> List[List[int]]:
    """按等级返回桶内 uuid 序列（顺序敏感）。"""
    if hasattr(pool, "bucket_uuids"):
        return [list(pool.bucket_uuids(level)) for level in range(7)]
    return [list(bucket) for bucket in pool.pool]


def do_sample(pool, **kwargs) -> Optional[int]:
    """调用采样接口（重构后为公开 ``sample``，重构前为 ``_sample``）。"""
    fn = getattr(pool, "sample", None) or pool._sample
    return fn(**kwargs)


def total_size(pool) -> int:
    if hasattr(pool, "total_size"):
        return pool.total_size()
    return sum(len(bucket) for bucket in pool.pool)


# ----------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def cards() -> List[Card]:
    loaded, _ = load_cards()
    return loaded


@pytest.fixture(scope="module")
def no_draw(cards) -> set:
    return {
        c.uuid
        for c in cards
        if set(getattr(c, "source", []) or []) & set(BASE_EXTRA_SOURCES)
    }


RACES = ["terran", "protoss", "zerg", "neutral"]


def build_pair(cards, no_draw, seed: int):
    """构造同 seed 的（真实实现, 参考实现）二元组。"""
    real = CardPool(cards, no_draw_uuids=no_draw, rng=random.Random(seed))
    legacy = LegacyPool(cards, no_draw_uuids=no_draw, rng=random.Random(seed))
    assert snapshot(real) == snapshot(legacy), "初始桶内容必须一致"
    return real, legacy


def make_script(cards, rng: random.Random, steps: int) -> List[tuple]:
    """生成与卡池状态无关的操作脚本，便于在两份实现上重放同一序列。"""
    drawable = [c for c in cards if 1 <= c.level <= 6]
    script: List[tuple] = []
    for _ in range(steps):
        kind = rng.choices(
            ["draw", "sample", "sample_tags", "sample_excepts", "take", "place_back", "count"],
            weights=[25, 25, 15, 5, 10, 15, 5],
        )[0]
        if kind == "draw":
            script.append(("draw", rng.randint(1, 7), rng.randint(1, 6)))
        elif kind == "sample":
            top = rng.randint(1, 6)
            levels = list(range(1, top + 1)) if rng.random() < 0.6 else [rng.randint(1, 6)]
            script.append(("sample", levels))
        elif kind == "sample_tags":
            tags = rng.sample(RACES, rng.randint(1, 3))
            top = rng.randint(1, 6)
            script.append(("sample_tags", list(range(1, top + 1)), tags))
        elif kind == "sample_excepts":
            blocked = [c.uuid for c in rng.sample(drawable, rng.randint(1, 5))]
            script.append(("sample_excepts", list(range(1, 7)), blocked))
        elif kind == "take":
            script.append(("take", rng.choice(drawable).uuid))
        elif kind == "place_back":
            picked = [c.uuid for c in rng.sample(drawable, rng.randint(1, 3))]
            script.append(("place_back", picked))
        else:
            script.append(("count", rng.choice(drawable).uuid))
    return script


def replay(pool, script: List[tuple], card_map: Dict[int, Card]) -> List:
    """在 ``pool`` 上重放脚本，返回每步的可观测结果。"""
    out: List = []
    for op in script:
        kind = op[0]
        if kind == "draw":
            drawn = pool.draw(op[1], op[2])
            out.append(("draw", tuple(c.uuid for c in drawn)))
        elif kind == "sample":
            out.append(("sample", do_sample(pool, levels=list(op[1]))))
        elif kind == "sample_tags":
            out.append(("sample_tags", do_sample(pool, levels=list(op[1]), tags=list(op[2]))))
        elif kind == "sample_excepts":
            out.append(
                ("sample_excepts", do_sample(pool, levels=list(op[1]), excepts=list(op[2])))
            )
        elif kind == "take":
            got = pool.take(card_map[op[1]])
            out.append(("take", None if got is None else got.uuid))
        elif kind == "place_back":
            pool.place_back([card_map[u] for u in op[1]])
            out.append(("place_back", None))
        else:
            out.append(("count", pool.count(card_map[op[1]])))
    return out


# ----------------------------------------------------------------------
# 对拍测试
# ----------------------------------------------------------------------
@pytest.mark.parametrize("seed", [0, 1, 7, 12345])
def test_matches_legacy_step_by_step(cards, no_draw, seed):
    """同 seed 下，真实实现与参考实现的每一步结果、桶内容、RNG 状态都相同。"""
    real, legacy = build_pair(cards, no_draw, seed)
    card_map = {c.uuid: c for c in cards}
    script = make_script(cards, random.Random(seed + 991), steps=400)

    for step, op in enumerate(script):
        got_real = replay(real, [op], card_map)
        got_legacy = replay(legacy, [op], card_map)
        assert got_real == got_legacy, f"第 {step} 步（{op[0]}）返回值不一致"
        assert snapshot(real) == snapshot(legacy), f"第 {step} 步（{op[0]}）后桶内容不一致"
        assert real.rng.getstate() == legacy.rng.getstate(), (
            f"第 {step} 步（{op[0]}）后 RNG 状态不一致"
        )
        if hasattr(real, "assert_consistent"):
            real.assert_consistent()


def test_matches_legacy_long_run(cards, no_draw):
    """长序列（2000 步）整体对拍，末态必须一致。"""
    real, legacy = build_pair(cards, no_draw, 2024)
    card_map = {c.uuid: c for c in cards}
    script = make_script(cards, random.Random(4242), steps=2000)

    assert replay(real, script, card_map) == replay(legacy, script, card_map)
    assert snapshot(real) == snapshot(legacy)
    assert real.rng.getstate() == legacy.rng.getstate()


def test_exhaustion_returns_none(cards, no_draw):
    """抽空某个等级后返回 None，且与参考实现一致。"""
    real, legacy = build_pair(cards, no_draw, 3)
    while True:
        a = do_sample(real, levels=[1])
        b = do_sample(legacy, levels=[1])
        assert a == b
        if a is None:
            break
    assert snapshot(real) == snapshot(legacy)
    assert do_sample(real, levels=[1]) is None


def test_tag_filter_respects_tags(cards, no_draw):
    """带标签采样只会抽到含该标签的卡。"""
    real = CardPool(cards, no_draw_uuids=no_draw, rng=random.Random(11))
    for _ in range(200):
        uuid = do_sample(real, levels=[1, 2, 3, 4, 5, 6], tags=["zerg"])
        if uuid is None:
            break
        assert "zerg" in real.card_map[uuid].tags


def test_excepts_are_never_sampled(cards, no_draw):
    """``excepts`` 中的 uuid 永远不会被抽到（此前无任何覆盖）。"""
    real = CardPool(cards, no_draw_uuids=no_draw, rng=random.Random(5))
    level1 = [c.uuid for c in cards if c.level == 1 and c.uuid not in no_draw]
    blocked = set(level1[: max(1, len(level1) // 2)])
    for _ in range(300):
        uuid = do_sample(real, levels=[1], excepts=list(blocked))
        if uuid is None:
            break
        assert uuid not in blocked


def test_no_draw_cards_never_enter_buckets(cards, no_draw):
    """``no_draw_uuids`` 的卡可查询但不进桶，也不会被 place_back 放回。"""
    real = CardPool(cards, no_draw_uuids=no_draw, rng=random.Random(0))
    in_bucket = {uuid for bucket in snapshot(real) for uuid in bucket}
    assert not (in_bucket & no_draw)
    assert all(uuid in real.card_map for uuid in no_draw)

    before = total_size(real)
    for uuid in list(no_draw)[:5]:
        real.place_back(real.card_map[uuid])
        assert real.take(real.card_map[uuid]) is None
    assert total_size(real) == before


def test_place_back_beyond_initial_capacity(cards, no_draw):
    """反复放回同一张卡（超过初始份数）不应丢失份数或崩溃。"""
    real = CardPool(cards, no_draw_uuids=no_draw, rng=random.Random(0))
    card = next(c for c in cards if c.level == 1 and c.uuid not in no_draw)
    base = real.count(card)
    extra = 50
    for _ in range(extra):
        real.place_back(card)
    assert real.count(card) == base + extra
    for _ in range(base + extra):
        assert real.take(card) is not None
    assert real.count(card) == 0
    assert real.take(card) is None


def test_derived_cards_are_not_placed_back(cards, no_draw):
    """衍生卡不回池。"""
    from dataclasses import replace

    real = CardPool(cards, no_draw_uuids=no_draw, rng=random.Random(0))
    card = next(c for c in cards if c.level == 1 and c.uuid not in no_draw)
    derived = replace(card, uuid=-999, derived=True)
    before = total_size(real)
    real.place_back(derived)
    assert total_size(real) == before
    assert real.take(derived) is None



def test_counts_stay_in_sync_with_buckets(cards, no_draw):
    """内部计数表在长序列混合操作后仍与桶内容一致。"""
    real = CardPool(cards, no_draw_uuids=no_draw, rng=random.Random(99))
    card_map = {c.uuid: c for c in cards}
    script = make_script(cards, random.Random(77), steps=800)
    real.assert_consistent()
    replay(real, script, card_map)
    real.assert_consistent()

    # set_bucket / clear_bucket 也必须重建计数表
    level1 = [c.uuid for c in cards if c.level == 1 and c.uuid not in no_draw]
    real.set_bucket(1, level1 * 2)
    real.assert_consistent()
    assert real.count(card_map[level1[0]]) == 2
    real.clear_bucket(1)
    real.assert_consistent()
    assert real.count(card_map[level1[0]]) == 0
    assert real.bucket_size(1) == 0



def test_sample_accepts_int_and_iterable_levels(cards, no_draw):
    """``levels`` 支持 None / int / 列表 / 任意可迭代（生成器亦可）。"""
    real = CardPool(cards, no_draw_uuids=no_draw, rng=random.Random(1))
    card_map = real.card_map

    uuid = real.sample(levels=3)
    assert uuid is not None and card_map[uuid].level == 3

    uuid = real.sample(levels=range(4, 6))
    assert uuid is not None and card_map[uuid].level in (4, 5)

    uuid = real.sample(levels=(x for x in (2,)))
    assert uuid is not None and card_map[uuid].level == 2

    assert real.sample() is not None
    assert real.sample(levels=[0, 7, 99]) is None


def test_legacy_alias_still_works(cards, no_draw):
    """历史调用 ``pool._sample`` 的代码不应被破坏。"""
    real = CardPool(cards, no_draw_uuids=no_draw, rng=random.Random(2))
    assert real._sample(levels=[1]) is not None


def test_discover_accepts_int_level(cards, no_draw):
    """``Tarven.discover(level=3)`` 不再抛 TypeError。"""
    from star_tarven_simulator.loader import build_game

    game = build_game(cards, user_count=1, rng=random.Random(0))
    tarven = game.tarvens[0]
    assert tarven.discover(level=3)
    options = tarven.force_action[-1].cards
    assert options and all(card.level == 3 for card in options)



# ----------------------------------------------------------------------
# 克隆（deepcopy）语义
# ----------------------------------------------------------------------
def test_deepcopy_pool_is_independent(cards, no_draw):
    """克隆后的卡池与原卡池互不影响，但静态数据共享。"""
    import copy

    real = CardPool(cards, no_draw_uuids=no_draw, rng=random.Random(8))
    real.draw(5, 4)
    clone = copy.deepcopy(real)

    assert snapshot(clone) == snapshot(real)
    assert clone.rng.getstate() == real.rng.getstate()
    # 静态数据共享
    assert clone.cards is real.cards
    assert clone.card_map is real.card_map
    assert clone.card_type_map is real.card_type_map
    # 可变状态独立
    assert clone._buckets is not real._buckets
    assert clone._counts is not real._counts
    assert clone.rng is not real.rng

    before = snapshot(real)
    before_state = real.rng.getstate()
    clone.draw(7, 6)
    for _ in range(20):
        do_sample(clone, levels=[1, 2, 3])
    assert snapshot(real) == before
    assert real.rng.getstate() == before_state
    clone.assert_consistent()
    real.assert_consistent()

    # 同一状态出发的两份克隆，抽卡序列一致
    a, b = copy.deepcopy(real), copy.deepcopy(real)
    assert [c.uuid for c in a.draw(7, 6)] == [c.uuid for c in b.draw(7, 6)]


def test_deepcopy_shares_card_instances(cards, no_draw):
    """Card 视作不可变静态值，深拷贝按引用共享。"""
    import copy

    card = cards[0]
    assert copy.deepcopy(card) is card
    real = CardPool(cards, no_draw_uuids=no_draw, rng=random.Random(0))
    clone = copy.deepcopy(real)
    uuid = next(iter(real.card_map))
    assert clone.card_map[uuid] is real.card_map[uuid]


def test_clone_game_stays_independent(cards):
    """整局克隆后，卡池 / 商店 / 场面互不影响，且 rng 别名关系保留。"""
    import copy

    from star_tarven_simulator.loader import build_game

    game = build_game(cards, user_count=1, rng=random.Random(3))
    tarven = game.tarvens[0]
    tarven.reload_shop()

    clone = copy.deepcopy(game)
    clone_t = clone.tarvens[0]
    # Tarven.rng 与 pool.rng 是同一实例的别名，克隆后必须仍然是别名
    assert clone_t.rng is clone_t.pool.rng
    assert clone_t.pool is not tarven.pool
    assert clone_t.card_engine is tarven.card_engine  # 无状态，共享

    before_pool = snapshot(tarven.pool)
    before_shop = [None if c is None else c.uuid for c in tarven.shop]
    for _ in range(5):
        clone_t.refresh()
    assert snapshot(tarven.pool) == before_pool
    assert [None if c is None else c.uuid for c in tarven.shop] == before_shop
    tarven.pool.assert_consistent()
    clone_t.pool.assert_consistent()
