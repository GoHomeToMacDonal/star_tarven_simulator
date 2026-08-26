"""泥巴流 Agent 主体：启发式骨架 + 关键分叉蒙特卡洛前向采样。

``MudAgent`` 通过 :meth:`take_one_action` 一次执行一个动作，返回 ``True`` 表示本回合还想
继续动作、``False`` 表示结束本回合。外层（:func:`play_game` 或 rollout）负责推进回合。

决策分阶段（见 :meth:`take_one_action`）：

A. 解决强制动作（发现 / 三连奖励 / 点升级选择）。
B. 起手/找牌：陆战队员发技能找低 1 级卡；抓理财卡与对子。
C. 凑三连：拿到能成三连的第三张（商店/暂存）。
D. 点黄金矿工：给"合法升级池最小"的卡点升级（技巧 #1）。
E. 升本：升本费降到 1/0 且本回合目标已满足时升本（技巧 #2）。
F. 兜底：把高价值理财/对子卡买进场或暂存。

``search_enabled=True`` 时，在"发现选哪张 / 是否升本"等关键分叉调用蒙特卡洛评估器
（:mod:`.mcts`）；``search_enabled=False``（rollout 内部）则全用骨架的确定性/随机 tie-break。
"""

from __future__ import annotations

from typing import List, Optional

from star_tarven_simulator.agents.mud_agent import heuristics as H
from star_tarven_simulator.agents.mud_agent.reward import FINAL_ROUND, RewardWeights
from star_tarven_simulator.agents.mud_agent.state import Observation, Trackers
from star_tarven_simulator.simulator.action import (
    BuyAction,
    CacheEnterAction,
    ChooseSynthesisAction,
    ChooseUpgradeAction,
    HeroChoiceAction,
    HeroPowerAction,
    SynthesisAction,
    UpgradeAction,
    UpgradeTarvenAction,
)
from star_tarven_simulator.simulator.card import Card
from star_tarven_simulator.simulator.game import Game

# 有主动"找牌/理财"技能、需要每回合发动的英雄。
_ACTIVE_FIND_HEROES = {"陆战队员"}


class MudAgent:
    def __init__(
        self,
        game: Game,
        player_idx: int,
        trackers: Optional[Trackers] = None,
        *,
        search_enabled: bool = True,
        samples: int = 64,
        weights: Optional[RewardWeights] = None,
        rng=None,
    ):
        self.game = game
        self.player_idx = player_idx
        self.t = game.tarvens[player_idx]
        self.trackers = trackers if trackers is not None else Trackers()
        self.search_enabled = search_enabled
        self.samples = samples
        self.weights = weights or RewardWeights()
        self.rng = rng if rng is not None else self.t.pool.rng
        # 本回合内已发过技能 / 已尝试过升本，避免同回合重复。
        self._acted_flags: dict = {}
        # 每个等级已为"在建对子"等待过的回合数（耐心计数）。
        self._wait_rounds: dict = {}

    # 同一等级为在建对子最多等待的回合数（骨架近似"何时放弃等三连"）。
    _WAIT_PATIENCE = 2

    # ------------------------------------------------------------------
    # 回合边界
    # ------------------------------------------------------------------
    def new_round(self) -> None:
        self._acted_flags = {}
        self.trackers.observe_level(self.t.level, self.t.round)

    # ------------------------------------------------------------------
    # 一次动作
    # ------------------------------------------------------------------
    def take_one_action(self) -> bool:
        """执行一个动作。返回 True 表示本回合继续，False 表示结束回合。"""
        t = self.t

        # 阶段 A：强制动作最优先。
        if t.force_action:
            return self._resolve_force_action(t.force_action[0])

        # 阶段 B：陆战队员每回合发技能找低 1 级卡。
        if (
            t.hero_controller.hero_name in _ACTIVE_FIND_HEROES
            and not self._acted_flags.get("hero_power")
            and t.mineral >= 2
            and t.can_receive_reward()
        ):
            self._acted_flags["hero_power"] = True
            if t.action(HeroPowerAction()):
                return True  # 会产生 force_action，下次循环解决

        # 阶段 C：拿到能成三连的第三张（商店优先，其次暂存）。
        if self._try_complete_triple():
            return True

        # 阶段 C2：把暂存区的重复卡搬上场，逐步凑对子/三连。
        if self._try_build_pair_from_cache():
            return True

        # 阶段 D：点黄金矿工（技巧 #1）。
        if self._try_gold_miner():
            return True

        # 阶段 E：升本（技巧 #2：费用降到 1/0 时再升，连升）。
        if self._try_level_up():
            return True

        # 阶段 F：买入高价值理财/对子卡（进场或暂存）。
        if self._try_buy_value_card():
            return True

        # 无更多有益动作，结束回合。
        return False

    # ------------------------------------------------------------------
    # 强制动作
    # ------------------------------------------------------------------
    def _resolve_force_action(self, fa) -> bool:
        t = self.t
        if isinstance(fa, HeroChoiceAction):
            return self._resolve_hero_choice(fa)
        if isinstance(fa, ChooseSynthesisAction):
            return self._resolve_synthesis_choice(fa)
        if isinstance(fa, ChooseUpgradeAction):
            return self._resolve_upgrade_choice(fa)
        # 其余强制动作（延迟发现等）：选第一个可选项兜底。
        from star_tarven_simulator.simulator.action import ChooseCardAction

        if isinstance(fa, ChooseCardAction):
            best = self._best_card_choice(fa.cards)
            fa.selected_card = best if best is not None else (fa.cards[0] if fa.cards else None)
            return t.action(fa)
        return False

    def _resolve_hero_choice(self, fa: HeroChoiceAction) -> bool:
        cards = [c for c in fa.options if isinstance(c, Card)]
        best = self._best_card_choice(cards) if cards else None
        fa.selected = best if best is not None else (fa.options[0] if fa.options else None)
        return self.t.action(fa)

    def _resolve_synthesis_choice(self, fa: ChooseSynthesisAction) -> bool:
        # 三连即将完成 → 记一次三连。
        self.trackers.on_triple()
        # 奖励优先选：能凑新对子/理财卡的卡；否则选聚能器；否则第一张。
        card_options = [o for o in fa.options if isinstance(o, Card)]
        best = self._best_card_choice(card_options) if card_options else None
        if best is not None and H.buy_score(best, self.t) > 60.0:
            fa.selected = best
        elif "聚能器" in fa.options:
            fa.selected = "聚能器"
        elif best is not None:
            fa.selected = best
        else:
            fa.selected = fa.options[0] if fa.options else None
        return self.t.action(fa)

    def _resolve_upgrade_choice(self, fa: ChooseUpgradeAction) -> bool:
        names = list(fa.upgrade_names)
        if H.GOLD_MINER in names:
            fa.selected_upgrade_name = H.GOLD_MINER
        else:
            # 未命中黄金矿工：选一个有战力倍率的升级（简单兜底取第一个）。
            fa.selected_upgrade_name = names[0] if names else None
        return self.t.action(fa)

    # ------------------------------------------------------------------
    # 卡牌选择评分（发现/三连奖励共用），可接入蒙特卡洛
    # ------------------------------------------------------------------
    def _best_card_choice(self, cards: List[Card]) -> Optional[Card]:
        cards = [c for c in cards if isinstance(c, Card)]
        if not cards:
            return None
        if self.search_enabled and len(cards) > 1:
            from star_tarven_simulator.agents.mud_agent.mcts import evaluate_card_choices

            chosen = evaluate_card_choices(self, cards)
            if chosen is not None:
                return chosen
        # 骨架：按启发式分选，最高分并列时随机。
        scored = [(H.buy_score(c, self.t), c) for c in cards]
        best_score = max(s for s, _ in scored)
        best = [c for s, c in scored if s == best_score]
        return self.rng.choice(best)

    # ------------------------------------------------------------------
    # 阶段 C：凑三连
    # ------------------------------------------------------------------
    def _try_complete_triple(self) -> bool:
        """在场上已有同名对子时，用商店 / 暂存的第三张直接三连。

        第三张走 ``SynthesisAction``（不需要空槽，合并进左槽），因此即使场满也能完成。
        """
        t = self.t
        # 商店里有能补成三连的第三张？（场上恰 2 张同名可三连）
        for idx, card in enumerate(t.shop):
            if not isinstance(card, Card):
                continue
            if H.pair_partner_on_board(t, card.name) == 2:
                if t.mineral >= t.hero_controller.shop_synthesis_price(card, shop_idx=idx):
                    if t.action(SynthesisAction(shop_idx=idx)):
                        return True
        # 暂存区里有能补成三连的第三张？
        for idx, item in enumerate(t.cache):
            name = H.card_name(item)
            if name is None:
                continue
            if H.pair_partner_on_board(t, name) == 2:
                if t.action(SynthesisAction(cache_idx=idx)):
                    return True
        return False

    def _try_build_pair_from_cache(self) -> bool:
        """把暂存区里的重复卡搬上场，逐步凑出场上对子/三连。

        暂存区累积同名卡时：
        * 场上已有 1 张、暂存 ≥1 张 → 进场 1 张形成对子；
        * 暂存有 ≥2 张、场上 0 张 → 进场 1 张（下一步再进第 2 张成对子）；
        * 场上 2 张的情形由 :meth:`_try_complete_triple` 直接三连。
        进场用空槽；场满则不搬（避免把场占满后无法三连）。
        """
        t = self.t
        target = self._first_empty_slot_idx()
        if target is None:
            return False
        from collections import Counter

        cache_counts = Counter(
            H.card_name(item) for item in t.cache if H.card_name(item) is not None
        )
        # 优先搬"最有希望凑成三连"的名字：场上张数 + 暂存张数 越接近 3 越优先。
        best_idx, best_score = None, -1.0
        for idx, item in enumerate(t.cache):
            name = H.card_name(item)
            if name is None:
                continue
            on_board = H.pair_partner_on_board(t, name)
            in_cache = cache_counts[name]
            if on_board >= 2:
                continue  # 交给 _try_complete_triple
            # 只有当（场上+暂存）能凑到 3 张时搬运才有意义。
            if on_board + in_cache < 2:
                # 单张理财卡仍值得进场（触发死神火车任务），但不在这里处理。
                continue
            score = on_board * 2 + in_cache + H.buy_score(
                t.pool.card_type_map.get(name), t
            ) / 100.0
            if score > best_score:
                best_idx, best_score = idx, score
        if best_idx is None:
            return False
        return t.action(CacheEnterAction(best_idx, slot_idx=target))

    # ------------------------------------------------------------------
    # 阶段 D：黄金矿工
    # ------------------------------------------------------------------
    def _try_gold_miner(self) -> bool:
        t = self.t
        if t.gas < 2:
            return False
        if self._acted_flags.get("gold_miner_exhausted"):
            return False
        targets = H.gold_miner_targets(t)
        if not targets:
            self._acted_flags["gold_miner_exhausted"] = True
            return False
        best = targets[0]
        if t.action(UpgradeAction(best.index)):
            return True  # 产生 ChooseUpgradeAction，下次循环解决
        self._acted_flags["gold_miner_exhausted"] = True
        return False

    # ------------------------------------------------------------------
    # 阶段 E：升本
    # ------------------------------------------------------------------
    def _try_level_up(self) -> bool:
        t = self.t
        if self._acted_flags.get("leveled_up"):
            return False
        if t.level >= 6:
            return False
        cost = t.hero_controller.level_up_cost(t.level_up_cost)
        if t.mineral < cost:
            return False

        # 本回合还能立刻完成一个三连吗？能就先别升，把三连做完（升本走非 Synthesis 路径，
        # 但同回合先做三连更稳）。
        can_finish_triple_now = bool(H.find_triple_pairs(t)) and (
            self._shop_or_cache_has_third()
        )
        if can_finish_triple_now:
            return False

        # 升本决策用骨架规则驱动（实证：骨架升本 76% 达5本，远优于 MC 升本 40%）。
        # MC 只负责"选卡/发现"，整局 rollout 内部走同一套骨架规则，终局标量里的
        # ``level_weight`` 仍会通过选卡引导朝向升本友好的局面。
        # 升本决策分两个子阶段（泥巴流）：
        # (a) 早期"省矿连升"到 3 本：技巧 #2，升本费降到 1/0 时再升，同时低本凑三连。
        # (b) 三连目标已达 / 场面饱和后："每回合不停上本"（教学原话），用溢出的矿推进到 5~6 本。
        cheap = cost <= 1
        has_immediate_triple = bool(H.find_triple_pairs(t)) or self._cache_can_triple()

        # 早期（<4 本）若还有"在建对子"，且三连数尚少（<3，对齐教学 853 目标），则暂缓升本。
        # 设"耐心上限"：同一等级最多为在建对子等待 _WAIT_PATIENCE 回合，避免永久卡低本。
        if cheap and t.level < 4 and self.trackers.triple_count < 3:
            if has_immediate_triple:
                return False
            if self._has_pair_prospect():
                waited = self._wait_rounds.get(t.level, 0)
                if waited < self._WAIT_PATIENCE:
                    self._wait_rounds[t.level] = waited + 1
                    return False

        # 进入转型/升本阶段（教学："每回合不停上本"）。
        board_full = self._first_empty_slot_idx() is None
        push_phase = (
            (self.trackers.triple_count >= 2 or t.level >= 3 or board_full)
            and t.mineral - cost >= 1
        )
        if not (cheap or push_phase):
            return False

        if t.action(UpgradeTarvenAction()):
            self._acted_flags["leveled_up"] = True
            self.trackers.observe_level(t.level, t.round)
            return True
        return False

    def _has_pair_prospect(self) -> bool:
        """场上有对子，或场上+暂存已累计 ≥2 张同名（在建三连）。"""
        t = self.t
        if H.find_triple_pairs(t):
            return True
        from collections import Counter

        cache_counts = Counter(
            H.card_name(item) for item in t.cache if H.card_name(item) is not None
        )
        names = set(cache_counts) | {s.card_type for s in t.slots if s.card_type}
        for name in names:
            if name is None:
                continue
            total = cache_counts.get(name, 0) + H.pair_partner_on_board(t, name)
            if total >= 2:
                return True
        return False

    def _shop_or_cache_has_third(self) -> bool:
        """商店或暂存里是否有能补齐场上对子的第三张（可立刻三连）。"""
        t = self.t
        pairs = set(H.find_triple_pairs(t))
        if not pairs:
            return False
        for idx, card in enumerate(t.shop):
            if isinstance(card, Card) and card.name in pairs:
                if t.mineral >= t.hero_controller.shop_synthesis_price(card, shop_idx=idx):
                    return True
        for item in t.cache:
            if H.card_name(item) in pairs:
                return True
        return False

    def _cache_can_triple(self) -> bool:
        """暂存区（含场上同名）里是否还有能立刻凑成三连的卡。"""
        t = self.t
        from collections import Counter

        cache_counts = Counter(
            H.card_name(item) for item in t.cache if H.card_name(item) is not None
        )
        for name, cnt in cache_counts.items():
            if cnt + H.pair_partner_on_board(t, name) >= 3:
                return True
            # 场上已有对子 + 暂存 ≥1 → 可直接三连
            if H.pair_partner_on_board(t, name) == 2 and cnt >= 1:
                return True
        return False

    # ------------------------------------------------------------------
    # 阶段 F：买价值卡
    # ------------------------------------------------------------------
    def _try_buy_value_card(self) -> bool:
        """买入高价值卡：优先进场（触发死神火车任务 +1 矿），否则**仅在有意义时**暂存。

        为避免暂存区被永远用不上的重复卡堵死，暂存只接受：
        * 能与场上/暂存已有拷贝凑成三连的卡（推进三连）；
        * 顶级理财卡（死神火车/蟑螂小队）。
        """
        t = self.t
        from collections import Counter

        cache_counts = Counter(
            H.card_name(item) for item in t.cache if H.card_name(item) is not None
        )

        best_idx, best_card, best_score = None, None, 0.0
        for idx, card in enumerate(t.shop):
            if not isinstance(card, Card):
                continue
            if t.card_price(idx) > t.mineral:
                continue
            s = H.buy_score(card, t)
            if s > best_score:
                best_idx, best_card, best_score = idx, card, s
        if best_idx is None or best_score < 30.0:
            return False

        target = self._first_empty_slot_idx()
        if target is not None:
            if t.action(BuyAction(best_idx, slot_idx=target)):
                return True

        # 场满 → 只在推进三连或顶级理财时暂存，且暂存区要有空位。
        if any(c is None for c in t.cache):
            name = best_card.name
            advances_triple = (
                cache_counts[name] + H.pair_partner_on_board(t, name) >= 2
            )
            if advances_triple or H.is_economy_card(name):
                if t.action(BuyAction(best_idx, slot_idx=None)):
                    return True
        return False

    # ------------------------------------------------------------------
    def _first_empty_slot_idx(self) -> Optional[int]:
        for s in self.t.slots:
            if s.card_type is None:
                return s.index
        return None

    def observation(self) -> Observation:
        return Observation.capture(self.t)


# ----------------------------------------------------------------------
# 便捷：完整跑一局
# ----------------------------------------------------------------------
def play_game(
    game: Game,
    player_idx: int = 0,
    *,
    search_enabled: bool = True,
    samples: int = 64,
    weights: Optional[RewardWeights] = None,
    trace: bool = False,
    final_round: int = FINAL_ROUND,
) -> Trackers:
    """从头跑一局到 ``final_round``（默认第 9 回合末），返回 Agent 的跨回合统计。"""
    agent = MudAgent(
        game,
        player_idx,
        search_enabled=search_enabled,
        samples=samples,
        weights=weights,
    )
    t = game.tarvens[player_idx]
    while t.round < final_round:
        game.round_start()
        agent.new_round()
        _run_round(agent)
        if trace:
            _print_trace(agent)
        game.round_end()
    return agent.trackers


def _run_round(agent: MudAgent) -> None:
    guard = 0
    while True:
        guard += 1
        if guard > 300:
            break
        if not agent.take_one_action():
            break


def _print_trace(agent: MudAgent) -> None:
    t = agent.t
    board = [
        f"{s.card_type}{'*' if s.tags.has('金色') else ''}"
        for s in t.slots
        if s.card_type is not None
    ]
    print(
        f"R{t.round:>2} L{t.level} 矿{t.mineral} 气{t.gas} "
        f"升本费{t.level_up_cost} 三连{agent.trackers.triple_count} "
        f"| {', '.join(board)}"
    )
