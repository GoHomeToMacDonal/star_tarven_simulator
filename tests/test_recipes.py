from __future__ import annotations

from collections import Counter

import pytest

from star_tarven_simulator.loader import load_cards
from star_tarven_simulator.recipes.catalog import generate_recipe_catalog
from star_tarven_simulator.recipes.effect_ir import SemanticCoverageError
from star_tarven_simulator.recipes.extractor import extract_effects
from star_tarven_simulator.recipes.generator import MAX_CATALOG_RECIPES, validate_recipe
from star_tarven_simulator.recipes.graph import build_fact_graph
from star_tarven_simulator.recipes.strategy import derive_strategy_guides, render_strategy_markdown
from star_tarven_simulator.simulator.event import Event


@pytest.fixture(scope="module")
def recipe_state():
    cards, execution = load_cards()
    master = build_fact_graph(cards, dataset_id="v20260826", strict=False)
    active = master.activate()
    return cards, execution, master, active


def test_every_executable_handler_is_graph_represented(recipe_state):
    cards, execution, master, _ = recipe_state
    report = master.extraction_report

    assert execution.rate == 1.0
    assert report.handler_coverage_rate == 1.0
    assert report.actionable_rate == 1.0
    assert report.opaque == 0
    assert report.represented_handlers == report.executable_descriptions
    # Semantically identical duplicate description lines intentionally share one
    # stable EffectSpec node; handler coverage is occurrence-based in the report.
    assert len(master.effects) <= report.represented_handlers
    assert {effect.extraction for effect in master.effects} <= {"automatic", "template", "override", "partial"}
    assert all(
        effect.events or any(action.kind == "modify_global" for action in effect.actions)
        for effect in master.effects
    )
    assert all(effect.actions for effect in master.effects)

    with pytest.raises(SemanticCoverageError):
        extract_effects(cards, strict=True)


def test_complete_runtime_event_vocabulary_and_listener_audit(recipe_state):
    _, _, master, _ = recipe_state
    expected = {event.value for event in Event}
    declared = {name for name, _, _ in master.event_coverage()}
    relation_events = {
        relation.target.removeprefix("event:")
        for relation in master.relations
        if relation.source == "schema:runtime_events" and relation.kind == "HAS_EVENT"
    }

    assert declared == expected == relation_events
    # Every current runtime Event has at least one handler in the complete card set,
    # including externally driven round_win/other-player events.
    assert all(listener_count > 0 for _, listener_count, _ in master.event_coverage())
    assert any(full < listeners for _, listeners, full in master.event_coverage())


def test_graph_is_stable_and_contains_typed_relations(recipe_state):
    cards, _, master, _ = recipe_state
    rebuilt = build_fact_graph(reversed(cards), dataset_id="v20260826", strict=False)
    assert rebuilt.fingerprint == master.fingerprint

    kinds = {relation.kind for relation in master.relations}
    assert {
        "STARTS_WITH", "LISTENS_TO", "PRODUCES", "REQUIRES", "EMITS",
        "TARGETS", "HAS_TAG", "AVAILABLE_FROM", "EXCLUSIVE_WITH",
    } <= kinds
    master.assert_consistent()


def test_gold_variant_models_three_merged_payloads(recipe_state):
    _, _, master, _ = recipe_state
    normal = next(
        variant for variant in master.variants
        if variant.ref.card.name == "万叉奔腾" and variant.ref.variant == "normal"
    )
    gold = next(
        variant for variant in master.variants
        if variant.ref.card.name == "万叉奔腾" and variant.ref.variant == "gold"
    )
    assert dict(gold.units) == {unit: count * 3 for unit, count in normal.units}


def test_gathering_override_preserves_wrapper_threshold(recipe_state):
    _, _, master, _ = recipe_state
    effects = master.effects_for("净化者军团")
    gathering = [effect for effect in effects if effect.mechanism == "gathering"]
    assert gathering
    assert all(master.gathering_cost(effect) == 13 for effect in gathering)
    assert all(any(action.kind == "move_units" for action in effect.actions) for effect in gathering)


def test_catalog_has_global_100_limit_and_mechanism_diversity(recipe_state):
    _, _, _, active = recipe_state
    catalog = generate_recipe_catalog(
        active, max_slots=99, limit_per_template=10_000, max_recipes=10_000
    )

    assert len(catalog.recipes) == MAX_CATALOG_RECIPES == 100
    counts = Counter(recipe.template_id for recipe in catalog.recipes)
    assert set(counts) == {
        "protoss-energy-gathering", "event-feedback-engine",
        "zerg-swarm-engine", "unit-supply-engine",
        "psi-ascension-engine", "darkness-carousel",
    }
    assert all(1 < len(recipe.slots) <= 7 for recipe in catalog.recipes)
    assert all(not recipe.unsatisfied_factors for recipe in catalog.recipes)
    assert all(len({slot.index for slot in recipe.slots}) == len(recipe.slots) for recipe in catalog.recipes)
    active_ids = active.active_variant_ids
    assert all(
        slot.card.canonical_id in active_ids
        for recipe in catalog.recipes
        for slot in recipe.slots
    )


def test_catalog_is_deterministic_and_max_parameter_is_global(recipe_state):
    _, _, _, active = recipe_state
    first = generate_recipe_catalog(active, max_recipes=17)
    second = generate_recipe_catalog(active, max_recipes=17)
    assert len(first.recipes) == 17
    assert [recipe.recipe_id for recipe in first.recipes] == [
        recipe.recipe_id for recipe in second.recipes
    ]
    assert first.to_json() == second.to_json()


def test_strategy_guides_are_graph_derived_and_queryable(recipe_state):
    _, _, _, active = recipe_state
    catalog = generate_recipe_catalog(active, max_recipes=100)
    guides = derive_strategy_guides(active, catalog, limit=6)

    assert len(guides) == 6
    assert {guide.template_id for guide in guides} == {
        "protoss-energy-gathering", "event-feedback-engine",
        "zerg-swarm-engine", "unit-supply-engine",
        "psi-ascension-engine", "darkness-carousel",
    }
    assert all(guide.formation and guide.core_loop and guide.evidence for guide in guides)
    assert "不保证随机商店" in render_strategy_markdown(guides)

    teleport = derive_strategy_guides(active, catalog, limit=5, event_name="any_card_teleport")
    assert teleport
    assert all(dict(next(
        recipe.derived_metrics for recipe in catalog.recipes
        if recipe.recipe_id == guide.recipe_id
    )).get("event") == "any_card_teleport" for guide in teleport)


def test_catalog_excludes_consumed_deployment_cards_and_revalidates_conditions(recipe_state):
    _, _, _, active = recipe_state
    catalog = generate_recipe_catalog(
        active, max_slots=7, limit_per_template=10_000, max_recipes=100
    )
    effects = {effect.effect_id: effect for effect in active.active_effects()}

    for recipe in catalog.recipes:
        valid, reasons = validate_recipe(active, recipe)
        assert valid, (recipe.recipe_id, reasons)
        for slot in recipe.slots:
            variant = active.variant_for(slot.card)
            assert variant is not None and variant.level > 0
            assert "辅助卡" not in variant.sources
            assert not any(
                "deployment" in effect.events
                for effect in active.effects_for(slot.card.card.name, slot.card.variant)
            )
        if recipe.template_id == "event-feedback-engine":
            assert all(
                not any(condition.hard for condition in effects[effect_id].conditions)
                for effect_id in recipe.provenance
            )


def test_event_chains_are_resolved_without_card_name_tables(recipe_state):
    _, _, _, active = recipe_state
    chains = active.event_chains()
    assert chains
    assert all(event in active.event_names() for event, _, _ in chains)
    assert all(event in listener.events for event, _, listener in chains)
    assert all(
        any(action.emits_event == event for action in emitter.actions)
        for event, emitter, _ in chains
    )
