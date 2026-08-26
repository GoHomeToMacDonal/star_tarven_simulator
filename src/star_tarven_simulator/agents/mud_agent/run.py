"""泥巴流 Agent 命令行入口 + 拓展包扫描。

单组运行::

    uv run python -m star_tarven_simulator.agents.mud_agent.run --hero 陆战队员 --seed 0
    uv run python -m star_tarven_simulator.agents.mud_agent.run --hero 工蜂 --games 20 --samples 8
    uv run python -m star_tarven_simulator.agents.mud_agent.run --hero 副官 --no-search   # 纯骨架
    uv run python -m star_tarven_simulator.agents.mud_agent.run --hero 陆战队员 --expansions 作战计划 比特狂潮

拓展包扫描（全部合法组合 × 3 英雄）::

    uv run python -m star_tarven_simulator.agents.mud_agent.run --sweep --games 50 --samples 16
    uv run python -m star_tarven_simulator.agents.mud_agent.run --sweep --no-search --games 50   # 骨架快扫
"""

from __future__ import annotations

import argparse
import random
import statistics

from star_tarven_simulator.agents.mud_agent.policy import play_game
from star_tarven_simulator.agents.mud_agent.reward import (
    FINAL_ROUND,
    RewardWeights,
)
from star_tarven_simulator.expansions import all_valid_selections
from star_tarven_simulator.loader import build_game, load_cards

SUPPORTED_HEROES = ("陆战队员", "工蜂", "副官", "default")
SWEEP_HEROES = ("陆战队员", "工蜂", "副官")


def run_one(
    cards,
    hero: str,
    seed: int,
    *,
    search: bool,
    samples: int,
    trace: bool,
    expansions=None,
    weights: RewardWeights | None = None,
):
    rng = random.Random(seed)
    game = build_game(
        cards, user_count=1, heroes=[hero], rng=rng, expansions=expansions
    )
    trackers = play_game(
        game,
        0,
        search_enabled=search,
        samples=samples,
        weights=weights or RewardWeights(),
        trace=trace,
        final_round=FINAL_ROUND,
    )
    t = game.tarvens[0]
    return {
        "level": t.level,
        "triples": trackers.triple_count,
        "reach_l5": trackers.round_reached_l5,
        "equiv_power": t.total_equivalent_power(),
    }


def _agg(results):
    levels = [r["level"] for r in results]
    triples = [r["triples"] for r in results]
    reached = [r["reach_l5"] for r in results if r["reach_l5"] <= FINAL_ROUND]
    n = len(results)
    return {
        "n": n,
        "avg_level": statistics.mean(levels) if levels else 0.0,
        "avg_triples": statistics.mean(triples) if triples else 0.0,
        "reach_l5_rate": (len(reached) / n) if n else 0.0,
        "avg_reach_round": statistics.mean(reached) if reached else None,
        "reach_l5_count": len(reached),
    }


def _run_config(cards, hero, expansions, *, games, seed0, search, samples):
    results = []
    for g in range(games):
        res = run_one(
            cards, hero, seed0 + g,
            search=search, samples=samples, trace=False,
            expansions=expansions,
        )
        results.append(res)
    return _agg(results)


def _fmt_exp(expansions) -> str:
    return "核心(无拓展)" if not expansions else "+".join(expansions)


def sweep(cards, *, games, seed0, search, samples, csv_path=None):
    """全部合法拓展包组合（0/1/2 个）× 3 英雄，报告平均三连 & 平均 5 本回合（分开看）。"""
    import sys

    # 组合：空（只核心）+ 1 个 + 2 个合法组合。
    combos = [[]] + all_valid_selections(1, 2)
    print(
        f"扫描 {len(combos)} 个拓展包组合 × {len(SWEEP_HEROES)} 英雄，"
        f"每格 {games} 局，samples={samples if search else 'N/A(骨架)'}\n",
        flush=True,
    )
    header = f"{'拓展包组合':<22}{'英雄':<8}{'达5本%':>8}{'平均5本回合':>12}{'平均三连':>10}{'平均本级':>10}"
    print(header, flush=True)
    print("-" * len(header), flush=True)

    csv_f = open(csv_path, "w", encoding="utf-8") if csv_path else None
    if csv_f:
        csv_f.write("expansions,hero,games,reach_l5_rate,avg_reach_round,avg_triples,avg_level\n")

    rows = []
    for expansions in combos:
        for hero in SWEEP_HEROES:
            agg = _run_config(
                cards, hero, expansions,
                games=games, seed0=seed0, search=search, samples=samples,
            )
            rows.append((expansions, hero, agg))
            reach_round = (
                f"{agg['avg_reach_round']:.2f}" if agg["avg_reach_round"] is not None else "—"
            )
            print(
                f"{_fmt_exp(expansions):<22}{hero:<8}"
                f"{agg['reach_l5_rate']:>7.0%} {reach_round:>11} "
                f"{agg['avg_triples']:>9.2f} {agg['avg_level']:>9.2f}",
                flush=True,
            )
            if csv_f:
                arr = agg["avg_reach_round"]
                csv_f.write(
                    f"{_fmt_exp(expansions)},{hero},{agg['n']},"
                    f"{agg['reach_l5_rate']:.4f},{arr if arr is not None else ''},"
                    f"{agg['avg_triples']:.4f},{agg['avg_level']:.4f}\n"
                )
                csv_f.flush()
            sys.stdout.flush()
    if csv_f:
        csv_f.close()
    _sweep_summary(rows)


def _sweep_summary(rows):
    print("\n=== 每英雄跨拓展包汇总 ===")
    for hero in SWEEP_HEROES:
        hrows = [(e, a) for (e, h, a) in rows if h == hero]
        best_triples = max(hrows, key=lambda ea: ea[1]["avg_triples"])
        # 最早 5 本：只在达成率高(>=0.9)的组合里比，避免样本少偏差。
        elig = [ea for ea in hrows if ea[1]["avg_reach_round"] is not None and ea[1]["reach_l5_rate"] >= 0.9]
        best_round = min(elig, key=lambda ea: ea[1]["avg_reach_round"]) if elig else None
        print(f"\n[{hero}]")
        print(
            f"  三连最多组合: {_fmt_exp(best_triples[0])} "
            f"(平均三连 {best_triples[1]['avg_triples']:.2f})"
        )
        if best_round is not None:
            print(
                f"  最早5本组合(达成率≥90%): {_fmt_exp(best_round[0])} "
                f"(平均5本回合 {best_round[1]['avg_reach_round']:.2f}, "
                f"达成率 {best_round[1]['reach_l5_rate']:.0%})"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="泥巴流蒙特卡洛 Agent")
    parser.add_argument("--hero", default="陆战队员", choices=SUPPORTED_HEROES)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--games", type=int, default=1, help="连续跑多少局并汇总统计")
    parser.add_argument("--samples", type=int, default=16, help="每个关键分叉的采样数")
    parser.add_argument("--no-search", action="store_true", help="禁用蒙特卡洛，只用骨架")
    parser.add_argument("--trace", action="store_true", help="打印逐回合 trace")
    parser.add_argument(
        "--expansions", nargs="*", default=None, help="本局开启的拓展包（0~2 个）"
    )
    parser.add_argument(
        "--sweep", action="store_true",
        help="扫描全部合法拓展包组合 × 3 英雄（忽略 --hero/--expansions）",
    )
    parser.add_argument("--csv", default=None, help="扫描结果写入 CSV 路径（逐行 flush）")
    # 权重调参（供超参数扫描）。
    parser.add_argument("--w-triple", type=float, default=None)
    parser.add_argument("--w-level", type=float, default=None)
    parser.add_argument("--w-pair", type=float, default=None)
    args = parser.parse_args()

    cards, _ = load_cards()
    search = not args.no_search

    weights = RewardWeights()
    if any(v is not None for v in (args.w_triple, args.w_level, args.w_pair)):
        weights = RewardWeights(
            triple_weight=args.w_triple if args.w_triple is not None else weights.triple_weight,
            level_weight=args.w_level if args.w_level is not None else weights.level_weight,
            pair_progress_weight=args.w_pair if args.w_pair is not None else weights.pair_progress_weight,
        )

    if args.sweep:
        sweep(
            cards, games=args.games, seed0=args.seed,
            search=search, samples=args.samples, csv_path=args.csv,
        )
        return

    results = []
    for g in range(args.games):
        seed = args.seed + g
        trace = args.trace and args.games == 1
        res = run_one(
            cards, args.hero, seed,
            search=search, samples=args.samples, trace=trace,
            expansions=args.expansions, weights=weights,
        )
        results.append(res)
        if args.games == 1:
            print(
                f"\n[{args.hero} seed={seed} 拓展={_fmt_exp(args.expansions)}] "
                f"终局等级={res['level']} 三连={res['triples']} "
                f"达成5本回合={'未达成' if res['reach_l5'] > FINAL_ROUND else res['reach_l5']} "
                f"R{FINAL_ROUND}等效战力={res['equiv_power']:.1f}"
            )

    if args.games > 1:
        _summarize(args.hero, args.expansions, results)


def _summarize(hero: str, expansions, results) -> None:
    agg = _agg(results)
    n = agg["n"]
    print(f"\n=== {hero} @ {_fmt_exp(expansions)} 汇总（{n} 局）===")
    print(f"平均终局等级: {agg['avg_level']:.2f}")
    print(f"平均三连次数: {agg['avg_triples']:.2f}")
    print(f"达成 5 本比例: {agg['reach_l5_count']}/{n} = {agg['reach_l5_rate']:.0%}")
    if agg["avg_reach_round"] is not None:
        print(f"达成 5 本平均回合: {agg['avg_reach_round']:.2f}")


if __name__ == "__main__":
    main()
