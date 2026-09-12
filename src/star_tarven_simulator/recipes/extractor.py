"""Extract immutable mechanism facts from prepared card descriptions.

The extractor has two deliberately separate coverage levels:

* fully declarative standard grammar/overrides;
* conservative ``partial`` facts for every remaining executable handler.

A partial fact always preserves the runtime event binding and an auditable
``execute_handler`` action.  Additional high-confidence actions are exposed for
queries, but partial facts are never presented as complete semantics.
"""

from __future__ import annotations

from collections.abc import Iterable
import re
from typing import Any

from star_tarven_simulator.cards import is_passive, resolve
from star_tarven_simulator.cards.parametric import detect_trigger
from star_tarven_simulator.parsing.parser import prepared_lines
from star_tarven_simulator.parsing.text import extract_colors, extract_units
from star_tarven_simulator.simulator.event_handler import GatheringActionHandler, TaskActionHandler

from .effect_ir import (
    Action, CardRef, CardVariantRef, Condition, EffectSpec, ExtractionIssue,
    ExtractionReport, SemanticCoverageError,
)
from .overrides import resolve_override

_SIMPLE_ACTION = re.compile(r"^(获得|折跃|注卵|孵化)(.+)$")
_TARGETED_ACTION = re.compile(
    r"^(?P<target>此卡牌|相应卡牌|相邻两侧(?:人族|神族|虫族)?卡牌|"
    r"相邻左侧(?:人族|神族|虫族)?卡牌|相邻右侧(?:人族|神族|虫族)?卡牌)?"
    r"(?P<verb>获得|折跃|注卵|孵化)(?P<rest>.+)$"
)
_GATHERING = re.compile(r"^集结\((\d+)\):(.+)$")
_TASK_REWARD = re.compile(r"(?:^| )奖励:(.+)$")
_FEED = re.compile(r"^供养\((\d+)\):(.+)$")


def _event_names(resolved: Any) -> tuple[tuple[str, ...], Any] | None:
    if resolved is None:
        return None
    events, handler = resolved
    if not isinstance(events, list):
        events = [events]
    return tuple(sorted(str(event.value if hasattr(event, "value") else event) for event in events)), handler


def _emit_for(kind: str) -> str | None:
    return {
        "teleport": "any_card_teleport",
        "larva": "any_card_larva",
        "hatch": "any_card_hatch",
    }.get(kind)


def _pure_unit_actions(
    body: str, *, times_scaled: bool = False, target_scope: str = "self"
) -> tuple[Action, ...]:
    """Recognise only pure standard unit lists; never infer mixed prose."""
    match = _SIMPLE_ACTION.match(body)
    if not match:
        return ()
    verb, rest = match.groups()
    units = extract_units(rest)
    if not units:
        if verb == "获得" and rest.strip() == "卵鞘":
            return (Action.make("modify_attribute", object="拥有卵鞘", quantity=1),)
        return ()
    remaining = rest
    for count, unit in units:
        remaining = re.sub(rf"{count}\s*{re.escape(unit)}", "", remaining, count=1)
    if re.sub(r"[和,、\s]", "", remaining):
        return ()
    kind = {"获得": "produce", "折跃": "teleport", "注卵": "larva", "孵化": "hatch"}[verb]
    details = {"quantity_relation": "trigger_times"} if times_scaled else {}
    return tuple(
        Action.make(
            kind, target_scope=target_scope, object=unit, quantity=count,
            emits_event=_emit_for(kind), details=details,
        )
        for count, unit in units
    )


def _targeted_unit_actions(body: str, *, times_scaled: bool = False) -> tuple[Action, ...]:
    match = _TARGETED_ACTION.match(body)
    if not match:
        return ()
    scope = {
        None: "self",
        "此卡牌": "self",
        "相应卡牌": "event_source",
        "相邻两侧卡牌": "neighbors",
        "相邻两侧人族卡牌": "terran_neighbors",
        "相邻两侧神族卡牌": "protoss_neighbors",
        "相邻两侧虫族卡牌": "zerg_neighbors",
        "相邻左侧卡牌": "left",
        "相邻左侧人族卡牌": "left_terran",
        "相邻左侧神族卡牌": "left_protoss",
        "相邻左侧虫族卡牌": "left_zerg",
        "相邻右侧卡牌": "right",
        "相邻右侧人族卡牌": "right_terran",
        "相邻右侧神族卡牌": "right_protoss",
        "相邻右侧虫族卡牌": "right_zerg",
    }.get(match.group("target"), "self")
    return _pure_unit_actions(
        match.group("verb") + match.group("rest"),
        times_scaled=times_scaled,
        target_scope=scope,
    )


def _scope_before(text: str, position: int) -> str:
    prefix = text[max(0, position - 18):position]
    if "相应卡牌" in prefix or "为其" in prefix:
        return "event_source"
    if "相邻两侧" in prefix:
        return "neighbors"
    if "相邻左侧" in prefix:
        return "left"
    if "相邻右侧" in prefix or "右侧" in prefix:
        return "right"
    if "每张卡牌" in prefix or "所有卡牌" in prefix or "场上" in prefix:
        return "board"
    return "self"


def _embedded_unit_actions(text: str) -> tuple[Action, ...]:
    """Extract conservative action candidates from mixed prose.

    These facts improve provider/event-chain queries, but callers can see the
    enclosing EffectSpec is ``partial`` and must not treat filters as complete.
    """
    actions: list[Action] = []
    for match in re.finditer(r"(获得|添加|折跃|注卵|孵化)([^,;；]+)", text):
        verb, rest = match.groups()
        units = extract_units(rest)
        if not units:
            continue
        kind = {
            "获得": "produce", "添加": "produce", "折跃": "teleport",
            "注卵": "larva", "孵化": "hatch",
        }[verb]
        for quantity, unit in units:
            actions.append(Action.make(
                kind, target_scope=_scope_before(text, match.start()), object=unit,
                quantity=quantity, random="随机" in rest,
                emits_event=_emit_for(kind), details={"confidence": "partial"},
            ))
    return tuple(dict.fromkeys(actions))


def _partial_structure(text: str) -> tuple[tuple[Condition, ...], tuple[Action, ...], str | None]:
    """Conservative typed hints for irregular handlers."""
    conditions: list[Condition] = []
    actions: list[Action] = list(_embedded_unit_actions(text))
    mechanism: str | None = None

    if "若" in text or "如果" in text or "至少" in text or "每有" in text:
        conditions.append(Condition.make(
            "text_condition", value=text, scope="board" if "场上" in text else "self",
            details={"confidence": "partial"},
        ))
    if "相邻" in text:
        conditions.append(Condition.make(
            "neighbor_filter", value="required", scope="neighbors",
            details={"confidence": "partial"},
        ))
    if "挂件" in text:
        mechanism = "addon"
        if "变为" in text or "改变" in text:
            actions.append(Action.make(
                "modify_attribute", target_scope=_scope_before(text, text.find("挂件")),
                object="addon_type", quantity=1, emits_event="any_card_addon_changed",
                details={"confidence": "partial"},
            ))
    if "升级" in text and ("获得" in text or "添加" in text):
        mechanism = mechanism or "upgrade"
        actions.append(Action.make(
            "add_upgrade", object="upgrade_from_text", quantity=1,
            details={"expression": text, "confidence": "partial"},
        ))
    if "发现" in text:
        mechanism = mechanism or "discover"
        amount = re.search(r"发现(\d+)张", text)
        actions.append(Action.make(
            "discover_card", object="card_filter_from_text",
            quantity=int(amount.group(1)) if amount else 1,
            random=True, details={"expression": text, "confidence": "partial"},
        ))
    if "变为" in text or "精英化" in text or "感染" in text:
        mechanism = mechanism or "transform"
        amount = re.search(r"(?:将|精英化其)(\d+)", text)
        actions.append(Action.make(
            "transform_units", target_scope="board" if "每张" in text or "场上" in text else _scope_before(text, text.find("变")),
            object="unit_filter_from_text", quantity=int(amount.group(1)) if amount else "expression",
            consumes_input=True, details={"expression": text, "confidence": "partial"},
        ))
    if "摧毁" in text and "卡牌" in text:
        actions.append(Action.make(
            "destroy_card", target_scope="board" if "所有" in text else "expression",
            object="card_filter_from_text", quantity="expression",
            details={"expression": text, "confidence": "partial"},
        ))
    if "夺取" in text:
        actions.append(Action.make(
            "seize_card", target_scope="self", object="card_filter_from_text",
            quantity="expression", random="随机" in text,
            details={"expression": text, "confidence": "partial"},
        ))
    if "刷新你的酒馆" in text:
        actions.append(Action.make("emit_event", target_scope="tavern", object="refresh", quantity=1, emits_event="refresh"))

    # This action is the explicit audit marker that semantics remain incomplete.
    actions.append(Action.make(
        "execute_handler", target_scope="runtime", object="normalized_description",
        quantity=1, details={"text": text, "semantic_status": "partial"},
    ))
    return tuple(dict.fromkeys(conditions)), tuple(dict.fromkeys(actions)), mechanism


def _automatic_facts(
    text: str, handler: Any
) -> tuple[tuple[Condition, ...], tuple[Action, ...], str | None, str]:
    """Return conditions/actions/mechanism/classification for reliable grammar."""
    semantic_text = text.removeprefix("唯一:")
    if semantic_text.startswith("快速生产:"):
        actions = _targeted_unit_actions(semantic_text.removeprefix("快速生产:"))
        if actions:
            return (), actions, "quick_produce", "automatic"

    reactor = re.match(r"^反应堆生产(.+)$", semantic_text)
    if reactor:
        units = [unit for _, unit in extract_units("1" + reactor.group(1))]
        if units:
            return (), tuple(Action.make(
                "produce", object=unit, quantity=1,
                details={"gold_bonus": "1", "trigger": "round_end"},
            ) for unit in units), "reactor", "automatic"

    swarm = re.match(r"^集群\((\d+)\):(.+)$", semantic_text)
    if swarm:
        actions = _targeted_unit_actions(swarm.group(2))
        condition = Condition.make(
            "race_count_threshold", value=int(swarm.group(1)), scope="board",
            aggregation="count", details={"race": "zerg", "narud_bonus": "true"},
        )
        if actions:
            return (condition,), actions, "swarm", "automatic"

    if semantic_text.startswith("灵能:"):
        body = semantic_text.removeprefix("灵能:")
        actions = _targeted_unit_actions(body)
        condition = Condition.make(
            "psi_below_max", operator="<", value="psi_level_max",
            scope="board", aggregation="max",
            details={"self_attribute": "psi_level"},
        )
        if actions:
            return (condition,), actions, "psi", "automatic"
        partial_conditions, partial_actions, _ = _partial_structure(body)
        return (
            tuple(dict.fromkeys((condition, *partial_conditions))),
            partial_actions, "psi", "partial",
        )

    diversity = re.match(
        r"^每回合结束时,若场上有(\d+)个种族的卡牌,则(获得.+)$",
        semantic_text,
    )
    if diversity:
        actions = _targeted_unit_actions(diversity.group(2))
        if actions:
            return (
                Condition.make(
                    "race_diversity_threshold", value=int(diversity.group(1)),
                    scope="board", aggregation="distinct_count",
                ),
            ), actions, "race_diversity", "automatic"

    feed = _FEED.match(semantic_text)
    if feed:
        return (
            Condition.make("unit_count_threshold", value=int(feed.group(1)), scope="self", aggregation="floor_div", details={"unit": "精华"}),
        ), (
            Action.make("move_units", target_scope="right", object=feed.group(2), quantity=f"精华//{feed.group(1)}", details={"input_unit": "精华"}),
        ), "feed", "automatic"

    if isinstance(handler, GatheringActionHandler):
        match = _GATHERING.match(semantic_text)
        body = match.group(2) if match else ""
        actions = _targeted_unit_actions(body, times_scaled=True)
        condition = Condition.make(
            "energy_threshold", value=handler.cost, scope="closed_neighborhood",
            aggregation="sum", details={"cap": "2", "artanis_bonus": "true", "consumes_energy": "false"},
        )
        if actions:
            return (condition,), actions, "gathering", "automatic"
        partial_conditions, partial_actions, mechanism = _partial_structure(semantic_text)
        return tuple(dict.fromkeys((condition, *partial_conditions))), partial_actions, mechanism or "gathering", "partial"

    if isinstance(handler, TaskActionHandler):
        reward = _TASK_REWARD.search(semantic_text)
        actions = _targeted_unit_actions(reward.group(1) if reward else "")
        condition = Condition.make(
            "event_count_threshold", value=handler.goal, scope="self",
            details={"auto_reset": str(handler.auto_reset).lower()},
        )
        completion = Action.make("emit_event", target_scope="board", object="any_task_finished", quantity=1, emits_event="any_task_finished")
        if actions:
            return (condition,), (*actions, completion), "task", "automatic"
        _, partial_actions, _ = _partial_structure(reward.group(1) if reward else semantic_text)
        return (condition,), (*partial_actions, completion), "task", "partial"

    trigger = detect_trigger(semantic_text)
    body = trigger[1] if trigger else semantic_text
    actions = _targeted_unit_actions(body)
    if actions:
        return (), actions, None, "automatic"

    conditions, partial_actions, mechanism = _partial_structure(semantic_text)
    return conditions, partial_actions, mechanism, "partial"


def _merge_unique(*groups: tuple[Any, ...]) -> tuple[Any, ...]:
    result: list[Any] = []
    for group in groups:
        for item in group:
            if item not in result:
                result.append(item)
    return tuple(result)


def _extract_variant(card: Any, variant: str, dataset_id: str | None):
    ref = CardVariantRef(CardRef.from_card(card, dataset_id), variant)  # type: ignore[arg-type]
    descriptions = card.description if variant == "normal" else card.gold_description
    for ordinal, (text, raw) in enumerate(prepared_lines(descriptions)):
        if not text:
            continue
        colors = tuple(extract_colors(raw))
        override = resolve_override(card, variant, text)
        if is_passive(text) and override is None:
            yield None, "passive", None
            continue
        validated = _event_names(resolve(text, colors))
        if validated is None and override is None:
            yield None, "no_handler", ExtractionIssue(ref, ordinal, text, "no execution handler")
            continue
        events, handler = validated if validated is not None else ((), None)
        base_conditions, base_actions, base_mechanism, base_extraction = _automatic_facts(text, handler)
        if override is not None:
            override_conditions, override_actions, override_mechanism = override
            # Overrides supplement wrapper metadata (Gathering cost / Task goal)
            # rather than erasing it. Drop the generic audit marker because the
            # controlled override supplies the missing action semantics.
            useful_base = tuple(action for action in base_actions if action.kind != "execute_handler")
            conditions = _merge_unique(base_conditions, override_conditions)
            actions = _merge_unique(useful_base, override_actions)
            mechanism = override_mechanism or base_mechanism
            extraction = "override"
        else:
            conditions, actions, mechanism, extraction = (
                base_conditions, base_actions, base_mechanism, base_extraction
            )
        spec = EffectSpec.make(
            card=ref, ordinal=ordinal, normalized_text=text, raw_text=raw, colors=colors,
            events=events, unique=text.startswith("唯一:"), mechanism=mechanism,
            conditions=conditions, actions=actions, extraction=extraction,  # type: ignore[arg-type]
        )
        issue = None
        if extraction in {"partial", "opaque"}:
            issue = ExtractionIssue(
                ref, ordinal, text,
                "handler is graph-represented but declarative semantics are partial"
                if extraction == "partial" else "executable effect has no reliable declarative semantics",
            )
        yield spec, extraction, issue


def extract_card_effects(
    card: Any, *, dataset_id: str | None = None, strict: bool = True
) -> tuple[EffectSpec, ...]:
    """Extract normal and gold effects from one card.

    Strict mode rejects both partial and opaque executable effects. Non-strict
    mode still represents every executable handler for event/graph auditing.
    """
    specs: list[EffectSpec] = []
    issues: list[ExtractionIssue] = []
    for variant in ("normal", "gold"):
        for spec, extraction, issue in _extract_variant(card, variant, dataset_id):
            if spec is not None:
                specs.append(spec)
            if extraction in {"partial", "opaque"} and issue is not None:
                issues.append(issue)
    if strict and issues:
        raise SemanticCoverageError("; ".join(
            f"{issue.card.card.name}[{issue.card.variant}]: {issue.normalized_text}"
            for issue in issues
        ))
    return tuple(specs)


def extract_effects(
    cards: Iterable[Any], *, dataset_id: str | None = None, strict: bool = True
) -> tuple[tuple[EffectSpec, ...], ExtractionReport]:
    """Extract all variants and return a coverage audit without mutating cards."""
    specs: list[EffectSpec] = []
    counts = {
        "automatic": 0, "template": 0, "override": 0, "partial": 0,
        "opaque": 0, "passive": 0, "no_handler": 0,
    }
    issues: list[ExtractionIssue] = []
    for card in cards:
        for variant in ("normal", "gold"):
            for spec, extraction, issue in _extract_variant(card, variant, dataset_id):
                counts[extraction] += 1
                if spec is not None:
                    specs.append(spec)
                if issue is not None:
                    issues.append(issue)
    report = ExtractionReport(
        total_descriptions=sum(counts.values()), automatic=counts["automatic"],
        template=counts["template"], override=counts["override"], partial=counts["partial"],
        opaque=counts["opaque"], passive=counts["passive"],
        no_execution_handler=counts["no_handler"], issues=tuple(issues),
    )
    if strict and report.has_gaps:
        raise SemanticCoverageError(report.summary())
    return tuple(sorted(specs, key=lambda spec: (
        spec.card.canonical_id, spec.ordinal, spec.effect_id
    ))), report


__all__ = ["extract_card_effects", "extract_effects"]
