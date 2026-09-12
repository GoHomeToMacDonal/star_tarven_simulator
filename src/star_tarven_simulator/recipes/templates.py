"""Recipe templates and immutable terminal-state models."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .effect_ir import CardVariantRef


@dataclass(frozen=True, slots=True)
class RecipeTemplate:
    id: str
    name: str
    core_requirements: tuple[str, ...]
    constraints: tuple[tuple[str, str], ...] = (("max_slots", "7"), ("active_expansions_only", "true"))
    optional_roles: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RecipeSlot:
    index: int
    card: CardVariantRef
    upgrades: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    total: float
    payoff: float
    threshold_completion: float
    slot_efficiency: float
    stability: float
    conflict_penalty: float = 0.0


@dataclass(frozen=True, slots=True)
class RecipeInstance:
    recipe_id: str
    template_id: str
    expansions: tuple[str, ...]
    slots: tuple[RecipeSlot, ...]
    extra_neighbors: tuple[tuple[int, int], ...]
    satisfied_factors: tuple[str, ...]
    unsatisfied_factors: tuple[str, ...]
    derived_metrics: tuple[tuple[str, str], ...]
    score: ScoreBreakdown
    provenance: tuple[str, ...]


PROTOSS_ENERGY_GATHERING = RecipeTemplate(
    id="protoss-energy-gathering",
    name="神族能量集结",
    core_requirements=("gathering_consumer", "stable_energy_supply"),
    optional_roles=("void_pylon_multiplier", "teleport_listener", "artanis_bonus"),
)

EVENT_FEEDBACK_ENGINE = RecipeTemplate(
    id="event-feedback-engine",
    name="事件反馈引擎",
    core_requirements=("event_emitter", "event_listener"),
    optional_roles=("second_listener", "stable_trigger"),
)

ZERG_SWARM_ENGINE = RecipeTemplate(
    id="zerg-swarm-engine",
    name="虫族集群终局",
    core_requirements=("swarm_consumer", "zerg_board_threshold"),
    optional_roles=("larva_emitter", "hatch_listener"),
)

UNIT_SUPPLY_ENGINE = RecipeTemplate(
    id="unit-supply-engine",
    name="单位供需链",
    core_requirements=("unit_consumer", "unit_provider"),
    optional_roles=("event_amplifier", "transform_payoff"),
)

DARKNESS_CAROUSEL = RecipeTemplate(
    id="darkness-carousel",
    name="刷牌黑暗值循环",
    core_requirements=("empty_cycle_slot", "two_darkness_broadcasters", "darkness_unit_listeners"),
    optional_roles=("gold_broadcast_multiplier", "extra_darkness_container"),
)

PSI_ASCENSION_ENGINE = RecipeTemplate(
    id="psi-ascension-engine",
    name="灵能精英化流水线",
    core_requirements=("lower_level_psi_producers", "higher_psi_anchor", "psi_board_elite"),
    optional_roles=("double_gathering_payload", "race_diversity_payoff"),
)

DEFAULT_TEMPLATES = (
    PROTOSS_ENERGY_GATHERING,
    EVENT_FEEDBACK_ENGINE,
    ZERG_SWARM_ENGINE,
    UNIT_SUPPLY_ENGINE,
    PSI_ASCENSION_ENGINE,
    DARKNESS_CAROUSEL,
)


__all__ = [
    "DARKNESS_CAROUSEL", "DEFAULT_TEMPLATES", "EVENT_FEEDBACK_ENGINE",
    "PROTOSS_ENERGY_GATHERING", "PSI_ASCENSION_ENGINE",
    "UNIT_SUPPLY_ENGINE", "ZERG_SWARM_ENGINE", "RecipeInstance", "RecipeSlot",
    "RecipeTemplate", "ScoreBreakdown",
]
