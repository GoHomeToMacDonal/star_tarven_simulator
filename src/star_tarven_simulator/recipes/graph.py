"""Stable full fact graph and expansion-filtered active views."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from typing import Any, Iterable

from star_tarven_simulator.expansions import (
    EXCLUSIVE_PACKS, EXPANSION_PACKS, enabled_sources, is_card_enabled,
    validate_selection,
)
from star_tarven_simulator.simulator.event import Event

from .effect_ir import CardRef, CardVariantRef, EffectSpec
from .extractor import extract_effects


@dataclass(frozen=True, slots=True)
class CardVariantFact:
    ref: CardVariantRef
    level: int
    units: tuple[tuple[str, int], ...]
    tags: tuple[str, ...]
    sources: tuple[str, ...]

    def unit_count(self, unit: str) -> int:
        return dict(self.units).get(unit, 0)


@dataclass(frozen=True, slots=True)
class Relation:
    source: str
    kind: str
    target: str
    details: tuple[tuple[str, str], ...] = ()


def _detail_map(details: tuple[tuple[str, str], ...]) -> dict[str, str]:
    return dict(details)


def _int_value(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class FactGraph:
    """A source-independent master graph, or a stable activated view of it.

    Query helpers deliberately use only ``EffectSpec`` and static card facts.  They
    provide the recipe solver with typed mechanics without re-reading card names or
    reflecting execution handlers.
    """

    dataset_id: str | None
    variants: tuple[CardVariantFact, ...]
    effects: tuple[EffectSpec, ...]
    relations: tuple[Relation, ...]
    extraction_report: Any
    active_variant_ids: frozenset[str] = field(default_factory=frozenset)
    expansions: tuple[str, ...] = ()

    @property
    def fingerprint(self) -> str:
        payload = {
            "dataset": self.dataset_id,
            "variants": [asdict(v) for v in self.variants],
            "effects": [asdict(e) for e in self.effects],
            "relations": [asdict(r) for r in self.relations],
            "active": sorted(self.active_variant_ids),
            "expansions": self.expansions,
        }
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
        return "sha256:" + sha256(text.encode("utf-8")).hexdigest()

    @property
    def is_active_view(self) -> bool:
        return bool(self.active_variant_ids) or self.expansions != ()

    def activate(self, expansions: Iterable[str] | None = None) -> "FactGraph":
        selected = tuple(validate_selection(expansions))
        allowed = enabled_sources(selected)
        active = frozenset(
            variant.ref.canonical_id for variant in self.variants
            if is_card_enabled(variant.sources, allowed)
        )
        graph = FactGraph(self.dataset_id, self.variants, self.effects, self.relations,
                          self.extraction_report, active, selected)
        graph.assert_consistent()
        return graph

    def active_variants(self) -> tuple[CardVariantFact, ...]:
        active = self.active_variant_ids or frozenset(v.ref.canonical_id for v in self.variants)
        return tuple(v for v in self.variants if v.ref.canonical_id in active)

    def active_effects(self) -> tuple[EffectSpec, ...]:
        active = self.active_variant_ids or frozenset(v.ref.canonical_id for v in self.variants)
        return tuple(effect for effect in self.effects if effect.card.canonical_id in active)

    def variant_for(self, reference: CardVariantRef | str) -> CardVariantFact | None:
        identifier = reference if isinstance(reference, str) else reference.canonical_id
        return next((variant for variant in self.active_variants() if variant.ref.canonical_id == identifier), None)

    def effects_for(self, card_name: str, variant: str | None = None) -> tuple[EffectSpec, ...]:
        return tuple(effect for effect in self.active_effects()
                     if effect.card.card.name == card_name and (variant is None or effect.card.variant == variant))

    def action_effects(self, kind: str, *, object_name: str | None = None,
                       variants: Iterable[CardVariantFact] | None = None) -> tuple[EffectSpec, ...]:
        allowed = None if variants is None else {variant.ref.canonical_id for variant in variants}
        return tuple(
            effect for effect in self.active_effects()
            if (allowed is None or effect.card.canonical_id in allowed)
            and any(action.kind == kind and (object_name is None or action.object == object_name)
                    for action in effect.actions)
        )

    def providers(self, object_name: str, *, action_kinds: tuple[str, ...] = ("produce", "teleport", "larva", "hatch")) -> tuple[EffectSpec, ...]:
        return tuple(effect for effect in self.active_effects()
                     if any(action.object == object_name and action.kind in action_kinds for action in effect.actions))

    def gathering_effects(self) -> tuple[EffectSpec, ...]:
        return tuple(effect for effect in self.active_effects() if effect.mechanism == "gathering")

    def gathering_cost(self, effect: EffectSpec) -> int | None:
        for condition in effect.conditions:
            if condition.kind == "energy_threshold":
                return _int_value(condition.value)
        return None

    def gathering_times(self, effect: EffectSpec, energy: int, *, bonus: int = 0) -> int:
        """Evaluate the declared gathering trigger formula without consuming energy."""
        cost = self.gathering_cost(effect)
        if cost is None or cost <= 0:
            return bonus
        condition = next(condition for condition in effect.conditions if condition.kind == "energy_threshold")
        cap = _int_value(_detail_map(condition.details).get("cap")) or 2
        return min(energy // cost, cap) + bonus

    def global_modifier_variants(self, object_name: str) -> tuple[CardVariantFact, ...]:
        ids = {effect.card.canonical_id for effect in self.action_effects("modify_global", object_name=object_name)}
        return tuple(variant for variant in self.active_variants() if variant.ref.canonical_id in ids)

    def void_pylon_energy_multiplier(self, selected: Iterable[CardVariantFact] | None = None) -> int:
        """Return the runtime-equivalent board-wide MAX multiplier (default 1)."""
        modifiers = self.action_effects("modify_global", object_name="void_pylon_energy_value", variants=selected)
        values = [value for effect in modifiers for action in effect.actions
                  if action.kind == "modify_global" and action.object == "void_pylon_energy_value"
                  if (value := _int_value(action.quantity)) is not None]
        return max((1, *values))

    def gathering_trigger_bonus(self, selected: Iterable[CardVariantFact] | None = None) -> int:
        """Return the global extra-trigger bonus represented by selected cards."""
        modifiers = self.action_effects("modify_global", object_name="gathering_trigger_bonus", variants=selected)
        # Runtime uses presence rather than stacking: an Artanis anywhere grants one.
        values = [value for effect in modifiers for action in effect.actions
                  if action.kind == "modify_global" and action.object == "gathering_trigger_bonus"
                  if (value := _int_value(action.quantity)) is not None]
        return max([0, *values])

    def energy_contribution(self, variant: CardVariantFact, *, void_multiplier: int = 1) -> int:
        """Static local energy supplied by a card's initial towers."""
        return variant.unit_count("水晶塔") + variant.unit_count("虚空水晶塔") * void_multiplier

    def energy_feedback_listeners(self, emitted_events: Iterable[str]) -> tuple[EffectSpec, ...]:
        """Find listeners that can add a tower after a declared emitted event."""
        events = frozenset(emitted_events)
        if not events:
            return ()
        return tuple(
            effect for effect in self.active_effects()
            if events.intersection(effect.events)
            and any(action.kind == "produce" and action.object in {"水晶塔", "虚空水晶塔"}
                    for action in effect.actions)
        )

    def event_names(self) -> tuple[str, ...]:
        """The complete runtime event vocabulary, including currently dormant events."""
        return tuple(event.value for event in Event)

    def listeners_to(self, event_name: str) -> tuple[EffectSpec, ...]:
        """All active effects bound to ``event_name`` (full and partial semantics)."""
        return tuple(effect for effect in self.active_effects() if event_name in effect.events)

    def emitters_of(self, event_name: str) -> tuple[EffectSpec, ...]:
        """Effects with a declared action that can emit ``event_name``."""
        return tuple(
            effect for effect in self.active_effects()
            if any(action.emits_event == event_name for action in effect.actions)
        )

    def event_chains(self) -> tuple[tuple[str, EffectSpec, EffectSpec], ...]:
        """Reusable emitter -> event -> listener dependency triples.

        The method is generic over the event vocabulary and does not encode card
        names. It is the shared primitive used by catalog and strategy queries.
        """
        chains: list[tuple[str, EffectSpec, EffectSpec]] = []
        for event_name in self.event_names():
            for emitter in self.emitters_of(event_name):
                for listener in self.listeners_to(event_name):
                    if emitter.card.canonical_id != listener.card.canonical_id:
                        chains.append((event_name, emitter, listener))
        return tuple(sorted(chains, key=lambda item: (
            item[0], item[1].effect_id, item[2].effect_id
        )))

    def effects_requiring(self, condition_kind: str) -> tuple[EffectSpec, ...]:
        return tuple(
            effect for effect in self.active_effects()
            if any(condition.kind == condition_kind for condition in effect.conditions)
        )

    def dependency_relations(self, effect: EffectSpec) -> tuple[Relation, ...]:
        """Relations directly explaining an effect's inputs, outputs and events."""
        effect_node = f"effect:{effect.effect_id}"
        return tuple(relation for relation in self.relations if relation.source == effect_node)

    def event_coverage(self) -> tuple[tuple[str, int, int], ...]:
        """Per-event ``(name, listeners, fully_semantic_listeners)`` audit matrix."""
        rows = []
        for event_name in self.event_names():
            listeners = self.listeners_to(event_name)
            full = sum(effect.extraction != "partial" and effect.extraction != "opaque" for effect in listeners)
            rows.append((event_name, len(listeners), full))
        return tuple(rows)

    def assert_consistent(self) -> None:
        ids = {variant.ref.canonical_id for variant in self.variants}
        if len(ids) != len(self.variants):
            raise AssertionError("duplicate CardVariantFact identity")
        effect_ids: set[str] = set()
        for effect in self.effects:
            if effect.card.canonical_id not in ids:
                raise AssertionError(f"effect refers to absent variant: {effect.effect_id}")
            if effect.effect_id in effect_ids:
                raise AssertionError(f"duplicate EffectSpec identity: {effect.effect_id}")
            effect_ids.add(effect.effect_id)
        if not self.active_variant_ids <= ids:
            raise AssertionError("active view includes unavailable card variant")


def _variant_fact(card: Any, variant: str, dataset_id: str | None) -> CardVariantFact:
    ref = CardVariantRef(CardRef.from_card(card, dataset_id), variant)  # type: ignore[arg-type]
    tags = card.tags if variant == "normal" else card.gold_tags
    # A terminal gold card is obtained by merging three normal copies. Runtime
    # merge_slots keeps their accumulated payload while swapping to gold handlers,
    # so the minimum reproducible starting payload is three times the card data.
    unit_multiplier = 3 if variant == "gold" else 1
    return CardVariantFact(
        ref=ref, level=int(card.level),
        units=tuple(sorted((str(k), int(v) * unit_multiplier) for k, v in card.units.items())),
        tags=tuple(sorted(tags)), sources=tuple(sorted(card.source or ())),
    )


def _condition_details(condition: Any) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((("scope", condition.scope), ("hard", str(condition.hard).lower()),
                         ("operator", condition.operator), ("aggregation", condition.aggregation), *condition.details)))


def _action_details(action: Any) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((("target_scope", action.target_scope), ("quantity", str(action.quantity)),
                         ("random", str(action.random).lower()), ("consumes_input", str(action.consumes_input).lower()),
                         *action.details)))


def build_fact_graph(cards: Iterable[Any], *, dataset_id: str | None = None, strict: bool = True) -> FactGraph:
    """Build a deterministic unfiltered graph from all supplied static cards."""
    cards = tuple(cards)
    extracted_effects, report = extract_effects(cards, dataset_id=dataset_id, strict=strict)
    effects = tuple(sorted({effect.effect_id: effect for effect in extracted_effects}.values(), key=lambda effect: effect.effect_id))
    variants = tuple(sorted(
        (_variant_fact(card, variant, dataset_id) for card in cards for variant in ("normal", "gold")),
        key=lambda item: item.ref.canonical_id,
    ))
    relations: list[Relation] = [
        Relation("unit:水晶塔", "CONTRIBUTES_TO", "attribute:local_energy",
                 (("scope", "closed_neighborhood"), ("value", "1"))),
        Relation("unit:虚空水晶塔", "CONTRIBUTES_TO", "attribute:local_energy",
                 (("scope", "closed_neighborhood"), ("value", "global:void_pylon_energy_value"))),
        Relation("action:sell_card", "EMITS", "event:gain_darkness",
                 (("scope", "adjacent_darkness_containers"), ("quantity", "1"))),
        Relation("scope:empty_cycle_slot", "ENABLES", "action:buy_then_sell",
                 (("purpose", "repeatable_shop_cycle"),)),
    ]
    # Keep the complete runtime event vocabulary and expansion constraints in the
    # source-independent master graph, even when a dataset has no listener for a
    # dormant event.
    relations.extend(Relation("schema:runtime_events", "HAS_EVENT", f"event:{event.value}") for event in Event)
    for exclusive in EXCLUSIVE_PACKS:
        for other in EXPANSION_PACKS:
            if other != exclusive:
                relations.append(Relation(
                    f"expansion:{exclusive}", "EXCLUSIVE_WITH", f"expansion:{other}"
                ))
    for variant in variants:
        source = variant.ref.canonical_id
        for unit, quantity in variant.units:
            relations.append(Relation(source, "STARTS_WITH", f"unit:{unit}", (("quantity", str(quantity)),)))
        for tag in variant.tags:
            relations.append(Relation(source, "HAS_TAG", f"tag:{tag}"))
        for package in variant.sources:
            relations.append(Relation(source, "AVAILABLE_FROM", f"expansion:{package}"))
    relation_for_action = {
        "produce": "PRODUCES", "teleport": "TELEPORTS", "larva": "LARVAS",
        "hatch": "HATCHES", "transform_units": "TRANSFORMS",
        "convert_units": "TRANSFORMS", "transform_card": "TRANSFORMS",
        "move_units": "MOVES", "modify_global": "MODIFIES",
        "modify_attribute": "MODIFIES", "add_upgrade": "ADDS_UPGRADE",
        "destroy_card": "DESTROYS", "seize_card": "SEIZES",
        "discover_card": "DISCOVERS", "emit_event": "EMITS",
    }
    object_prefix = {
        "produce": "unit", "teleport": "unit", "larva": "unit", "hatch": "unit",
        "add_upgrade": "upgrade", "modify_global": "attribute",
        "modify_attribute": "attribute", "discover_card": "card_filter",
        "destroy_card": "card_filter", "seize_card": "card_filter",
        "emit_event": "event",
    }
    for effect in effects:
        effect_node = f"effect:{effect.effect_id}"
        source = effect.card.canonical_id
        relations.append(Relation(source, "HAS_EFFECT", effect_node))
        for event in effect.events:
            relations.append(Relation(effect_node, "LISTENS_TO", f"event:{event}"))
        for condition in effect.conditions:
            target = f"condition:{condition.kind}:{condition.value}"
            relations.append(Relation(effect_node, "REQUIRES", target, _condition_details(condition)))
        for action in effect.actions:
            relation = relation_for_action.get(action.kind, "ACTS")
            prefix = object_prefix.get(action.kind, "action_object")
            target = f"{prefix}:{action.object}" if action.object else f"action:{action.kind}"
            relations.append(Relation(effect_node, relation, target, _action_details(action)))
            relations.append(Relation(
                effect_node, "TARGETS", f"scope:{action.target_scope}",
                (("action", action.kind),),
            ))
            if action.emits_event:
                relations.append(Relation(effect_node, "EMITS", f"event:{action.emits_event}"))
    graph = FactGraph(dataset_id, variants, effects,
                      tuple(sorted(set(relations), key=lambda r: (r.source, r.kind, r.target, r.details))), report)
    graph.assert_consistent()
    return graph


def active_subgraph(graph: FactGraph, expansions: Iterable[str] | None = None) -> FactGraph:
    """Compatibility API mirroring the design document."""
    return graph.activate(expansions)


__all__ = ["CardVariantFact", "FactGraph", "Relation", "active_subgraph", "build_fact_graph"]
