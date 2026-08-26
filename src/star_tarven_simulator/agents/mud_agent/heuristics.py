"""泥巴流启发式：卡牌评分、理财卡识别、黄金矿工目标选择、对子检测。

评分尽量数据驱动（读 tags / level / 描述关键词 / 合法升级池），只对教学明确点名的
1 本卡用固定优先级表，不硬编码已在当前卡池失效的高本卡名。
"""

from __future__ import annotations

from typing import List, Optional

from star_tarven_simulator.simulator.card import Card
from star_tarven_simulator.simulator.game import Tarven
from star_tarven_simulator.simulator.slot import Slot
from star_tarven_simulator.upgrades import available_upgrades

# 教学（cv18106246）给出的 1 本卡三连优先级（分值越高越优先）。
# 死神火车 > 好兄弟 > 不死队 > 折跃援军 > 蟑螂小队 > 原始蟑螂 > 虫群先锋 > 发电站
MUD_ONE_COST_PRIORITY = {
    "死神火车": 100,
    "好兄弟": 90,
    "不死队": 80,
    "折跃援军": 70,
    "蟑螂小队": 60,
    "原始蟑螂": 50,
    "虫群先锋": 40,
    "发电站": 30,
}

# 理财卡（滚矿引擎）。死神火车靠"进场任务 +1 矿"，蟑螂小队注卵值钱。
ECONOMY_CARDS = {"死神火车", "蟑螂小队"}

# 拾荒猎人：出售发现 1 张 1 星卡，提供额外过牌/找关键卡（技巧 #3）。
SCAVENGER_CARD = "拾荒猎人"

GOLD_MINER = "黄金矿工"


def card_name(card) -> Optional[str]:
    if isinstance(card, Card):
        return card.name
    if isinstance(card, str):
        return card
    return None


def is_economy_card(name: Optional[str]) -> bool:
    return name in ECONOMY_CARDS


def one_cost_priority(name: Optional[str]) -> int:
    """返回教学 1 本卡优先级分；未列出的返回 0。"""
    return MUD_ONE_COST_PRIORITY.get(name or "", 0)


def buy_score(card: Card, t: Tarven) -> float:
    """给'是否购买/发现选择'一张卡打启发式分（越高越想要）。

    泥巴流关注：理财卡、能凑三连的对子、教学优先级高的 1 本卡。
    """
    if not isinstance(card, Card):
        return 0.0
    name = card.name
    score = 0.0

    # 理财引擎最优先。
    if is_economy_card(name):
        score += 120.0

    # 能与场上现有非金对子凑三连 → 极高价值（直接成三连）。
    if pair_partner_on_board(t, name) == 2:
        score += 200.0
    elif pair_partner_on_board(t, name) == 1:
        # 已有一张，拿到能形成对子，锁第三张。
        score += 60.0

    # 教学 1 本卡优先级。
    score += one_cost_priority(name)

    # 拾荒猎人：过牌/找关键卡。
    if name == SCAVENGER_CARD:
        score += 45.0

    # 低星卡更贴合"1 本理财三连"节奏（越低越好，1 星最佳）。
    score += max(0, 6 - card.level) * 2.0

    return score


def pair_partner_on_board(t: Tarven, name: Optional[str]) -> int:
    """场上有几张可与 ``name`` 组成三连的同名非金、非'无法三连'卡（0/1/2）。"""
    if not name:
        return 0
    return sum(
        1
        for s in t.slots
        if s.card_type == name
        and not s.tags.has("金色")
        and not s.tags.has("无法三连")
    )


def gold_miner_targets(t: Tarven) -> List[Slot]:
    """按'点黄金矿工命中率'从高到低排序的候选槽位。

    命中率 ≈ min(3, pool) / pool，pool 越小越高（黄金矿工是公共升级，
    ``discover_upgrades`` 从合法升级池均匀抽 3 个）。因此按 ``pool`` 升序排。
    只返回：非金色、升级槽未满、其合法升级池中仍含黄金矿工的槽。
    """
    candidates = []
    for s in t.slots:
        if s.card_type is None:
            continue
        if s.tags.has("金色"):
            continue
        if len(s.upgrades) >= s.upgrades_limit:
            continue
        pool = available_upgrades(s)
        if GOLD_MINER not in pool:
            continue
        candidates.append((len(pool), s))
    candidates.sort(key=lambda kv: kv[0])
    return [s for _, s in candidates]


def gold_miner_hit_rate(pool_size: int) -> float:
    """点一次升级抽中黄金矿工的概率（无放回抽 3 个）。"""
    if pool_size <= 0:
        return 0.0
    if pool_size <= 3:
        return 1.0
    return 3.0 / pool_size


def find_triple_pairs(t: Tarven) -> List[str]:
    """返回场上'恰有两张可三连同名卡'的卡名列表（拿到第三张即可合成）。"""
    from collections import Counter

    counts = Counter(
        s.card_type
        for s in t.slots
        if s.card_type is not None
        and not s.tags.has("金色")
        and not s.tags.has("无法三连")
    )
    result = []
    for name, cnt in counts.items():
        if cnt != 2:
            continue
        levels = {
            s.level
            for s in t.slots
            if s.card_type == name and not s.tags.has("金色") and not s.tags.has("无法三连")
        }
        if len(levels) == 1:  # merge_slots 要求同等级
            result.append(name)
    return result
