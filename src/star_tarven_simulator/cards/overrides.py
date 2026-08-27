"""不规则效果的文本注册表（无法参数化的效果写在这里）。

key = 归一化描述文本（见 :func:`parsing.text.normalize`），普通/金色变体分别注册。
逻辑主要移植自旧版 ``cards/actions/*``，并修正了其中的已知 bug（larva 用 dict、
折跃落点、事件属性名等）。仍无法在"酒馆经济"层面建模的战斗/跨玩家效果标注 TODO 并置空。
"""

from __future__ import annotations

from star_tarven_simulator.cards.mechanics import feed, hatch, teleport
from star_tarven_simulator.cards.registry import register, register_value
from star_tarven_simulator.expansions import CORE_SOURCES
from star_tarven_simulator.constants.unit_prices import UNIT_PRICES
from star_tarven_simulator.constants.unit_type import (
    BIOLOGICAL_UNITS,
    ELITE_UNITS,
    HERO_UNITS,
    ROYAL_UNITS,
)
from star_tarven_simulator.simulator.action import ChooseCardAction
from star_tarven_simulator.simulator.event import EnteringEvent, Event
from star_tarven_simulator.simulator.card import Tags
from star_tarven_simulator.simulator.event_handler import (
    EventHandler,
    GatheringActionHandler,
    TaskActionHandler,
)
from star_tarven_simulator.simulator.slot import Slot


def reg(desc, event, fn):
    register_value(desc, event, fn)


def _transform(slot: Slot, event, card_name: str, reset_units: bool = False) -> None:
    """把 slot 变为另一张卡牌（用于「变为难民营地/刀锋女王/随机卡牌」等自定义变身）。

    默认保留原有单位（``reset_units=False``），仅替换 card_type / 星级 / tags / handler；
    ``reset_units=True`` 时清空并采用目标卡牌自带的单位。
    """
    engine = event.tarven.card_engine
    card = getattr(engine, "card_map", {}).get(card_name)
    if card is None:
        return
    slot.card_type = card.name
    slot.level = card.level
    slot.tags = Tags(card.tags)
    if reset_units:
        slot.units = {}
        slot.unit_count = 0
        for unit, cnt in card.units.items():
            slot.add_unit(unit, cnt)
    slot.event_handlers = [h.copy(slot.state, slot) for h in card.event_handlers]


def _bio_units_on(slot: Slot):
    """返回 slot 上的非英雄生物单位名列表（去重）。"""
    return [u for u in slot.units if u in BIOLOGICAL_UNITS and u not in HERO_UNITS]


def _upgrade_shop_card(tarven, idx: int, delta: int) -> None:
    """把商店第 idx 张卡替换成"星级 +delta"的同随机卡牌。"""
    if idx < 0 or idx >= len(tarven.shop):
        return
    card = tarven.shop[idx]
    if card is None:
        return
    new_level = min(6, card.level + delta)
    uuid = tarven.pool.sample(levels=[new_level])
    if uuid is not None:
        tarven.pool.place_back(card)
        tarven.shop[idx] = tarven.pool.card_map[uuid]


def tech_addon_count(slot: Slot) -> int:
    """全场含科技实验室 / 高级科技实验室的卡牌数。"""
    return sum(
        1
        for s in slot.all
        if s.count("科技实验室") > 0 or s.count("高级科技实验室") > 0
    )


# ===========================================================================
# 引擎特判占位（Tarven.trigger_selling 特殊处理，此处仅标记"已处理"）
# ===========================================================================
def _noop(slot, event):
    return None


reg("出售时,发现1张其他1星卡牌,不获得出售晶体矿且不触发其他出售特效", "selling", _noop)
reg("出售时,发现2张其他1星卡牌,不获得出售晶体矿且不触发其他出售特效", "selling", _noop)


# ===========================================================================
# 人族：挂件 / 科技挂件 / 反应堆
# ===========================================================================
def _addon_change_neighbors(slot, event):
    for s in slot.neighbors:
        if s.tags.has("terran"):
            s.change_add_on()


reg("进场时,相邻两侧人族卡牌的挂件类型改变", "entering", _addon_change_neighbors)


def _addon_left_advanced(slot, event):
    left = slot.left
    if left is not None and left.card_type is not None and left.tags.has("terran"):
        left.change_add_on("高级科技实验室")


reg("进场时,相邻左侧人族卡牌的挂件类型变为高级科技实验室", "entering", _addon_left_advanced)


def _reno_on_addon_change(n):
    def h(slot, event):
        target = getattr(event, "addon_changed_slot", None)
        if target is not None:
            target.add_unit("雷诺(狙击手)", n)
    return h


reg("任意卡牌挂件变更时,相应卡牌获得1雷诺(狙击手)", "any_card_addon_changed", _reno_on_addon_change(1))
reg("任意卡牌挂件变更时,相应卡牌获得2雷诺(狙击手)", "any_card_addon_changed", _reno_on_addon_change(2))


def _same_addon_gain(n):
    def h(slot, event):
        addons = ["反应堆", "科技实验室", "高级科技实验室", "信号塔"]
        if max((sum(s.count(a) for s in slot.all) for a in addons), default=0) >= 4:
            slot.add_unit("恶蝠游骑兵", n)
    return h


reg("每回合结束时,至少4个挂件相同,获得1恶蝠游骑兵", "round_end", _same_addon_gain(1))
reg("每回合结束时,至少4个挂件相同,获得2恶蝠游骑兵", "round_end", _same_addon_gain(2))


def _tech_addon_gain(k, n, unit):
    def h(slot, event):
        if tech_addon_count(slot) >= k:
            slot.add_unit(unit, n)
    return h


reg("每回合结束时,若拥有至少4科技挂件,则获得1攻城坦克", "round_end", _tech_addon_gain(4, 1, "攻城坦克"))
reg("每回合结束时,若拥有至少4科技挂件,则获得2攻城坦克", "round_end", _tech_addon_gain(4, 2, "攻城坦克"))
reg("每回合结束时,若拥有至少5科技挂件,则获得2战狼", "round_end", _tech_addon_gain(5, 2, "战狼"))
reg("每回合结束时,若拥有至少5科技挂件,则获得4战狼", "round_end", _tech_addon_gain(5, 4, "战狼"))
reg("每回合结束时,若拥有至少4科技挂件,则获得3女妖(精英)", "round_end", _tech_addon_gain(4, 3, "女妖(精英)"))
reg("每回合结束时,若拥有至少4科技挂件,则获得6女妖(精英)", "round_end", _tech_addon_gain(4, 6, "女妖(精英)"))


def _reactor_terran_gain(n):
    def h(slot, event):
        for s in slot.all:
            if s.tags.has("terran") and s.count("反应堆") > 0:
                s.add_unit("陆战队员", n)
    return h


reg("每回合结束时,每张具有反应堆的人族卡牌获得2陆战队员", "round_end", _reactor_terran_gain(2))
reg("每回合结束时,每张具有反应堆的人族卡牌获得4陆战队员", "round_end", _reactor_terran_gain(4))


def _elite_tank_wolf(repeat):
    def h(slot, event):
        for _ in range(repeat):
            candidates = []
            for s in slot.all:
                candidates += [(s, "攻城坦克")] * s.count("攻城坦克")
                candidates += [(s, "战狼")] * s.count("战狼")
            cnt = tech_addon_count(slot)
            if len(candidates) > cnt:
                candidates = event.tarven.rng.sample(candidates, cnt) if cnt <= len(candidates) else candidates
            for s, unit in candidates:
                s.replace_unit(unit, 1, unit + "(精英)", 1)
    return h


reg("每回合结束时,场上每有1科技挂件,精英化场上1攻城坦克或战狼", "round_end", _elite_tank_wolf(1))
reg("每回合结束时,场上每有1科技挂件,精英化场上1攻城坦克或战狼,重复2次", "round_end", _elite_tank_wolf(2))


def _elite_marine_marauder(n):
    def h(slot, event):
        for s in slot.all:
            if s.tags.has("terran"):
                s.replace_unit("陆战队员", n, "陆战队员(精英)", n)
                s.replace_unit("劫掠者", n, "劫掠者(精英)", n)
    return h


reg("每回合结束时,每张人族卡牌将3陆战队员和3劫掠者精英化", "round_end", _elite_marine_marauder(3))
reg("每回合结束时,每张人族卡牌将5陆战队员和5劫掠者精英化", "round_end", _elite_marine_marauder(5))


def _elite_marine_to_shield(n):
    def h(slot, event):
        for s in slot.all:
            for _ in range(n):
                s.replace_unit("陆战队员(精英)", 1, "帝盾卫兵", 1)
    return h


reg("每回合结束时,每张卡牌将1陆战队员(精英)变为帝盾卫兵", "round_end", _elite_marine_to_shield(1))
reg("每回合结束时,每张卡牌将2陆战队员(精英)变为帝盾卫兵", "round_end", _elite_marine_to_shield(2))


def _quick_elite_goliath_viking(n):
    def h(slot, event):
        for s in slot.neighbors:
            s.replace_unit("歌利亚", n, "歌利亚(精英)", n)
            s.replace_unit("维京战机", n, "维京战机(精英)", n)
    return h


reg("快速生产:相邻两侧卡牌将3歌利亚和3维京战机精英化", "quick_produce", _quick_elite_goliath_viking(3))
reg("快速生产:相邻两侧卡牌将6歌利亚和6维京战机精英化", "quick_produce", _quick_elite_goliath_viking(6))


def _first_sold_terran(count):
    def h(slot, event):
        current = event.tarven.round
        key = "first_sold_round"
        if slot.task_vars.get(key) != current:
            slot.task_vars[key] = current
            slot.task_vars["first_sold_cnt"] = 0
        sold = getattr(event, "sold_slot", None)
        if sold is None or not sold.tags.has("terran"):
            return
        if slot.task_vars["first_sold_cnt"] < count:
            for u, c in sold.units.items():
                slot.add_unit(u, c)
            slot.task_vars["first_sold_cnt"] += 1
    return h


reg("唯一:获得每回合出售的第一张人族卡牌的单位", "any_card_sold", _first_sold_terran(1))
reg("唯一:获得每回合出售的前两张人族卡牌的单位", "any_card_sold", _first_sold_terran(2))


def _marine_to_kentaur(per):
    def h(slot, event):
        for s in slot.neighbors:
            s.replace_all_units("陆战队员", per, "牛头人陆战队员", 1)
            # 精英视为 2 个 -> 每 ceil(per/2) 个精英换 1
            s.replace_all_units("陆战队员(精英)", max(1, (per + 1) // 2), "牛头人陆战队员", 1)
    return h


reg("进场时,相邻两侧卡牌将每5个陆战队员变为1牛头人陆战队员(精英视为2个)", "entering", _marine_to_kentaur(5))


def _royal_until_top3(slot, event):
    prices = sorted((s.price() for s in slot.all), reverse=True)
    if len(prices) < 3:
        return
    guard = 0
    while slot.price() < prices[2] and guard < 100:
        slot.add_unit(event.tarven.rng.choice(ROYAL_UNITS), 1)
        guard += 1


reg("进场时,重复获得皇家单位,直到价值成为你卡牌前三", "entering", _royal_until_top3)


def _neighbors_gain(unit, n):
    def h(slot, event):
        for s in slot.neighbors:
            s.add_unit(unit, n)
    return h


reg("每回合结束时,相邻两侧卡牌获得1牛头人陆战队员", "round_end", _neighbors_gain("牛头人陆战队员", 1))


def _neighbors_terran_gain(unit, n):
    def h(slot, event):
        for s in slot.neighbors:
            if s.tags.has("terran"):
                s.add_unit(unit, n)
    return h


reg("每回合结束时,相邻两侧人族卡牌获得2蜘蛛雷", "round_end", _neighbors_terran_gain("蜘蛛雷", 2))
reg("每回合结束时,相邻两侧人族卡牌获得4蜘蛛雷", "round_end", _neighbors_terran_gain("蜘蛛雷", 4))


def _royal_when_no_terran(slot, event):
    if len(slot.terran) == 0:
        slot.add_unit("雷神(皇家卫队)", 1)


reg("每回合结束时,若场上没有人族卡牌,获得1雷神(皇家卫队)", "round_end", _royal_when_no_terran)


def _destroy_zerg_neighbors(unit):
    def h(slot, event):
        count = 0
        for s in list(slot.neighbors):
            if s.tags.has("zerg"):
                event.tarven.destroy(s)
                count += 1
        if count:
            slot.add_unit(unit, count)
    return h


reg("每回合结束时,摧毁相邻两侧虫族卡牌,获得等量的劫掠者(皇家卫队)", "round_end", _destroy_zerg_neighbors("劫掠者(皇家卫队)"))
reg("每回合结束时,摧毁相邻两侧虫族卡牌,获得等量的攻城坦克(皇家卫队)", "round_end", _destroy_zerg_neighbors("攻城坦克(皇家卫队)"))


def _teleport_stormcrow_if_full(n):
    def h(slot, event):
        if len(slot.all) == 7:
            teleport(slot, event, {"风暴战舰(精英)": n})
    return h


reg("每回合结束时,若场上没有空格,折跃1风暴战舰(精英)", "round_end", _teleport_stormcrow_if_full(1))
reg("每回合结束时,若场上没有空格,折跃2风暴战舰(精英)", "round_end", _teleport_stormcrow_if_full(2))


# ===========================================================================
# 升级类（进场/集群 获得升级）
# ===========================================================================
def _gain_upgrade(name):
    def h(slot, event):
        slot.upgrade(name)
    return h


reg("进场时,获得暗影战士升级", "entering", _gain_upgrade("暗影战士"))
reg("进场时,获得轨道空降升级", "entering", _gain_upgrade("轨道空降"))


def _swarm_gain_upgrade(k, name):
    def h(slot, event):
        if (len(slot.zerg) + slot.has_narud) >= k:
            slot.upgrade(name)
    return h


reg("集群(7):获得轨道空降升级", "round_end", _swarm_gain_upgrade(7, "轨道空降"))


def _discover_aux(slot, event):
    event.tarven.discover(tags=["辅助卡"])


reg("进场时,发现一张辅助卡", "entering", _discover_aux)


_RANDOM_UPGRADES = ["聚能器", "轨道空降", "暗影战士", "折跃援军", "护盾充能", "吸血"]


def _consume_gas_upgrade(slot, event):
    if event.tarven.gas >= 1 and len(slot.upgrades) < slot.upgrades_limit:
        event.tarven.gas -= 1
        event.tarven.trigger_upgrade(slot, event.tarven.rng.choice(_RANDOM_UPGRADES))


reg("进场时,此卡牌尝试消耗1瓦斯获得随机升级", "entering", _consume_gas_upgrade)


# ===========================================================================
# 任务
# ===========================================================================
def _reset_tasks(slot, event):
    for h in slot.event_handlers:
        if isinstance(h.action_handler, TaskActionHandler):
            h.action_handler.reset()


reg("每回合结束时,重置任务", "round_end", _reset_tasks)
reg("提升酒馆等级时,重置任务", "level_up", _reset_tasks)
reg("升级酒馆时,重置任务", "level_up", _reset_tasks)


def _task_advanced_addon(slot, event):
    for s in slot.neighbors:
        if s.tags.has("terran"):
            s.change_add_on("高级科技实验室")


def _task_terran_entered(slot, event):
    entered = getattr(event, "entered_slot", None)
    if entered is None or not entered.tags.has("terran"):
        return
    counter = slot.task_vars.get("terran_enter_task", 0)
    if counter >= 3:
        return
    counter += 1
    slot.task_vars["terran_enter_task"] = counter
    if counter == 3:
        _task_advanced_addon(slot, event)
        event.tarven.trigger_any_task_finished(slot)


register_value(
    "任务:进场3张人族卡牌 奖励:相邻两侧人族卡牌的挂件类型变为高级科技实验室",
    "any_card_entered",
    _task_terran_entered,
)


def _jackson_on_finished_sold(n):
    def h(slot, event):
        sold = getattr(event, "sold_slot", None)
        if sold is None:
            return
        for handler in sold.event_handlers:
            if isinstance(handler.action_handler, TaskActionHandler) and handler.action_handler.is_finished():
                slot.add_unit("杰克森的复仇号", n)
                return
    return h


reg("任务已完成的卡牌出售时,此卡牌获得1杰克森的复仇号", "any_card_sold", _jackson_on_finished_sold(1))
reg("任务已完成的卡牌出售时,此卡牌获得2杰克森的复仇号", "any_card_sold", _jackson_on_finished_sold(2))


# --- 帝国舰队：金色任务奖励尾部 "触发2次" 不再被静默忽略 --------------------
#     金色完整文本：任务:进场或出售6张卡牌 奖励:获得1战列巡航舰并重置此任务,触发2次
#     参数化解析过去把 "触发2次" 当作普通 +1（尾句被静默吞掉）。这里注册精确
#     TaskActionHandler：goal=6、事件 any_card_entered_or_sold、auto_reset=True、
#     一次完成获得 2 战列巡航舰；任务完成广播仍只发生一次（handler 在一次 __call__
#     内加 2 个，TaskActionHandler 只在达到 goal 的那次调用广播）。
#     （普通版本保持每次完成 +1，由参数化 resolve_task 处理。）
def _battlecruiser_reward(n):
    def h(slot, event):
        slot.add_unit("战列巡航舰", n)
    return h


register_value(
    "任务:进场或出售6张卡牌 奖励:获得1战列巡航舰并重置此任务,触发2次",
    "any_card_entered_or_sold",
    TaskActionHandler(_battlecruiser_reward(2), 6, auto_reset=True),
)


# ===========================================================================
# 灵能
# ===========================================================================
def _psi_balance_roach_hydra(slot, event):
    if slot.psi_level >= event.tarven.psi_level_max:
        return
    for s in slot.all:
        roach = s.count("蟑螂") + s.count("蟑螂(精英)")
        hydra = s.count("刺蛇") + s.count("刺蛇(精英)")
        if roach > hydra:
            s.add_unit("刺蛇", roach - hydra)
        elif hydra > roach:
            s.add_unit("蟑螂", hydra - roach)


reg("灵能:所有卡牌的蟑螂/刺蛇补至等量", "round_end", _psi_balance_roach_hydra)


def _psi_elite_all(slot, event):
    if slot.psi_level >= event.tarven.psi_level_max:
        return
    for s in slot.all:
        if s.psi_level > 0:
            for unit in list(s.units.keys()):
                if unit in ELITE_UNITS:
                    cnt = s.count(unit)
                    s.remove_unit(unit, cnt)
                    s.add_unit(unit + "(精英)", cnt)


reg("灵能:每张具有灵能的卡牌将其所有单位精英化", "round_end", _psi_elite_all)


def _psi_discover(n):
    def h(slot, event):
        if slot.psi_level < event.tarven.psi_level_max:
            for _ in range(n):
                event.tarven.discover(tags=["灵能"])
    return h


reg("灵能:发现1张其他具有灵能的卡牌", "round_end", _psi_discover(1))
reg("灵能:发现2张其他具有灵能的卡牌", "round_end", _psi_discover(2))


def _psi_darkness_container(slot, event):
    if slot.psi_level >= event.tarven.psi_level_max:
        return
    for s in slot.neighbors:
        s.tags.add("具有黑暗容器")


reg("灵能:使相邻卡牌获得黑暗容器", "round_end", _psi_darkness_container)


def _psi_discover_kuangcu(slot, event):
    if slot.psi_level < event.tarven.psi_level_max:
        event.tarven.store_card_to_cache("矿簇")


reg("灵能:获得卡牌「矿簇」", "round_end", _psi_discover_kuangcu)


def _trigger_all_psi(slot, event):
    from star_tarven_simulator.simulator.event import RoundEndEvent

    for s in slot.all:
        if s.psi_level > 0:
            for handler in s.event_handlers:
                if handler.description.startswith("灵能"):
                    handler.action_handler(s, RoundEndEvent(event.tarven))


reg("进场时,触发所有卡牌的灵能效果", "entering", _trigger_all_psi)


# ===========================================================================
# 折跃 / 集结
# ===========================================================================
def _teleport_dragoon_by_parts(slot, event):
    teleport(slot, event, {"龙骑士": slot.count("零件")})


reg("出售时,折跃n龙骑士 (n=零件数量)", "selling", _teleport_dragoon_by_parts)


def _gain_and_teleport_random(n):
    def h(slot, event):
        slot.add_unit("激励者", n)
        for _ in range(n):
            teleport(slot, event, {event.tarven.rng.choice(["不朽者", "掠夺者", "巨像"]): 1})
    return h


reg("每回合结束时,获得1激励者,并随机折跃1个不朽者/掠夺者/巨像", "round_end", _gain_and_teleport_random(1))
reg("每回合结束时,获得2激励者,并随机折跃2个不朽者/掠夺者/巨像", "round_end", _gain_and_teleport_random(2))


def _teleport_colossus_by_energy(per):
    def h(slot, event):
        times = min(slot.energy // 7, 2)
        teleport(slot, event, {"巨像(精英)": times * per})
    return h


reg("每回合结束时,每有7能量强度折跃1巨像(精英),最多2", "round_end", _teleport_colossus_by_energy(1))
reg("每回合结束时,每有7能量强度折跃2巨像(精英),最多4", "round_end", _teleport_colossus_by_energy(2))


def _gathering_kadarin(per):
    def inner(slot, event, times):
        teleport(slot, event, {"凯达林巨石": times * per})
    return GatheringActionHandler(inner, 7)


register_value("集结(7):折跃1凯达林巨石", "round_end", _gathering_kadarin(1))
register_value("集结(7):折跃2凯达林巨石", "round_end", _gathering_kadarin(2))


def _gathering_immortal_templar(n):
    def inner(slot, event, times):
        num = min(times * n, slot.count("不朽者"))
        slot.remove_unit("不朽者", num)
        slot.add_unit("英雄不朽者", num)
        # 随机若干非英雄生物 -> 高阶圣堂武士
        pool = []
        for u, c in slot.units.items():
            if u in BIOLOGICAL_UNITS and u not in HERO_UNITS:
                pool += [u] * c
        take = min(times * n, len(pool))
        for u in event.tarven.rng.sample(pool, take) if take else []:
            slot.remove_unit(u, 1)
            slot.add_unit("高阶圣堂武士", 1)
    return GatheringActionHandler(inner, 7)


register_value("集结(7):将1不朽者变为英雄不朽者并将1非英雄生物单位变为高阶圣堂武士", "round_end", _gathering_immortal_templar(1))
register_value("集结(7):将2不朽者变为英雄不朽者并将2非英雄生物单位变为高阶圣堂武士", "round_end", _gathering_immortal_templar(2))


def _gathering_take_elites(slot, event, times):
    for s in slot.protoss:
        if s is slot:
            continue
        for u in list(s.units.keys()):
            if u.endswith("(精英)"):
                c = s.count(u)
                s.remove_unit(u, c)
                slot.add_unit(u, c)


register_value("唯一:集结(13):抽取场上神族卡牌中的精英单位", "round_end", GatheringActionHandler(_gathering_take_elites, 13))


# ===========================================================================
# 集群 / 虫族转化
# ===========================================================================
def _swarm_replace(k, src, n, dst):
    def h(slot, event):
        if (len(slot.zerg) + slot.has_narud) >= k:
            for _ in range(n):
                slot.replace_unit(src, 1, dst, 1)
    return h


reg("集群(2):将此卡牌1陆战队员变为被感染的陆战队员", "round_end", _swarm_replace(2, "陆战队员", 1, "被感染的陆战队员"))
reg("集群(2):将此卡牌2陆战队员变为被感染的陆战队员", "round_end", _swarm_replace(2, "陆战队员", 2, "被感染的陆战队员"))


def _swarm_seize_by_level(k, levels):
    def h(slot, event):
        if (len(slot.zerg) + slot.has_narud) >= k:
            for s in list(slot.all):
                if s is not slot and s.level in levels:
                    event.tarven.seize(s, slot)
    return h


reg("集群(7):夺取场上所有1-2星卡牌", "round_end", _swarm_seize_by_level(7, {1, 2}))
reg("集群(7):夺取场上所有1-3星卡牌", "round_end", _swarm_seize_by_level(7, {1, 2, 3}))


def _infest_marines_larva(n):
    def h(slot, event):
        for s in slot.all:
            cnt = min(s.count("陆战队员"), n)
            if cnt:
                s.remove_unit("陆战队员", cnt)
                event.tarven.larva({"被感染的陆战队员": cnt})
    return h


reg("每回合结束时,每张卡牌将2陆战队员感染,并将他们注卵", "round_end", _infest_marines_larva(2))
reg("每回合结束时,每张卡牌将4陆战队员感染,并将他们注卵", "round_end", _infest_marines_larva(4))


def _infested_to_aberration(n):
    def h(slot, event):
        for s in slot.all:
            for _ in range(n):
                s.replace_unit("被感染的陆战队员", 1, "畸变体", 1)
    return h


reg("每回合开始时,每张卡牌将1被感染的陆战队员变为畸变体", "round_start", _infested_to_aberration(1))
reg("每回合开始时,每张卡牌将2被感染的陆战队员变为畸变体", "round_start", _infested_to_aberration(2))


def _roach_to_destroyer(n):
    def h(slot, event):
        for _ in range(n):
            slot.replace_unit("蟑螂", 1, "破坏者", 1)
    return h


reg("每回合开始时,将1蟑螂变为破坏者", "round_start", _roach_to_destroyer(1))
reg("每回合开始时,将2蟑螂变为破坏者", "round_start", _roach_to_destroyer(2))


def _corruptor_neighbors(gain_n, larva_extra):
    def h(slot, event):
        add_cnt = 0
        larva_cnt = 0
        for s in slot.neighbors:
            if s.tags.has("zerg"):
                larva_cnt += 1
            else:
                add_cnt += gain_n
        if larva_cnt:
            payload = {"腐化者": larva_cnt}
            if larva_extra:
                payload["异龙"] = larva_cnt
            event.tarven.larva(payload)
        if add_cnt:
            slot.add_unit("腐化者", add_cnt)
    return h


reg("每回合结束时,每相邻1非虫族卡牌,获得2腐化者;每相邻1虫族卡牌,注卵1腐化者", "round_end", _corruptor_neighbors(2, False))
reg("每回合结束时,每相邻1非虫族卡牌,获得3腐化者;每相邻1虫族卡牌,注卵1腐化者和1异龙", "round_end", _corruptor_neighbors(3, True))


def _replace_larva_unit(src, n, dst):
    def h(slot, event):
        for _ in range(n):
            slot.replace_unit(src, 1, dst, 1)
    return h


reg("任意卡牌注卵时,此卡牌将1幼雷兽变为雷兽", "any_card_larva", _replace_larva_unit("幼雷兽", 1, "雷兽"))
reg("任意卡牌注卵时,此卡牌将2幼雷兽变为雷兽", "any_card_larva", _replace_larva_unit("幼雷兽", 2, "雷兽"))


def _nonzerg_gain(unit, n):
    def h(slot, event):
        entered = getattr(event, "entered_slot", None)
        if entered is not None and not entered.tags.has("zerg"):
            slot.add_unit(unit, n)
    return h


reg("任意非虫族卡牌进场时,获得1破坏者(精英)", "any_card_entered", _nonzerg_gain("破坏者(精英)", 1))
reg("任意非虫族卡牌进场时,获得2破坏者(精英)", "any_card_entered", _nonzerg_gain("破坏者(精英)", 2))


def _neighbors_replace(src, dst, n):
    def h(slot, event):
        for s in slot.neighbors:
            for _ in range(n):
                s.replace_unit(src, 1, dst, 1)
    return h


reg("进场时,相邻两侧卡牌将1蟑螂变为莽兽", "entering", _neighbors_replace("蟑螂", "莽兽", 1))
reg("进场时,相邻两侧卡牌将2蟑螂变为莽兽", "entering", _neighbors_replace("蟑螂", "莽兽", 2))


def _each_zerg_gain(unit, n):
    def h(slot, event):
        for s in slot.zerg:
            s.add_unit(unit, n)
    return h


reg("进场时,每张虫族卡牌获得2腐化者", "entering", _each_zerg_gain("腐化者", 2))
reg("进场时,每张虫族卡牌获得4腐化者", "entering", _each_zerg_gain("腐化者", 4))


def _neighbors_zerg_gain(unit, n):
    def h(slot, event):
        for s in slot.neighbors:
            if s.tags.has("zerg"):
                s.add_unit(unit, n)
    return h


reg("每回合结束时,相邻两侧虫族卡牌获得2守卫", "round_end", _neighbors_zerg_gain("守卫", 2))
reg("每回合结束时,相邻两侧虫族卡牌获得4守卫", "round_end", _neighbors_zerg_gain("守卫", 4))


def _to_brood_lord(n):
    def h(slot, event):
        for _ in range(n):
            slot.replace_unit("异龙", 1, "巢虫领主", 1)
            slot.replace_unit("守卫", 1, "巢虫领主", 1)
            slot.replace_unit("腐化者", 1, "巢虫领主", 1)
    return h


reg("每回合结束时,此卡牌将1异龙、1守卫、1腐化者变为巢虫领主", "round_end", _to_brood_lord(1))
reg("每回合结束时,此卡牌将2异龙、2守卫、2腐化者变为巢虫领主", "round_end", _to_brood_lord(2))


def _steal_zerglings_to_banelings(slot, event):
    sold = getattr(event, "sold_slot", None)
    if sold is None:
        return
    cnt = sold.count("跳虫") + sold.count("跳虫(精英)")
    if cnt:
        slot.add_unit("爆虫", cnt)


reg("唯一:获得任意出售卡牌中的跳虫,并将其变为爆虫", "any_card_sold", _steal_zerglings_to_banelings)


def _hatch_baneling(threshold):
    def h(slot, event):
        total = sum(s.count("爆虫") for s in slot.all)
        hatch(slot, event, {"爆虫": total // threshold})
    return h


reg("唯一:每回合结束时,场上每有20爆虫此卡牌孵化1爆虫", "round_end", _hatch_baneling(20))
reg("唯一:每回合结束时,场上每有15爆虫此卡牌孵化1爆虫", "round_end", _hatch_baneling(15))


def _doom_on_valuable_zerg_sold(slot, event):
    sold = getattr(event, "sold_slot", None)
    if sold is not None and sold.tags.has("zerg") and sold.price() >= 3600:
        slot.add_unit("末日巨兽", 1)


reg("唯一:任意价值达到3600的虫族卡牌出售时,此卡牌获得1末日巨兽", "any_card_sold", _doom_on_valuable_zerg_sold)


def _hatch_extra_to_self(gain_brood):
    def h(slot, event):
        for u, c in getattr(event, "units", {}).items():
            slot.add_unit(u, c)
        if gain_brood:
            slot.add_unit("巢虫领主", 1)
    return h


reg("唯一:任意卡牌进行孵化时,单位额外孵化到此卡牌", "any_card_hatch", _hatch_extra_to_self(False))
reg("唯一:任意卡牌进行孵化时,单位额外孵化到此卡牌,并且此卡牌获得1巢虫领主", "any_card_hatch", _hatch_extra_to_self(True))


# ===========================================================================
# 神族：水晶塔 / 虚空 / 精英化
# ===========================================================================
def _random_pylon_to_void(n):
    def h(slot, event):
        from star_tarven_simulator.simulator.event import AnyCardGainVoidCrystalTowerEvent

        for _ in range(n):
            candidates = [s for s in slot.all for _ in range(s.count("水晶塔"))]
            if not candidates:
                break
            s = event.tarven.rng.choice(candidates)
            s.replace_unit("水晶塔", 1, "虚空水晶塔", 1)
            event.tarven.trigger_any_card_event(
                AnyCardGainVoidCrystalTowerEvent(event.tarven, s)
            )
    return h


reg("任意卡牌进场时,将场上随机2水晶塔变为虚空水晶塔", "any_card_entered", _random_pylon_to_void(2))
reg("任意卡牌进场时,将场上随机4水晶塔变为虚空水晶塔", "any_card_entered", _random_pylon_to_void(4))


def _neighbors_protoss_gain(unit, n):
    def h(slot, event):
        for s in slot.neighbors:
            if s.tags.has("protoss"):
                s.add_unit(unit, n)
    return h


reg("进场时,相邻两侧神族卡牌获得1水晶塔", "entering", _neighbors_protoss_gain("水晶塔", 1))
reg("进场时,相邻两侧神族卡牌获得2水晶塔", "entering", _neighbors_protoss_gain("水晶塔", 2))


def _protoss_bio_to_immortal(n):
    def h(slot, event):
        for s in slot.protoss:
            pool = [u for u in s.units for _ in range(s.count(u)) if u in BIOLOGICAL_UNITS]
            take = min(n, len(pool))
            for u in (event.tarven.rng.sample(pool, take) if take else []):
                s.remove_unit(u, 1)
            s.add_unit("不朽者", take)
    return h


reg("进场时,每张神族卡牌将2生物单位变为不朽者", "entering", _protoss_bio_to_immortal(2))
reg("进场时,每张神族卡牌将4生物单位变为不朽者", "entering", _protoss_bio_to_immortal(4))


def _elite_entered_protoss(n):
    def h(slot, event):
        entered = getattr(event, "entered_slot", None)
        if entered is None or not entered.tags.has("protoss"):
            return
        pool = [u for u in entered.units for _ in range(entered.count(u)) if u in ELITE_UNITS]
        take = min(n, len(pool))
        for u in (event.tarven.rng.sample(pool, take) if take else []):
            entered.remove_unit(u, 1)
            entered.add_unit(u + "(精英)", 1)
    return h


reg("任意神族卡牌进场时,精英化其3单位", "any_card_entered", _elite_entered_protoss(3))
reg("任意神族卡牌进场时,精英化其6单位", "any_card_entered", _elite_entered_protoss(6))


def _zealot_apostle_to_stalker(slot, event):
    for s in slot.neighbors:
        s.replace_all_units("狂热者", 1, "旋风狂热者", 1)
        s.replace_all_units("狂热者(精英)", 1, "旋风狂热者(精英)", 1)
        s.replace_all_units("使徒", 1, "旋风狂热者", 1)
        s.replace_all_units("使徒(精英)", 1, "旋风狂热者(精英)", 1)


reg("进场时,相邻两侧卡牌的狂热者和使徒变为旋风狂热者", "entering", _zealot_apostle_to_stalker)


def _void_elite(n):
    def h(slot, event):
        for s in slot.all:
            if s.count("虚空水晶塔") > 0:
                pool = [u for u in s.units for _ in range(s.count(u)) if u in ELITE_UNITS]
                take = min(n, len(pool))
                for u in (event.tarven.rng.sample(pool, take) if take else []):
                    s.replace_unit(u, 1, u + "(精英)", 1)
    return h


reg("进场及每回合结束时,拥有虚空水晶塔的卡牌精英化其1单位", ["entering", "round_end"], _void_elite(1))
reg("进场及每回合结束时,拥有虚空水晶塔的卡牌精英化其2单位", ["entering", "round_end"], _void_elite(2))


def _void_gain_dt(n):
    def h(slot, event):
        target = getattr(event, "slot", None)
        if target is not None:
            target.add_unit("黑暗圣堂武士", n)
    return h


reg("唯一:任意卡牌获得虚空水晶塔时,为其添加3黑暗圣堂武士", "any_card_gain_void_crystal_tower", _void_gain_dt(3))
reg("唯一:任意卡牌获得虚空水晶塔时,为其添加5黑暗圣堂武士", "any_card_gain_void_crystal_tower", _void_gain_dt(5))


def _void_glaive_check(slot, event):
    if slot.count("虚空辉光舰") > slot.energy:
        from star_tarven_simulator.simulator.event import AnyCardGainVoidCrystalTowerEvent

        slot.add_unit("虚空水晶塔", 1)
        event.tarven.trigger_any_card_event(AnyCardGainVoidCrystalTowerEvent(event.tarven, slot))


reg("每回合结束时,若此卡牌虚空辉光舰数量大于能量强度,则获得1虚空水晶塔", "round_end", _void_glaive_check)


def _highest_energy_protoss(n):
    def h(slot, event):
        protoss = slot.protoss
        if not protoss:
            return
        best = max(protoss, key=lambda s: s.energy)
        best.add_unit("不朽者(精英)", n)
    return h


reg("每回合结束时,能量强度最高的神族卡牌获得2不朽者(精英)", "round_end", _highest_energy_protoss(2))
reg("每回合结束时,能量强度最高的神族卡牌获得4不朽者(精英)", "round_end", _highest_energy_protoss(4))


def _hero_immortal_if_5_protoss(n):
    def h(slot, event):
        if (len(slot.protoss) + slot.has_narud) >= 5:
            slot.add_unit("英雄不朽者", n)
    return h


reg("唯一:每回合结束时,若拥有至少5张神族卡牌,则获得1英雄不朽者", "round_end", _hero_immortal_if_5_protoss(1))
reg("唯一:每回合结束时,若拥有至少5张神族卡牌,则获得2英雄不朽者", "round_end", _hero_immortal_if_5_protoss(2))


def _heal_on_protoss_entered(n):
    def h(slot, event):
        entered = getattr(event, "entered_slot", None)
        if entered is not None and entered.tags.has("protoss"):
            event.tarven.health += n
    return h


reg("任意神族卡牌进场时,恢复2生命值", "any_card_entered", _heal_on_protoss_entered(2))
reg("任意神族卡牌进场时,恢复4生命值", "any_card_entered", _heal_on_protoss_entered(4))


def _levelup_apostle_teleport_all(n):
    def h(slot, event):
        slot.add_unit("使徒(精英)", n)
        payload = dict(slot.units)
        for u, c in list(slot.units.items()):
            slot.remove_unit(u, c)
        teleport(slot, event, payload)
    return h


reg("提升酒馆等级时,获得6使徒(精英),再将此牌中所有单位一同折跃", "level_up", _levelup_apostle_teleport_all(6))
reg("提升酒馆等级时,获得12使徒(精英),再将此牌中所有单位一同折跃", "level_up", _levelup_apostle_teleport_all(12))


# ===========================================================================
# 黑暗值
# ===========================================================================
def _darkness_gain_with_upgrade(n):
    def h(slot, event):
        slot.add_unit("鲜血猎手", n)
        slot.upgrade("吸血")
    return h


reg("获得黑暗值时,获得1鲜血猎手并获得吸血升级", "gain_darkness", _darkness_gain_with_upgrade(1))
reg("获得黑暗值时,获得2鲜血猎手并获得吸血升级", "gain_darkness", _darkness_gain_with_upgrade(2))


def _sell_darkness_destroyer(cap):
    def h(slot, event):
        left = slot.left
        if left is not None and left.card_type is not None:
            left.add_unit("毁灭者", min(slot.darkness, cap))
    return h


reg("出售时,每有1黑暗值,相邻左侧卡牌获得1毁灭者,最多获得30", "selling", _sell_darkness_destroyer(30))


def _seize_darkness(slot, event):
    num = 0
    for s in slot.all:
        if s.level <= 4:
            num += s.darkness
            s.darkness = 0
    slot.add_unit("天罚行者", num // 5)


reg("进场时,夺取4星及以下所有卡牌的黑暗值,每夺取5点获得1天罚行者", "entering", _seize_darkness)


def _darkness_vanguard(n, cap):
    def h(slot, event):
        slot.add_unit("先锋", min((slot.darkness // 6) * n, cap))
    return h


reg("每回合结束时,每有6黑暗值,获得1先锋,最多获得4", "round_end", _darkness_vanguard(1, 4))
reg("每回合结束时,每有6黑暗值,获得2先锋,最多获得8", "round_end", _darkness_vanguard(2, 8))


def _darkness_share(multiplier):
    def h(slot, event):
        amount = getattr(event, "amount", 0) * multiplier
        for s in slot.all:
            if s is not slot and s.tags.has("具有黑暗容器"):
                event.tarven.gain_darkness(s, amount)
    return h


reg("唯一:获得黑暗值时,其他卡牌获得等量黑暗值", "gain_darkness", _darkness_share(1))
reg("唯一:获得黑暗值时,其他卡牌获得双倍黑暗值", "gain_darkness", _darkness_share(2))


# ===========================================================================
# 中立 / 其它
# ===========================================================================
def _refresh_tavern(slot, event):
    event.tarven.refresh()


reg("出售时,刷新你的酒馆", "selling", _refresh_tavern)


def _multi_race_gain(n):
    def h(slot, event):
        if (len(slot.terran) + slot.has_narud) > 0:
            slot.add_unit("歌利亚", n)
        if (len(slot.zerg) + slot.has_narud) > 0:
            slot.add_unit("蟑螂", n)
        if (len(slot.protoss) + slot.has_narud) > 0:
            slot.add_unit("龙骑士", n)
    return h


reg("每回合结束时,如果你拥有人族卡牌,获得1歌利亚;拥有虫族卡牌,获得1蟑螂;拥有神族卡牌,获得1龙骑士", "round_end", _multi_race_gain(1))
reg("每回合结束时,如果你拥有人族卡牌,获得2歌利亚;拥有虫族卡牌,获得2蟑螂;拥有神族卡牌,获得2龙骑士", "round_end", _multi_race_gain(2))


def _annihilate(slot, event):
    left, right = slot.left, slot.right
    if left is None or right is None:
        return
    if left.card_type is None or right.card_type is None:
        return
    if left.card_type != right.card_type:
        return
    units = list(slot.units.items())
    event.tarven.destroy(slot)
    for u, c in units:
        left.add_unit(u, c // 2)
        right.add_unit(u, c // 2)
        if c % 2:
            (left if event.tarven.rng.random() < 0.5 else right).add_unit(u, 1)


reg("每回合结束时,若相邻两侧卡牌相同,则摧毁此卡牌并且相邻两侧卡牌各获得此卡牌一半的单位", "round_end", _annihilate)


def _seize_random_two(slot, event):
    others = [s for s in slot.all if s is not slot]
    for s in event.tarven.rng.sample(others, min(2, len(others))):
        event.tarven.seize(s, slot)


reg("进场时,夺取除去自身随机两张卡牌", "entering", _seize_random_two)


def _four_race_gain(n):
    def h(slot, event):
        if (
            (len(slot.terran) + slot.has_narud) > 0
            and (len(slot.zerg) + slot.has_narud) > 0
            and (len(slot.protoss) + slot.has_narud) > 0
            and (len(slot.neutral) + slot.has_narud) > 0
        ):
            slot.add_unit("混合体毁灭者", n)
    return h


reg("每回合结束时,若场上有4个种族的卡牌,则获得2混合体毁灭者", "round_end", _four_race_gain(2))
reg("每回合结束时,若场上有4个种族的卡牌,则获得4混合体毁灭者", "round_end", _four_race_gain(4))


_VOID_PROJECTION_TAG = "具有虚空投影"


def _neighbors_void_projection(slot, event):
    for s in slot.neighbors:
        s.tags.add(_VOID_PROJECTION_TAG)


reg("每回合结束时,相邻两侧卡牌获得虚空投影增益", "round_end", _neighbors_void_projection)


def _left_void_projection(slot, event):
    left = slot.left
    if left is not None and left.card_type is not None:
        left.tags.add(_VOID_PROJECTION_TAG)


reg("进场时,相邻左侧卡牌获得虚空投影增益", "entering", _left_void_projection)


def _void_projection_gain(n):
    def h(slot, event):
        for s in slot.all:
            if s.tags.has(_VOID_PROJECTION_TAG):
                s.add_unit("混合体天罚者", n)
    return h


reg("每回合开始时,每张具有虚空投影的卡牌获得1混合体天罚者", "round_start", _void_projection_gain(1))
reg("每回合开始时,每张具有虚空投影的卡牌获得2混合体天罚者", "round_start", _void_projection_gain(2))


def _shop_same_race(times):
    def h(slot, event):
        for _ in range(times):
            for card in event.tarven.shop:
                if card is None:
                    continue
                if slot.tags.has(card.race):
                    for u, c in card.units.items():
                        if u not in HERO_UNITS:
                            slot.add_unit(u, c)
    return h


reg("唯一:每回合结束时,获得商店中与此卡牌同种族卡牌的非英雄单位", "round_end", _shop_same_race(1))
reg("唯一:每回合结束时,获得商店中与此卡牌同种族卡牌的非英雄单位,触发2次", "round_end", _shop_same_race(2))


def _race_follow_first_entered(slot, event):
    key = "race_follow_round"
    if slot.task_vars.get(key) == event.tarven.round:
        return
    slot.task_vars[key] = event.tarven.round
    entered = getattr(event, "entered_slot", None)
    if entered is None:
        return
    for race in ["zerg", "terran", "protoss", "neutral"]:
        slot.tags.remove(race)
        if entered.tags.has(race):
            slot.tags.add(race)


reg("卡牌种族变为每回合首张进场卡牌的种族", "any_card_entered", _race_follow_first_entered)


def _highest_value_seizes(slot, event):
    best = None
    best_price = slot.price()
    for s in slot.all:
        if s is not slot and s.price() > best_price:
            best_price = s.price()
            best = s
    if best is not None:
        event.tarven.seize(slot, best)


reg("每回合结束时,价值最高的卡牌夺取此卡牌", "round_end", _highest_value_seizes)


def _parts_gain(slot, event):
    cnt = len(slot.terran) + slot.has_narud
    for s in slot.all:
        if s.tags.has("虚空投影"):
            cnt += 1
    slot.add_unit("零件", cnt)


reg("每回合结束时,每有1张人族卡牌,获得1零件;每有1张具有虚空投影的卡牌,获得1零件", "round_end", _parts_gain)


def _craft_hybrid(slot, event):
    if slot.count("零件") >= 10:
        slot.remove_unit("零件", 10)
        slot.add_unit("混合体实验体", 1)


reg("制造(10):获得1混合体实验体", "round_end", _craft_hybrid)


# ===========================================================================
# 原始虫群 / 精华
# ===========================================================================
def _essence_to_dragon(slot, event):
    if event.tarven.mineral >= 1:
        total = 0
        for s in slot.all:
            total += s.count("精华")
            s.remove_unit("精华", s.count("精华"))
        slot.add_unit("原始异龙", total // 2)


reg("每回合结束时,若晶体矿数量≥1,则摧毁所有卡牌的精华,每摧毁2精华,获得1原始异龙", "round_end", _essence_to_dragon)


def _refill_igniter(cap):
    def h(slot, event):
        target = min(cap, 2 * slot.count("精华"))
        slot.add_unit("原始点火虫", max(target - slot.count("原始点火虫"), 0))
    return h


reg("每回合结束时,补充原始点火虫至精华数量的两倍,最多补充到16", "round_end", _refill_igniter(16))
reg("每回合结束时,补充原始点火虫至精华数量的两倍,最多补充到32", "round_end", _refill_igniter(32))


def _primal_ultra_essence(n):
    def h(slot, event):
        slot.add_unit("原始雷兽", n)
        slot.add_unit("精华", len(slot.neutral) + slot.has_narud)
    return h


reg("每回合结束时,获得1原始雷兽并每有1张中立卡牌获得1精华", "round_end", _primal_ultra_essence(1))
reg("每回合结束时,获得2原始雷兽并每有1张中立卡牌获得1精华", "round_end", _primal_ultra_essence(2))


def _discover_primal(slot, event):
    if event.tarven.mineral >= 1:
        event.tarven.discover(level=[1, 2, 3, 4], tags=["属于原始虫群"])


reg("唯一:每回合结束时,若晶体矿数量≥1,下回合发现一张5星以下的原始虫群卡牌", "round_end", _discover_primal)


def _essence_per_star(n):
    def h(slot, event):
        stars = {s.level for s in slot.all if s.level > 0}
        slot.add_unit("精华", len(stars) * n)
    return h


reg("每回合开始时,场上每有一种星级的卡牌,获得1精华", "round_start", _essence_per_star(1))
reg("每回合开始时,场上每有一种星级的卡牌,获得2精华", "round_start", _essence_per_star(2))


# ===========================================================================
# 生命值 / 升级费用
# ===========================================================================
def _heal(n):
    def h(slot, event):
        event.tarven.health += n
    return h


# 这两条与紧随其后的“若场上无核心包卡牌,改为…”共同构成条件分支，
# 由后者的组合 handler 统一结算，避免同时回血和扣血。
reg("唯一:每回合开始时,恢复9生命值", "round_start", _noop)
reg("唯一:每回合开始时,恢复18生命值", "round_start", _noop)


def _has_core_card(slot):
    return any(
        s is not slot
        and getattr(s.source_card, "source", None)
        and bool(set(s.source_card.source) & set(CORE_SOURCES))
        for s in slot.all
    )


def _heal_or_damage_opponents(amount):
    def h(slot, event):
        if _has_core_card(slot):
            event.tarven.health += amount
            return
        for tarven in event.tarven.game.tarvens:
            if tarven is not event.tarven:
                tarven.health -= amount
    return h


reg("若场上无核心包卡牌,改为所有对手扣除9生命值", "round_start", _heal_or_damage_opponents(9))
reg("若场上无核心包卡牌,改为所有对手扣除18生命值", "round_start", _heal_or_damage_opponents(18))


def _cost_up_discover(condition):
    def h(slot, event):
        if condition and event.tarven.level >= 5:
            return
        event.tarven.level_up_cost += 3
        event.tarven.discover()
    return h


reg("唯一:每回合开始时,若酒馆等级小于5,酒馆升级费用+3并发现1张任意星级的卡牌", "round_start", _cost_up_discover(True))
reg("唯一:每回合开始时,酒馆升级费用+3并发现1张任意星级的卡牌", "round_start", _cost_up_discover(False))


# ===========================================================================
# 星级 / 选择 / 商店
# ===========================================================================
def _choose_card_delayed(level):
    def h(slot, event):
        cards = []
        for _ in range(3):
            uuid = event.tarven.pool.sample(levels=[level])
            if uuid is not None:
                cards.append(event.tarven.pool.card_map[uuid])
        if cards:
            event.tarven.force_action.append(ChooseCardAction(cards=cards, delay=3))
    return h


reg("进场时,选择1张2星卡牌并在3回合后获得该卡牌", "entering", _choose_card_delayed(2))
reg("进场时,选择1张4星卡牌并在3回合后获得该卡牌", "entering", _choose_card_delayed(4))


def _left_lower_level(n):
    def h(slot, event):
        left = slot.left
        if left is not None and left.card_type is not None:
            left.level = max(1, left.level - n)
    return h


reg("进场时,相邻左侧卡牌降低1星级", "entering", _left_lower_level(1))
reg("进场时,相邻左侧卡牌降低2星级", "entering", _left_lower_level(2))


def _seize_nonzerg(n):
    def h(slot, event):
        targets = [s for s in slot.all if s is not slot and not s.tags.has("zerg")]
        for s in event.tarven.rng.sample(targets, min(n, len(targets))):
            event.tarven.seize(s, slot)
    return h


reg("每回合结束时,夺取场上1张非虫族卡牌", "round_end", _seize_nonzerg(1))
reg("每回合结束时,夺取场上2张非虫族卡牌", "round_end", _seize_nonzerg(2))


def _random_baneling(n):
    def h(slot, event):
        if event.tarven.rng.random() < 0.5:
            slot.add_unit("跳虫(精英)", n)
        else:
            slot.remove_unit("跳虫(精英)", n)
    return h


reg("每回合结束时,随机增加或减少1跳虫(精英)", "round_end", _random_baneling(1))
reg("每回合结束时,随机增加或减少2跳虫(精英)", "round_end", _random_baneling(2))



# ===========================================================================
# 新增批次：v260822 剩余可移植效果
# ===========================================================================

# --- 原始异龙：任意卡牌进场时,相邻右侧卡牌获得 N 精华 -----------------------
def _right_gain(unit, n):
    def h(slot, event):
        r = slot.right
        if r is not None and r.card_type is not None:
            r.add_unit(unit, n)
    return h


reg("任意卡牌进场时,相邻右侧卡牌获得1精华", "any_card_entered", _right_gain("精华", 1))
reg("任意卡牌进场时,相邻右侧卡牌获得2精华", "any_card_entered", _right_gain("精华", 2))


# --- 适者生存：每回合结束时,将场上随机 N 个生物单位精英化 -------------------
def _elite_random_bio(n):
    def h(slot, event):
        pool = []
        for s in slot.all:
            for u in list(s.units.keys()):
                if u in BIOLOGICAL_UNITS and not u.endswith("(精英)"):
                    pool += [(s, u)] * s.count(u)
        for s, u in event.tarven.rng.sample(pool, min(n, len(pool))):
            s.replace_unit(u, 1, u + "(精英)", 1)
    return h


reg("每回合结束时,将场上随机6个生物单位变为精英单位(f8可查看所有精英单位)", "round_end", _elite_random_bio(6))
reg("每回合结束时,将场上随机10个生物单位变为精英单位(f8可查看所有精英单位)", "round_end", _elite_random_bio(10))
# 归一化不改大小写，数据里是大写 F8；两种都注册以防
reg("每回合结束时,将场上随机6个生物单位变为精英单位(F8可查看所有精英单位)", "round_end", _elite_random_bio(6))
reg("每回合结束时,将场上随机10个生物单位变为精英单位(F8可查看所有精英单位)", "round_end", _elite_random_bio(10))


# --- 德哈卡：任意精华≥3的卡牌出售时,获得 N 德哈卡的分身 ---------------------
def _dehaka_clone(n):
    def h(slot, event):
        sold = getattr(event, "sold_slot", None)
        if sold is not None and sold.count("精华") >= 3:
            slot.add_unit("德哈卡分身", n)
    return h


reg("任意精华≥3的卡牌出售时,获得2德哈卡的分身", "any_card_sold", _dehaka_clone(2))
reg("任意精华≥3的卡牌出售时,获得4德哈卡的分身", "any_card_sold", _dehaka_clone(4))


# --- 屠猎者：回合胜利时或提升酒馆等级时,获得 N 刺蛇(精英) -------------------
#     "回合胜利"是休眠事件（见 RoundWinEvent），"提升酒馆等级"走 level_up。
def _gain_hydra_elite(n):
    def h(slot, event):
        slot.add_unit("刺蛇(精英)", n)
    return h


reg("回合胜利时或提升酒馆等级时,获得1刺蛇(精英)", ["round_win", "level_up"], _gain_hydra_elite(1))
reg("回合胜利时或提升酒馆等级时,获得2刺蛇(精英)", ["round_win", "level_up"], _gain_hydra_elite(2))


# --- 优柔寡断：提升酒馆等级时,自毁 -----------------------------------------
def _self_destroy(slot, event):
    event.tarven.destroy(slot)


reg("提升酒馆等级时,自毁", "level_up", _self_destroy)


# --- 归天的加多宝：回合开始时,注卵卡牌内的所有单位并自毁 --------------------
def _larva_all_and_destroy(slot, event):
    payload = {u: c for u, c in slot.units.items()}
    if payload:
        event.tarven.larva(payload)
    event.tarven.destroy(slot)


reg("回合开始时,注卵卡牌内的所有单位并自毁", "round_start", _larva_all_and_destroy)
# 数据中出现的 OCR 错字变体（"注册"）一并注册，语义同上
reg("回合开始时,注册卡牌内的所有单位并自毁", "round_start", _larva_all_and_destroy)


# --- 香料贸易：任意卡牌进场时,从中复制未拥有的每种非英雄单位 N 次 -----------
def _copy_new_types(n):
    def h(slot, event):
        entered = getattr(event, "entered_slot", None)
        if entered is None or entered is slot:
            return
        for u in list(entered.units.keys()):
            if u not in HERO_UNITS and slot.count(u) == 0:
                slot.add_unit(u, n)
    return h


reg("任意卡牌进场时,从中复制未拥有的每种非英雄单位1次", "any_card_entered", _copy_new_types(1))
reg("任意卡牌进场时,从中复制未拥有的每种非英雄单位2次", "any_card_entered", _copy_new_types(2))


# --- 军事学院：任意卡牌注卵时,自身及相邻卡牌将 N 最低价值非英雄生物变为最高价值 -
def _low_to_high_bio(n):
    def h(slot, event):
        for s in [slot] + slot.neighbors:
            bios = _bio_units_on(s)
            if not bios:
                continue
            dst = max(bios, key=lambda u: UNIT_PRICES.get(u, 0.0))
            expanded = []
            for u in bios:
                if u != dst:
                    expanded += [u] * s.count(u)
            expanded.sort(key=lambda u: UNIT_PRICES.get(u, 0.0))
            for u in expanded[:n]:
                s.remove_unit(u, 1)
                s.add_unit(dst, 1)
    return h


reg("任意卡牌注卵时,自身及相邻卡牌将1最低价值非英雄生物变为最高价值非英雄生物", "any_card_larva", _low_to_high_bio(1))
# 金色描述里的触发词被数据管线替换成占位符 "~A~"，按注卵语义注册
reg("任意卡牌~A~时,自身及相邻卡牌将2最低价值非英雄生物变为最高价值非英雄生物", "any_card_larva", _low_to_high_bio(2))


# --- 斯旺舰队：每回合结束时,相邻两侧卡牌获得 2 重工厂 -----------------------
reg("每回合结束时,相邻两侧卡牌获得2重工厂", "round_end", _neighbors_gain("重工厂", 2))


# --- 神圣壁垒：回合结束时,为具有凯达林巨石的卡牌添加升级 --------------------
def _upgrade_kadarin(names):
    def h(slot, event):
        for s in slot.all:
            if s.count("凯达林巨石") > 0 or s.count("凯达琳巨石") > 0:
                for name in names:
                    s.upgrade(name)
    return h


reg("回合结束时,为具有凯达林巨石的卡牌添加折跃援军升级", "round_end", _upgrade_kadarin(["折跃援军"]))
reg("回合结束时,为具有凯达林巨石的卡牌添加折跃援军和护盾充能升级", "round_end", _upgrade_kadarin(["折跃援军", "护盾充能"]))


# --- 恶性瘟疫：变为「难民营地」并注卵 6 被感染的陆战队员 --------------------
def _plague(repeat):
    def h(slot, event):
        for _ in range(repeat):
            target = None
            for s in slot.terran:
                if s.count("陆战队员") > 0:
                    target = s
                    break
            if target is None:
                break
            _transform(target, event, "难民营地")
            event.tarven.larva({"被感染的陆战队员": 6})
    return h


reg("每回合结束时,若拥有1张具有陆战队员的人族卡牌,则将其变为「难民营地」并且注卵6被感染的陆战队员", "round_end", _plague(1))
reg("每回合结束时,若拥有1张具有陆战队员的人族卡牌,则将其变为「难民营地」并且注卵6被感染的陆战队员,触发2次", "round_end", _plague(2))


# --- 清理裂隙：提升酒馆等级时,消耗 1 虚空裂隙+1 蟑螂 注卵 1 莽兽 -------------
def _clear_rift(repeat):
    def h(slot, event):
        for _ in range(repeat):
            rift = next((s for s in slot.all if s.count("虚空裂隙") > 0), None)
            roach = next((s for s in slot.all if s.count("蟑螂") > 0), None)
            if rift is None or roach is None:
                break
            rift.remove_unit("虚空裂隙", 1)
            roach.remove_unit("蟑螂", 1)
            event.tarven.larva({"莽兽": 1})
    return h


reg("提升酒馆等级时,尝试同时摧毁1虚空裂隙和1蟑螂以注卵1莽兽", "level_up", _clear_rift(1))
reg("提升酒馆等级时,尝试同时摧毁1虚空裂隙和1蟑螂以注卵1莽兽,重复2次", "level_up", _clear_rift(2))


# --- 思而不学：唯一,任意其他虫族卡牌进场时,摧毁并注卵其所有最高价值单位 -----
def _think_destroy_larva(check_level):
    def h(slot, event):
        e = getattr(event, "entered_slot", None)
        if e is None or e is slot or not e.tags.has("zerg"):
            return
        if check_level and e.level > slot.level:
            return
        if e.units:
            best = max(e.units, key=lambda u: UNIT_PRICES.get(u, 0.0))
            cnt = e.count(best)
            event.tarven.destroy(e)
            event.tarven.larva({best: cnt})
        else:
            event.tarven.destroy(e)
    return h


reg("唯一:任意其他虫族卡牌进场时,若星级不大于此卡牌,将其摧毁并注卵所有最高价值单位", "any_card_entered", _think_destroy_larva(True))
reg("唯一:任意其他虫族卡牌进场时,将其摧毁并注卵所有最高价值单位", "any_card_entered", _think_destroy_larva(False))


# --- 斯托科夫：唯一,每进场 N 张六星以下非虫族卡牌,注卵其非英雄单位 ----------
def _stukov(every):
    def h(slot, event):
        e = getattr(event, "entered_slot", None)
        if e is None or e is slot or e.tags.has("zerg") or e.level >= 6:
            return
        cnt = slot.task_vars.get("stukov", 0) + 1
        slot.task_vars["stukov"] = cnt
        if cnt % every == 0:
            payload = {u: e.count(u) for u in e.units if u not in HERO_UNITS}
            if payload:
                event.tarven.larva(payload)
    return h


reg("唯一:每进场两张六星以下的非虫族卡牌,注卵第二张卡牌的非英雄单位到最左侧虫卵牌", "any_card_entered", _stukov(2))
reg("唯一:每进场一张六星以下的非虫族卡牌,注卵进场卡牌的非英雄单位到最左侧虫卵牌", "any_card_entered", _stukov(1))


# --- 凯瑞甘：吞噬左侧 / 双凯瑞甘合并为刀锋女王 -----------------------------
def _kerrigan_merge(slot, event):
    for other in slot.neighbors:
        if other.card_type == "凯瑞甘":
            _transform(other, event, "刀锋女王", reset_units=True)
            event.tarven.destroy(slot)
            return


def _kerrigan_devour(slot, event):
    # 若相邻存在另一张凯瑞甘则交由合并逻辑处理，这里不吞噬
    if any(s.card_type == "凯瑞甘" for s in slot.neighbors):
        return
    left = slot.left
    if left is not None and left.card_type is not None:
        for u, c in list(left.units.items()):
            slot.add_unit(u, c)  # 吞噬其单位（生命/护盾属于战斗层，不建模）
        event.tarven.destroy(left)


reg("进场时,若两张凯瑞甘相邻,合并为刀锋女王", "entering", _kerrigan_merge)
reg("进场时,吞噬相邻左侧卡牌所有单位,获得这些单位150%的生命和护盾值", "entering", _kerrigan_devour)


# --- 望梅止渴：进场时,变为星级为 n 的随机卡牌并触发其进场特效 --------------
def _become_random_and_enter(slot, event):
    lv = event.tarven.level
    uuid = event.tarven.pool.sample(levels=[lv])
    if uuid is None:
        return
    card = event.tarven.pool.card_map[uuid]
    _transform(slot, event, card.name, reset_units=True)
    # 抽走的随机卡真实构成该实例（身份变换保留底牌来源）：出售/摧毁时
    # 连同底牌原卡一并归还，避免公共池泄漏。
    if event.tarven.pool.is_pool_entity(card):
        slot.origin_cards.append(card)
    slot.trigger([EnteringEvent(event.tarven, slot)])


reg("进场时,变为星级为n的随机卡牌并触发其进场特效(n=当前酒馆等级)", "entering", _become_random_and_enter)


# --- 我叫小明：复制左侧卡牌到暂存区,被其夺取,使其获得星空加速 --------------
def _xiaoming(slot, event):
    left = slot.left
    if left is None or left.card_type is None or not (1 <= left.level <= 6):
        return
    event.tarven.store_card_to_cache(left.card_type)
    left.upgrade("星空加速")
    event.tarven.seize(slot, left)  # 此卡牌被左侧卡牌夺取（单位转移后自毁）


reg("进场时,复制左侧相邻1-6星的卡牌到暂存区并被其夺取,并使其获得星空加速", "entering", _xiaoming)


# --- 入景随风：复制各卡牌最高价值非英雄单位;价值超 6000 则被夺取 -----------
def _rush_follow(slot, event):
    for s in slot.all:
        if s is slot:
            continue
        bios = [u for u in s.units if u not in HERO_UNITS]
        if bios:
            best = max(bios, key=lambda u: UNIT_PRICES.get(u, 0.0))
            slot.add_unit(best, 1)
    if slot.price() > 6000:
        others = [s for s in slot.all if s is not slot]
        if others:
            best = max(others, key=lambda s: s.price())
            event.tarven.seize(slot, best)


reg("每回合结束时,复制场上各卡牌1价值最高的非英雄单位到此卡牌;若此卡牌价值大于6000,则被最高价值的其他卡牌夺取", "round_end", _rush_follow)


# --- 风暴英雄：获得升级时,随机获得 1 英雄 ---------------------------------
_STORM_HEROES = ["马拉什", "阿拉纳克", "利维坦", "虚空构造体", "科罗拉里昂"]


def _storm_hero(slot, event):
    if getattr(event, "upgrade_slot", None) is slot:
        slot.add_unit(event.tarven.rng.choice(_STORM_HEROES), 1)


reg("获得升级时,随机获得1英雄(包含:马拉什、阿拉纳克、利维坦、虚空构造体、科罗拉里昂)", "upgrade", _storm_hero)


# --- 人格上传：出售价值/升级数比较 ----------------------------------------
def _upload(n):
    def h(slot, event):
        sold = getattr(event, "sold_slot", None)
        if sold is None:
            return
        if sold.price() > slot.price():
            slot.add_unit("菲尼克斯", n)
        if len(sold.upgrades) > len(slot.upgrades):
            copied = 0
            for up in sold.upgrades:
                if copied >= n:
                    break
                if up not in slot.upgrades:
                    slot.upgrade(up)
                    copied += 1
    return h


reg("任意卡牌出售时,若其价值高于此卡牌,获得1菲尼克斯;若其升级数多于此卡,复制1不重复的升级", "any_card_sold", _upload(1))
reg("任意卡牌出售时,若其价值高于此卡牌,获得3菲尼克斯;若其升级数多于此卡,复制3不重复的升级", "any_card_sold", _upload(3))


# --- 焦土策略：出售时摧毁暂存区(±相邻)非衍生卡,每张返还 3 晶体矿 -----------
#     "非衍生" 无法在数据层严格判定，这里把暂存区/相邻的所有卡牌视为可返还。
def _scorched(destroy_neighbors):
    def h(slot, event):
        refunded = 0
        for i in range(len(event.tarven.cache)):
            if event.tarven.cache[i] is not None:
                # 摧毁暂存区卡牌：归还其公共池来源并同步元数据，
                # 免费静态定义/复制来源为空则自然不归池。
                event.tarven.clear_cache(i)
                refunded += 1
        if destroy_neighbors:
            for s in list(slot.neighbors):
                if s.card_type is not None:
                    event.tarven.destroy(s)
                    refunded += 1
        event.tarven.mineral += refunded * 3
    return h


reg("出售时,可摧毁暂存区所有非衍生牌,每张返还3晶体矿", "selling", _scorched(False))
reg("出售时,可摧毁相邻卡牌和暂存区所有非衍生卡牌,每张返还3晶体矿", "selling", _scorched(True))


# --- 以逸待劳：本回合无其他卡牌进场则自身及相邻获得 N 行星要塞 --------------
def _bunker(n):
    def h(slot, event):
        if event.event_name == Event.ANY_CARD_ENTERED:
            entered = getattr(event, "entered_slot", None)
            if entered is not None and entered is not slot:
                slot.task_vars["bunker_entered_round"] = event.tarven.round
        else:  # round_end
            if slot.task_vars.get("bunker_entered_round") != event.tarven.round:
                for s in [slot] + slot.neighbors:
                    s.add_unit("行星要塞", n)
    return h


reg("每回合结束时,若本回合没有进场其他卡牌,则自身及相邻两侧卡牌获得1行星要塞", ["any_card_entered", "round_end"], _bunker(1))
reg("每回合结束时,若本回合没有进场其他卡牌,则自身及相邻两侧卡牌获得2行星要塞", ["any_card_entered", "round_end"], _bunker(2))


# --- 中性对冲：刷新商店时,将最低星级的商店卡 +N 星 ------------------------
def _hedge_lowest(delta):
    def h(slot, event):
        idxs = [i for i, c in enumerate(event.tarven.shop) if c is not None]
        if not idxs:
            return
        i = min(idxs, key=lambda i: event.tarven.shop[i].level)
        _upgrade_shop_card(event.tarven, i, delta)
        slot.task_vars["hedge_refreshed_round"] = event.tarven.round
    return h


reg("刷新商店时,将商店中1张最低星级的卡牌变为加1星级的卡牌", "refresh", _hedge_lowest(1))
reg("刷新商店时,将商店中1张最低星级的卡牌变为加2星级的卡牌", "refresh", _hedge_lowest(2))


def _hedge_repeat(slot, event):
    # "本回合中,若你刷新过商店,额外重复2次"：再执行 2 次最低星级 +1
    if slot.task_vars.get("hedge_refreshed_round") == event.tarven.round:
        for _ in range(2):
            idxs = [i for i, c in enumerate(event.tarven.shop) if c is not None]
            if not idxs:
                break
            i = min(idxs, key=lambda i: event.tarven.shop[i].level)
            _upgrade_shop_card(event.tarven, i, 1)


reg("本回合中,若你刷新过商店,额外重复2次", "refresh", _hedge_repeat)


# --- 跳虫币：出售时,将商店前 2 张卡牌 +N 星 -------------------------------
def _coin_shop(delta):
    def h(slot, event):
        for i in range(min(2, len(event.tarven.shop))):
            _upgrade_shop_card(event.tarven, i, delta)
    return h


reg("出售时,将商店中前2张卡牌变为加1星级的卡牌", "selling", _coin_shop(1))
reg("出售时,将商店中前2张卡牌变为加2星级的卡牌", "selling", _coin_shop(2))


# --- 先锋概念：每回合结束时消耗 11 晶体矿获得 N 张辅助卡「冷钱包」 ----------
def _pioneer(n):
    def h(slot, event):
        if event.tarven.mineral >= 11:
            event.tarven.mineral -= 11
            for _ in range(n):
                event.tarven.store_card_to_cache("冷钱包")
    return h


reg("每回合结束时,尝试消耗11晶体矿获得2张辅助卡「冷钱包」,部署后发现1张6星卡牌", "round_end", _pioneer(2))
reg("每回合结束时,尝试消耗11晶体矿获得3张辅助卡「冷钱包」,部署后发现1张6星卡牌", "round_end", _pioneer(3))


# ===========================================================================
# 辅助卡：部署时（deployment 为休眠事件，需外部部署动作驱动，效果本身可正确执行）
# event.deployment_slot 为部署目标（"指定卡牌"）。
# ===========================================================================
def _deploy_target(event):
    return getattr(event, "deployment_slot", None) or getattr(event, "slot", None)


def _deploy_advanced_addon(slot, event):
    target = _deploy_target(event)
    if target is not None and event.tarven.level >= 5:
        target.change_add_on("高级科技实验室")


reg("部署时,若酒馆等级大于等于5,则为指定卡牌切换挂件为高级科技实验室", "deployment", _deploy_advanced_addon)


def _deploy_upgrade(name):
    def h(slot, event):
        target = _deploy_target(event)
        if target is not None:
            target.upgrade(name)
    return h


reg("部署时,指定卡牌获得轨道空降升级", "deployment", _deploy_upgrade("轨道空降"))


def _deploy_gain_mineral(slot, event):
    event.tarven.mineral += 1


reg("部署时,获得1晶体矿", "deployment", _deploy_gain_mineral)


def _deploy_discover_6star(slot, event):
    event.tarven.discover(level=[6])


reg("部署时,发现一张6星卡牌", "deployment", _deploy_discover_6star)


def _deploy_infest(slot, event):
    target = _deploy_target(event)
    if target is None:
        return
    infested = 0
    for u in _bio_units_on(target):
        while target.count(u) > 0 and infested < 5:
            target.remove_unit(u, 1)
            infested += 1
    if infested:
        event.tarven.larva({"被感染的陆战队员": infested})


reg("部署时,将指定卡牌的5生物单位变为5被感染的陆战队员并注卵", "deployment", _deploy_infest)


def _deploy_darkness_essence(slot, event):
    target = _deploy_target(event)
    if target is not None:
        event.tarven.gain_darkness(target, 5)
        target.add_unit("精华", 5)


reg("部署时,为指定卡牌添加5黑暗值和5精华", "deployment", _deploy_darkness_essence)
# 数据里被切碎产生的前缀残片 "n/>部署时,..."，同义注册
reg("n/>部署时,为指定卡牌添加5黑暗值和5精华", "deployment", _deploy_darkness_essence)


def _deploy_destroy_retrigger(slot, event):
    target = _deploy_target(event)
    if target is None or target.card_type is None:
        return
    # 再次触发其进场特效，然后摧毁
    target.trigger([EnteringEvent(event.tarven, target)])
    event.tarven.destroy(target)


reg("部署时,摧毁指定卡牌,并再次触发它的进场特效", "deployment", _deploy_destroy_retrigger)


def _deploy_ghost_finish_task(slot, event):
    target = _deploy_target(event)
    if target is None:
        return
    target.add_unit("幽魂", 1)
    for handler in target.event_handlers:
        ah = handler.action_handler
        if isinstance(ah, TaskActionHandler) and not ah.is_finished():
            ah.counter = max(0, ah.goal - 1)
            ah(target, event)


reg("部署时,指定卡牌获得1幽魂并完成任务", "deployment", _deploy_ghost_finish_task)



# --- 征兵令：进场时,抽取相邻两侧人族卡牌 2/3 的单位,挂件相同则抽取全部 -------
#     "挂件相同" 解释为：相邻的两张人族卡牌拥有相同（且非空）的挂件类型时抽取全部，
#     否则各抽取 2/3（向下取整）。
_ADDONS = ("反应堆", "科技实验室", "高级科技实验室", "信号塔")


def _addon_sig(slot: Slot):
    return tuple(sorted(a for a in _ADDONS if slot.count(a) > 0))


def _draft(slot, event):
    terran_neighbors = [s for s in slot.neighbors if s.tags.has("terran")]
    same_addon = (
        len(terran_neighbors) == 2
        and _addon_sig(terran_neighbors[0]) == _addon_sig(terran_neighbors[1])
        and _addon_sig(terran_neighbors[0]) != ()
    )
    for s in terran_neighbors:
        for u, c in list(s.units.items()):
            take = c if same_addon else (c * 2) // 3
            if take > 0:
                s.remove_unit(u, take)
                slot.add_unit(u, take)


reg("进场时,抽取相邻两侧人族卡牌2/3的单位,若挂件相同,抽取全部单位", "entering", _draft)


# --- 一鼓作气：唯一,所有虚空水晶塔提供 N 点能量强度 ------------------------
# Slot.energy 按场上仍存在的一鼓作气实例动态计算；handler 仅保留描述注册。
def _void_tower_energy(n):
    def h(slot, event):
        return None
    return h


reg("唯一:所有虚空水晶塔提供2点能量强度", ["round_start", "entering"], _void_tower_energy(2))
reg("唯一:所有虚空水晶塔提供3点能量强度", ["round_start", "entering"], _void_tower_energy(3))


# --- 星灵科技：部署时,为指定非神族卡牌添加描述 -----------------------------
#     该卡随后一行 "每回合结束时,折跃1陆战队员(精英)" 即要添加的能力。
def _xingling_add_desc(slot, event):
    target = _deploy_target(event)
    if target is None or target.tags.has("protoss"):
        return
    from star_tarven_simulator.cards import resolve

    desc = "每回合结束时,折跃1陆战队员(精英)"
    resolved = resolve(desc)
    if resolved is None:
        return
    event_names, handler = resolved
    if not isinstance(event_names, list):
        event_names = [event_names]
    for ev in event_names:
        target.event_handlers.append(
            EventHandler(event.tarven, target, desc, handler, ev)
        )


reg("部署时,为指定非神族卡牌添加描述:", "deployment", _xingling_add_desc)


# --- 海盗商人：任务 回合胜利 -> 奖励 随机获得 N 张一星卡牌 -----------------
#     "回合胜利" 走休眠事件 round_win（需外部战斗结果驱动）。
def _draw_one_star(count):
    def h(slot, event):
        for _ in range(count):
            drawn = event.tarven.pool.draw(1, 1)
            if not drawn:
                continue
            card = drawn[0]
            # 与 parametric 的"随机获得N张一星卡牌"一致：缓存优先、缓存满则强制
            # 进场（可能触发三连）；发放失败时把原 Card 实体放回卡池，不存 name
            # 以免丢失实体/来源。
            if not event.tarven.grant_reward_card(card):
                event.tarven.pool.place_back(card)
    return h


register_value(
    "任务:回合胜利 奖励:随机获得1张一星卡牌",
    "round_win",
    TaskActionHandler(_draw_one_star(1), 1, auto_reset=True),
)
register_value(
    "任务:回合胜利 奖励:随机获得2张一星卡牌",
    "round_win",
    TaskActionHandler(_draw_one_star(2), 1, auto_reset=True),
)



# --- 回归起源：摧毁所有其他卡牌,把总价值换算成等值原始单位 + 3 瓦斯 ---------
#     "相同价值的原始单位"按用户口径：把被摧毁卡牌的总价值换算成等值的某种原始单位，
#     这里选用「原始异龙」(价值 250) 作为换算基准。
_ORIGIN_PRIMAL_UNIT = "原始异龙"


def _return_to_origin(slot, event):
    others = [s for s in slot.all if s is not slot]
    if not others:
        return
    levels = [s.level for s in others]
    races = [s.tags for s in others]
    # 星级互不相同
    if len(set(levels)) != len(levels):
        return
    # 种族互不相同（以四大种族 tag 为准，取每张卡的种族标记组合）
    race_sigs = []
    for s in others:
        sig = tuple(sorted(r for r in ("zerg", "terran", "protoss", "neutral") if s.tags.has(r)))
        race_sigs.append(sig)
    if len(set(race_sigs)) != len(race_sigs):
        return

    total_value = sum(s.price() for s in others)
    for s in list(others):
        event.tarven.destroy(s)

    unit_price = UNIT_PRICES.get(_ORIGIN_PRIMAL_UNIT, 0.0)
    if unit_price > 0:
        slot.add_unit(_ORIGIN_PRIMAL_UNIT, int(total_value // unit_price))
    event.tarven.gas = min(event.tarven.gas + 3, event.tarven.gas_max)


reg("每回合结束时,若场上其他卡牌的星级与种族均不同,则摧毁所有其他卡牌并获得相同价值的原始单位和3瓦斯", "round_end", _return_to_origin)


# --- 英灵殿：唯一,其他玩家出售/出局英雄卡时,折跃其中的 1 英雄单位 ----------
#     休眠事件 other_player_sold_hero_card，需多人驱动（见 Tarven.trigger_other_player_hero_card）。
def _valhalla(first_only):
    def h(slot, event):
        source = getattr(event, "source", None)
        if source is None or source.card_type is None:
            return
        heroes = [u for u in source.units if u in HERO_UNITS]
        if not heroes:
            return
        if first_only:
            key = "valhalla_round"
            if slot.task_vars.get(key) != event.tarven.round:
                slot.task_vars[key] = event.tarven.round
                slot.task_vars["valhalla_done"] = False
            if slot.task_vars.get("valhalla_done"):
                return
            slot.task_vars["valhalla_done"] = True
        hero = heroes[0]
        teleport(slot, event, {hero: 1})
    return h


reg("唯一:每回合,任意其他玩家出局或出售首张具有英雄单位的卡牌时,折跃其中的1英雄单位", "other_player_sold_hero_card", _valhalla(True))
reg("唯一:每回合,任意其他玩家出局或出售具有英雄单位的卡牌时,折跃其中的1英雄单位", "other_player_sold_hero_card", _valhalla(False))
