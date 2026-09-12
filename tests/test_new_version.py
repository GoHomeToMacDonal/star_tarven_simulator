"""v20260822 新版本机制测试。

覆盖 change_log 0826 引入的能力：
- 卵鞘词条：被出售时注卵价值较高的 n 个非英雄生物（n = 酒馆等级）。
- 地底伏击：集群(6) 效果改为「获得卵鞘」。
- 刀锋女王：降低虚空投影效率由 50% 增至 100%（效率归零）。
- 新英雄 维京战机 / 狂热者 进入 heros.json。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from star_tarven_simulator.loader import build_game, load_cards
from star_tarven_simulator.simulator.game import Tarven
from star_tarven_simulator.simulator.slot import Slot

HEROS_PATH = Path(__file__).resolve().parents[1] / "data" / "heros.json"


@pytest.fixture(scope="module")
def cards():
    cards, _ = load_cards()
    return cards


def _by_name(cards, name):
    return next(c for c in cards if c.name == name)


def _fresh_tarven(cards) -> Tarven:
    return build_game(cards, user_count=1).tarvens[0]


def _place(tarven: Tarven, card, idx: int) -> Slot:
    tarven.slots[idx] = Slot(idx, tarven)
    tarven.card_engine.assign_card_to_slot(card, tarven.slots[idx])
    return tarven.slots[idx]


def _egg(tarven: Tarven) -> Slot | None:
    return next((s for s in tarven.slots if s.card_type == "虫卵"), None)


# ---------------------------------------------------------------------------
# 卵鞘：出售时注卵卡牌内单位价值最高的 min(n, m) 个非英雄生物
# ---------------------------------------------------------------------------
def test_sheath_hatch_on_sell_level1(cards):
    tarven = _fresh_tarven(cards)
    roach = _by_name(cards, "蟑螂小队")  # 3 蟑螂(100) + 1 爆虫(65)
    slot = _place(tarven, roach, 0)
    assert slot.tags.has("拥有卵鞘")

    tarven.trigger_selling(slot)

    egg = _egg(tarven)
    assert egg is not None
    # n=1 → 单位价值最高的 1 个非英雄生物：蟑螂(100) > 爆虫(65)
    assert egg.units == {"蟑螂": 1}


def test_sheath_hatch_scales_with_level(cards):
    tarven = _fresh_tarven(cards)
    tarven.level = 3
    roach = _by_name(cards, "蟑螂小队")  # 3 蟑螂 + 1 爆虫
    slot = _place(tarven, roach, 0)

    tarven.trigger_selling(slot)

    egg = _egg(tarven)
    assert egg is not None
    # min(3, 4)=3 → 价值最高的 3 个 = 3 蟑螂
    assert egg.units == {"蟑螂": 3}


def test_sheath_hatch_caps_at_m(cards):
    tarven = _fresh_tarven(cards)
    tarven.level = 6
    roach = _by_name(cards, "蟑螂小队")  # 3 蟑螂 + 1 爆虫
    slot = _place(tarven, roach, 0)

    tarven.trigger_selling(slot)

    egg = _egg(tarven)
    assert egg is not None
    # min(6, 4)=4 → 全部 4 个非英雄生物单位
    assert egg.units == {"蟑螂": 3, "爆虫": 1}


def test_sheath_hatch_tie_break(cards):
    tarven = _fresh_tarven(cards)
    tarven.level = 2
    dragon = _by_name(cards, "腐化大龙")  # 4 守卫(250) + 4 腐化者(250)
    slot = _place(tarven, dragon, 0)

    tarven.trigger_selling(slot)

    egg = _egg(tarven)
    assert egg is not None
    # 同单位价值(250)随机选；min(2, 8)=2 个单位，且只可能是守卫/腐化者
    assert sum(egg.units.values()) == 2
    assert set(egg.units) <= {"守卫", "腐化者"}


# ---------------------------------------------------------------------------
# 集群(6):获得卵鞘 —— 给本卡添加「拥有卵鞘」词条
# ---------------------------------------------------------------------------
def test_swarm_grants_sheath(cards):
    tarven = _fresh_tarven(cards)
    ambush = _by_name(cards, "地底伏击")
    slot = _place(tarven, ambush, 0)

    zerg_cards = [c for c in cards if c.race == "zerg" and c.name != "地底伏击"]
    for i, zc in enumerate(zerg_cards[:5], start=1):
        _place(tarven, zc, i)

    assert not slot.tags.has("拥有卵鞘")
    tarven.round_end()
    assert slot.tags.has("拥有卵鞘")


# ---------------------------------------------------------------------------
# 刀锋女王：降低虚空投影效率 100%（效率归零）
# ---------------------------------------------------------------------------
def test_void_projection_efficiency_blade_queen(cards):
    tarven = _fresh_tarven(cards)
    assert tarven.void_projection_efficiency == 1.0

    tarven.slots[2] = Slot(2, tarven)
    tarven.slots[2].card_type = "刀锋女王"
    assert tarven.void_projection_efficiency == 0.0


def test_void_projection_gain_blocked_by_blade_queen(cards):
    army = _by_name(cards, "虚空大军")          # 具有虚空投影
    missionary = _by_name(cards, "不洁传教")     # 每回合开始给虚空投影卡 +1 混合体天罚者

    # 无刀锋女王：虚空大军获得 1 混合体天罚者
    tarven = _fresh_tarven(cards)
    army_slot = _place(tarven, army, 0)
    _place(tarven, missionary, 1)
    tarven.round_start()
    assert army_slot.count("混合体天罚者") == 1

    # 有刀锋女王：效率归零，不再获得
    tarven2 = _fresh_tarven(cards)
    army_slot2 = _place(tarven2, army, 0)
    _place(tarven2, missionary, 1)
    tarven2.slots[2] = Slot(2, tarven2)
    tarven2.slots[2].card_type = "刀锋女王"
    tarven2.round_start()
    assert army_slot2.count("混合体天罚者") == 0


# ---------------------------------------------------------------------------
# 覆盖率：新版本数据应达到 100% 解析
# ---------------------------------------------------------------------------
def test_new_version_full_coverage(cards):
    _, coverage = load_cards()
    assert coverage.rate == 1.0, coverage.summary()
    # 629 = 从地图提取的 v4.6.1.7 合并后描述行数（157 张卡）
    assert coverage.total_lines == 629


# ---------------------------------------------------------------------------
# 新英雄元数据
# ---------------------------------------------------------------------------
def test_new_heroes_in_heros_json():
    heros = json.loads(HEROS_PATH.read_text(encoding="utf-8"))
    by_name = {h["name"]: h for h in heros}

    assert by_name["维京战机"]["ability"] == "随机应变：每回合可以转换晶体矿和瓦斯"
    assert by_name["维京战机"]["description"] == "每回合，消耗2瓦斯以获得4晶体矿；或消耗4晶体矿以获得2瓦斯"

    assert by_name["狂热者"]["ability"] == "荣耀之战：你的所有单位在战斗时获得星空加速"
    assert "获得战士的财宝" in by_name["狂热者"]["description"]
    assert "获得10晶体矿10瓦斯" in by_name["狂热者"]["description"]
