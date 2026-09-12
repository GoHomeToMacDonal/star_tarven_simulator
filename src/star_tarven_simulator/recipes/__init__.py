"""Read-only mechanism graph and terminal recipe catalog API."""

from .effect_ir import Action, CardRef, CardVariantRef, Condition, EffectSpec
from .extractor import extract_card_effects
from .graph import FactGraph, active_subgraph, build_fact_graph
from .generator import validate_recipe
from .templates import RecipeInstance, RecipeSlot, RecipeTemplate

__all__ = [
    "Action", "CardRef", "CardVariantRef", "CatalogDiff", "Condition", "EffectSpec",
    "FactGraph", "RecipeCatalog", "RecipeInstance", "RecipeSlot", "RecipeTemplate",
    "StrategyGuide", "active_subgraph", "build_fact_graph", "derive_strategy_guides",
    "diff_catalogs", "extract_card_effects", "generate_recipe_catalog",
    "render_strategy_markdown", "validate_recipe",
]


def __getattr__(name: str):
    # Keep `python -m ...recipes.catalog` free of runpy's double-import warning.
    if name in {"CatalogDiff", "RecipeCatalog", "diff_catalogs", "generate_recipe_catalog"}:
        from .catalog import CatalogDiff, RecipeCatalog, diff_catalogs, generate_recipe_catalog
        return {
            "CatalogDiff": CatalogDiff,
            "RecipeCatalog": RecipeCatalog,
            "diff_catalogs": diff_catalogs,
            "generate_recipe_catalog": generate_recipe_catalog,
        }[name]
    if name in {"StrategyGuide", "derive_strategy_guides", "render_strategy_markdown"}:
        from .strategy import StrategyGuide, derive_strategy_guides, render_strategy_markdown
        return {
            "StrategyGuide": StrategyGuide,
            "derive_strategy_guides": derive_strategy_guides,
            "render_strategy_markdown": render_strategy_markdown,
        }[name]
    raise AttributeError(name)
