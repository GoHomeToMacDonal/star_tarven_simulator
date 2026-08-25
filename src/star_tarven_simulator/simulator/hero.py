"""固定英雄注册表与单英雄控制器。

英雄规则是代码常量，不在运行时加载英雄 JSON。控制器仅管理一个当前英雄，
并通过窄生命周期钩子与 :mod:`simulator.game` 集成。
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Iterable, Optional

from star_tarven_simulator.constants.unit_prices import UNIT_PRICES
from star_tarven_simulator.constants.unit_type import (
    AIR_UNITS,
    BIOLOGICAL_UNITS,
    GROUND_UNITS,
    HERO_UNITS,
    MECHANICAL_UNITS,
    ZERG_UNITS,
)
from star_tarven_simulator.simulator.action import HeroChoiceAction, HeroPowerAction
from star_tarven_simulator.simulator.card import Card
from star_tarven_simulator.simulator.event import RoundEndEvent, RoundStartEvent
from star_tarven_simulator.simulator.event_handler import EventHandler

if TYPE_CHECKING:
    from star_tarven_simulator.simulator.game import Tarven
    from star_tarven_simulator.simulator.slot import Slot


SUPPORTED_HEROES = frozenset(
    {
        "陆战队员", "工蜂", "副官", "矿骡", "米拉", "德拉肯钻机",
        "追猎者", "使徒", "阿尔达瑞斯", "飞蛇", "收割者", "解放者（防卫模式）", "眼虫",
        "航母", "诺娃", "异龙", "响尾蛇", "休伯利安点唱机", "大力神", "飓风",
        "阿巴瑟", "雷诺", "雷神", "机械哨兵", "德哈卡", "干扰者",
        "感染虫", "SCV", "探机", "斯旺", "蒙斯克", "医疗兵", "爆虫",
        "执政官", "阿塔尼斯", "母舰核心", "分裂池", "混合体", "进化腔", "扎加拉",
        "星港", "凯瑞甘", "汉森博士", "科学球", "火蝠", "战列巡航舰", "坑道虫",
    }
)
DEFERRED_HEROES = frozenset({"泰凯斯", "界徐盛", "埃蒙", "亚顿之矛", "纳鲁德博士"})
TRANSFORM_ONLY_HEROES = frozenset(
    {"凯瑞甘（异虫形态）", "汉森博士（异虫形态）", "解放者（战机模式）"}
)
SELECTABLE_HEROES = frozenset({"default"}) | SUPPORTED_HEROES

# 数据缺失时仍可安全生成的固定专属卡/研究定义。
AUXILIARY_CARD_NAMES = (
    "私人团队", "尖端科技", "星灵科技", "生化实验室", "秽暗饵食",
    "超负荷", "隐秘行动", "冷钱包", "矿簇",
)
ZERG_RESEARCH_UPGRADES = ("几丁质甲壳", "代谢加速", "肾上腺", "强化甲壳")
HANSEN_STUDIES = (
    "战地勘察", "样本日志", "新式血清", "科研成本", "采集神器"
)
PRIMAL_EVOLUTION = {
    "原始蟑螂": "原始点火虫",
    "原始刺蛇": "穿刺者",
    "原始异龙": "守卫",
    "原始雷兽": "暴龙兽",
    "暴掠龙": "毒裂兽",
    "德哈卡分身": "德哈卡",
}


ROYAL_GUARD_BY_UNIT = {
    "战列巡航舰": "皇家战列巡航舰",
    "雷神": "皇家雷神",
    "攻城坦克": "皇家攻城坦克",
    "维京战机": "皇家维京战机",
    "幽灵": "皇家幽灵",
    "劫掠者": "帝盾卫兵",
}
ROYAL_GUARD_PRIORITY = tuple(ROYAL_GUARD_BY_UNIT.items())


def validate_hero_assignment(heroes: Optional[Iterable[str]], user_count: int) -> list[str]:
    names = ["default"] * user_count if heroes is None else list(heroes)
    if len(names) != user_count:
        raise ValueError(f"英雄数量 {len(names)} 与玩家数量 {user_count} 不匹配")
    seen: set[str] = set()
    for name in names:
        if name in DEFERRED_HEROES:
            raise ValueError(f"英雄 {name} 暂缓实现，不能初选")
        if name in TRANSFORM_ONLY_HEROES:
            raise ValueError(f"英雄形态 {name} 只能通过变身进入")
        if name not in SELECTABLE_HEROES:
            raise ValueError(f"未知英雄: {name}")
        if name != "default" and name in seen:
            raise ValueError(f"非默认英雄不能重复: {name}")
        seen.add(name)
    return names


def _dynamic_card(name: str, *, level: int = 0, race: str = "neutral", units=None, tags=None) -> Card:
    """创建安全的固定衍生卡；负 UUID 与 ``derived`` 防止误归普通卡池。"""
    return Card(
        uuid=-(abs(hash((name, level))) % 1_000_000_000 + 1),
        name=name,
        level=level,
        race=race,
        description=[],
        gold_description=[],
        units=dict(units or {}),
        tags=list(tags or [race, "无法三连"]),
        gold_tags=list(tags or [race, "无法三连", "金色"]),
        source=["英雄专属"],
        derived=True,
    )


class HeroController:
    """一个酒馆唯一的固定英雄控制器。

    ``state`` 是稳定公开的扩展面：计数、冷却与固定选择均记录于此，避免英雄
    私有状态散落到 ``Tarven``。
    """

    def __init__(self, tarven: "Tarven", hero_name: str = "default"):
        self.tarven = tarven
        self.hero_name = hero_name
        self.state: dict = {
            "uses": 0,
            "last_power_round": -1,
            "purchases_this_round": 0,
            "refreshes_this_round": 0,
            "entered": 0,
            "syntheses": 0,
        }
        self._initialize()

    @property
    def is_kerrigan_zerg(self) -> bool:
        return self.hero_name == "凯瑞甘（异虫形态）"

    def _initialize(self) -> None:
        t = self.tarven
        if self.hero_name == "航母":
            t.level = 2
            t.level_up_cost = 7
            t.resize_shop_for_level()
            self.state["carrier_entries"] = []
            self.state["carrier_pending"] = {}
            self.state["carrier_history"] = {}
            self.state["carrier_completed"] = set()
        if self.hero_name == "感染虫":
            t.egg_hatches_mechanical = True
        if self.hero_name == "分裂池":
            t.egg_hatches_any_race = True
        if self.hero_name == "德哈卡":
            self.state["essence"] = 0
        if self.hero_name == "德拉肯钻机":
            self.state["drill_points"] = 0
        if self.hero_name == "科学球":
            self.state["observed_units"] = {}
        if self.hero_name == "汉森博士":
            self.state["studies"] = []
            self.state["hansen_field_due"] = None
        if self.hero_name == "SCV":
            self.state["charges"] = 1
        if self.hero_name == "米拉":
            self.state["last_enter_level"] = 6
        if self.hero_name == "战列巡航舰":
            self.state["ready"] = True
        if self.hero_name == "大力神":
            self.state["pending_rewards"] = {}
            # 开局分别预选一张 3/5 星卡；候选在选择完成前保持从池中抽出。
            self.discover(levels=[3], kind="hercules-card", payload={"level": 3})
            self.discover(levels=[5], kind="hercules-card", payload={"level": 5})
        if self.hero_name == "飓风":
            self.state["excluded_race"] = None

    # ------------------------------------------------------------------
    # 通用发现 / 选择
    # ------------------------------------------------------------------
    def discover(
        self,
        *,
        levels: Optional[list[int]] = None,
        tags: Optional[list[str]] = None,
        count: int = 3,
        kind: str = "card",
        payload: Optional[dict] = None,
    ) -> bool:
        options: list[Card] = []
        for _ in range(count):
            uuid = self.tarven.pool._sample(levels=levels, tags=tags)
            if uuid is None:
                break
            options.append(self.tarven.pool.card_map[uuid])
        if not options:
            return False
        self.tarven.force_action.append(
            HeroChoiceAction(options=options, kind=kind, payload=payload or {}, pool_owned=True)
        )
        return True

    def choose_static(self, options: Iterable[object], *, kind: str, payload=None) -> bool:
        values = list(options)
        if not values:
            return False
        self.tarven.force_action.append(
            HeroChoiceAction(options=values, kind=kind, payload=payload or {}, pool_owned=False)
        )
        return True

    def handle_choice(self, action: HeroChoiceAction) -> bool:
        t = self.tarven
        if action.selected not in action.options:
            return False

        selected = action.selected
        unselected = list(action.options)
        unselected.remove(selected)  # 多重集语义：同 UUID/同对象候选只消费一份。
        if action.kind == "abathur":
            slot = self._slot(action.payload.get("slot_idx"))
            if not isinstance(selected, Card) or slot is None or t.mineral < 2:
                if action.pool_owned:
                    t.pool.place_back(action.options)
                action.payload["_terminal_failure"] = True
                return False
            destroyed_index = slot.index
            was_level_six = slot.level == 6
            t.mineral -= 2
            t.destroy(slot)
            granted = t.store_card_to_cache(selected) or t.enter_card_direct(
                selected, slot_idx=destroyed_index
            )
            if not granted:  # Defensive only: destruction guarantees an empty slot.
                t.pool.place_back(action.options)
                action.payload["_terminal_failure"] = True
                return False
            if was_level_six:
                t.level_up_cost = max(0, t.level_up_cost - 4)
            self.state["last_power_round"] = t.round
            if action.pool_owned:
                t.pool.place_back(unselected)
            return True
        overflow_rewards = {
            "card", "marine", "mutalisk", "rattlesnake", "firebat",
            "hurricane-synthesis",
        }
        cache_only_rewards = {"nova", "carrier-combo"}
        if action.kind in overflow_rewards | cache_only_rewards:
            if not isinstance(selected, Card):
                if action.pool_owned:
                    t.pool.place_back(action.options)
                action.payload["_terminal_failure"] = True
                return False
            granted = (
                t.grant_reward_card(selected)
                if action.kind in overflow_rewards
                else t.store_card_to_cache(selected)
            )
            if not granted:
                if action.pool_owned:
                    t.pool.place_back(action.options)
                action.payload["_terminal_failure"] = True
                return False
            if action.pool_owned:
                t.pool.place_back(unselected)
            if action.kind == "firebat":
                remaining = int(action.payload.get("remaining", 0))
                if remaining > 0:
                    self.discover(
                        levels=[5],
                        kind="firebat",
                        payload={"remaining": remaining - 1},
                    )
            return True

        if action.kind == "delayed-card":
            delay = int(action.payload.get("delay", 1))
            t.delay_enter_card.setdefault(t.round + delay, []).append(selected)
            if action.pool_owned:
                t.pool.place_back(unselected)
            return True

        if action.kind == "thor-description":
            slot = self._slot(action.payload.get("slot_idx"))
            if slot is None or not isinstance(selected, Card):
                if action.pool_owned:
                    t.pool.place_back(action.options)
                return False
            t.card_engine.apply_temporary_definition(slot, selected)
            if action.pool_owned:
                t.pool.place_back(action.options)
            slot.trigger([self._enter_event(slot)])
            return True

        if action.kind == "hansen-study":
            if self.hero_name != "汉森博士" or selected in self.state["studies"]:
                return False
            study = str(selected)
            self.state["studies"].append(study)
            if study == "战地勘察":
                self.state["hansen_field_due"] = t.round + 1
            elif study == "新式血清":
                self._apply_hansen_serum()
            return True

        if action.kind == "dehaka-evolution":
            slot = self._slot(action.payload.get("slot_idx"))
            choices = action.payload.get("choices", {})
            old = choices.get(selected)
            if (
                slot is None or self.state.get("essence", 0) < 1
                or old is None or slot.count(old) <= 0
            ):
                return False
            self.state["essence"] -= 1
            slot.replace_unit(old, 1, str(selected), 1)
            return True

        if action.kind == "evolution":
            self.state["mutation"] = selected
            return True

        if action.kind == "eye-race":
            granted = self._grant_eye_cards(str(selected))
            if not granted:
                action.payload["_terminal_failure"] = True
            return granted

        if action.kind == "hercules-card":
            self.state["pending_rewards"][int(action.payload["level"])] = selected
            if action.pool_owned:
                t.pool.place_back(unselected)
            return True

        if action.kind == "kerrigan-upgrade":
            slot = self._slot(action.payload.get("slot_idx"))
            if slot is None or selected not in ZERG_RESEARCH_UPGRADES:
                return False
            t.trigger_upgrade(slot, str(selected))
            return True

        # 未识别的池所有选择也必须终结并精确归还每份候选，不能让卡泄漏。
        if action.pool_owned:
            t.pool.place_back(action.options)
            action.payload["_terminal_failure"] = True
        return False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def round_start_before_income(self) -> None:
        self.state["purchases_this_round"] = 0
        self.state["refreshes_this_round"] = 0
        # 这些免费次数属于产生它们的单个回合，不能跨回合囤积。
        if self.hero_name in {"副官", "凯瑞甘", "凯瑞甘（异虫形态）"}:
            self.tarven.free_refresh = 0
        if self.hero_name == "解放者（防卫模式）":
            self.tarven.free_refresh = 0
        elif self.hero_name == "解放者（战机模式）":
            self.tarven.free_refresh = 2 if self.tarven.level >= 4 else 1
        if self.hero_name == "飞蛇":
            self.state["viper_discount_used"] = False
        if self.hero_name == "阿尔达瑞斯":
            pending = self.state.pop("pending_discounted_positions", set())
            self.state["discounted_positions"] = {
                index
                for index in pending
                if 0 <= index < len(self.tarven.shop)
                and self.tarven.shop[index] is not None
            }
        if self.hero_name == "副官":
            self.tarven.free_refresh = 1
        elif self.hero_name == "SCV":
            self.state["charges"] = min(3, self.state.get("charges", 0) + 1)
        elif self.hero_name == "眼虫" and self.tarven.round == 4:
            self.tarven.level = 3
            self.tarven.level_up_cost = 8
            self.tarven.resize_shop_for_level()
        elif self.hero_name == "干扰者":
            self.state["charges"] = 1 if self.tarven.level >= 6 else 2

    def round_start_after_income(self) -> None:
        t = self.tarven
        if self.hero_name == "矿骡" and self.state.get("penalty_round") == t.round:
            t.mineral = 2
        if self.hero_name == "工蜂":
            if t.round % 2:
                t.gas = min(t.gas + 1, t.gas_max)
            else:
                t.mineral += 1
        elif self.hero_name == "副官" and self.state.pop("next_round_mineral", False):
            t.mineral += 1
        elif self.hero_name == "德拉肯钻机":
            t.mineral += self.state.get("drill_points", 0) // 3
        elif self.hero_name == "德哈卡":
            self.state["essence"] += t.gas
        elif self.hero_name == "诺娃":
            if any(item is None for item in t.cache):
                cards = [t.pool.card_type_map[n] for n in AUXILIARY_CARD_NAMES if n in t.pool.card_type_map]
                if cards:
                    self.choose_static(t.rng.sample(cards, min(3, len(cards))), kind="nova")
        elif self.hero_name == "母舰核心" and t.round == 1 and not self.state.get("core_granted"):
            card = t.pool.card_type_map.get("母舰核心") or _dynamic_card(
                "母舰核心", race="protoss", units={"母舰核心": 1}, tags=["protoss", "无法三连"]
            )
            if t.mineral >= 3:
                empty = next((slot for slot in t.slots if slot.card_type is None), None)
                if empty is not None and t.enter_card_direct(card, slot_idx=empty.index):
                    t.mineral -= 3
                    self.state["core_granted"] = True
                    self.state["core_ref"] = empty
        elif self.hero_name == "汉森博士" and self.state.get("hansen_field_due") == t.round:
            card = t.pool.card_type_map.get("斯台特曼")
            if card is not None:
                t.grant_reward_card(card)
            self.state["hansen_field_due"] = None
        elif self.hero_name == "进化腔":
            self.state["mutation_refreshes"] = max(0, 3 - len([s for s in t.slots if s.tags.has("zerg")]))
            self.choose_static(self._mutation_options(), kind="evolution")

    def round_start_after_shop(self) -> None:
        """商店完成抽取后建立依赖剩余卡池的回合选择。"""
        t = self.tarven
        if self.hero_name == "眼虫" and t.round == 4:
            races = [
                race
                for race in ("terran", "protoss", "zerg", "neutral")
                if any(
                    card.level == 3 and card.race == race and t.pool.count(card) >= 2
                    for card in t.pool.cards
                )
            ]
            if races:
                self.choose_static(races, kind="eye-race")

    def round_start_before_cards(self) -> None:
        t = self.tarven
        if self.hero_name == "扎加拉":
            occupied = [s for s in t.slots if s.card_type is not None]
            if len(occupied) >= 6:
                low = min(occupied, key=lambda s: (s.price(), s.index))
                remaining = [s for s in occupied if s is not low]
                high = max(remaining, key=lambda s: (s.price(), -s.index))
                t.destroy(low)
                t.destroy(high)
                t.mineral += 11
        elif self.hero_name == "星港":
            target = self.state.get("air_mode", "怨灵战机")
            limit = max(0, t.level - 1)
            for slot in list(t.slots):
                if slot.card_type is None:
                    continue
                candidates = [
                    unit
                    for unit, count in slot.units.items()
                    if unit in GROUND_UNITS
                    for _ in range(count)
                ]
                for unit in t.rng.sample(candidates, min(limit, len(candidates))):
                    replacement = "战列巡航舰" if unit in HERO_UNITS else target
                    slot.replace_unit(unit, 1, replacement, 1)

    def round_end_after_cards(self) -> None:
        t = self.tarven
        if self.hero_name == "副官":
            self.state["next_round_mineral"] = t.mineral > 0
        elif self.hero_name == "汉森博士" and "样本日志" in self.state.get("studies", ()) and t.lock:
            t.level_up_cost = max(0, t.level_up_cost - t.rng.randint(1, 2))
        elif self.hero_name == "混合体":
            self._hybrid_rewards()
        elif self.hero_name == "星港":
            for slot in t.slots:
                if slot.card_type is None or slot.count("怨灵战机") <= 0:
                    continue
                candidates = [
                    unit
                    for unit in slot.units
                    if unit in AIR_UNITS and unit not in HERO_UNITS and unit != "怨灵战机"
                ]
                if candidates:
                    highest = max(UNIT_PRICES.get(unit, 0) for unit in candidates)
                    tied = [unit for unit in candidates if UNIT_PRICES.get(unit, 0) == highest]
                    slot.replace_unit("怨灵战机", 1, t.rng.choice(tied), 1)
        elif self.hero_name == "进化腔":
            mutation = self.state.get("mutation")
            if mutation:
                old, new, amount = mutation
                for slot in t.slots:
                    if slot.tags.has("zerg"):
                        converted = min(amount, slot.count(old))
                        if converted:
                            slot.replace_unit(old, converted, new, converted)

    def on_level_up(self, old_level: int, cost: int) -> None:
        t = self.tarven
        if self.hero_name == "米拉":
            self.state["last_enter_level"] = 0
        elif self.hero_name in {"凯瑞甘", "凯瑞甘（异虫形态）"}:
            t.free_refresh = 1
        elif self.hero_name == "战列巡航舰":
            for slot in list(t.slots):
                if slot.card_type is not None:
                    slot.trigger([RoundEndEvent(t)])
        elif self.hero_name == "汉森博士":
            if t.level in (2, 4, 6):
                options = [s for s in HANSEN_STUDIES if s not in self.state["studies"]]
                self.choose_static(options, kind="hansen-study")
            if "科研成本" in self.state.get("studies", ()):
                remaining = t.mineral
                t.mineral = 0
                if remaining > 0:
                    self.discover(
                        levels=list(range(1, min(6, remaining) + 1)),
                        kind="card",
                    )
        elif self.hero_name == "飓风" and t.level >= 6:
            self.state["uses"] = 0
        elif self.hero_name == "爆虫" and old_level < 4 <= t.level:
            self.state["uses"] = 0
        elif self.hero_name == "大力神":
            reward = self.state["pending_rewards"].get(t.level)
            if reward is not None:
                if not t.grant_reward_card(reward):
                    t.pool.place_back(reward)
                self.state["pending_rewards"].pop(t.level, None)
        if self.hero_name == "干扰者" and t.level >= 6:
            self.state["charges"] = min(1, self.state.get("charges", 1))
        if self.hero_name == "响尾蛇":
            self.state["rattlesnake_refreshes"] = 0

    def on_enter(self, slot: "Slot") -> None:
        t = self.tarven
        self.state["entered"] += 1
        if self.hero_name == "米拉":
            previous = self.state.get("last_enter_level", 0)
            if slot.level > previous:
                t.mineral += 1
            self.state["last_enter_level"] = slot.level
        elif self.hero_name == "探机":
            slot.add_unit("水晶塔", 1)
        elif self.hero_name == "科学球":
            if slot.units:
                highest = max(UNIT_PRICES.get(unit, 0) for unit in slot.units)
                tied = [unit for unit in slot.units if UNIT_PRICES.get(unit, 0) == highest]
                unit = t.rng.choice(tied)
                observed = self.state["observed_units"]
                observed[unit] = observed.get(unit, 0) + 1
        elif self.is_kerrigan_zerg:
            self.choose_static(ZERG_RESEARCH_UPGRADES, kind="kerrigan-upgrade", payload={"slot_idx": slot.index})
        elif self.hero_name == "战列巡航舰":
            self.state["ready"] = True

    def on_enter_after(self, slot: "Slot") -> None:
        """进场自身效果和全场广播完成后执行的英雄钩子。"""
        if self.hero_name != "阿塔尼斯" or self.state["entered"] != 10:
            return
        definition = self.tarven.pool.card_type_map.get("阿塔尼斯") or _dynamic_card(
            "阿塔尼斯",
            level=6,
            race="protoss",
            units={"阿塔尼斯": 1},
            tags=["protoss", "金色", "无法三连"],
        )
        if slot.card_type == "阿塔尼斯":
            for unit, count in definition.units.items():
                slot.add_unit(unit, count)
            for race in ("terran", "zerg", "neutral"):
                slot.tags.remove(race)
            slot.tags.add("protoss")
            slot.tags.add("金色")
            return
        self.tarven.card_engine.fuse_card_definition(slot, definition)

    def on_buy(self, card: Card) -> None:
        t = self.tarven
        if self.hero_name == "使徒" and self.state.get("adept_one_cost_charges", 0) > 0:
            self.state["adept_one_cost_charges"] -= 1
        self.state["purchases_this_round"] += 1
        count = self.state["purchases_this_round"]
        if self.hero_name == "追猎者" and (self.state.get("stalker_upgraded") or count == 1):
            t.refresh()
        elif self.hero_name == "凯瑞甘" and count >= 5:
            self.transform("凯瑞甘（异虫形态）")
        if self.hero_name == "飞蛇":
            bought = self.state.setdefault("bought_names", set())
            is_new = card.name not in bought
            bought.add(card.name)
            if is_new:
                self.state["viper_discount_used"] = True

    def on_shop_synthesis_purchase(self, card: Card) -> None:
        """Record a shop card consumed by synthesis as a purchase where required."""
        if self.hero_name == "使徒":
            # 商店三连不属于使徒的普通购买：既不计数，也不消耗已有的一费购买充能。
            return
        self.on_buy(card)

    def on_refresh(self, *, automatic: bool = False) -> None:
        self.state["refreshes_this_round"] += 1
        if self.hero_name == "阿尔达瑞斯":
            self.state.pop("pending_discounted_positions", None)
            self.state["discounted_positions"] = set()
        if self.hero_name == "追猎者" and self.state["refreshes_this_round"] >= 4:
            self.state["stalker_upgraded"] = True
        if self.hero_name == "响尾蛇":
            count = self.state.get("rattlesnake_refreshes", 0) + 1
            self.state["rattlesnake_refreshes"] = count
            used = self.state.setdefault("used_levels", set())
            if count >= 3 and self.tarven.level not in used and self.tarven.can_receive_reward():
                if self.discover(levels=[self.tarven.level], kind="rattlesnake"):
                    used.add(self.tarven.level)

    def on_lock(self) -> None:
        if self.hero_name == "阿尔达瑞斯":
            self.state["pending_discounted_positions"] = {
                index
                for index, card in enumerate(self.tarven.shop)
                if card is not None
            }

    def on_sell(self, slot: "Slot") -> None:
        """出售效果前调用；此时槽位静态来源、单位和星级仍完整可查。"""
        if self.hero_name == "汉森博士" and "采集神器" in self.state.get("studies", ()) and slot.tags.has("protoss"):
            for neighbor in slot.neighbors:
                if self.tarven.rng.randrange(2):
                    neighbor.add_unit("劫掠者", 1)
                else:
                    neighbor.add_unit("陆战队员", 2)
        self.state["sold"] = self.state.get("sold", 0) + 1
        self.state["last_sold_level"] = slot.level

    def on_synthesis(self, slot: "Slot") -> None:
        self.state["syntheses"] += 1
        if self.hero_name == "使徒":
            self.state["adept_one_cost_charges"] = self.state.get("adept_one_cost_charges", 0) + 1
        elif self.hero_name == "母舰核心":
            candidate = self.state.get("core_ref")
            if candidate in self.tarven.slots and candidate.card_type in {"母舰核心", "母舰"}:
                candidate.add_unit("虚空辉光舰(精英)", self.tarven.level)
                if self.state["syntheses"] == 2:
                    candidate.card_type = "母舰"
        elif self.hero_name == "飓风":
            race = getattr(slot.source_card, "race", None)
            if race in {"terran", "protoss", "zerg", "neutral"}:
                self.discover(tags=[race], kind="hurricane-synthesis")

    # ------------------------------------------------------------------
    # 动作策略
    # ------------------------------------------------------------------
    def purchase_delay(self) -> int:
        """返回本次购买应延迟的回合数；0 表示走普通放置路径。"""
        if self.hero_name != "航母":
            return 0
        purchase_number = self.state.get("purchases_this_round", 0) + 1
        return purchase_number if purchase_number <= 2 else 0

    def schedule_delayed_purchase(self, card: Card, delay: int) -> None:
        if self.hero_name != "航母":
            return
        arrival_round = self.tarven.round + delay
        delay_index = len(self.tarven.delay_enter_card.get(arrival_round, ())) - 1
        self.state["carrier_pending"].setdefault(arrival_round, []).append(
            (card, delay, self.tarven.round, delay_index)
        )

    def receive_delayed_card(self, card: Card, *, delay_index: int) -> bool:
        """仅结算与当前延迟队列位置绑定的航母购买记录。"""
        if self.hero_name != "航母":
            return False
        t = self.tarven
        pending = self.state["carrier_pending"].get(t.round, [])
        record = next(
            (item for item in pending if item[0] is card and item[3] == delay_index),
            None,
        )
        if record is None:
            return False
        pending.remove(record)
        delay = record[1]
        empty = next((slot for slot in t.slots if slot.card_type is None), None)
        if empty is not None:
            t.card_engine.assign_card_to_slot(card, empty)
            t.trigger_entering(empty)
            self.state["carrier_entries"].append((t.round, card))
            self.state["carrier_history"].setdefault(t.round, {})[delay] = card
        else:
            # 到期时场满则进入暂存区；双满按航母契约直接丢弃，不能归还公共卡池。
            t.store_card_to_cache(card)
        return True

    def after_delayed_cards(self) -> None:
        if self.hero_name != "航母":
            return
        completed = self.state["carrier_completed"]
        for purchase_round, pair in sorted(self.state["carrier_history"].items()):
            if purchase_round in completed or not {1, 2} <= set(pair):
                continue
            completed.add(purchase_round)
            self.discover(
                levels=[pair[1].level], tags=[pair[2].race], count=2,
                kind="carrier-combo",
            )

    def level_up_cost(self, base: int) -> int:
        if self.hero_name == "大力神" and self.tarven.level + 1 in {3, 5}:
            return base + 1
        return base

    def permits_action(self, action) -> bool:
        from star_tarven_simulator.simulator.action import BuyAction, RefreshAction, SynthesisAction
        if (
            self.hero_name == "眼虫" and self.tarven.round <= 3
            and (
                isinstance(action, BuyAction)
                or (isinstance(action, SynthesisAction) and isinstance(action.shop_idx, int))
            )
        ):
            return False
        if self.hero_name == "解放者（防卫模式）" and isinstance(action, RefreshAction):
            return False
        return True

    def card_price(self, card: Optional[Card], base: int, *, shop_idx: int | None = None) -> int:
        if card is None:
            return base
        if self.hero_name == "解放者（防卫模式）":
            return 2
        if self.hero_name == "解放者（战机模式）":
            return 4
        if self.hero_name == "收割者" and "能够定点部署" in card.tags:
            return max(0, base - 1)
        if self.hero_name == "使徒":
            if self.state.get("adept_one_cost_charges", 0) > 0:
                return 1
            if self.state["purchases_this_round"] == 2:
                return max(0, base - 2)
        if self.hero_name == "飞蛇":
            bought = self.state.setdefault("bought_names", set())
            if card.name not in bought and not self.state.get("viper_discount_used"):
                return max(0, base - 1)
        if (
            self.hero_name == "阿尔达瑞斯"
            and shop_idx in self.state.get("discounted_positions", set())
        ):
            return 2
        return base

    def shop_synthesis_price(self, card: Optional[Card], *, shop_idx: int) -> int:
        """商店三连投入卡的价格。

        使徒明确不把商店三连视为普通购买，因此不能复用“下一张 1 矿”或
        “本回合第 3 张减 2”的购买折扣；其它单英雄价格规则照常生效。
        """
        if self.hero_name == "使徒":
            return 3
        return self.card_price(card, 3, shop_idx=shop_idx)

    def placement_anywhere(self, card: Card) -> bool:
        return self.hero_name == "收割者"

    def power(self, action: HeroPowerAction) -> bool:
        t, h = self.tarven, self.hero_name
        slot = self._slot(action.slot_idx)

        if h == "陆战队员":
            if (
                t.mineral < 2 or self.state["last_power_round"] == t.round
                or not t.can_receive_reward()
            ):
                return False
            level = max(1, t.level - 1)
            if not self.discover(levels=[level], kind="marine"):
                return False
            t.mineral -= 2
            self.state["last_power_round"] = t.round
            return True
        if h == "矿骡":
            last = self.state.get("last_power_round", -1)
            if last >= 0 and last >= t.round - 1:
                return False
            t.mineral += t.mineral_max
            self.state["penalty_round"] = t.round + 1
            self.state["last_power_round"] = t.round
            return True
        if h == "德拉肯钻机":
            if self.state.get("drill_level") == t.level:
                return False
            amount = min(5, t.mineral, action.amount if action.amount is not None else 5)
            if amount <= 0:
                return False
            t.mineral -= amount
            self.state["drill_points"] += amount
            self.state["drill_level"] = t.level
            return True
        if h in {"解放者（防卫模式）", "解放者（战机模式）"}:
            if self.state["last_power_round"] == t.round:
                return False
            self.state["last_power_round"] = t.round
            if h == "解放者（防卫模式）":
                self.transform("解放者（战机模式）")
                t.free_refresh = 2 if t.level >= 4 else 1
            else:
                self.transform("解放者（防卫模式）")
                t.free_refresh = 0
            return True
        if h == "异龙":
            if (
                t.mineral < 2 or self.state.get("used_level") == t.level
                or not t.can_receive_reward()
            ):
                return False
            t.mineral -= 2
            if self.discover(levels=list(range(1, t.level + 1)), tags=["zerg"], kind="mutalisk"):
                self.state["used_level"] = t.level
                return True
            t.mineral += 2
            return False
        if h == "休伯利安点唱机":
            if self.state["uses"] or not isinstance(action.card, str) or not t.can_receive_reward():
                return False
            definition = t.pool.card_type_map.get(action.card)
            if (
                definition is None or not 1 <= definition.level <= t.level
                or definition.uuid in t.pool.no_draw_uuids or definition.derived
            ):
                return False
            card = self._initial_copy(definition, "休伯利安点唱机")
            if not t.grant_reward_card(card):
                return False
            self.state["uses"] = 1
            return True
        if h == "飓风":
            if (
                self.state["uses"] or t.mineral < 1
                or action.race not in {"terran", "protoss", "zerg", "neutral"}
                or action.race == self.state.get("excluded_race")
            ):
                return False
            t.mineral -= 1
            self.state["uses"] = 1
            self.state["excluded_race"] = action.race
            return True
        if h == "雷诺":
            if (
                self.state["uses"] or slot is None or not 1 <= slot.level < 6
                or slot.tags.has("金色") or slot.card_type == "虫卵" or slot.derived
                or not isinstance(slot.source_card, Card)
                or slot.source_card.uuid in t.pool.no_draw_uuids
                or len(slot.upgrades) >= slot.upgrades_limit
            ):
                return False
            t.card_engine.make_gold(slot)
            t.trigger_upgrade(slot, "金光闪闪")
            self.state["uses"] = 1
            return True
        if h == "雷神":
            if slot is None or slot.level >= 6 or self.state.get("used_level") == t.level:
                return False
            core_sources = {"核心人族", "核心神族", "核心虫族", "核心中立"}
            candidates = [
                c for c in t.pool.cards
                if c.level == slot.level
                and c.race == getattr(slot.source_card, "race", None)
                and c is not slot.source_card
                and bool(c.source)
                and any(source in core_sources for source in c.source)
            ]
            if not candidates:
                return False
            self.state["used_level"] = t.level
            return self.choose_static(candidates, kind="thor-description", payload={"slot_idx": slot.index})
        if h == "机械哨兵":
            if (
                slot is None or not 1 <= slot.level <= 4 or slot.derived
                or not isinstance(slot.source_card, Card)
                or slot.source_card.uuid in t.pool.no_draw_uuids
                or t.mineral < 3 or self.state["uses"] >= 3
                or self.state["last_power_round"] == t.round
                or not t.can_receive_reward()
            ):
                return False
            copied = self._initial_copy(slot.source_card, "机械哨兵")
            if not t.grant_reward_card(copied):
                return False
            t.mineral -= 3
            self.state["uses"] += 1
            self.state["last_power_round"] = t.round
            return True
        if h == "阿巴瑟":
            if slot is None or self.state["last_power_round"] == t.round or t.mineral < 2:
                return False
            level = min(6, slot.level + 1)
            # Draw candidates before payment/destruction so an empty pool is atomic.
            options = []
            for _ in range(3):
                uuid = t.pool._sample(levels=[level])
                if uuid is None:
                    break
                options.append(t.pool.card_map[uuid])
            if not options:
                return False
            t.force_action.append(
                HeroChoiceAction(
                    options=options,
                    kind="abathur",
                    payload={"slot_idx": slot.index},
                    pool_owned=True,
                )
            )
            return True
        if h == "德哈卡":
            if slot is None:
                return False
            is_primal = bool(slot.tags.has("属于原始虫群") or slot.tags.has("原始虫群"))
            if not is_primal:
                if self.state["essence"] < 6:
                    return False
                definition = t.pool.card_type_map.get("原始刺蛇")
                if definition is None:
                    return False
                self.state["essence"] -= 6
                t.card_engine.replace_definition_preserving_payload(slot, definition)
                return True
            if self.state["essence"] < 1:
                return False
            choices = {
                new: old
                for old, new in PRIMAL_EVOLUTION.items()
                if slot.count(old) > 0
            }
            if not choices:
                return False
            return self.choose_static(
                choices,
                kind="dehaka-evolution",
                payload={"slot_idx": slot.index, "choices": choices},
            )
        if h == "干扰者":
            charges = self.state.get("charges", 2)
            if charges <= 0:
                return False
            changed = 0
            for index, item in enumerate(t.cache):
                card = t.pool.card_type_map.get(item) if isinstance(item, str) else item
                if not isinstance(card, Card) or card.derived or not 1 <= card.level <= 6:
                    continue
                uuid = t.pool._sample(levels=[card.level])
                if uuid is None:
                    continue
                replacement = t.pool.card_map[uuid]
                # 每一项独立提交：只有成功抽到替代项后才归还旧卡并覆盖缓存。
                t.pool.place_back(card)
                t.cache[index] = replacement
                changed += 1
            if changed == 0:
                return False
            self.state["charges"] = charges - 1
            return True
        if h == "感染虫":
            if slot is None or not slot.tags.has("terran") or self.state["last_power_round"] == t.round:
                return False
            slot.remove_unit("过载水晶塔", slot.count("过载水晶塔"))
            slot.tags.add("无法三连")
            slot.temporary_description = ["无法三连；每回合结束时从当前单位中随机选择一种并注卵1个"]
            slot.temporary_handler_descriptions = set(slot.temporary_description)
            slot.event_handlers = [EventHandler(t, slot, slot.temporary_description[0], self._infected_larva, "round_end")]
            self.state["last_power_round"] = t.round
            return True
        if h == "SCV":
            if slot is None or not slot.tags.has("terran") or self.state.get("charges", 0) <= 0:
                return False
            addons = ["反应堆"] * slot.count("反应堆") + ["科技实验室"] * slot.count("科技实验室")
            if not addons:
                return False
            selected = t.rng.choice(addons)
            replacement = "科技实验室" if selected == "反应堆" else "反应堆"
            slot.remove_unit(selected, 1)
            slot.add_unit(replacement, 1)
            from star_tarven_simulator.simulator.event import AnyCardAddonChangedEvent, QuickProduceEvent
            slot.trigger([QuickProduceEvent(t, slot)])
            t.trigger_any_card_event(AnyCardAddonChangedEvent(t, slot))
            self.state["charges"] -= 1
            return True
        if h == "探机":
            if slot is None or t.mineral < 1 or self.state["last_power_round"] == t.round:
                return False
            t.mineral -= 1
            slot.add_unit("过载水晶塔", 1)
            slot.add_unit("虚空水晶塔", 2)
            self.state["last_power_round"] = t.round
            return True
        if h == "斯旺":
            if slot is None:
                return False
            initial_mechanical = [unit for unit in slot.units if unit in MECHANICAL_UNITS]
            if sum(slot.count(unit) for unit in initial_mechanical) <= 0:
                return False
            factory = next((s for s in t.slots if s.card_type == "机械工厂"), None)
            if factory is None:
                empty = next((s for s in t.slots if s.card_type is None), None)
                if empty is None:
                    return False
                definition = _dynamic_card(
                    "机械工厂", level=0, race="neutral",
                    tags=["neutral", "无法三连"],
                )
                if not t.enter_card_direct(definition, slot_idx=empty.index):
                    return False
                factory = empty
            # 机械工厂正常进场可能触发其它卡牌效果并改变目标单位；按结算时的
            # 实际机械单位重新统计，确保每个被移除单位都产生一个零件。
            mechanical = [unit for unit in list(slot.units) if unit in MECHANICAL_UNITS]
            removed = sum(slot.count(unit) for unit in mechanical)
            for unit in mechanical:
                slot.remove_unit(unit, slot.count(unit))
            factory.add_unit("零件", removed)
            return True
        if h == "蒙斯克":
            cost = self.state["uses"] + 1
            if t.mineral < cost:
                return False
            changed = 0
            for candidate in t.slots:
                for old, new in ROYAL_GUARD_PRIORITY:
                    if candidate.count(old):
                        candidate.replace_unit(old, 1, new, 1)
                        changed += 1
                        break
            if not changed:
                return False
            t.mineral -= cost
            self.state["uses"] += 1
            return True
        if h == "医疗兵":
            if slot is None or self.state["last_power_round"] == t.round:
                return False
            recipients = [s for s in t.slots if s.card_type is not None and s is not slot and s.card_type != "虫卵"]
            biological = [
                unit
                for unit, count in slot.units.items()
                if unit in BIOLOGICAL_UNITS and unit not in HERO_UNITS
                for _ in range(count)
            ]
            t.rng.shuffle(biological)
            for unit, count in list(slot.units.items()):
                slot.remove_unit(unit, count)
            for cursor, unit in enumerate(biological):
                if recipients:
                    recipients[cursor % len(recipients)].add_unit(unit, 1)
            self.state["last_power_round"] = t.round
            return True
        if h == "爆虫":
            if self.state["uses"]:
                return False
            for candidate in t.slots:
                for unit, count in list(candidate.units.items()):
                    candidate.add_unit(unit, count // 2)
            self.state["uses"] = 1
            return True
        if h == "执政官":
            if slot is None or slot.right is None or self.state["last_power_round"] == t.round:
                return False
            right = slot.right
            left_race = self._slot_race(slot)
            right_race = self._slot_race(right)
            if (
                not slot.tags.has("金色") or not right.tags.has("金色")
                or slot.tags.has("无法融合") or right.tags.has("无法融合")
                or left_race == right_race
            ):
                return False
            t.card_engine.fuse_slots(slot, right)
            self.state["last_power_round"] = t.round
            return True
        if h == "分裂池":
            if slot is None or self.state["last_power_round"] == t.round:
                return False
            handlers = [
                handler for handler in slot.event_handlers
                if handler.event_name == RoundStartEvent.event_name
                and (slot.card_type == "虫卵" or "孵化" in handler.description)
            ]
            if not handlers:
                return False
            event = RoundStartEvent(t)
            for handler in handlers:
                handler.handle(event)
            self.state["last_power_round"] = t.round
            return True
        if h == "进化腔":
            free = self.state.get("mutation_refreshes", 0)
            if free > 0:
                self.state["mutation_refreshes"] = free - 1
            elif t.mineral >= 1:
                t.mineral -= 1
            else:
                return False
            return self.choose_static(self._mutation_options(), kind="evolution")
        if h == "星港":
            if action.unit not in {"怨灵战机", "维京战机", "女妖"}:
                return False
            self.state["air_mode"] = action.unit
            return True
        if h == "科学球":
            if (
                self.state["uses"] >= 2 or self.state["last_power_round"] == t.round
                or not t.can_receive_reward(direct_only=True)
            ):
                return False
            card = _dynamic_card("观察样本", units=self.state["observed_units"], tags=["neutral", "无法三连"])
            if not t.grant_reward_card(card, direct_only=True):
                return False
            self.state["uses"] += 1
            self.state["last_power_round"] = t.round
            return True
        if h == "火蝠":
            if self.state["uses"] >= 4:
                return False
            discoveries = int(t.total_power() // 2000)
            if discoveries <= 0:
                return False
            for candidate in list(t.slots):
                t.destroy(candidate)
            self.state["uses"] += 1
            # Board destruction commits before the first sequential discovery.
            self.discover(
                levels=[5],
                kind="firebat",
                payload={"remaining": discoveries - 1},
            )
            return True
        if h == "战列巡航舰":
            if t.level < 2 or not self.state.get("ready", True):
                return False
            t.level = 1
            t.resize_shop_for_level()
            from star_tarven_simulator.constants.tarven import TARVEN_UPGRADE_COST
            t.level_up_cost = TARVEN_UPGRADE_COST[2]
            if not t.refresh():
                return False
            self.state["ready"] = False
            return True
        if h == "坑道虫":
            if (
                self.state["last_power_round"] == t.round
                or action.slot_idx in {2, 3, 4} or action.slot_idx is None
                or not 0 <= action.slot_idx < 7
            ):
                return False
            t.clear_extra_neighbors(3)
            t.add_extra_neighbor(action.slot_idx, 3)
            self.state["last_power_round"] = t.round
            return True
        if h == "大力神":
            if slot is None or action.target_idx is None or not 0 <= action.target_idx < 7 or self.state["last_power_round"] == t.round:
                return False
            other = t.slots[action.target_idx]
            source_index = slot.index
            target_index = other.index
            t.slots[source_index], t.slots[target_index] = other, slot
            other.index = source_index
            slot.index = target_index
            self.state["last_power_round"] = t.round
            return True
        if h == "飞蛇":
            if self.state["uses"] or action.card is not None or not t.can_receive_reward():
                return False
            card = t.game.current_opponent_highest_card(t)
            if card is None:
                return False
            copied = replace(
                card,
                uuid=-(abs(hash(("飞蛇", id(t), card.uuid))) % 1_000_000_000 + 1),
                units=dict(card.units), tags=list(card.tags), gold_tags=list(card.gold_tags),
                event_handlers=list(card.event_handlers), gold_event_handlers=list(card.gold_event_handlers),
                derived=True,
            )
            if not t.grant_reward_card(copied):
                return False
            self.state["uses"] = 1
            return True
        return False

    # ------------------------------------------------------------------
    def transform(self, hero_name: str) -> None:
        self.hero_name = hero_name
        if hero_name == "凯瑞甘（异虫形态）":
            self.tarven.gas = 0
            self.tarven.gas_max = 0
            for slot in self.tarven.slots:
                if slot.source_card is not None:
                    for unit, count in slot.source_card.units.items():
                        slot.add_unit(unit, count)
        if hero_name == "汉森博士（异虫形态）":
            self.state["studies"] = []

    def _slot(self, index) -> Optional["Slot"]:
        if isinstance(index, int) and 0 <= index < len(self.tarven.slots):
            slot = self.tarven.slots[index]
            return slot if slot.card_type is not None else None
        return None

    def _resolve_card(self, value) -> Optional[Card]:
        if isinstance(value, Card):
            return value
        if isinstance(value, str):
            return self.tarven.pool.card_type_map.get(value)
        return None

    @staticmethod
    def _infected_larva(slot: "Slot", event) -> None:
        choices = list(slot.units)
        if choices:
            event.tarven.larva({event.tarven.rng.choice(choices): 1})

    def _initial_copy(self, definition: Card, source: str) -> Card:
        """Create an initial-state ordinary reward without consuming the pool."""
        return replace(
            definition,
            uuid=-(abs(hash((source, id(self.tarven), definition.uuid, self.state.get("uses", 0)))) % 1_000_000_000 + 1),
            units=dict(definition.units),
            tags=list(definition.tags),
            gold_tags=list(definition.gold_tags),
            event_handlers=list(definition.event_handlers),
            gold_event_handlers=list(definition.gold_event_handlers),
            derived=False,
        )

    @staticmethod
    def _slot_race(slot: "Slot") -> str:
        races = [race for race in ("terran", "protoss", "zerg") if slot.tags.has(race)]
        return races[0] if len(races) == 1 else "neutral"

    def _enter_event(self, slot):
        from star_tarven_simulator.simulator.event import EnteringEvent
        return EnteringEvent(self.tarven, slot)

    def _grant_eye_cards(self, race: str) -> bool:
        t = self.tarven
        if sum(item is None for item in t.cache) < 2:
            return False
        candidates = [
            card for card in t.pool.cards
            if card.level == 3 and card.race == race and t.pool.count(card) >= 2
        ]
        if not candidates:
            return False
        card = t.rng.choice(candidates)
        drawn = [t.pool.take(card), t.pool.take(card)]
        if any(item is None for item in drawn):
            t.pool.place_back([item for item in drawn if item is not None])
            return False
        stored = []
        for item in drawn:
            if not t.store_card_to_cache(item):
                t.pool.place_back(stored + [remaining for remaining in drawn if remaining not in stored])
                return False
            stored.append(item)
        return True

    def _apply_hansen_serum(self) -> None:
        t = self.tarven
        for slot in t.slots:
            if slot.card_type is not None and len(slot.upgrades) < slot.upgrades_limit:
                t.trigger_upgrade(slot, "强化药剂")
        occupied = [slot for slot in t.slots if slot.card_type is not None]
        if all(slot.tags.has("zerg") for slot in occupied):
            self.transform("汉森博士（异虫形态）")

    def _mutation_options(self):
        basic = ("跳虫", "爆虫", "蟑螂", "刺蛇", "破坏者", "异龙", "雷兽")
        excluded = {
            "虫卵", "菌毯肿瘤", "刺蛇卵", "侦测器", "地堡", "零件",
            "反应堆", "科技实验室", "高级科技实验室", "水晶塔",
            "虚空水晶塔", "过载水晶塔", "信号塔", "虚空裂隙",
        }
        first_level: dict[str, int] = {}
        for card in self.tarven.pool.cards:
            for unit in card.units:
                first_level[unit] = min(first_level.get(unit, 7), card.level)
        outcomes = [
            unit
            for unit in dict.fromkeys(ZERG_UNITS)
            if unit in first_level
            and unit not in excluded
            and not unit.endswith(("(精英)", "(英雄)", "(皇家卫队)"))
        ]
        options = []
        for _ in range(3):
            old = self.tarven.rng.choice(basic)
            eligible = [unit for unit in outcomes if unit != old]
            if not eligible:
                break
            weights = [1 / first_level[unit] for unit in eligible]
            new = self.tarven.rng.choices(eligible, weights=weights, k=1)[0]
            n = first_level[new]
            amount = self.tarven.rng.randint(1, max(1, 13 - 2 * n))
            options.append((old, new, amount))
        return options

    def _hybrid_rewards(self) -> None:
        by_level = {}
        for slot in self.tarven.slots:
            if slot.card_type is not None:
                by_level.setdefault(slot.level, []).append(slot)
        units = {1: "混合体掠夺者", 2: "混合体天罚者", 3: "混合体毁灭者", 4: "混合体巨兽", 5: "混合体支配者", 6: "混合体实验体"}
        for level, slots in by_level.items():
            protoss = [s for s in slots if s.tags.has("protoss")]
            zerg = [s for s in slots if s.tags.has("zerg")]
            for protoss_slot, zerg_slot in zip(protoss, zerg):
                left = min((protoss_slot, zerg_slot), key=lambda slot: slot.index)
                left.add_unit(units.get(level, "混合体实验体"), 1)

