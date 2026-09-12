"""CLI coverage report for the declarative recipe-extraction layer."""

from __future__ import annotations

import argparse
from pathlib import Path

from star_tarven_simulator.loader import DEFAULT_DATA_PATH, load_cards

from .extractor import extract_effects
from .graph import build_fact_graph


def main() -> None:
    parser = argparse.ArgumentParser(description="终局配方库语义抽取覆盖率报告")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA_PATH, help="卡牌 JSON 路径")
    parser.add_argument("--strict", action="store_true", help="存在 partial/opaque 执行效果时以非零状态退出")
    parser.add_argument("--events", action="store_true", help="展示全部运行时事件的监听/完整语义矩阵")
    parser.add_argument("--issues", type=int, default=30, help="展示的覆盖缺口数量")
    args = parser.parse_args()

    cards, execution_coverage = load_cards(args.data)
    _, semantic = extract_effects(cards, dataset_id=args.data.stem, strict=False)
    print(f"卡牌: {len(cards)}; 执行{execution_coverage.summary().splitlines()[0]}")
    print(semantic.summary())
    if args.events:
        graph = build_fact_graph(cards, dataset_id=args.data.stem, strict=False)
        print("事件覆盖矩阵 (listeners/full):")
        for event_name, listeners, full in graph.event_coverage():
            print(f"  {event_name}: {listeners}/{full}")
    if semantic.issues:
        print("部分语义（已保留事件绑定与可查询动作）:")
        for issue in semantic.issues[:args.issues]:
            print(f"  {issue.card.card.name}[{issue.card.variant}] #{issue.ordinal}: {issue.normalized_text} ({issue.reason})")
    if args.strict and semantic.has_gaps:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
