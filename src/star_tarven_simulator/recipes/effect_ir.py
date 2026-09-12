"""Immutable, serialisable intermediate representation for recipe analysis.

This module deliberately has no dependency on the runtime simulator.  It captures
facts declared by card data so a graph/catalog can be rebuilt deterministically.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
from typing import Any, Literal


Variant = Literal["normal", "gold"]
# ``partial`` means the executable handler is represented in the graph and its
# event/mechanism is known, but some target/filter/quantity semantics remain
# intentionally conservative.  It is distinct from ``opaque`` (no useful
# semantics) so coverage reports never confuse event-handler coverage with full
# semantic coverage.
ExtractionKind = Literal["automatic", "template", "override", "partial", "opaque"]


def _canonical(value: Any) -> str:
    """Stable JSON used for IDs/fingerprints (including tuple-heavy dataclasses)."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def stable_id(prefix: str, value: Any) -> str:
    return f"{prefix}:{sha256(_canonical(value).encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class CardRef:
    """Stable card identity plus replay identity for a specific data snapshot."""

    canonical_id: str
    name: str
    race: str
    uuid: int
    dataset_id: str | None
    sources: tuple[str, ...] = ()

    @classmethod
    def from_card(cls, card: Any, dataset_id: str | None = None) -> "CardRef":
        # Name/race remains stable when a dataset reassigns its local UUID.
        canonical_id = stable_id("card", {"name": card.name, "race": card.race})
        return cls(
            canonical_id=canonical_id,
            name=card.name,
            race=card.race,
            uuid=int(card.uuid),
            dataset_id=dataset_id,
            sources=tuple(sorted(getattr(card, "source", ()) or ())),
        )


@dataclass(frozen=True, slots=True)
class CardVariantRef:
    card: CardRef
    variant: Variant

    @property
    def canonical_id(self) -> str:
        return f"{self.card.canonical_id}:{self.variant}"


@dataclass(frozen=True, slots=True)
class Condition:
    """Typed requirement/factor. ``details`` stores extensible named metadata."""

    kind: str
    operator: str = ">="
    value: int | float | str | tuple[str, ...] | None = None
    scope: str = "self"
    aggregation: str = "any"
    hard: bool = True
    details: tuple[tuple[str, str], ...] = ()

    @classmethod
    def make(cls, kind: str, /, **kwargs: Any) -> "Condition":
        details = kwargs.pop("details", ())
        if isinstance(details, dict):
            details = tuple(sorted((str(k), str(v)) for k, v in details.items()))
        return cls(kind=kind, details=tuple(details), **kwargs)


@dataclass(frozen=True, slots=True)
class Action:
    """A declarative output or transformation of an effect."""

    kind: str
    target_scope: str = "self"
    object: str | None = None
    quantity: int | float | str | None = None
    random: bool = False
    consumes_input: bool = False
    emits_event: str | None = None
    details: tuple[tuple[str, str], ...] = ()

    @classmethod
    def make(cls, kind: str, /, **kwargs: Any) -> "Action":
        details = kwargs.pop("details", ())
        if isinstance(details, dict):
            details = tuple(sorted((str(k), str(v)) for k, v in details.items()))
        return cls(kind=kind, details=tuple(details), **kwargs)


@dataclass(frozen=True, slots=True)
class EffectSpec:
    """A single prepared description line and its declared semantics."""

    effect_id: str
    card: CardVariantRef
    ordinal: int
    normalized_text: str
    raw_text: str
    colors: tuple[tuple[str, str], ...]
    events: tuple[str, ...]
    unique: bool
    mechanism: str | None
    conditions: tuple[Condition, ...] = ()
    actions: tuple[Action, ...] = ()
    extraction: ExtractionKind = "opaque"

    @classmethod
    def make(
        cls,
        *,
        card: CardVariantRef,
        ordinal: int,
        normalized_text: str,
        raw_text: str,
        colors: tuple[tuple[str, str], ...],
        events: tuple[str, ...],
        unique: bool,
        mechanism: str | None,
        conditions: tuple[Condition, ...] = (),
        actions: tuple[Action, ...] = (),
        extraction: ExtractionKind = "opaque",
    ) -> "EffectSpec":
        # Deliberately exclude raw markup, colours, ordinal, source and dataset from
        # semantic identity.  Those are provenance rather than effect semantics.
        semantic = {
            "card": card.canonical_id,
            "text": normalized_text,
            "events": events,
            "unique": unique,
            "mechanism": mechanism,
            "conditions": [asdict(c) for c in conditions],
            "actions": [asdict(a) for a in actions],
        }
        return cls(
            effect_id=stable_id("effect", semantic), card=card, ordinal=ordinal,
            normalized_text=normalized_text, raw_text=raw_text, colors=colors,
            events=events, unique=unique, mechanism=mechanism,
            conditions=conditions, actions=actions, extraction=extraction,
        )


@dataclass(frozen=True, slots=True)
class ExtractionIssue:
    card: CardVariantRef
    ordinal: int
    normalized_text: str
    reason: str


@dataclass(frozen=True, slots=True)
class ExtractionReport:
    total_descriptions: int = 0
    automatic: int = 0
    template: int = 0
    override: int = 0
    partial: int = 0
    opaque: int = 0
    passive: int = 0
    no_execution_handler: int = 0
    issues: tuple[ExtractionIssue, ...] = ()

    @property
    def executable_descriptions(self) -> int:
        return self.total_descriptions - self.passive - self.no_execution_handler

    @property
    def represented_handlers(self) -> int:
        return self.automatic + self.template + self.override + self.partial + self.opaque

    @property
    def handler_coverage_rate(self) -> float:
        """Executable descriptions represented by an auditable graph node."""
        eligible = self.executable_descriptions
        return self.represented_handlers / eligible if eligible else 1.0

    @property
    def semantic_rate(self) -> float:
        """Fully declarative semantics; partial/opaque handlers are excluded."""
        handled = self.automatic + self.template + self.override
        eligible = self.executable_descriptions
        return handled / eligible if eligible else 1.0

    @property
    def actionable_rate(self) -> float:
        """Handlers with at least conservative actions usable for graph queries."""
        eligible = self.executable_descriptions
        actionable = self.automatic + self.template + self.override + self.partial
        return actionable / eligible if eligible else 1.0

    @property
    def has_gaps(self) -> bool:
        return self.partial > 0 or self.opaque > 0

    def summary(self) -> str:
        return (
            f"handler图谱覆盖率: {self.handler_coverage_rate:.1%}; "
            f"完整语义覆盖率: {self.semantic_rate:.1%}; "
            f"可查询覆盖率: {self.actionable_rate:.1%} "
            f"(automatic={self.automatic}, template={self.template}, "
            f"override={self.override}, partial={self.partial}, opaque={self.opaque}; "
            f"passive={self.passive}, no_handler={self.no_execution_handler})"
        )


class SemanticCoverageError(ValueError):
    """Raised only when strict semantic extraction encounters opaque executable text."""


__all__ = [
    "Action", "CardRef", "CardVariantRef", "Condition", "EffectSpec",
    "ExtractionIssue", "ExtractionReport", "SemanticCoverageError", "stable_id",
]
