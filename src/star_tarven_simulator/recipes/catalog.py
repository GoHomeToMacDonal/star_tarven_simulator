"""Versioned recipe catalogs, JSON serialization, diffing, and CLI entry point."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import argparse
import json
from pathlib import Path
from typing import Iterable

from star_tarven_simulator.loader import DEFAULT_DATA_PATH, load_cards

from .generator import generate_recipe_instances
from .graph import FactGraph, build_fact_graph
from .templates import DEFAULT_TEMPLATES, RecipeInstance, RecipeTemplate


SCHEMA_VERSION = 2
EXTRACTOR_VERSION = 2


@dataclass(frozen=True, slots=True)
class RecipeCatalog:
    schema_version: int
    extractor_version: int
    dataset_id: str | None
    expansions: tuple[str, ...]
    graph_fingerprint: str
    recipes: tuple[RecipeInstance, ...]

    def to_dict(self) -> dict:
        return asdict(self)

    def to_json(self, *, indent: int | None = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=indent)


@dataclass(frozen=True, slots=True)
class CatalogDiff:
    added_recipe_ids: tuple[str, ...]
    removed_recipe_ids: tuple[str, ...]
    score_changes: tuple[tuple[str, float, float], ...]


def generate_recipe_catalog(
    graph: FactGraph, *, templates: Iterable[RecipeTemplate] | None = None,
    max_slots: int = 7, limit_per_template: int = 100, max_recipes: int = 100,
) -> RecipeCatalog:
    """Create a catalog with a hard global upper bound of 100 recipes."""
    recipes = generate_recipe_instances(
        graph, templates=templates or DEFAULT_TEMPLATES, max_slots=max_slots,
        limit_per_template=limit_per_template, max_recipes=max_recipes,
    )
    return RecipeCatalog(SCHEMA_VERSION, EXTRACTOR_VERSION, graph.dataset_id, graph.expansions,
                         graph.fingerprint, recipes)


def diff_catalogs(old: RecipeCatalog, new: RecipeCatalog) -> CatalogDiff:
    old_by_id = {recipe.recipe_id: recipe for recipe in old.recipes}
    new_by_id = {recipe.recipe_id: recipe for recipe in new.recipes}
    score_changes = tuple(sorted(
        (identifier, old_by_id[identifier].score.total, new_by_id[identifier].score.total)
        for identifier in old_by_id.keys() & new_by_id.keys()
        if old_by_id[identifier].score.total != new_by_id[identifier].score.total
    ))
    return CatalogDiff(tuple(sorted(new_by_id.keys() - old_by_id.keys())),
                       tuple(sorted(old_by_id.keys() - new_by_id.keys())), score_changes)


def main() -> None:
    parser = argparse.ArgumentParser(description="生成终局配方 JSON 目录")
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA_PATH)
    parser.add_argument("--expansions", nargs="*", default=None, metavar="拓展包")
    parser.add_argument("--output", type=Path, default=None, help="输出路径；省略时写到标准输出")
    parser.add_argument("--max", dest="max_recipes", type=int, default=100,
                        help="全局最多输出多少条配方（硬上限100）")
    parser.add_argument("--limit-per-template", type=int, default=100,
                        help="每种机制候选上限（最终仍受 --max 全局限制）")
    parser.add_argument("--strict", action="store_true", help="拒绝含 partial/opaque 执行效果的全图")
    args = parser.parse_args()
    if args.max_recipes < 0 or args.max_recipes > 100:
        parser.error("--max 必须在 0..100 之间")
    cards, _ = load_cards(args.data)
    graph = build_fact_graph(cards, dataset_id=args.data.stem, strict=args.strict).activate(args.expansions)
    catalog = generate_recipe_catalog(
        graph, limit_per_template=args.limit_per_template, max_recipes=args.max_recipes,
    )
    result = catalog.to_json()
    if args.output:
        args.output.write_text(result + "\n", encoding="utf-8")
        print(f"已写入 {args.output}: {len(catalog.recipes)} 个配方")
    else:
        print(result)


if __name__ == "__main__":
    main()


__all__ = ["CatalogDiff", "RecipeCatalog", "diff_catalogs", "generate_recipe_catalog"]
