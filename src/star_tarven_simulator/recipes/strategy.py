"""Graph-derived structural game plans for terminal recipes.

These guides explain what to assemble and which events/thresholds to maintain.
They intentionally do not claim a deterministic shop purchase path: random shop
availability and current economy remain the responsibility of an online agent.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import argparse
import json
from pathlib import Path
from typing import Iterable

from star_tarven_simulator.loader import DEFAULT_DATA_PATH, load_cards

from .catalog import RecipeCatalog, generate_recipe_catalog
from .graph import FactGraph, build_fact_graph
from .templates import RecipeInstance

_DORMANT_EVENTS = {"round_win", "other_player_sold_hero_card"}


@dataclass(frozen=True, slots=True)
class StrategyGuide:
    recipe_id: str
    template_id: str
    title: str
    expansions: tuple[str, ...]
    formation: tuple[str, ...]
    priorities: tuple[str, ...]
    core_loop: tuple[str, ...]
    risks: tuple[str, ...]
    evidence: tuple[str, ...]
    score: float


def _slot_line(recipe: RecipeInstance) -> tuple[str, ...]:
    return tuple(
        f"槽位{slot.index}: {slot.card.card.name}[{'金色' if slot.card.variant == 'gold' else '普通'}]"
        f"（{', '.join(slot.roles)}）"
        for slot in sorted(recipe.slots, key=lambda item: item.index)
    )


def _template_plan(recipe: RecipeInstance) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if recipe.template_id == "protoss-energy-gathering":
        return (
            (
                "前期保留能产水晶塔/虚空水晶塔的神族卡，避免把供能槽与集结核心拆开。",
                "中期把集结核心固定在槽位3，供塔组件放槽位2和4，先满足第一档能量再追第二档。",
                "后期优先金化全局集结加次或虚空塔倍率组件；折跃监听只是反馈强化，不计入初始阈值。",
            ),
            (
                "每回合结束读取闭邻域能量，能量不消耗。",
                "达到阈值后结算集结；若产物通过折跃发出事件，再驱动监听者产塔/增益。",
            ),
        )
    if recipe.template_id == "event-feedback-engine":
        event_name = dict(recipe.derived_metrics).get("event", "目标事件")
        return (
            (
                "先拿能稳定执行发射动作的组件，再补事件监听者；监听者单独上场通常没有收益。",
                f"围绕 {event_name} 控制触发频率，保留发射者和监听者直到终局。",
                "若两张卡都占关键槽，优先保证动作目标合法（折跃要有神族落点、孵化要有相邻虫族、注卵要有虫卵或空槽）。",
            ),
            (
                f"发射者执行动作并广播 {event_name}。",
                "监听者收到同一事件后追加单位、属性或下一层事件，形成跨卡反馈。",
            ),
        )
    if recipe.template_id == "zerg-swarm-engine":
        threshold = dict(recipe.derived_metrics).get("zerg_count", "目标")
        return (
            (
                f"优先铺虫族卡达到集群计数 {threshold}，核心卡不要过早卖掉。",
                "用高星虫族承担计数槽，空槽留给虫卵；需要孵化时把产卵核心放在虫族邻位。",
                "终局在不跌破集群阈值的前提下替换低价值计数卡。",
            ),
            (
                "回合结束检查虫族卡数量（纳鲁德加成按存在性处理）。",
                "阈值满足后执行集群产出，并可继续接注卵/孵化监听链。",
            ),
        )
    if recipe.template_id == "psi-ascension-engine":
        return (
            (
                "按槽位顺序放置步兵连队、势不可挡、黑暗预兆、虚空构造体，让精英化最后结算。",
                "把势不可挡累积到10座水晶塔，稳定达到集结(5)的两档触发；这10塔是终局载荷要求，不是卡牌初始单位。",
                "黑暗预兆作为5星最高灵能锚点，使3星步兵连队、4星势不可挡以及0灵能等级的虚空构造体触发灵能。",
            ),
            (
                "步兵连队和势不可挡先在回合结束产兵/折跃。",
                "黑暗预兆提供最高灵能等级，但自身灵能效果不会触发。",
                "虚空构造体最后把所有具有灵能标签卡牌中的可精英化单位整体精英化。",
            ),
        )
    if recipe.template_id == "darkness-carousel":
        return (
            (
                "保持槽位0与2为同一变体的死亡舰队，槽位1永久留作买入—出售循环位。",
                "其余槽位优先放自带黑暗容器且监听 gain_darkness 产兵的卡；鲜血猎手默认没有黑暗容器，不能直接吃死亡舰队传播。",
                "每次只在槽位1买牌并出售；不要用常驻卡填掉循环空位，也不要混用普通/金色死亡舰队的不同唯一描述。",
            ),
            (
                "出售槽位1卡牌，左右死亡舰队各直接获得一次黑暗值。",
                "左侧唯一传播效果再向右侧死亡舰队和其他黑暗容器广播一次 gain_darkness。",
                "所有监听者按事件次数产兵；金色死亡舰队加倍的是黑暗值数额，不会加倍事件触发次数。",
            ),
        )
    return (
        (
            "先获得原料供给者并积累阈值单位，再部署消耗/转移核心。",
            "把需要右侧目标的供养卡放在目标左边，出售前确认精华与目标槽合法。",
            "终局只保留能持续补原料或把原料转成高价值单位的组件。",
        ),
        (
            "供给者产生或携带需求单位。",
            "消费者达到图谱中的单位阈值后执行转移、转换或奖励。",
        ),
    )


def _risks(graph: FactGraph, recipe: RecipeInstance) -> tuple[str, ...]:
    risks = ["本指南描述终局结构，不保证随机商店中的经济到达路径。"]
    metrics = dict(recipe.derived_metrics)
    if metrics.get("semantic_confidence") == "partial":
        risks.append("至少一个不规则 handler 仅有部分声明语义；实战前应以卡面与执行 handler 为准。")
    event_name = metrics.get("event")
    if event_name in _DORMANT_EVENTS:
        risks.append(f"{event_name} 是外部驱动事件，单人酒馆循环不会自动触发。")
    effect_by_id = {effect.effect_id: effect for effect in graph.active_effects()}
    if any(
        action.random
        for effect_id in recipe.provenance
        if (effect := effect_by_id.get(effect_id)) is not None
        for action in effect.actions
    ):
        risks.append("图谱包含随机目标/发现，实际收益存在方差。")
    if any("teleport" in factor for factor in recipe.satisfied_factors):
        risks.append("折跃必须存在合法神族落点；固定折跃标签会改变落点分布。")
    if recipe.template_id == "psi-ascension-engine":
        risks.extend((
            "势不可挡必须在终局前实际累积到10座水晶塔；配方只声明载荷要求，不模拟获取路径。",
            "黑暗预兆核心四卡只有人/神/中立三种族；若要同时触发其四种族产出，还需用剩余槽补虫族卡。",
            "持续生产和全体精英化容易撞到单卡200单位上限。",
        ))
    if recipe.template_id == "darkness-carousel":
        risks.extend((
            "买入再出售通常产生净晶体矿消耗，循环次数受经济和商店供给限制。",
            "只有带“具有黑暗容器”标签的卡会收到死亡舰队传播；鲜血猎手默认不满足。",
            "普通与金色死亡舰队的唯一描述不同，混用会破坏同描述唯一抑制并可能形成非预期反馈。",
        ))
    return tuple(risks)


def derive_strategy_guides(
    graph: FactGraph,
    catalog: RecipeCatalog | None = None,
    *,
    limit: int = 10,
    template_id: str | None = None,
    card_name: str | None = None,
    event_name: str | None = None,
) -> tuple[StrategyGuide, ...]:
    """Query explainable strategies from graph-backed recipes.

    Filters are conjunctive. Selection first preserves template diversity, then
    follows catalog order, so a small limit still demonstrates distinct engines.
    """
    if limit <= 0:
        return ()
    catalog = catalog or generate_recipe_catalog(graph, max_recipes=100)
    candidates = []
    for recipe in catalog.recipes:
        if template_id and recipe.template_id != template_id:
            continue
        if card_name and all(slot.card.card.name != card_name for slot in recipe.slots):
            continue
        if event_name and dict(recipe.derived_metrics).get("event") != event_name:
            continue
        candidates.append(recipe)

    # One best item from every template first, then fill by catalog rank.
    ordered: list[RecipeInstance] = []
    seen_templates: set[str] = set()
    for recipe in candidates:
        if recipe.template_id not in seen_templates:
            ordered.append(recipe)
            seen_templates.add(recipe.template_id)
    ordered.extend(recipe for recipe in candidates if recipe not in ordered)

    guides: list[StrategyGuide] = []
    for recipe in ordered[:limit]:
        priorities, core_loop = _template_plan(recipe)
        title_name = {
            "protoss-energy-gathering": "神族能量—集结",
            "event-feedback-engine": "跨卡事件反馈",
            "zerg-swarm-engine": "虫族集群",
            "unit-supply-engine": "单位供需转换",
            "psi-ascension-engine": "灵能精英化流水线",
            "darkness-carousel": "刷牌黑暗值循环",
        }.get(recipe.template_id, recipe.template_id)
        evidence = tuple(recipe.satisfied_factors) + tuple(recipe.provenance)
        guides.append(StrategyGuide(
            recipe_id=recipe.recipe_id,
            template_id=recipe.template_id,
            title=f"{title_name}（结构分 {recipe.score.total:.3f}）",
            expansions=recipe.expansions,
            formation=_slot_line(recipe), priorities=priorities,
            core_loop=core_loop, risks=_risks(graph, recipe),
            evidence=evidence, score=recipe.score.total,
        ))
    return tuple(guides)


def render_strategy_markdown(guides: Iterable[StrategyGuide]) -> str:
    lines = [
        "# 图谱终局策略指南",
        "",
        "> 这是静态终局结构与条件策略，不是随机商店中的保证购买路线。",
    ]
    for index, guide in enumerate(guides, 1):
        lines.extend(("", f"## {index}. {guide.title}", f"- 配方：`{guide.recipe_id}`", f"- 模板：`{guide.template_id}`"))
        if guide.expansions:
            lines.append(f"- 拓展包：{', '.join(guide.expansions)}")
        lines.append("- 站位：" + "；".join(guide.formation))
        lines.append("- 运营优先级：" + " ".join(f"{i + 1}) {text}" for i, text in enumerate(guide.priorities)))
        lines.append("- 核心循环：" + " → ".join(guide.core_loop))
        lines.append("- 风险：" + " ".join(guide.risks))
        lines.append("- 图谱证据：" + "；".join(guide.evidence))
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="从终局依赖图谱输出可解释游戏策略")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA_PATH)
    parser.add_argument("--expansions", nargs="*", default=None, metavar="拓展包")
    parser.add_argument("--max", dest="max_guides", type=int, default=10)
    parser.add_argument("--template", default=None)
    parser.add_argument("--card", default=None)
    parser.add_argument("--event", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.max_guides < 0 or args.max_guides > 100:
        parser.error("--max 必须在 0..100 之间")
    cards, _ = load_cards(args.data)
    graph = build_fact_graph(cards, dataset_id=args.data.stem, strict=False).activate(args.expansions)
    catalog = generate_recipe_catalog(graph, max_recipes=100)
    guides = derive_strategy_guides(
        graph, catalog, limit=args.max_guides, template_id=args.template,
        card_name=args.card, event_name=args.event,
    )
    if args.json:
        print(json.dumps([asdict(guide) for guide in guides], ensure_ascii=False, indent=2))
    else:
        print(render_strategy_markdown(guides), end="")


if __name__ == "__main__":
    main()


__all__ = ["StrategyGuide", "derive_strategy_guides", "render_strategy_markdown"]
