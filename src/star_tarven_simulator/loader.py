"""数据加载：读取 v260822 卡牌 JSON、解析效果、构建引擎，并给出覆盖率报告。"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

from star_tarven_simulator.parsing.parser import parse_card
from star_tarven_simulator.simulator.card import Card, CardPool
from star_tarven_simulator.simulator.card_engine import CardEngine
from star_tarven_simulator.simulator.game import Game

DEFAULT_DATA_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "v260822_card.json"
)


@dataclass
class Coverage:
    """效果解析覆盖率统计。"""

    total_lines: int = 0
    handled_lines: int = 0
    unhandled: Counter = field(default_factory=Counter)  # 归一化文本 -> 出现次数
    unhandled_by_card: Dict[str, List[str]] = field(default_factory=dict)

    @property
    def rate(self) -> float:
        return self.handled_lines / self.total_lines if self.total_lines else 1.0

    def summary(self, top: int = 40) -> str:
        lines = [
            f"覆盖率: {self.handled_lines}/{self.total_lines} = {self.rate:.1%}",
            f"未处理的不同描述条目数: {len(self.unhandled)}",
            "",
            f"最常见的未处理描述 (top {top}):",
        ]
        for text, cnt in self.unhandled.most_common(top):
            lines.append(f"  x{cnt:<3} {text}")
        return "\n".join(lines)


def load_cards(path=DEFAULT_DATA_PATH) -> Tuple[List[Card], Coverage]:
    """加载并解析所有卡牌，返回 (cards, coverage)。"""
    from star_tarven_simulator.cards import is_passive, resolve
    from star_tarven_simulator.parsing.parser import prepared_lines
    from star_tarven_simulator.parsing.text import extract_colors, register_units

    data = json.loads(Path(path).read_text(encoding="utf-8"))

    # 第一遍：构建 Card，并把数据中出现的全部单位名注册进单位词典
    raw_cards = [Card.from_json(cj) for cj in data]
    unit_names = set()
    for card in raw_cards:
        unit_names.update(card.units.keys())
    register_units(unit_names)

    cards: List[Card] = []
    coverage = Coverage()

    # 第二遍：解析效果并统计覆盖率
    for card in raw_cards:
        parse_card(card)

        for desc_list in (card.description, card.gold_description):
            for norm, raw in prepared_lines(desc_list):
                if not norm:
                    continue
                coverage.total_lines += 1
                if is_passive(norm) or resolve(norm, extract_colors(raw)) is not None:
                    coverage.handled_lines += 1
                else:
                    coverage.unhandled[norm] += 1
                    coverage.unhandled_by_card.setdefault(card.name, [])
                    if norm not in coverage.unhandled_by_card[card.name]:
                        coverage.unhandled_by_card[card.name].append(norm)

        cards.append(card)

    return cards, coverage


def build_game(
    cards: List[Card],
    user_count: int = 8,
    max_round: int = 20,
    expansions=None,
    random_pick: bool = False,
) -> Game:
    """由卡牌列表构建一局对战。

    :param expansions: 本局开启的拓展包名称列表（0~2 个，遵守独占规则）。默认 ``None``
        表示只启用核心种族与基础内容。非法选择会抛 :class:`ValueError`。
    :param random_pick: 为 ``True`` 时忽略 ``expansions``，随机挑选一组合法拓展包。
    """
    from star_tarven_simulator.expansions import BASE_EXTRA_SOURCES, filter_cards
    from star_tarven_simulator.expansions import random_expansions as _pick

    if random_pick:
        expansions = _pick()

    pool_cards = filter_cards(cards, expansions)
    # 辅助卡 / 特殊卡：保留在卡池对象内（可查询、可被引擎使用），但不进入可抽取的等级桶。
    _no_draw = {
        c.uuid
        for c in pool_cards
        if set(getattr(c, "source", []) or []) & set(BASE_EXTRA_SOURCES)
    }
    pool = CardPool(pool_cards, no_draw_uuids=_no_draw)
    engine = CardEngine(pool_cards)
    game = Game(pool, engine, user_count=user_count, max_round=max_round)
    game.enabled_expansions = list(expansions) if expansions else []
    return game


def main() -> None:
    """打印覆盖率报告，并可选地演示拓展包过滤。

    用法::

        uv run python -m star_tarven_simulator.loader
        uv run python -m star_tarven_simulator.loader --expansions 作战计划 比特狂潮
        uv run python -m star_tarven_simulator.loader --random
    """
    import argparse

    from star_tarven_simulator.expansions import (
        EXPANSION_PACKS,
        filter_cards,
        random_expansions,
        validate_selection,
    )

    parser = argparse.ArgumentParser(description="星际酒馆模拟器加载器 / 覆盖率报告")
    parser.add_argument(
        "--expansions",
        nargs="*",
        default=None,
        metavar="拓展包",
        help=f"开启的拓展包（至多 2 个）。可选：{', '.join(EXPANSION_PACKS)}",
    )
    parser.add_argument(
        "--random",
        action="store_true",
        help="随机挑选一组合法拓展包（忽略 --expansions）",
    )
    args = parser.parse_args()

    cards, coverage = load_cards()
    print(f"已加载 {len(cards)} 张卡牌")
    print(coverage.summary())

    expansions = None
    if args.random:
        expansions = random_expansions()
    elif args.expansions is not None:
        expansions = validate_selection(args.expansions)

    if expansions is not None:
        pool_cards = filter_cards(cards, expansions)
        label = "、".join(expansions) if expansions else "（仅核心）"
        print()
        print(f"启用拓展包: {label}")
        print(f"过滤后卡池: {len(pool_cards)}/{len(cards)} 张")


if __name__ == "__main__":
    main()
