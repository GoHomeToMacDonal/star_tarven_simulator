"""Deterministic graph-driven terminal recipe generation.

The generator keeps the specialised energy solver and adds reusable builders for
any declared event chain, race threshold and unit requirement.  Catalog selection
is globally bounded to 100 recipes and round-robins templates for diversity.
"""

from __future__ import annotations

from hashlib import sha256
import itertools
import json
from typing import Iterable

from .effect_ir import EffectSpec
from .graph import CardVariantFact, FactGraph
from .scoring import score_recipe
from .templates import DEFAULT_TEMPLATES, RecipeInstance, RecipeSlot, RecipeTemplate, ScoreBreakdown

MAX_CATALOG_RECIPES = 100


def _variant_map(graph: FactGraph) -> dict[str, CardVariantFact]:
    return {variant.ref.canonical_id: variant for variant in graph.active_variants()}


def _emitted_events(effect: EffectSpec) -> tuple[str, ...]:
    return tuple(sorted({action.emits_event for action in effect.actions if action.emits_event}))


def _recipe_id(
    template_id: str, slots: tuple[RecipeSlot, ...], edges: tuple[tuple[int, int], ...]
) -> str:
    # Expansion selection is availability provenance, not terminal identity. An
    # unrelated enabled pack must not churn stable recipe IDs.
    body = {
        "schema": 2,
        "template": template_id,
        "slots": [
            {
                "index": slot.index,
                "card": slot.card.canonical_id,
                "upgrades": tuple(sorted(slot.upgrades)),
            }
            for slot in sorted(slots, key=lambda item: item.index)
        ],
        "edges": tuple(sorted(tuple(sorted(edge)) for edge in edges)),
    }
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + sha256(encoded.encode()).hexdigest()


def _is_terminal_variant(graph: FactGraph, variant: CardVariantFact) -> bool:
    """Whether a card variant may legally remain in a terminal board slot."""
    if variant.level <= 0 or "辅助卡" in variant.sources:
        return False
    # Some deployment cards are identified by their handler rather than a tag or
    # source field. Runtime consumes these cards instead of assigning them to a
    # persistent slot.
    return not any(
        "deployment" in effect.events
        for effect in graph.effects_for(variant.ref.card.name, variant.ref.variant)
    )


def validate_recipe(graph: FactGraph, recipe: RecipeInstance) -> tuple[bool, tuple[str, ...]]:
    """Independently validate terminal legality and template hard constraints."""
    reasons: list[str] = []
    variants = _variant_map(graph)
    if not 1 < len(recipe.slots) <= 7:
        reasons.append("terminal recipe must contain 2..7 persistent slots")
    indices = [slot.index for slot in recipe.slots]
    if len(indices) != len(set(indices)) or any(index < 0 or index > 6 for index in indices):
        reasons.append("slot indices must be unique and within 0..6")
    selected: list[CardVariantFact] = []
    for slot in recipe.slots:
        variant = variants.get(slot.card.canonical_id)
        if variant is None:
            reasons.append(f"inactive variant:{slot.card.canonical_id}")
        elif not _is_terminal_variant(graph, variant):
            reasons.append(f"non-persistent card:{variant.ref.card.name}")
        else:
            selected.append(variant)
    if recipe.unsatisfied_factors:
        reasons.extend(recipe.unsatisfied_factors)

    effects = {effect.effect_id: effect for effect in graph.active_effects()}
    if recipe.template_id == "event-feedback-engine":
        for effect_id in recipe.provenance:
            effect = effects.get(effect_id)
            if effect is not None and any(condition.hard for condition in effect.conditions):
                reasons.append(f"unresolved hard condition:{effect_id}")

    if recipe.template_id == "zerg-swarm-engine":
        required = int(dict(recipe.derived_metrics).get("zerg_count", "0"))
        actual = sum(
            variant.ref.card.race in {"虫族", "zerg"} or "zerg" in variant.tags
            for variant in selected
        )
        if actual < required:
            reasons.append(f"zerg threshold:{actual}<{required}")

    if recipe.template_id == "protoss-energy-gathering":
        core_slot = next((slot for slot in recipe.slots if "gathering_consumer" in slot.roles), None)
        if core_slot is None:
            reasons.append("missing gathering consumer")
        else:
            multiplier = graph.void_pylon_energy_multiplier(selected)
            neighbor_indices = {core_slot.index - 1, core_slot.index, core_slot.index + 1}
            for left, right in recipe.extra_neighbors:
                if core_slot.index == left:
                    neighbor_indices.add(right)
                if core_slot.index == right:
                    neighbor_indices.add(left)
            actual_energy = sum(
                graph.energy_contribution(variants[slot.card.canonical_id], void_multiplier=multiplier)
                for slot in recipe.slots if slot.index in neighbor_indices
            )
            required = int(dict(recipe.derived_metrics).get("gathering_cost", "0"))
            if actual_energy < required:
                reasons.append(f"energy threshold:{actual_energy}<{required}")

    if recipe.template_id == "unit-supply-engine":
        unit = dict(recipe.derived_metrics).get("required_unit")
        selected_ids = {variant.ref.canonical_id for variant in selected}
        available = bool(unit) and (
            any(variant.unit_count(unit) > 0 for variant in selected)
            or any(effect.card.canonical_id in selected_ids for effect in graph.providers(unit))
        )
        if not available:
            reasons.append(f"missing unit supply:{unit}")

    if recipe.template_id == "psi-ascension-engine":
        role_slots = {
            role: slot for slot in recipe.slots for role in slot.roles
        }
        required_roles = {
            "psi_reactor_producer", "psi_gathering_producer",
            "higher_psi_anchor", "psi_board_elite",
        }
        if not required_roles <= role_slots.keys():
            reasons.append("missing psi ascension role")
        else:
            producer = variants[role_slots["psi_reactor_producer"].card.canonical_id]
            gatherer = variants[role_slots["psi_gathering_producer"].card.canonical_id]
            anchor = variants[role_slots["higher_psi_anchor"].card.canonical_id]
            ascender_slot = role_slots["psi_board_elite"]
            if anchor.level <= max(producer.level, gatherer.level):
                reasons.append("psi anchor must exceed producer levels")
            if ascender_slot.index <= max(
                role_slots["psi_reactor_producer"].index,
                role_slots["psi_gathering_producer"].index,
            ):
                reasons.append("board elite resolver must run after psi producers")
            gather_effect = next((
                effect for effect in graph.effects_for(gatherer.ref.card.name, gatherer.ref.variant)
                if effect.mechanism == "gathering"
            ), None)
            cost = graph.gathering_cost(gather_effect) if gather_effect is not None else None
            required_payload = int(dict(recipe.derived_metrics).get("required_tower_payload", "0"))
            if cost is None or required_payload != cost * 2:
                reasons.append("psi gathering payload must declare the double threshold")
            ascender = variants[ascender_slot.card.canonical_id]
            if not any(
                effect.mechanism == "psi"
                and any(action.kind == "transform_units" for action in effect.actions)
                for effect in graph.effects_for(ascender.ref.card.name, ascender.ref.variant)
            ):
                reasons.append("missing psi board elite effect")

    if recipe.template_id == "darkness-carousel":
        metrics = dict(recipe.derived_metrics)
        cycle_slot = int(metrics.get("cycle_slot", "-1"))
        occupied = {slot.index for slot in recipe.slots}
        broadcasters = [slot for slot in recipe.slots if "darkness_broadcaster" in slot.roles]
        listeners = [slot for slot in recipe.slots if "darkness_unit_listener" in slot.roles]
        if cycle_slot in occupied or len(broadcasters) != 2:
            reasons.append("darkness carousel requires one empty slot and two broadcasters")
        elif {slot.index for slot in broadcasters} != {cycle_slot - 1, cycle_slot + 1}:
            reasons.append("darkness broadcasters must flank the cycle slot")
        elif broadcasters[0].card.canonical_id != broadcasters[1].card.canonical_id:
            reasons.append("broadcasters must use the same variant for unique suppression")
        for slot in (*broadcasters, *listeners):
            variant = variants[slot.card.canonical_id]
            if "具有黑暗容器" not in variant.tags:
                reasons.append(f"darkness target lacks container:{variant.ref.card.name}")
        for slot in broadcasters:
            variant = variants[slot.card.canonical_id]
            if not any(
                effect.mechanism == "darkness_broadcast"
                and any(action.emits_event == "gain_darkness" for action in effect.actions)
                for effect in graph.effects_for(variant.ref.card.name, variant.ref.variant)
            ):
                reasons.append("missing darkness broadcast effect")
        for slot in listeners:
            variant = variants[slot.card.canonical_id]
            if not any(
                "gain_darkness" in effect.events
                and any(action.kind == "produce" for action in effect.actions)
                for effect in graph.effects_for(variant.ref.card.name, variant.ref.variant)
            ):
                reasons.append(f"darkness listener has no production:{variant.ref.card.name}")

    return not reasons, tuple(reasons)


def _role_for_provider(graph: FactGraph, provider: CardVariantFact) -> tuple[str, ...]:
    roles = ["stable_energy_supply"]
    if provider in graph.global_modifier_variants("void_pylon_energy_value"):
        roles.append("void_pylon_multiplier")
    return tuple(roles)


def _energy_factors(
    graph: FactGraph, slots: Iterable[RecipeSlot],
    variants: dict[str, CardVariantFact], multiplier: int,
) -> tuple[str, ...]:
    facts: list[str] = []
    for slot in sorted(slots, key=lambda item: item.index):
        variant = variants[slot.card.canonical_id]
        normal = variant.unit_count("水晶塔")
        void = variant.unit_count("虚空水晶塔")
        contribution = graph.energy_contribution(variant, void_multiplier=multiplier)
        if contribution:
            facts.append(
                f"energy_supply:slot={slot.index}:水晶塔={normal}:虚空水晶塔={void}:"
                f"void_multiplier={multiplier}:value={contribution}"
            )
    return tuple(facts)


def _make_energy_recipe(
    graph: FactGraph,
    consumer: EffectSpec,
    providers: tuple[CardVariantFact, ...],
    *,
    bonus_provider: CardVariantFact | None,
    listener_effect: EffectSpec | None,
    variants: dict[str, CardVariantFact],
    void_modifier_values: dict[str, int],
    gathering_bonus_values: dict[str, int],
    gathering_bonus_effects: dict[str, tuple[str, ...]],
) -> RecipeInstance | None:
    core = variants.get(consumer.card.canonical_id)
    if core is None:
        return None
    cost = graph.gathering_cost(consumer)
    if cost is None:
        return None

    slots: list[RecipeSlot] = [RecipeSlot(3, core.ref, roles=("gathering_consumer",))]
    for index, provider in zip((2, 4), providers):
        slots.append(RecipeSlot(index, provider.ref, roles=_role_for_provider(graph, provider)))
    occupied = {slot.index for slot in slots}
    selected = [core, *providers]

    selected_ids = {item.ref.canonical_id for item in selected}
    if bonus_provider is not None and bonus_provider.ref.canonical_id not in selected_ids:
        slots.append(RecipeSlot(0, bonus_provider.ref, roles=("artanis_bonus", "gathering_trigger_bonus")))
        occupied.add(0)
        selected.append(bonus_provider)
        selected_ids.add(bonus_provider.ref.canonical_id)

    feedback = False
    if listener_effect is not None:
        listener_variant = variants.get(listener_effect.card.canonical_id)
        if listener_variant is not None and listener_variant.ref.canonical_id not in selected_ids:
            index = next((candidate for candidate in (0, 1, 5, 6) if candidate not in occupied), None)
            if index is not None:
                slots.append(RecipeSlot(index, listener_variant.ref, roles=("teleport_listener", "energy_feedback")))
                selected.append(listener_variant)
                selected_ids.add(listener_variant.ref.canonical_id)
                feedback = True

    multiplier = max((1, *(void_modifier_values.get(item.ref.canonical_id, 1) for item in selected)))
    neighborhood = tuple(slot for slot in slots if slot.index in {2, 3, 4})
    energy = sum(
        graph.energy_contribution(variants[slot.card.canonical_id], void_multiplier=multiplier)
        for slot in neighborhood
    )
    gathering_bonus = max((0, *(gathering_bonus_values.get(item.ref.canonical_id, 0) for item in selected)))
    gathering_times = graph.gathering_times(consumer, energy, bonus=gathering_bonus)
    unmet = [] if energy >= cost else [f"energy:{energy}<{cost}"]

    factors = [
        f"energy:total={energy}>=cost={cost}",
        "closed_neighborhood:slots(2,3,4)",
        "gathering_reads_energy:not_consumes",
        *_energy_factors(graph, neighborhood, variants, multiplier),
    ]
    if gathering_bonus:
        factors.append(f"gathering_bonus:+{gathering_bonus}:triggers={gathering_times}")
    if feedback and listener_effect is not None:
        emitted = ",".join(_emitted_events(consumer))
        factors.append(f"event:{emitted}->listener:{listener_effect.effect_id}->tower_production")

    slot_tuple = tuple(sorted(slots, key=lambda slot: slot.index))
    score = score_recipe(
        energy=energy, cost=cost, slots_used=len(slot_tuple),
        has_artanis=bool(gathering_bonus), teleport_feedback=feedback,
        unmet=len(unmet),
    )
    provenance = [consumer.effect_id]
    provenance.extend(
        f"initial:{variants[slot.card.canonical_id].ref.canonical_id}"
        for slot in neighborhood
    )
    if bonus_provider is not None and bonus_provider in selected:
        provenance.extend(gathering_bonus_effects.get(bonus_provider.ref.canonical_id, ()))
    if listener_effect is not None and feedback:
        provenance.append(listener_effect.effect_id)
    teleport_calls = int(
        gathering_times > 0 and any(action.kind == "teleport" for action in consumer.actions)
    )
    return RecipeInstance(
        recipe_id=_recipe_id("protoss-energy-gathering", slot_tuple, ()),
        template_id="protoss-energy-gathering", expansions=graph.expansions,
        slots=slot_tuple, extra_neighbors=(), satisfied_factors=tuple(factors),
        unsatisfied_factors=tuple(unmet),
        derived_metrics=(
            ("energy", str(energy)), ("gathering_cost", str(cost)),
            ("gathering_trigger_count", str(gathering_times)),
            ("void_pylon_multiplier", str(multiplier)),
            ("teleport_call_count", str(teleport_calls)),
        ),
        score=score, provenance=tuple(dict.fromkeys(provenance)),
    )


def _generic_score(*, slots: int, payoff: float, stability: float, conflict: float = 0.0) -> ScoreBreakdown:
    slot_efficiency = max(0.0, 1.0 - (slots - 1) / 7.0)
    total = max(0.0, 0.5 * payoff + 0.25 * stability + 0.25 * slot_efficiency - conflict)
    return ScoreBreakdown(
        total=round(total, 6), payoff=round(payoff, 6), threshold_completion=1.0,
        slot_efficiency=round(slot_efficiency, 6), stability=round(stability, 6),
        conflict_penalty=round(conflict, 6),
    )


def _event_recipes(graph: FactGraph, max_slots: int) -> tuple[RecipeInstance, ...]:
    if max_slots < 2:
        return ()
    variants = _variant_map(graph)
    recipes: dict[str, RecipeInstance] = {}
    for event_name, emitter, listener in graph.event_chains():
        emitter_variant = variants.get(emitter.card.canonical_id)
        listener_variant = variants.get(listener.card.canonical_id)
        if emitter_variant is None or listener_variant is None:
            continue
        slots = (
            RecipeSlot(2, emitter_variant.ref, roles=("event_emitter",)),
            RecipeSlot(3, listener_variant.ref, roles=("event_listener",)),
        )
        partial = emitter.extraction == "partial" or listener.extraction == "partial"
        score = _generic_score(slots=2, payoff=0.9, stability=0.65 if partial else 0.9)
        factors = (
            f"event_chain:{emitter.effect_id}->event:{event_name}->{listener.effect_id}",
            f"emitter_trigger:{','.join(emitter.events) or 'passive'}",
            f"listener_action:{','.join(sorted({a.kind for a in listener.actions if a.kind != 'execute_handler'})) or 'runtime_handler'}",
        )
        recipe = RecipeInstance(
            recipe_id=_recipe_id("event-feedback-engine", slots, ()),
            template_id="event-feedback-engine", expansions=graph.expansions,
            slots=slots, extra_neighbors=(), satisfied_factors=factors,
            unsatisfied_factors=(),
            derived_metrics=(("event", event_name), ("semantic_confidence", "partial" if partial else "full")),
            score=score, provenance=(emitter.effect_id, listener.effect_id),
        )
        recipes[recipe.recipe_id] = recipe
    return tuple(sorted(recipes.values(), key=lambda recipe: (-recipe.score.total, recipe.recipe_id)))


def _swarm_recipes(graph: FactGraph, max_slots: int) -> tuple[RecipeInstance, ...]:
    variants = _variant_map(graph)
    zerg = tuple(sorted(
        (
            variant for variant in graph.active_variants()
            if variant.ref.card.race in {"虫族", "zerg"} or "zerg" in variant.tags
        ),
        key=lambda item: (-item.level, item.ref.canonical_id),
    ))
    recipes: dict[str, RecipeInstance] = {}
    for effect in graph.active_effects():
        if effect.mechanism != "swarm":
            continue
        threshold_condition = next(
            (condition for condition in effect.conditions if condition.kind == "race_count_threshold"), None
        )
        if threshold_condition is None or not isinstance(threshold_condition.value, int):
            continue
        core = variants.get(effect.card.canonical_id)
        if core is None:
            continue
        # A threshold of one is a self-contained card, not a dependency recipe.
        if threshold_condition.value <= 1:
            continue
        core_counts = int(core in zerg)
        needed = max(0, threshold_condition.value - core_counts)
        if needed + 1 > max_slots:
            continue
        candidates = tuple(item for item in zerg if item.ref.canonical_id != core.ref.canonical_id)[:10]
        for providers in itertools.combinations(candidates, needed):
            slots = [RecipeSlot(3, core.ref, roles=("swarm_consumer",))]
            indices = iter((0, 1, 2, 4, 5, 6))
            slots.extend(
                RecipeSlot(next(indices), provider.ref, roles=("zerg_board_threshold",))
                for provider in providers
            )
            slot_tuple = tuple(sorted(slots, key=lambda slot: slot.index))
            score = _generic_score(slots=len(slot_tuple), payoff=0.85, stability=0.95)
            recipe = RecipeInstance(
                recipe_id=_recipe_id("zerg-swarm-engine", slot_tuple, ()),
                template_id="zerg-swarm-engine", expansions=graph.expansions,
                slots=slot_tuple, extra_neighbors=(),
                satisfied_factors=(
                    f"race_count:zerg={threshold_condition.value}",
                    f"swarm_payoff:{effect.effect_id}",
                ),
                unsatisfied_factors=(),
                derived_metrics=(("zerg_count", str(threshold_condition.value)),),
                score=score, provenance=(effect.effect_id,),
            )
            recipes[recipe.recipe_id] = recipe
    return tuple(sorted(recipes.values(), key=lambda recipe: (-recipe.score.total, recipe.recipe_id)))


def _unit_supply_recipes(graph: FactGraph, max_slots: int) -> tuple[RecipeInstance, ...]:
    if max_slots < 2:
        return ()
    variants = _variant_map(graph)
    recipes: dict[str, RecipeInstance] = {}
    for consumer in graph.active_effects():
        for condition in consumer.conditions:
            if condition.kind != "unit_count_threshold":
                continue
            detail = dict(condition.details)
            unit = detail.get("unit")
            if not unit:
                continue
            provider_ids = {
                effect.card.canonical_id for effect in graph.providers(unit)
            }
            provider_ids.update(
                variant.ref.canonical_id for variant in graph.active_variants()
                if variant.unit_count(unit) > 0
            )
            core = variants.get(consumer.card.canonical_id)
            if core is None:
                continue
            for provider_id in sorted(provider_ids):
                if provider_id == core.ref.canonical_id:
                    continue
                provider = variants.get(provider_id)
                if provider is None:
                    continue
                slots = (
                    RecipeSlot(2, provider.ref, roles=("unit_provider",)),
                    RecipeSlot(3, core.ref, roles=("unit_consumer",)),
                )
                score = _generic_score(slots=2, payoff=0.75, stability=0.8)
                recipe = RecipeInstance(
                    recipe_id=_recipe_id("unit-supply-engine", slots, ()),
                    template_id="unit-supply-engine", expansions=graph.expansions,
                    slots=slots, extra_neighbors=(),
                    satisfied_factors=(
                        f"unit_supply:{provider.ref.canonical_id}->unit:{unit}->{consumer.effect_id}",
                        f"required_quantity:{condition.value}",
                    ),
                    unsatisfied_factors=(),
                    derived_metrics=(("required_unit", unit), ("threshold", str(condition.value))),
                    score=score, provenance=(consumer.effect_id,),
                )
                recipes[recipe.recipe_id] = recipe
    return tuple(sorted(recipes.values(), key=lambda recipe: (-recipe.score.total, recipe.recipe_id)))


def _psi_ascension_recipes(graph: FactGraph, max_slots: int) -> tuple[RecipeInstance, ...]:
    if max_slots < 4:
        return ()
    variants = _variant_map(graph)
    effects_by_variant = {
        identifier: graph.effects_for(variant.ref.card.name, variant.ref.variant)
        for identifier, variant in variants.items()
    }
    psi_variants = tuple(
        variant for variant in variants.values()
        if "灵能" in variant.tags and _is_terminal_variant(graph, variant)
    )
    producers = tuple(
        variant for variant in psi_variants
        if any(effect.mechanism == "reactor" for effect in effects_by_variant[variant.ref.canonical_id])
        and any(
            effect.mechanism == "psi" and any(action.kind == "produce" for action in effect.actions)
            for effect in effects_by_variant[variant.ref.canonical_id]
        )
    )
    gatherers = tuple(
        variant for variant in psi_variants
        if any(effect.mechanism == "gathering" for effect in effects_by_variant[variant.ref.canonical_id])
        and any(effect.mechanism == "psi" for effect in effects_by_variant[variant.ref.canonical_id])
    )
    anchors = tuple(
        variant for variant in psi_variants
        if variant.ref.card.race in {"中立", "neutral"}
        and any(effect.mechanism == "psi" for effect in effects_by_variant[variant.ref.canonical_id])
    )
    ascenders = tuple(
        variant for variant in variants.values()
        if _is_terminal_variant(graph, variant)
        and any(
            effect.mechanism == "psi"
            and any(action.kind == "transform_units" and action.target_scope == "board" for action in effect.actions)
            for effect in effects_by_variant[variant.ref.canonical_id]
        )
    )

    recipes: dict[str, RecipeInstance] = {}
    for producer in producers:
        for gatherer in gatherers:
            gathering_effect = next(
                effect for effect in effects_by_variant[gatherer.ref.canonical_id]
                if effect.mechanism == "gathering"
            )
            cost = graph.gathering_cost(gathering_effect)
            if cost is None:
                continue
            for anchor in anchors:
                if anchor.level <= max(producer.level, gatherer.level):
                    continue
                for ascender in ascenders:
                    names = {
                        producer.ref.card.name, gatherer.ref.card.name,
                        anchor.ref.card.name, ascender.ref.card.name,
                    }
                    if len(names) != 4:
                        continue
                    slots = (
                        RecipeSlot(0, producer.ref, roles=("psi_reactor_producer", "lower_psi_level")),
                        RecipeSlot(1, gatherer.ref, roles=("psi_gathering_producer", "double_gathering_payload")),
                        RecipeSlot(2, anchor.ref, roles=("higher_psi_anchor",)),
                        RecipeSlot(3, ascender.ref, roles=("psi_board_elite", "last_round_end_resolver")),
                    )
                    required_towers = cost * 2
                    provenance = tuple(dict.fromkeys(
                        effect.effect_id
                        for variant in (producer, gatherer, anchor, ascender)
                        for effect in effects_by_variant[variant.ref.canonical_id]
                        if effect.mechanism in {"psi", "gathering", "reactor"}
                    ))
                    score = _generic_score(slots=4, payoff=1.0, stability=0.95)
                    recipe = RecipeInstance(
                        recipe_id=_recipe_id("psi-ascension-engine", slots, ()),
                        template_id="psi-ascension-engine", expansions=graph.expansions,
                        slots=slots, extra_neighbors=(),
                        satisfied_factors=(
                            f"psi_levels:{producer.level},{gatherer.level},0<{anchor.level}",
                            "round_end_order:psi_producers_before_board_elite",
                            f"gathering_payload:slot=1:水晶塔>={required_towers}",
                            "psi_anchor:highest_does_not_trigger_own_psi",
                            "psi_board_elite:all_eligible_units_on_psi_cards",
                        ),
                        unsatisfied_factors=(),
                        derived_metrics=(
                            ("psi_anchor_level", str(anchor.level)),
                            ("gathering_cost", str(cost)),
                            ("required_tower_payload", str(required_towers)),
                            ("elite_resolver_slot", "3"),
                        ),
                        score=score, provenance=provenance,
                    )
                    recipes[recipe.recipe_id] = recipe
    return tuple(sorted(recipes.values(), key=lambda recipe: (-recipe.score.total, recipe.recipe_id)))


def _darkness_carousel_recipes(graph: FactGraph, max_slots: int) -> tuple[RecipeInstance, ...]:
    if max_slots < 6:
        return ()
    variants = _variant_map(graph)
    effects_by_variant = {
        identifier: graph.effects_for(variant.ref.card.name, variant.ref.variant)
        for identifier, variant in variants.items()
    }
    broadcasters = tuple(
        variant for variant in variants.values()
        if _is_terminal_variant(graph, variant)
        and "具有黑暗容器" in variant.tags
        and any(effect.mechanism == "darkness_broadcast" for effect in effects_by_variant[variant.ref.canonical_id])
        and any(
            "gain_darkness" in effect.events
            and any(action.kind == "produce" for action in effect.actions)
            for effect in effects_by_variant[variant.ref.canonical_id]
        )
    )
    listeners = tuple(sorted(
        (
            variant for variant in variants.values()
            if _is_terminal_variant(graph, variant)
            and "具有黑暗容器" in variant.tags
            and not any(effect.mechanism == "darkness_broadcast" for effect in effects_by_variant[variant.ref.canonical_id])
            and any(
                "gain_darkness" in effect.events
                and any(action.kind == "produce" for action in effect.actions)
                for effect in effects_by_variant[variant.ref.canonical_id]
            )
        ),
        key=lambda variant: (variant.ref.card.name, variant.ref.variant),
    ))
    if not listeners:
        return ()

    recipes: dict[str, RecipeInstance] = {}
    for broadcaster in broadcasters:
        share = next(
            effect for effect in effects_by_variant[broadcaster.ref.canonical_id]
            if effect.mechanism == "darkness_broadcast"
        )
        share_action = next(action for action in share.actions if action.emits_event == "gain_darkness")
        multiplier = int(dict(share_action.details).get("multiplier", "1"))
        for payload in itertools.product(listeners, repeat=4):
            slots = (
                RecipeSlot(0, broadcaster.ref, roles=("darkness_broadcaster", "left_sale_neighbor")),
                RecipeSlot(2, broadcaster.ref, roles=("darkness_broadcaster", "right_sale_neighbor")),
                *(RecipeSlot(index, listener.ref, roles=("darkness_unit_listener",))
                  for index, listener in zip((3, 4, 5, 6), payload)),
            )
            listener_effects = tuple(
                effect.effect_id
                for listener in payload
                for effect in effects_by_variant[listener.ref.canonical_id]
                if "gain_darkness" in effect.events
                and any(action.kind == "produce" for action in effect.actions)
            )
            production_events = 3 + len(payload)
            score = _generic_score(slots=6, payoff=1.0, stability=0.85)
            recipe = RecipeInstance(
                recipe_id=_recipe_id("darkness-carousel", slots, ()),
                template_id="darkness-carousel", expansions=graph.expansions,
                slots=slots, extra_neighbors=(),
                satisfied_factors=(
                    "empty_cycle_slot:slot=1",
                    "sell_card:slot=1->adjacent_gain_darkness:slots=0,2",
                    f"darkness_broadcast:slot=0->other_containers:x{multiplier}",
                    "unique_suppression:same_variant_broadcasters",
                    f"gain_darkness_production_events_per_sale:{production_events}",
                ),
                unsatisfied_factors=(),
                derived_metrics=(
                    ("cycle_slot", "1"),
                    ("persistent_slots", "6"),
                    ("darkness_multiplier", str(multiplier)),
                    ("production_events_per_sale", str(production_events)),
                ),
                score=score,
                provenance=tuple(dict.fromkeys((share.effect_id, *listener_effects))),
            )
            recipes[recipe.recipe_id] = recipe
    return tuple(sorted(recipes.values(), key=lambda recipe: (-recipe.score.total, recipe.recipe_id)))


def _energy_recipes(graph: FactGraph, max_slots: int) -> tuple[RecipeInstance, ...]:
    variant_map = _variant_map(graph)
    protoss_variants = tuple(
        variant for variant in graph.active_variants()
        if variant.ref.card.race in ("神族", "protoss") or "protoss" in variant.tags
    )
    # Only the strongest deterministic initial suppliers are needed for top-k.
    # This bounds the candidate search before combinations without hard-coding
    # card names and keeps full-catalog rebuilds sub-second.
    providers = tuple(sorted(
        (variant for variant in protoss_variants if graph.energy_contribution(variant) > 0),
        key=lambda item: (-graph.energy_contribution(item, void_multiplier=3), -item.level, item.ref.canonical_id),
    )[:10])

    void_modifier_values: dict[str, int] = {}
    gathering_bonus_values: dict[str, int] = {}
    gathering_bonus_effects: dict[str, tuple[str, ...]] = {}
    for effect in graph.active_effects():
        for action in effect.actions:
            try:
                value = int(action.quantity)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if action.kind == "modify_global" and action.object == "void_pylon_energy_value":
                identifier = effect.card.canonical_id
                void_modifier_values[identifier] = max(void_modifier_values.get(identifier, 1), value)
            elif action.kind == "modify_global" and action.object == "gathering_trigger_bonus":
                identifier = effect.card.canonical_id
                gathering_bonus_values[identifier] = max(gathering_bonus_values.get(identifier, 0), value)
                gathering_bonus_effects[identifier] = tuple(sorted({
                    *gathering_bonus_effects.get(identifier, ()), effect.effect_id,
                }))
    bonus_providers = (
        None,
        *(variant_map[identifier] for identifier in sorted(gathering_bonus_values) if identifier in variant_map),
    )

    recipes: dict[str, RecipeInstance] = {}
    for consumer in graph.gathering_effects():
        core = variant_map.get(consumer.card.canonical_id)
        if core is None or core not in protoss_variants:
            continue
        emitted = _emitted_events(consumer)
        listener_options = (
            None,
            *graph.energy_feedback_listeners(emitted)[:4],
        )
        candidates = tuple(
            provider for provider in providers
            if provider.ref.canonical_id != core.ref.canonical_id
        )
        for count in (0, 1, 2):
            for supplier_tuple in itertools.combinations(candidates, count):
                for bonus_provider in bonus_providers:
                    for listener_effect in listener_options:
                        recipe = _make_energy_recipe(
                            graph, consumer, supplier_tuple,
                            bonus_provider=bonus_provider, listener_effect=listener_effect,
                            variants=variant_map,
                            void_modifier_values=void_modifier_values,
                            gathering_bonus_values=gathering_bonus_values,
                            gathering_bonus_effects=gathering_bonus_effects,
                        )
                        if (
                            recipe and 2 <= len(recipe.slots) <= max_slots
                            and not recipe.unsatisfied_factors
                        ):
                            existing = recipes.get(recipe.recipe_id)
                            if existing is None or recipe.score.total > existing.score.total:
                                recipes[recipe.recipe_id] = recipe
    return tuple(sorted(recipes.values(), key=lambda recipe: (-recipe.score.total, recipe.recipe_id)))


def _diverse_top_k(
    by_template: dict[str, tuple[RecipeInstance, ...]], limit: int
) -> tuple[RecipeInstance, ...]:
    """Deterministic round-robin selection prevents one mechanism monopolising top-k."""
    selected: list[RecipeInstance] = []
    seen: set[str] = set()
    template_ids = sorted(by_template)
    index = 0
    while len(selected) < limit:
        added = False
        for template_id in template_ids:
            bucket = by_template[template_id]
            if index < len(bucket):
                recipe = bucket[index]
                if recipe.recipe_id not in seen:
                    selected.append(recipe)
                    seen.add(recipe.recipe_id)
                    added = True
                    if len(selected) >= limit:
                        break
        if not added:
            break
        index += 1
    return tuple(selected)


def generate_recipe_instances(
    graph: FactGraph, *, templates: Iterable[RecipeTemplate] | None = None,
    max_slots: int = 7, limit_per_template: int = 100,
    max_recipes: int = MAX_CATALOG_RECIPES,
) -> tuple[RecipeInstance, ...]:
    """Generate a deterministic, globally bounded multi-mechanism catalog.

    ``max_recipes`` is hard-capped at 100 by contract. ``limit_per_template`` is
    retained for API compatibility but no longer controls total catalog size.
    """
    if max_slots < 1 or max_recipes < 0 or limit_per_template < 0:
        return ()
    effective_slots = min(max_slots, 7)
    effective_max = min(max_recipes, MAX_CATALOG_RECIPES)
    selected_ids = {template.id for template in (templates or DEFAULT_TEMPLATES)}
    builders = {
        "protoss-energy-gathering": _energy_recipes,
        "event-feedback-engine": _event_recipes,
        "zerg-swarm-engine": _swarm_recipes,
        "unit-supply-engine": _unit_supply_recipes,
        "psi-ascension-engine": _psi_ascension_recipes,
        "darkness-carousel": _darkness_carousel_recipes,
    }
    by_template: dict[str, tuple[RecipeInstance, ...]] = {}
    for template_id in sorted(selected_ids):
        builder = builders.get(template_id)
        if builder is None:
            continue
        candidates = builder(graph, effective_slots)
        by_template[template_id] = tuple(
            recipe for recipe in candidates
            if validate_recipe(graph, recipe)[0]
        )[:limit_per_template]
    return _diverse_top_k(by_template, effective_max)


__all__ = ["MAX_CATALOG_RECIPES", "generate_recipe_instances", "validate_recipe"]
