#!/usr/bin/env python3
"""Audit (and optionally repair) the ``price`` field of a curated card snapshot.

A card's price is the sum of its units' values, so a snapshot is an
overdetermined linear system::

    for every card c:   sum_u  units[c][u] * value[u]  =  price[c]

Two tables claim to hold the unit values and they disagree on a dozen units:

``constants/unit_prices.py``
    Reverse-engineered from the value the game *displays* on a card.
``UnitData.xml`` + ``data/maps/card_overrides.json``
    The map's raw Minerals+Vespene cost, which ``card_extractor.py`` sums into
    the ``price`` field of the extracted snapshot.

Only a hand-transcribed snapshot can arbitrate: its prices were read off the
game, independently of either table.  Scoring against an *extracted* snapshot
proves nothing, because its prices were computed from the map costs in the first
place.  So this script brute-forces every combination of the disputed values and
keeps the assignment that balances the most cards; whatever still fails to
balance after that is a transcription error in the price column.

Values are exact :class:`~fractions.Fraction` arithmetic, so "does not balance"
never means floating-point noise.

Usage::

    uv run python scripts/audit_card_prices.py
    uv run python scripts/audit_card_prices.py --fix
    uv run python scripts/audit_card_prices.py --cards data/v4.6.1.7_card.json --no-arbitrate
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "src"))

# Hand-transcribed snapshot: the only independent witness to displayed prices.
DEFAULT_CARDS = REPO_ROOT / "data" / "v20260826_card.json"

# Extra candidates the two tables do not offer, with the reason they are
# plausible.  Elite variants in constants/unit_prices.py sit 50-150 above their
# base unit (劫掠者 125/225, 歌利亚 200/300, 异龙 150/300), yet 维京战机(精英) is
# listed at the base unit's own 225 - the value of an untested assumption.
EXTRA_CANDIDATES: dict[str, list[Fraction]] = {
    "维京战机(精英)": [Fraction(275)],
}


def F(value: Any) -> Fraction:
    return Fraction(str(value))


def s(value: Fraction) -> str:
    return str(value) if value.denominator == 1 else f"{value}(~{float(value):.4g})"


def load_cards(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def map_unit_values() -> dict[str, Fraction]:
    """Unit name -> Minerals+Vespene, resolved the way card_extractor does."""
    import paths
    from star_tavern_cards import (
        load_localization,
        load_overrides,
        load_unit_info,
        merge_unit_info,
        parse_unit_catalog,
        resolved_costs,
    )

    extracted = paths.DEFAULT_EXTRACTED
    localization = load_localization(extracted / "text" / "zhCN.SC2Data" / "LocalizedData")
    catalog = parse_unit_catalog(extracted / "text" / "Base.SC2Data" / "GameData" / "UnitData.xml")
    overrides = merge_unit_info(
        load_overrides(paths.DEFAULT_OVERRIDES), load_unit_info(paths.DEFAULT_UNIT_INFO)
    )
    forced = {u: v["value"] for u, v in overrides["units"].items() if v.get("value") is not None}
    names = {u: v["name"] for u, v in overrides["units"].items() if v.get("name")}

    # One display name can map to several unit ids (VikingFighter/VikingAssault);
    # keep it only when they agree, otherwise the name pins nothing down.
    seen: dict[str, set[Fraction]] = {}
    for unit_id in set(catalog) | set(names):
        name = localization.get(f"Unit/Name/{unit_id}") or names.get(unit_id)
        if not name:
            continue
        if unit_id in forced:
            value = F(forced[unit_id])
        else:
            costs = resolved_costs(unit_id, catalog)
            if costs is None:
                continue
            value = F(costs.get("Minerals", 0.0)) + F(costs.get("Vespene", 0.0))
        seen.setdefault(name, set()).add(value)
    return {name: next(iter(values)) for name, values in seen.items() if len(values) == 1}


def unbalanced(rows: list[dict[str, Any]], values: dict[str, Fraction]) -> list[str]:
    """Names of cards whose price differs from the sum of their units."""
    out = []
    for card in rows:
        total = Fraction(0)
        for unit, count in (card["units"] or {}).items():
            if unit not in values:
                total = None
                break
            total += values[unit] * count
        if total is not None and total != F(card["price"]):
            out.append(card["name"])
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--cards", type=Path, default=DEFAULT_CARDS, help="Card JSON to audit")
    parser.add_argument(
        "--no-arbitrate",
        action="store_true",
        help="Skip the joint search and just verify against constants/unit_prices.py",
    )
    parser.add_argument("--fix", action="store_true", help="Rewrite unbalanced prices in place")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from star_tarven_simulator.constants.unit_prices import UNIT_PRICES

    cards = load_cards(args.cards)
    rows = [c for c in cards if c.get("price") is not None]
    used = {u for c in cards for u in (c["units"] or {})}
    constants = {u: F(p) for u, p in UNIT_PRICES.items()}

    print(f"{args.cards.name}: {len(cards)} 张卡，其中 {len(rows)} 张有价格，{len(used)} 种单位")

    values = dict(constants)
    if not args.no_arbitrate:
        from_map = map_unit_values()
        choices: dict[str, list[Fraction]] = {}
        for unit in sorted(used):
            options: list[Fraction] = []
            if unit in constants:
                options.append(constants[unit])
            if unit in from_map and from_map[unit] not in options:
                options.append(from_map[unit])
            for extra in EXTRA_CANDIDATES.get(unit, []):
                if extra not in options:
                    options.append(extra)
            if len(options) > 1 or unit not in constants:
                if not options:
                    print(f"  警告: {unit} 两张表都没有值，相关卡牌无法校验")
                    continue
                choices[unit] = options

        print(f"\n有争议 / 缺失的单位 ({len(choices)}):")
        for unit, options in choices.items():
            current = s(constants[unit]) if unit in constants else "缺失"
            print(f"  {unit:<18} 常量={current:<8} 候选={[s(v) for v in options]}")

        keys = list(choices)
        combos = list(itertools.product(*(choices[k] for k in keys)))
        scored = []
        for combo in combos:
            trial = dict(constants)
            trial.update(dict(zip(keys, combo)))
            scored.append((len(unbalanced(rows, trial)), combo))
        scored.sort(key=lambda item: item[0])
        best_score = scored[0][0]
        winners = {
            tuple((k, v) for k, v in zip(keys, combo) if constants.get(k) != v)
            for count, combo in scored
            if count == best_score
        }

        print(f"\n穷举 {len(combos)} 种组合，最优可让 {len(rows) - best_score}/{len(rows)} 张卡平账")
        if len(winners) > 1:
            print(f"  ! {len(winners)} 组赋值并列最优，需人工裁决:")
            for winner in sorted(winners, key=lambda w: [str(x) for x in w]):
                print(f"    {[(k, s(v)) for k, v in winner]}")
        best = next(iter(winners))
        print("  采用的单位价值修正:")
        for unit, value in sorted(best):
            current = s(constants[unit]) if unit in constants else "缺失"
            print(f"    {unit:<18} {current} -> {s(value)}")
        values.update(dict(best))

    print("\n价格与单位之和不符的卡:")
    fixes: list[tuple[dict[str, Any], Fraction, Fraction]] = []
    for card in rows:
        total = Fraction(0)
        blocked = False
        for unit, count in (card["units"] or {}).items():
            if unit not in values:
                blocked = True
                break
            total += values[unit] * count
        if blocked or total == F(card["price"]):
            continue
        fixes.append((card, F(card["price"]), total))
        print(f"  {card['name']:<12} price={s(F(card['price'])):<10} 单位之和={s(total):<10} "
              f"units={json.dumps(card['units'], ensure_ascii=False)}")
    print(f"  共 {len(fixes)} 张")

    if args.fix and fixes:
        for card, _stored, total in fixes:
            card["price"] = float(total)
        args.cards.write_text(
            json.dumps(cards, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"\n已把 {len(fixes)} 张卡的 price 改为单位之和，写回 {args.cards}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
