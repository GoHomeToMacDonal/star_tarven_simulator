"""Card 密封（seal）契约测试：加载后的静态卡牌运行时不可变。

对应 16.10 裁决：把"加载后 Card 只读"从纪律约定加固为运行时强制。
加载器在 ``parse_card`` 完成后调用 :meth:`Card.seal`，此后：

* 任何字段重新赋值抛 :class:`AttributeError`；
* 列表字段转 tuple：``append`` / ``remove`` / 下标赋值抛 ``AttributeError``；
* ``units`` 转 :class:`types.MappingProxyType`：原地写入抛 ``TypeError``；
* :meth:`Card.seal` 幂等；
* ``Card.from_json`` / 直接构造的卡保持未 seal，加载器与测试仍可写；
* :func:`dataclasses.replace` 生成未 seal 的新卡，原卡不变；
* 默认 ``copy.copy`` 保留封印：sealed 卡的浅拷贝副本仍 sealed、不可写；
* ``Card.__deepcopy__`` 仍按引用共享密封卡（``CardPool`` 克隆性能契约不变）。
"""

from __future__ import annotations

import copy
import random
import types
from dataclasses import replace

import pytest

from star_tarven_simulator.constants.unit_prices import UNIT_PRICES
from star_tarven_simulator.loader import load_cards
from star_tarven_simulator.parsing.parser import parse_card
from star_tarven_simulator.simulator.card import Card, CardPool


@pytest.fixture(scope="module")
def cards():
    loaded, _ = load_cards()
    return loaded


def test_loaded_cards_reject_field_assignment(cards):
    """load_cards 返回的卡已密封：字段重新赋值抛 AttributeError。"""
    card = cards[0]
    with pytest.raises(AttributeError):
        card.name = "篡改"
    with pytest.raises(AttributeError):
        card.level = 99
    with pytest.raises(AttributeError):
        card.description = ["篡改"]
    with pytest.raises(AttributeError):
        card.event_handlers = []
    with pytest.raises(AttributeError):
        card.units = {}


def test_loaded_cards_tags_and_handlers_have_no_append(cards):
    """密封后列表字段为 tuple：append / remove / 下标赋值都不可用。"""
    card = next(c for c in cards if c.tags)
    assert isinstance(card.tags, tuple)
    assert isinstance(card.event_handlers, tuple)
    assert isinstance(card.source, tuple)
    with pytest.raises(AttributeError):
        card.tags.append("篡改")
    with pytest.raises(AttributeError):
        card.tags.remove(card.tags[0])
    with pytest.raises(AttributeError):
        card.event_handlers.append(None)
    with pytest.raises(TypeError):
        card.tags[0] = "篡改"


def test_loaded_cards_units_are_mappingproxy(cards):
    """密封后 units 为 MappingProxyType：原地写入抛 TypeError，读取不受影响。"""
    card = next(c for c in cards if c.units)
    assert isinstance(card.units, types.MappingProxyType)
    unit = next(iter(card.units))
    with pytest.raises(TypeError):
        card.units[unit] = 999
    with pytest.raises(AttributeError):
        card.units.update({unit: 999})
    # 读取路径兼容：dict() / items() / 遍历 / price
    assert dict(card.units) == {u: n for u, n in card.units.items()}
    assert card.price == sum(
        UNIT_PRICES.get(u, 0.0) * n for u, n in card.units.items()
    )


def test_seal_is_idempotent(cards):
    """seal 可重复调用；第二次是空操作且密封状态保持。"""
    card = cards[0]
    card.seal()
    card.seal()
    assert card._sealed is True
    with pytest.raises(AttributeError):
        card.name = "篡改"


def test_from_json_card_is_unsealed_and_parse_card_can_write():
    """from_json / 直接构造的卡保持未 seal：parse_card 可写、测试可 append。"""
    raw = {
        "uuid": 999_001,
        "name": "密封测试卡",
        "level": 1,
        "race": "neutral",
        "description": ["进场时,获得1晶体矿"],
        "gold_description": [],
        "units": {"陆战队员": 2},
        "tags": [],
        "gold_tags": [],
    }
    card = Card.from_json(raw)
    assert card._sealed is False
    # 未 seal：字段可赋值、列表可原地修改
    card.tags.append("测试标签")
    card.level = 2
    # 加载解析器可正常就地填充 handler 模板
    unhandled_normal, unhandled_gold = parse_card(card)
    assert isinstance(card.event_handlers, list)
    before = len(card.event_handlers)
    card.event_handlers.append(None)
    assert len(card.event_handlers) == before + 1
    assert unhandled_gold == []
    # 需要时也可手动密封
    card.seal()
    with pytest.raises(AttributeError):
        card.tags.append("再写")
    with pytest.raises(AttributeError):
        card.level = 3


def test_dataclasses_replace_keeps_original_and_yields_mutable_copy(cards):
    """replace 从密封卡生成新对象：不修改原卡，新对象未 seal 可赋值。"""
    card = next(c for c in cards if c.event_handlers)
    new = replace(card, name="替身", derived=True)
    assert new is not card
    assert new.name == "替身" and new.derived is True
    # 原卡不变
    assert card.name != "替身" and card.derived is False
    assert new.uuid == card.uuid  # 未覆盖字段保持原值
    # 新卡默认未 seal：replace 直接 Card(**changes) 建新实例（不经过 copy.copy），
    # _sealed 是 init=False 字段、__post_init__ 把它复位为 False，故可自由赋值
    assert new._sealed is False
    new.name = "替身2"
    new.level = 6
    new.tags = ["全新"]
    assert card.name != "替身2" and card.level != 6
    assert card.tags != ["全新"]
    # 未显式覆盖的容器字段按契约与原卡共享（要可变副本请显式传 list/dict）
    assert new.units is card.units
    assert new.event_handlers is card.event_handlers


def test_replace_with_explicit_fresh_containers(cards):
    """英雄代码式 replace：显式传 dict/list 得到独立可变容器，原卡仍密封。"""
    card = next(c for c in cards if c.event_handlers)
    new = replace(
        card,
        uuid=-999,
        derived=True,
        units=dict(card.units),
        tags=list(card.tags),
        gold_tags=list(card.gold_tags),
        event_handlers=list(card.event_handlers),
        gold_event_handlers=list(card.gold_event_handlers),
    )
    assert new is not card
    assert new.uuid == -999 and new.derived is True
    assert new.units is not card.units
    assert dict(new.units) == dict(card.units)
    assert isinstance(new.tags, list) and new.tags == list(card.tags)
    assert new.event_handlers == list(card.event_handlers)
    # 原卡保持密封、内容不变
    assert card.uuid != -999 and card.derived is False
    with pytest.raises(AttributeError):
        card.name = "篡改"


def test_deepcopy_still_shares_sealed_card(cards):
    """deepcopy 契约不变：Card 按引用共享（CardPool 克隆性能不受影响）。"""
    card = cards[0]
    assert copy.deepcopy(card) is card
    pool = CardPool(cards[:20])
    clone = copy.deepcopy(pool)
    assert clone.cards is pool.cards
    assert clone.card_map is pool.card_map
    uuid = next(iter(pool.card_map))
    assert clone.card_map[uuid] is pool.card_map[uuid]


def test_copy_of_sealed_card_stays_sealed(cards):
    """默认浅拷贝保留封印：sealed 卡的 copy.copy 副本仍 sealed、不可写。

    Card 不自定义 ``__copy__``（旧版曾错误声称 replace 依赖 copy.copy 而返回
    解除封印的副本）；默认 shallow copy 复制 ``__dict__``（含 ``_sealed``），
    副本与原卡一样只读，不可变保护不被绕过。
    """
    card = cards[0]
    clone = copy.copy(card)
    assert clone is not card
    assert clone._sealed is True
    # 副本仍不可写：字段赋值抛 AttributeError
    with pytest.raises(AttributeError):
        clone.name = "篡改"
    with pytest.raises(AttributeError):
        clone.level = 99
    with pytest.raises(AttributeError):
        clone.description = ["篡改"]
    # 浅拷贝共享容器引用；原卡不受影响
    assert clone.tags is card.tags
    assert clone.units is card.units
    assert clone.event_handlers is card.event_handlers
    assert card._sealed is True
    with pytest.raises(AttributeError):
        card.name = "篡改"


def test_copy_of_unsealed_card_stays_unsealed():
    """对称契约：未 seal 卡的 copy.copy 副本仍未 seal、可写。"""
    raw = {
        "uuid": 999_002,
        "name": "浅拷贝测试卡",
        "level": 1,
        "race": "neutral",
        "description": [],
        "gold_description": [],
        "units": {},
        "tags": [],
        "gold_tags": [],
    }
    card = Card.from_json(raw)
    clone = copy.copy(card)
    assert clone is not card
    assert clone._sealed is False
    clone.level = 5  # 副本未 seal，可写
    assert card.level == 1  # 原卡不受影响


def test_build_game_with_sealed_cards(cards):
    """密封卡参与构建整局与抽卡无碍（CardPool / 引擎只读卡字段）。"""
    from star_tarven_simulator.loader import build_game

    game = build_game(cards, user_count=1, rng=random.Random(0))
    tarven = game.tarvens[0]
    tarven.reload_shop()
    assert any(card is not None for card in tarven.shop)
    assert game.pool.total_size() > 0
    assert tarven.discover(level=3)
