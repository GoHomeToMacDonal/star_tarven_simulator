"""16.5/16.9 落实测试：颜色提示路由、灵能 tag、金色任务 "触发2次"、奖励尾部保护。

覆盖三个逐项审计修复：

1. 颜色语义参与参数化解析（16.5）：``colors`` 不再是死参数——先尝试颜色提示的
   机制解析器，再退回默认解析链（缺色旧数据行为不变）。
2. 黑暗预兆（数据 tag 修复）：普通/金色 ``tags`` 补 ``灵能``，psi_level 等于卡牌
   等级；单独在场不触发灵能奖励，搭配更高灵能等级卡触发。
3. 帝国舰队金色任务 "触发2次"：精确 override（一次完成 +2 战列巡航舰，
   auto_reset）＋ 通用 ``_reward_handler`` 对尾部 ``触发N次`` 的重复执行保护，
   不再被静默当普通 +1；任务完成广播仍只发生一次。
"""

from __future__ import annotations

import pytest

from star_tarven_simulator.cards import resolve
from star_tarven_simulator.cards import parametric as P
from star_tarven_simulator.cards.registry import ACTION_HANDLERS
from star_tarven_simulator.loader import build_game, load_cards
from star_tarven_simulator.parsing.text import extract_colors
from star_tarven_simulator.simulator.event import AnyTaskFinishedEvent, RoundEndEvent
from star_tarven_simulator.simulator.event_handler import TaskActionHandler
from star_tarven_simulator.simulator.slot import Slot

# 帝国舰队任务：6 次任意卡牌进场/出售完成一次；进场用固定 6 个非任务槽位
_IMPERIAL_GOAL = 6
_ENTRY_IDXS = [0, 1, 2, 4, 5, 6]
_GOLD_TASK_TEXT = "任务:进场或出售6张卡牌 奖励:获得1战列巡航舰并重置此任务,触发2次"


@pytest.fixture(scope="module")
def cards():
    loaded, _ = load_cards()
    return loaded


def _fresh_tarven(cards):
    game = build_game(cards, user_count=1)
    return game.tarvens[0]


def _place(tarven, card, idx: int) -> Slot:
    tarven.slots[idx] = Slot(idx, tarven)
    tarven.card_engine.assign_card_to_slot(card, tarven.slots[idx])
    return tarven.slots[idx]


def _task_handler(slot: Slot) -> TaskActionHandler:
    return next(
        h.action_handler
        for h in slot.event_handlers
        if isinstance(h.action_handler, TaskActionHandler)
    )


def _complete_task_rounds(tarven, card, rounds: int) -> None:
    """每轮在 6 个非任务槽各进场一次（推进任务一次完成），共 ``rounds`` 轮。"""
    for _ in range(rounds):
        for idx in _ENTRY_IDXS:
            other = _place(tarven, card, idx)
            tarven.trigger_entering(other)


# ===========================================================================
# 1) 颜色提示路由（16.5）
# ===========================================================================
def test_color_mechanism_mapping():
    """颜色 -> 机制类别映射：008000 任务 / 00FF00 快速生产 / 8080FF 灵能 /
    DEDE00+FFFF00 集结 / FF8000 反应堆·集群 / 8000FF 供养；未知颜色为 None。"""
    assert P.color_mechanism("008000") == "task"
    assert P.color_mechanism("00FF00") == "quick_produce"
    assert P.color_mechanism("8080FF") == "psi"
    assert P.color_mechanism("DEDE00") == "gathering"
    assert P.color_mechanism("FFFF00") == "gathering"
    assert P.color_mechanism("FF8000") == "reactor_swarm"
    assert P.color_mechanism("8000FF") == "feed"
    assert P.color_mechanism("0080FF") is None  # 折跃色不在提示表（不抢路由）
    assert P.color_mechanism("000000") is None


def test_hinted_resolver_names_order_and_dedup():
    """颜色提示返回应优先尝试的解析器名：按出现顺序、同机制去重、未知色忽略。"""
    assert P.hinted_resolver_names([]) == []
    assert P.hinted_resolver_names([("0080FF", "折跃")]) == []  # 未知颜色忽略
    assert P.hinted_resolver_names([("8080FF", "灵能：")]) == ["resolve_psi"]
    assert P.hinted_resolver_names(
        [("FF8000", "反应堆"), ("00FF00", "快速生产：")]
    ) == ["resolve_reactor", "resolve_swarm", "resolve_quick_produce"]
    # FFFF00 与 DEDE00 同为集结 -> 去重为一条
    assert P.hinted_resolver_names(
        [("DEDE00", "集结"), ("FFFF00", "(7)：")]
    ) == ["resolve_gathering"]


def test_resolve_parametric_tries_hinted_resolver_first(monkeypatch):
    """colors 会改变优先尝试顺序：提示灵能时先试 resolve_psi（即使文本是快速生产）。"""
    calls = []
    for name, fn in P._RESOLVER_BY_NAME.items():

        def spy(text, colors, _fn=fn, _name=name):
            calls.append(_name)
            return _fn(text, colors)

        monkeypatch.setitem(P._RESOLVER_BY_NAME, name, spy)

    # 默认链中 quick_produce 排在 psi 之前（psi 根本不会先被调用）
    assert P.RESOLVERS.index(P.resolve_quick_produce) < P.RESOLVERS.index(P.resolve_psi)

    text = "快速生产:获得1陆战队员"
    colors = [("8080FF", "灵能："), ("00FF00", "快速生产：")]
    result = P.resolve_parametric(text, colors)
    assert result is not None
    assert calls[0] == "resolve_psi"  # 颜色提示的灵能最先被尝试
    assert calls.index("resolve_psi") < calls.index("resolve_quick_produce")
    assert calls[-1] == "resolve_quick_produce"  # 随后提示的快速生产命中


def test_no_colors_keeps_default_chain(monkeypatch):
    """旧数据缺色：不走提示循环，默认链解析结果不变。"""
    calls = []
    for name, fn in P._RESOLVER_BY_NAME.items():

        def spy(text, colors, _fn=fn, _name=name):
            calls.append(_name)
            return _fn(text, colors)

        monkeypatch.setitem(P._RESOLVER_BY_NAME, name, spy)

    result = P.resolve_parametric("快速生产:获得1陆战队员", [])
    assert result is not None
    assert calls == []  # 提示循环未运行（无颜色）


def test_color_hint_does_not_break_fallback():
    """颜色提示与文本前缀不一致时退回完整链，解析结果与无颜色一致。"""
    text = "反应堆生产陆战队员"
    a = P.resolve_parametric(text, [])
    b = P.resolve_parametric(text, [("00FF00", "快速生产：")])
    assert a is not None and b is not None
    assert a[0] == b[0] == "round_end"


def test_real_card_color_hint_routes_to_quick_produce(cards):
    """真实数据：带 00FF00 提示的描述被路由到快速生产解析器。"""
    card_map = {c.name: c for c in cards}
    qp = card_map["快速生产"]
    assert any(
        P.hinted_resolver_names(extract_colors(raw)) == ["resolve_quick_produce"]
        for raw in qp.description
    )


# ===========================================================================
# 2) 黑暗预兆：灵能 tag / psi_level / 触发语义
# ===========================================================================
def test_dark_omen_psi_tag_in_data(cards):
    """普通与金色 tags 都应含「灵能」（描述有 8080FF 灵能，tag 缺失是数据 bug）。"""
    dark = {c.name: c for c in cards}["黑暗预兆"]
    assert "灵能" in dark.tags
    assert "灵能" in dark.gold_tags


def test_dark_omen_psi_level_equals_card_level(cards):
    """普通 / 金色 psi_level 都等于卡牌等级（5）。"""
    dark = {c.name: c for c in cards}["黑暗预兆"]
    tarven = _fresh_tarven(cards)
    slot = _place(tarven, dark, 3)
    assert slot.psi_level == dark.level == 5

    # 三连合成金色：tags 保留（含 灵能），psi_level 仍等于卡牌等级
    right = _place(tarven, dark, 4)
    tarven.card_engine.merge_slots(slot, right)
    assert slot.tags.has("金色")
    assert slot.psi_level == dark.level == 5


def test_dark_omen_psi_alone_does_not_reward(cards):
    """单独在场：psi_level(5) == psi_level_max(5)，灵能奖励不触发。"""
    dark = {c.name: c for c in cards}["黑暗预兆"]
    tarven = _fresh_tarven(cards)
    slot = _place(tarven, dark, 3)
    assert tarven.psi_level_max == 5
    before = slot.count("混合体巨兽")
    slot.trigger([RoundEndEvent(tarven)])
    assert slot.count("混合体巨兽") == before


def test_dark_omen_psi_triggers_with_higher_psi_level(cards):
    """搭配更高灵能等级（6 星灵能卡）时触发：+1 混合体巨兽。"""
    dark = {c.name: c for c in cards}["黑暗预兆"]
    tarven = _fresh_tarven(cards)
    slot = _place(tarven, dark, 3)

    high = Slot(4, tarven)
    high.card_type = "高灵能测试卡"
    high.level = 6
    high.tags.add("灵能")
    tarven.slots[4] = high
    assert tarven.psi_level_max == 6

    before = slot.count("混合体巨兽")
    slot.trigger([RoundEndEvent(tarven)])
    assert slot.count("混合体巨兽") == before + 1


# ===========================================================================
# 3) 帝国舰队金色 "触发2次"：精确 override + 通用尾部保护
# ===========================================================================
def test_imperial_fleet_gold_override_registered():
    """精确 override 已注册：goal=6、any_card_entered_or_sold、auto_reset=True。"""
    assert _GOLD_TASK_TEXT in ACTION_HANDLERS
    event_name, handler = ACTION_HANDLERS[_GOLD_TASK_TEXT]
    assert event_name == "any_card_entered_or_sold"
    assert isinstance(handler, TaskActionHandler)
    assert handler.goal == _IMPERIAL_GOAL
    assert handler.auto_reset


def test_imperial_fleet_normal_rewards_one_per_completion(cards):
    """普通：6 次进场/出售 -> +1 战列巡航舰；auto_reset 后下一轮还能再奖励。"""
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    fleet = _place(tarven, card_map["帝国舰队"], 3)

    task = _task_handler(fleet)
    assert task.goal == _IMPERIAL_GOAL
    assert task.auto_reset

    before = fleet.count("战列巡航舰")
    _complete_task_rounds(tarven, card_map["好兄弟"], rounds=1)
    assert fleet.count("战列巡航舰") == before + 1  # 普通：一次完成 +1
    assert task.counter == 0  # auto_reset 已归零

    _complete_task_rounds(tarven, card_map["好兄弟"], rounds=1)
    assert fleet.count("战列巡航舰") == before + 2  # 重置后下一轮还能再奖励


def test_imperial_fleet_gold_rewards_two_per_completion(cards):
    """金色：6 次进场/出售 -> +2 战列巡航舰（触发2次不再被吞）；
    一次完成只广播一次 AnyTaskFinishedEvent；重置后还能再奖励。"""
    card_map = {c.name: c for c in cards}
    tarven = _fresh_tarven(cards)
    fleet = _place(tarven, card_map["帝国舰队"], 3)
    tarven.card_engine.make_gold(fleet)
    assert fleet.tags.has("金色")

    task = _task_handler(fleet)
    assert task.goal == _IMPERIAL_GOAL
    assert task.auto_reset

    before = fleet.count("战列巡航舰")
    broadcasts = []
    orig = tarven.trigger_any_card_event

    def spy(event):
        if isinstance(event, AnyTaskFinishedEvent):
            broadcasts.append(event)
        return orig(event)

    tarven.trigger_any_card_event = spy
    try:
        _complete_task_rounds(tarven, card_map["好兄弟"], rounds=1)
    finally:
        tarven.trigger_any_card_event = orig

    assert fleet.count("战列巡航舰") == before + 2  # 金色：一次完成 +2
    assert len(broadcasts) == 1  # 任务完成广播仍只发生一次

    _complete_task_rounds(tarven, card_map["好兄弟"], rounds=1)
    assert fleet.count("战列巡航舰") == before + 4  # 重置后下一轮还能再奖励


def test_reward_tail_trigger_twice_not_silently_swallowed(cards):
    """通用保护：直接走参数化解析（绕过注册表），"触发2次" 也按重复执行基础奖励
    解析（+2），不再被当普通 +1；广播仍一次。"""
    text = _GOLD_TASK_TEXT

    # 注册表（resolve 优先命中精确 override）
    resolved = resolve(text)
    assert resolved is not None
    event_name, handler = resolved
    assert event_name == "any_card_entered_or_sold"
    assert isinstance(handler, TaskActionHandler)

    # 参数化层（没有 override 兜底也必须正确）
    p_resolved = P.resolve_parametric(text, [])
    assert p_resolved is not None
    p_event, p_handler = p_resolved
    assert p_event == "any_card_entered_or_sold"
    assert isinstance(p_handler, TaskActionHandler)
    assert p_handler.goal == _IMPERIAL_GOAL
    assert p_handler.auto_reset

    for h in (handler.copy(), p_handler):
        tarven = _fresh_tarven(cards)
        slot = Slot(0, tarven)
        slot.card_type = "测试任务卡"
        slot.level = 6
        broadcasts = []
        orig = tarven.trigger_any_card_event

        def spy(event, _orig=orig, _b=broadcasts):
            if isinstance(event, AnyTaskFinishedEvent):
                _b.append(event)
            return _orig(event)

        tarven.trigger_any_card_event = spy
        try:
            for _ in range(_IMPERIAL_GOAL):
                h(slot, RoundEndEvent(tarven))
        finally:
            tarven.trigger_any_card_event = orig

        assert slot.count("战列巡航舰") == 2  # 触发2次 -> +2，而非 +1
        assert len(broadcasts) == 1  # 一次任务完成只广播一次
