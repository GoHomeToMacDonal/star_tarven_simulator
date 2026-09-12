#!/usr/bin/env python3
"""Assemble Star Tavern card JSON from an extracted SC2 map.

The map's Galaxy script is treated as an ordered registration program.  The
extractor follows reachable card-pack functions, tracks the current star level,
and preserves diagnostics whenever runtime-only Galaxy DSL or dependency data
cannot be reconstructed offline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import uuid as uuid_module
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

try:
    from .paths import (
        DEFAULT_CARD_OUTPUT,
        DEFAULT_EXTRACTED,
        DEFAULT_OVERRIDES,
        DEFAULT_UNIT_INFO,
    )
except ImportError:  # Allows running this module directly as a script.
    from paths import (  # type: ignore[no-redef]
        DEFAULT_CARD_OUTPUT,
        DEFAULT_EXTRACTED,
        DEFAULT_OVERRIDES,
        DEFAULT_UNIT_INFO,
    )

DEFAULT_OUTPUT = DEFAULT_CARD_OUTPUT
ADD_CARD = "gf_AddCardModule"
APPEND_UNIT = "gf_E4B8BAE5889AE6898DE79A84E6A8A1E69DBFE8A1A5E58585E9A29DE5A496E58D95E4BD8D"
APPEND_DECORATION = "gf_E4B8BAE5889AE6898DE79A84E58DA1E7898CE8AEBEE5AE9AE8A385E9A5B0E58D95E4BD8D"
SPECIAL_CARDS = "gf_SpecialCardsInit"
LEVEL_VARIABLE = "lv_e6989FE7BAA7"
UUID_NAMESPACE = uuid_module.UUID("f01eb459-ea22-5a3c-acef-4af0dc1c9352")
RACES = {
    "gv__Terran": ("terran", "人族"),
    "gv__Zerg": ("zerg", "虫族"),
    "gv__Protoss": ("protoss", "神族"),
    "gv__Void": ("neutral", "中立"),
}
SPECIAL_QUICK_PRODUCTION = "gf_E5AD90E789B9E69588E5AD97E7ACA6E4B8B2E7AE80E69893E5BFABE9809FE7949FE4BAA7"
SPECIAL_REACTOR_PRODUCTION = "gf_E5AD90E789B9E69588E5AD97E7ACA6E4B8B2E58F8DE5BA94E5A086E7949FE4BAA7"
SPECIAL_UNIT_REWARD = "gf_E5A596E58AB1E69588E69E9CE58D95E4BD8D"
SPECIAL_DEPLOYMENT = "gf_E5AD90E789B9E69588E5AD97E7ACA6E4B8B2E5AE9AE782B9E983A8E7BDB2"
SPECIAL_DARK_CONTAINER = "gf_E5AD90E789B9E69588E5AD97E7ACA6E4B8B2E9BB91E69A97E5AEB9E599A8"
SPECIAL_NO_AUTO_DESCRIPTION = "gf_E5AD90E789B9E69588E5AD97E7ACA6E4B8B2E4B880E888ACE4B88DE887AAE58AA8E7949FE68890E68F8FE8BFB0"


class AssembleError(RuntimeError):
    """A user-facing assembly error."""


@dataclass(frozen=True)
class Function:
    name: str
    body: str
    body_offset: int


@dataclass
class Pack:
    function: str
    name: str
    validity: str
    normal: bool | None
    source_line: int


@dataclass
class RawCard:
    args: list[str]
    pack: Pack
    level: int | None
    source_function: str
    source_line: int
    extra_units: list[tuple[str, int]] = field(default_factory=list)
    decorations: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class UnitCatalogEntry:
    parent: str | None
    costs: dict[str, float]


@dataclass
class SpecialRender:
    text: str
    complete: bool
    fragments: list[str] = field(default_factory=list)
    reasons: list[dict[str, str]] = field(default_factory=list)


def mask_comments(source: str) -> str:
    """Replace comments with spaces while preserving strings, offsets and lines."""
    result = list(source)
    i = 0
    in_string = False
    escaped = False
    while i < len(source):
        char = source[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            i += 1
            continue
        if char == '"':
            in_string = True
            i += 1
            continue
        if source.startswith("//", i):
            end = source.find("\n", i)
            if end < 0:
                end = len(source)
            for position in range(i, end):
                result[position] = " "
            i = end
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            end = len(source) if end < 0 else end + 2
            for position in range(i, end):
                if result[position] not in "\r\n":
                    result[position] = " "
            i = end
            continue
        i += 1
    return "".join(result)


def matching_delimiter(source: str, start: int, opening: str, closing: str) -> int:
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(source)):
        char = source[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return i
    raise AssembleError(f"Unbalanced {opening}{closing} at offset {start}")


def delimiter_pairs(source: str) -> dict[int, int]:
    """Build opening-to-closing delimiter indexes in one linear pass."""
    pairs: dict[int, int] = {}
    stack: list[tuple[str, int]] = []
    expected = {")": "(", "]": "[", "}": "{"}
    in_string = False
    escaped = False
    for position, char in enumerate(source):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "([{":
            stack.append((char, position))
        elif char in ")]}" and stack and stack[-1][0] == expected[char]:
            _, opening = stack.pop()
            pairs[opening] = position
    return pairs


def split_arguments(arguments: str) -> list[str]:
    parts: list[str] = []
    start = 0
    stack: list[str] = []
    in_string = False
    escaped = False
    pairs = {")": "(", "]": "[", "}": "{"}
    for i, char in enumerate(arguments):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "([{":
            stack.append(char)
        elif char in ")]}" and stack and stack[-1] == pairs[char]:
            stack.pop()
        elif char == "," and not stack:
            parts.append(arguments[start:i].strip())
            start = i + 1
    tail = arguments[start:].strip()
    if tail or parts:
        parts.append(tail)
    return parts


def decode_galaxy_string(value: str) -> str | None:
    value = value.strip()
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return None
    output: list[str] = []
    i = 1
    escapes = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
    while i < len(value) - 1:
        if value[i] == "\\" and i + 1 < len(value) - 1:
            output.append(escapes.get(value[i + 1], value[i + 1]))
            i += 2
        else:
            output.append(value[i])
            i += 1
    return "".join(output)


def strip_outer_parentheses(value: str) -> str:
    value = value.strip()
    while value.startswith("("):
        try:
            end = matching_delimiter(value, 0, "(", ")")
        except AssembleError:
            break
        if end != len(value) - 1:
            break
        value = value[1:-1].strip()
    return value


def full_call(expression: str, expected: str | None = None) -> tuple[str, list[str]] | None:
    expression = strip_outer_parentheses(expression)
    match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", expression)
    if not match or (expected is not None and match.group(1) != expected):
        return None
    opening = expression.find("(", match.start())
    try:
        closing = matching_delimiter(expression, opening, "(", ")")
    except AssembleError:
        return None
    if expression[closing + 1 :].strip():
        return None
    return match.group(1), split_arguments(expression[opening + 1 : closing])


def top_level_plus(expression: str) -> list[str]:
    expression = strip_outer_parentheses(expression)
    parts: list[str] = []
    start = 0
    stack: list[str] = []
    in_string = False
    escaped = False
    pairs = {")": "(", "]": "[", "}": "{"}
    for i, char in enumerate(expression):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "([{":
            stack.append(char)
        elif char in ")]}" and stack and stack[-1] == pairs[char]:
            stack.pop()
        elif char == "+" and not stack:
            parts.append(expression[start:i].strip())
            start = i + 1
    if parts:
        parts.append(expression[start:].strip())
    return parts


def extract_functions(source: str, masked: str) -> dict[str, Function]:
    functions: dict[str, Function] = {}
    pairs = delimiter_pairs(masked)
    pattern = re.compile(r"\b(?:void|bool|int|fixed|text|string|trigger)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")
    for match in pattern.finditer(masked):
        opening = masked.find("(", match.start())
        parameter_end = pairs.get(opening)
        if parameter_end is None:
            continue
        cursor = parameter_end + 1
        while cursor < len(masked) and masked[cursor].isspace():
            cursor += 1
        if cursor >= len(masked) or masked[cursor] != "{":
            continue
        body_end = pairs.get(cursor)
        if body_end is None:
            continue
        functions[match.group(1)] = Function(
            name=match.group(1),
            body=source[cursor + 1 : body_end],
            body_offset=cursor + 1,
        )
    return functions


def iter_named_calls(source: str, names: set[str] | None = None) -> Iterable[tuple[int, str, list[str]]]:
    pattern = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
    for match in pattern.finditer(source):
        name = match.group(1)
        if names is not None and name not in names:
            continue
        opening = source.find("(", match.start())
        try:
            closing = matching_delimiter(source, opening, "(", ")")
        except AssembleError:
            continue
        yield match.start(), name, split_arguments(source[opening + 1 : closing])


def load_localization(locale_dir: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not locale_dir.is_dir():
        raise AssembleError(f"Localization directory does not exist: {locale_dir}")
    for path in sorted(locale_dir.glob("*.txt")):
        for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            if "=" not in raw_line or raw_line.lstrip().startswith("//"):
                continue
            key, value = raw_line.split("=", 1)
            values[key.strip()] = value
    return values


def load_overrides(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"units": {}, "cards": {}}
    if not path.is_file():
        raise AssembleError(f"Override file does not exist: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AssembleError("Override file root must be an object")
    data.setdefault("units", {})
    data.setdefault("cards", {})
    return data


def load_unit_info(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    if not path.is_file():
        raise AssembleError(f"Unit info file does not exist: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise AssembleError("Unit info file root must be an object")
    units = data.get("units")
    if not isinstance(units, dict):
        raise AssembleError("Unit info file must contain a units object")

    normalized: dict[str, dict[str, Any]] = {}
    for unit_id, unit_info in units.items():
        if not isinstance(unit_id, str) or not unit_id:
            raise AssembleError("Unit info IDs must be non-empty strings")
        if not isinstance(unit_info, dict):
            raise AssembleError(f"Unit info for {unit_id!r} must be an object")
        name = unit_info.get("name")
        if not isinstance(name, str) or not name.strip():
            raise AssembleError(f"Unit info for {unit_id!r} must have a non-empty name")
        value = unit_info.get("value")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
            raise AssembleError(f"Unit info value for {unit_id!r} must be a number or null")
        normalized[unit_id] = {"name": name, "value": value}
    return normalized


def merge_unit_info(overrides: dict[str, Any], unit_info: dict[str, dict[str, Any]]) -> dict[str, Any]:
    override_units = overrides.get("units")
    if not isinstance(override_units, dict):
        raise AssembleError("Override file units must be an object")
    merged_units = {unit_id: dict(values) for unit_id, values in unit_info.items()}
    for unit_id, values in override_units.items():
        if not isinstance(values, dict):
            raise AssembleError(f"Override unit {unit_id!r} must be an object")
        merged_units[unit_id] = {**merged_units.get(unit_id, {}), **values}
    return {**overrides, "units": merged_units}


def parse_unit_catalog(path: Path) -> dict[str, UnitCatalogEntry]:
    if not path.is_file():
        raise AssembleError(f"Unit catalog does not exist: {path}")
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as error:
        raise AssembleError(f"Cannot parse unit catalog: {error}") from error
    entries: dict[str, UnitCatalogEntry] = {}
    for node in root.iter("CUnit"):
        unit_id = node.get("id")
        if not unit_id:
            continue
        previous = entries.get(unit_id)
        parent = node.get("parent") or (previous.parent if previous else None)
        costs = dict(previous.costs) if previous else {}
        for cost in node.findall("CostResource"):
            index = cost.get("index")
            value = cost.get("value")
            if index and value is not None:
                try:
                    costs[index] = float(value)
                except ValueError:
                    pass
        entries[unit_id] = UnitCatalogEntry(parent=parent, costs=costs)
    return entries


def resolved_costs(unit_id: str, catalog: dict[str, UnitCatalogEntry], stack: set[str] | None = None) -> dict[str, float] | None:
    entry = catalog.get(unit_id)
    if entry is None:
        return None
    stack = set() if stack is None else stack
    if unit_id in stack:
        return None
    stack.add(unit_id)
    inherited = resolved_costs(entry.parent, catalog, stack) if entry.parent else {}
    costs = dict(inherited or {})
    costs.update(entry.costs)
    # UnitData.xml is an override layer. A catalog entry with no explicit or
    # locally inherited CostResource data does not prove that the unit is free.
    if not costs:
        return None
    return costs


def expression_key(expression: str) -> str | None:
    match = re.search(r'Param/Value/([0-9A-Fa-f]+)', expression)
    return match.group(1).upper() if match else None


def resolve_text(expression: str, localization: dict[str, str], tokens: dict[str, dict[str, str]]) -> tuple[str | None, bool]:
    expression = strip_outer_parentheses(expression)
    if expression == "null":
        return None, False
    literal = decode_galaxy_string(expression)
    if literal is not None:
        return literal, True
    plus = top_level_plus(expression)
    if plus:
        resolved: list[str] = []
        complete = True
        for part in plus:
            value, part_complete = resolve_text(part, localization, tokens)
            if value is None:
                complete = False
            else:
                resolved.append(value)
            complete = complete and part_complete
        return "".join(resolved) if resolved else None, complete
    call = full_call(expression)
    if call is None:
        return None, False
    name, args = call
    if name == "StringExternal" and args:
        key = decode_galaxy_string(args[0])
        return (localization.get(key), key in localization) if key else (None, False)
    if name in {"StringToText", "TextToString"} and args:
        return resolve_text(args[0], localization, tokens)
    if name == "TextExpressionAssemble" and args:
        key = decode_galaxy_string(args[0])
        if not key or key not in localization:
            return None, False
        value = localization[key]
        complete = True
        for token in re.findall(r"~([A-Za-z0-9_]+)~", value):
            replacement = tokens.get(key, {}).get(token)
            if replacement is None:
                complete = False
            else:
                value = value.replace(f"~{token}~", replacement)
        return value, complete
    return None, False


def parse_bool(value: str) -> bool | None:
    value = strip_outer_parentheses(value)
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def parse_int_literal(value: str) -> int | None:
    value = strip_outer_parentheses(value)
    return int(value) if re.fullmatch(r"-?\d+", value) else None


def parse_fixed_literal(value: str) -> float | None:
    value = strip_outer_parentheses(value)
    return float(value) if re.fullmatch(r"-?\d+(?:\.\d+)?", value) else None


def resolve_unit_display_name(
    expression: str,
    localization: dict[str, str],
    overrides: dict[str, Any],
) -> tuple[str | None, bool, str | None]:
    expression = strip_outer_parentheses(expression)
    if expression == "null":
        return None, True, None
    unit_id = decode_galaxy_string(expression)
    if unit_id is None:
        return None, False, None
    unit_override = overrides["units"].get(unit_id, {})
    display_name = unit_override.get("name") or localization.get(f"Unit/Name/{unit_id}")
    return (display_name or unit_id), display_name is not None, unit_id


def special_reason(reason: str, expression: str, callee: str | None = None) -> dict[str, str]:
    result = {"reason": reason, "expression": expression.strip()}
    if callee:
        result["callee"] = callee
    return result


def render_unit_reward(
    expression: str,
    args: list[str],
    upgraded: bool,
    localization: dict[str, str],
    overrides: dict[str, Any],
) -> SpecialRender:
    if len(args) != 8:
        return SpecialRender("", False, reasons=[special_reason("unexpected_argument_count", expression, SPECIAL_UNIT_REWARD)])
    multiplier = parse_fixed_literal(args[6])
    method = strip_outer_parentheses(args[7])
    if multiplier is None or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", method):
        return SpecialRender("", False, reasons=[special_reason("non_literal_reward_parameter", expression, SPECIAL_UNIT_REWARD)])

    rendered_units: list[str] = []
    reasons: list[dict[str, str]] = []
    for unit_index, count_index in ((0, 1), (2, 3), (4, 5)):
        name, name_complete, unit_id = resolve_unit_display_name(args[unit_index], localization, overrides)
        count = parse_int_literal(args[count_index])
        if count is None:
            reasons.append(special_reason("non_literal_unit_count", args[count_index], SPECIAL_UNIT_REWARD))
            continue
        if name is None:
            if strip_outer_parentheses(args[unit_index]) != "null":
                reasons.append(special_reason("non_literal_unit", args[unit_index], SPECIAL_UNIT_REWARD))
            continue
        if not name_complete and unit_id:
            reasons.append(special_reason("unresolved_unit_name", unit_id, SPECIAL_UNIT_REWARD))
        rendered_count = int(count * multiplier) if upgraded else count
        if rendered_count:
            rendered_units.append(f"{rendered_count}{name}")

    if not rendered_units:
        reasons.append(special_reason("no_renderable_units", expression, SPECIAL_UNIT_REWARD))
        return SpecialRender("", False, reasons=reasons)
    text = "获得" + "和".join(rendered_units)
    # The game may color individual special units through a runtime keyword table.
    # Keep this useful text as a diagnostic fragment, but never claim it is exact.
    reasons.append(special_reason("runtime_unit_color_unknown", expression, SPECIAL_UNIT_REWARD))
    return SpecialRender(text, False, [text], reasons)


def collect_nested_special_fragments(
    expression: str,
    upgraded: bool,
    localization: dict[str, str],
    overrides: dict[str, Any],
) -> SpecialRender:
    expression = strip_outer_parentheses(expression)
    plus = top_level_plus(expression)
    if plus:
        results = [collect_nested_special_fragments(part, upgraded, localization, overrides) for part in plus]
        own_reasons: list[dict[str, str]] = []
        complete = all(result.complete for result in results)
    else:
        call = full_call(expression)
        if call is None:
            return SpecialRender("", True)
        name, args = call
        if name == SPECIAL_NO_AUTO_DESCRIPTION:
            return SpecialRender("", True)
        if name in {
            SPECIAL_QUICK_PRODUCTION,
            SPECIAL_REACTOR_PRODUCTION,
            SPECIAL_UNIT_REWARD,
            SPECIAL_DEPLOYMENT,
            SPECIAL_DARK_CONTAINER,
        }:
            return render_special_node(expression, upgraded, localization, overrides)
        results = [collect_nested_special_fragments(arg, upgraded, localization, overrides) for arg in args]
        own_reasons = [special_reason("unsupported_constructor", expression, name)]
        complete = False
    return SpecialRender(
        "".join(result.text for result in results),
        complete,
        [fragment for result in results for fragment in result.fragments],
        own_reasons + [reason for result in results for reason in result.reasons],
    )


def render_special_node(
    expression: str,
    upgraded: bool,
    localization: dict[str, str],
    overrides: dict[str, Any],
) -> SpecialRender:
    expression = strip_outer_parentheses(expression)
    literal = decode_galaxy_string(expression)
    if literal is not None:
        return SpecialRender(literal, True, [literal] if literal else [])
    if expression == "null":
        return SpecialRender("", False, reasons=[special_reason("null_special_string", expression)])
    plus = top_level_plus(expression)
    if plus:
        results = [render_special_node(part, upgraded, localization, overrides) for part in plus]
        return SpecialRender(
            "".join(result.text for result in results),
            all(result.complete for result in results),
            [fragment for result in results for fragment in result.fragments],
            [reason for result in results for reason in result.reasons],
        )

    call = full_call(expression)
    if call is None:
        return SpecialRender("", False, reasons=[special_reason("unsupported_expression", expression)])
    name, args = call
    if name == SPECIAL_NO_AUTO_DESCRIPTION:
        if len(args) == 4:
            return SpecialRender("", True)
        return SpecialRender("", False, reasons=[special_reason("unexpected_argument_count", expression, name)])
    if name == SPECIAL_DEPLOYMENT:
        if args:
            return SpecialRender("", False, reasons=[special_reason("unexpected_argument_count", expression, name)])
        text = '能够<c val="008080">定点部署</c>'
        return SpecialRender(text, True, [text])
    if name == SPECIAL_DARK_CONTAINER:
        if args:
            return SpecialRender("", False, reasons=[special_reason("unexpected_argument_count", expression, name)])
        text = '具有<c val="A65353">黑暗容器</c>'
        return SpecialRender(text, True, [text])
    if name == SPECIAL_QUICK_PRODUCTION:
        if len(args) != 4:
            return SpecialRender("", False, reasons=[special_reason("unexpected_argument_count", expression, name)])
        unit_name, unit_complete, unit_id = resolve_unit_display_name(args[0], localization, overrides)
        normal_count = parse_int_literal(args[1])
        gold_spec = parse_int_literal(args[2])
        round_up = parse_bool(args[3])
        if unit_name is None or normal_count is None or gold_spec is None or round_up is None or normal_count <= 0:
            return SpecialRender("", False, reasons=[special_reason("non_literal_quick_production_parameter", expression, name)])
        gold_count = normal_count if gold_spec == -1 else normal_count * 1.5 if gold_spec == -2 else gold_spec
        count = normal_count if not upgraded else math.ceil(gold_count) if round_up else int(gold_count)
        text = f'<c val="00FF00">快速生产</c>：获得{count}{unit_name}'
        reasons = [] if unit_complete else [special_reason("unresolved_unit_name", unit_id or args[0], name)]
        return SpecialRender(text, unit_complete, [text], reasons)
    if name == SPECIAL_REACTOR_PRODUCTION:
        if len(args) != 2:
            return SpecialRender("", False, reasons=[special_reason("unexpected_argument_count", expression, name)])
        first_name, first_complete, first_id = resolve_unit_display_name(args[0], localization, overrides)
        second_name, second_complete, second_id = resolve_unit_display_name(args[1], localization, overrides)
        if first_name is None:
            return SpecialRender("", False, reasons=[special_reason("missing_primary_unit", expression, name)])
        text = f'<c val="FF8000">反应堆</c>生产{first_name}'
        if second_name is not None:
            text += f"和{second_name}"
        reasons: list[dict[str, str]] = []
        if not first_complete:
            reasons.append(special_reason("unresolved_unit_name", first_id or args[0], name))
        if not second_complete:
            reasons.append(special_reason("unresolved_unit_name", second_id or args[1], name))
        return SpecialRender(text, first_complete and second_complete, [text], reasons)
    if name == SPECIAL_UNIT_REWARD:
        return render_unit_reward(expression, args, upgraded, localization, overrides)

    nested = collect_nested_special_fragments(expression, upgraded, localization, overrides)
    return SpecialRender(nested.text, False, nested.fragments, nested.reasons)


def render_special_expression(
    expression: str,
    upgraded: bool,
    localization: dict[str, str],
    overrides: dict[str, Any],
) -> SpecialRender:
    """Render only proven Galaxy specialString constructor patterns."""
    return render_special_node(expression, upgraded, localization, overrides)


def discover_packs(source: str, localization: dict[str, str]) -> list[Pack]:
    packs: list[Pack] = []
    for position, _, args in iter_named_calls(source, {"CardPackInit"}):
        if len(args) != 8:
            continue
        trigger = strip_outer_parentheses(args[0])
        if not re.fullmatch(r"gt_[A-Za-z0-9_]+", trigger):
            continue
        name, _ = resolve_text(args[1], localization, {})
        validity_value = parse_bool(args[5])
        validity = "enabled" if validity_value is True else "disabled" if validity_value is False else f"conditional:{args[5].strip()}"
        packs.append(
            Pack(
                function=f"{trigger}_Func",
                name=name or expression_key(args[1]) or trigger,
                validity=validity,
                normal=parse_bool(args[6]),
                source_line=source.count("\n", 0, position) + 1,
            )
        )
    return packs


def function_call_graph(functions: dict[str, Function]) -> tuple[dict[str, set[str]], set[str]]:
    known = set(functions)
    graph: dict[str, set[str]] = {}
    direct: set[str] = set()
    for name, function in functions.items():
        calls = {called for _, called, _ in iter_named_calls(function.body) if called in known and called != name}
        graph[name] = calls
        if any(called == ADD_CARD for _, called, _ in iter_named_calls(function.body, {ADD_CARD})):
            direct.add(name)
    can_add = set(direct)
    changed = True
    while changed:
        changed = False
        for name, calls in graph.items():
            if name not in can_add and calls & can_add:
                can_add.add(name)
                changed = True
    return graph, can_add


def collect_cards(
    source: str,
    functions: dict[str, Function],
    pack: Pack,
    can_add: set[str],
    localization: dict[str, str],
) -> list[RawCard]:
    cards: list[RawCard] = []
    active_stack: set[str] = set()

    def execute(function_name: str) -> None:
        if function_name in active_stack:
            return
        function = functions.get(function_name)
        if function is None:
            return
        active_stack.add(function_name)
        events: list[tuple[int, str, Any]] = []
        for match in re.finditer(rf"\b{re.escape(LEVEL_VARIABLE)}\s*=\s*(-?\d+)\s*;", function.body):
            events.append((match.start(), "level", int(match.group(1))))
        relevant_calls = {ADD_CARD, APPEND_UNIT, APPEND_DECORATION, "TextExpressionSetToken"} | can_add
        for position, name, args in iter_named_calls(function.body, relevant_calls):
            events.append((position, "call", (name, args)))
        events.sort(key=lambda event: (event[0], 0 if event[1] == "level" else 1))
        level: int | None = None
        tokens: dict[str, dict[str, str]] = {}
        last_card: RawCard | None = None
        for position, event_type, payload in events:
            if event_type == "level":
                level = payload
                continue
            name, args = payload
            if name == ADD_CARD and len(args) == 16:
                star = strip_outer_parentheses(args[15])
                if re.fullmatch(r"-?\d+", star):
                    card_level = int(star)
                elif star == LEVEL_VARIABLE:
                    card_level = level
                else:
                    card_level = None
                last_card = RawCard(
                    args=args,
                    pack=pack,
                    level=card_level,
                    source_function=function_name,
                    source_line=source.count("\n", 0, function.body_offset + position) + 1,
                )
                setattr(last_card, "tokens", {key: dict(value) for key, value in tokens.items()})
                cards.append(last_card)
            elif name == APPEND_UNIT and len(args) >= 2 and last_card is not None:
                unit_id = decode_galaxy_string(strip_outer_parentheses(args[0]))
                count = strip_outer_parentheses(args[1])
                if unit_id is not None and re.fullmatch(r"\d+", count):
                    last_card.extra_units.append((unit_id, int(count)))
            elif name == APPEND_DECORATION and len(args) >= 4 and last_card is not None:
                last_card.decorations.append({"unit_id": decode_galaxy_string(args[0]), "count": args[1], "x": args[2], "y": args[3]})
            elif name == "TextExpressionSetToken" and len(args) >= 3:
                expression_id = decode_galaxy_string(args[0])
                token = decode_galaxy_string(args[1])
                value, complete = resolve_text(args[2], localization, tokens)
                if expression_id and token and complete and value is not None:
                    tokens.setdefault(expression_id, {})[token] = value
            elif name in can_add and name != ADD_CARD:
                execute(name)
                if cards:
                    last_card = cards[-1]
        active_stack.remove(function_name)

    execute(pack.function)
    return cards


def unit_pairs(card: RawCard) -> list[tuple[str, int]]:
    pairs: list[tuple[str, int]] = []
    for unit_index, count_index in ((4, 5), (6, 7), (8, 9)):
        unit_id = decode_galaxy_string(strip_outer_parentheses(card.args[unit_index]))
        count_value = strip_outer_parentheses(card.args[count_index])
        if unit_id and re.fullmatch(r"\d+", count_value) and int(count_value) > 0:
            pairs.append((unit_id, int(count_value)))
    pairs.extend(card.extra_units)
    return pairs


def split_description(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in re.split(r"<n\s*/?>|\r?\n", value) if part.strip()]


def card_uuid(strategy: str, order: int, name_key: str, pack_id: str) -> int | str:
    if strategy == "order":
        return order
    if strategy == "name-key":
        return name_key
    return str(uuid_module.uuid5(UUID_NAMESPACE, f"{pack_id}:{name_key}"))


def order_uuid_values(raw_cards: list[RawCard], overrides: dict[str, Any]) -> tuple[list[int], int]:
    """Apply explicit external numeric IDs while preserving global uniqueness.

    An overridden ID swaps with its current registration-order owner. This keeps
    the numeric domain contiguous without inventing duplicate identifiers.
    """
    values = list(range(1, len(raw_cards) + 1))
    overridden = 0
    claimed: set[int] = set()
    for index, raw in enumerate(raw_cards):
        name_key = expression_key(raw.args[0])
        requested = overrides["cards"].get(name_key or "", {}).get("uuid")
        if requested is None:
            continue
        if not isinstance(requested, int) or isinstance(requested, bool):
            raise AssembleError(f"Card override UUID for {name_key} must be an integer")
        if requested < 1 or requested > len(raw_cards):
            raise AssembleError(f"Card override UUID for {name_key} is outside 1..{len(raw_cards)}")
        if requested in claimed:
            raise AssembleError(f"Duplicate overridden card UUID: {requested}")
        claimed.add(requested)
        owner = values.index(requested)
        values[owner], values[index] = values[index], values[owner]
        overridden += 1
    return values, overridden


def assemble_card(
    raw: RawCard,
    order: int,
    uuid_strategy: str,
    localization: dict[str, str],
    catalog: dict[str, UnitCatalogEntry],
    overrides: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    name_key = expression_key(raw.args[0]) or f"line-{raw.source_line}"
    name, name_complete = resolve_text(raw.args[0], localization, getattr(raw, "tokens", {}))
    race_key, race_suffix = RACES.get(strip_outer_parentheses(raw.args[3]), (strip_outer_parentheses(raw.args[3]), strip_outer_parentheses(raw.args[3])))
    card_override = overrides["cards"].get(name_key, {})
    unresolved: list[str] = []
    if not name_complete or name is None:
        name = name or name_key
        unresolved.append("name")

    raw_normal_is_null = strip_outer_parentheses(raw.args[13]) == "null"
    raw_gold_is_null = strip_outer_parentheses(raw.args[14]) == "null"
    normal_dsl: SpecialRender | None = None
    gold_dsl: SpecialRender | None = None

    if "description" in card_override:
        descriptions = list(card_override["description"])
        normal_complete = True
        normal_source = "override"
    elif not raw_normal_is_null:
        normal_text, normal_complete = resolve_text(raw.args[13], localization, getattr(raw, "tokens", {}))
        descriptions = split_description(normal_text)
        normal_source = "explicit"
    else:
        normal_dsl = render_special_expression(raw.args[10], False, localization, overrides)
        descriptions = split_description(normal_dsl.text) if normal_dsl.complete else []
        normal_complete = normal_dsl.complete
        normal_source = "dsl_fallback"

    if "gold_description" in card_override:
        gold_descriptions = list(card_override["gold_description"])
        gold_complete = True
        gold_source = "override"
    elif not raw_gold_is_null:
        gold_text, gold_complete = resolve_text(raw.args[14], localization, getattr(raw, "tokens", {}))
        gold_descriptions = split_description(gold_text)
        gold_source = "explicit"
    elif not raw_normal_is_null:
        gold_descriptions = list(descriptions)
        gold_complete = normal_complete
        gold_source = "normal_copy"
    else:
        gold_dsl = render_special_expression(raw.args[10], True, localization, overrides)
        gold_descriptions = split_description(gold_dsl.text) if gold_dsl.complete else []
        gold_complete = gold_dsl.complete
        gold_source = "dsl_fallback"

    if not normal_complete:
        unresolved.append("description")
    if not gold_complete:
        unresolved.append("gold_description")

    units: Counter[str] = Counter()
    unit_ids: Counter[str] = Counter()
    unresolved_names: list[str] = []
    unresolved_costs: list[str] = []
    price = 0.0
    for unit_id, count in unit_pairs(raw):
        unit_ids[unit_id] += count
        unit_override = overrides["units"].get(unit_id, {})
        display_name, name_resolved, _ = resolve_unit_display_name(json.dumps(unit_id), localization, overrides)
        if not name_resolved:
            unresolved_names.append(unit_id)
        units[display_name or unit_id] += count
        override_value = unit_override.get("value")
        if override_value is not None:
            value = float(override_value)
        else:
            costs = resolved_costs(unit_id, catalog)
            if costs is None:
                unresolved_costs.append(unit_id)
                continue
            value = costs.get("Minerals", 0.0) + costs.get("Vespene", 0.0)
        price += count * value
    if unresolved_names:
        unresolved.append("unit_names")
    if unresolved_costs:
        unresolved.append("price")
        price_value: float | None = None
    else:
        price_value = float(price)

    tags = [race_key]
    for tag in card_override.get("tags", []):
        if tag not in tags:
            tags.append(tag)
    if "黑暗容器" in "".join(descriptions) and "具有黑暗容器" not in tags:
        tags.append("具有黑暗容器")
    source_name = f"{raw.pack.name}{race_suffix}"
    result = {
        "name": name,
        "level": raw.level,
        "description": descriptions,
        "gold_description": gold_descriptions,
        "gold_tags": list(tags),
        "units": dict(units),
        "uuid": card_uuid(uuid_strategy, order, name_key, raw.pack.function),
        "race": race_key,
        "price": price_value,
        "tags": tags,
        "source": [source_name],
    }
    diagnostics = {
        "uuid": result["uuid"],
        "name": name,
        "name_key": name_key,
        "pack": raw.pack.name,
        "pack_validity": raw.pack.validity,
        "source_function": raw.source_function,
        "source_line": raw.source_line,
        "unresolved_fields": sorted(set(unresolved)),
        "unit_ids": dict(unit_ids),
        "unresolved_unit_names": sorted(set(unresolved_names)),
        "unresolved_unit_costs": sorted(set(unresolved_costs)),
        "raw_special_string": raw.args[10],
        "raw_description": raw.args[13],
        "raw_gold_description": raw.args[14],
        "description_source": normal_source,
        "gold_description_source": gold_source,
        "dsl_description_fragments": normal_dsl.fragments if normal_dsl else [],
        "dsl_gold_description_fragments": gold_dsl.fragments if gold_dsl else [],
        "dsl_description_reasons": normal_dsl.reasons if normal_dsl else [],
        "dsl_gold_description_reasons": gold_dsl.reasons if gold_dsl else [],
        "unresolved_dsl_helpers": sorted({
            reason["callee"]
            for render in (normal_dsl, gold_dsl)
            if render is not None
            for reason in render.reasons
            if reason.get("reason") == "unsupported_constructor" and "callee" in reason
        }),
        "dsl_complete": {
            "description": normal_dsl.complete if normal_dsl else None,
            "gold_description": gold_dsl.complete if gold_dsl else None,
        },
        "decorations": raw.decorations,
    }
    return result, diagnostics


REFERENCE_IDENTITY_FIELDS = ("level", "race", "source", "units")


def load_reference_cards(path: Path | None) -> dict[str, dict[str, Any]]:
    """Load prior card output as a name index for conservative field fallback."""
    if path is None:
        return {}
    if not path.is_file():
        raise AssembleError(f"Reference card file does not exist: {path}")

    def reject_non_finite_constant(value: str) -> None:
        raise AssembleError(f"Reference card file contains non-finite JSON value: {value}")

    def parse_finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise AssembleError(f"Reference card file contains non-finite JSON number: {value}")
        return parsed

    data = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=reject_non_finite_constant,
        parse_float=parse_finite_float,
    )
    if not isinstance(data, list):
        raise AssembleError("Reference card file root must be an array")

    cards: dict[str, dict[str, Any]] = {}
    for index, card in enumerate(data):
        if not isinstance(card, dict):
            raise AssembleError(f"Reference card at index {index} must be an object")
        name = card.get("name")
        if not isinstance(name, str) or not name:
            raise AssembleError(f"Reference card at index {index} must have a non-empty name")
        if name in cards:
            raise AssembleError(f"Reference card names must be unique; duplicate: {name}")
        for field_name in ("description", "gold_description", "tags", "gold_tags"):
            value = card.get(field_name)
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise AssembleError(f"Reference card {name!r} field {field_name!r} must be a string array")
        price = card.get("price")
        if price is not None and (
            isinstance(price, bool)
            or not isinstance(price, (int, float))
            or not math.isfinite(price)
        ):
            raise AssembleError(f"Reference card {name!r} field 'price' must be a finite number or null")
        cards[name] = card
    return cards


def normalized_reference_text(value: str) -> str:
    return re.sub(r"\s+", "", value)


def reference_contains_fragments(lines: list[str], fragments: list[str]) -> bool:
    reference_text = normalized_reference_text("".join(lines))
    normalized_fragments = [normalized_reference_text(fragment) for fragment in fragments]
    return bool(normalized_fragments) and all(
        fragment and fragment in reference_text
        for fragment in normalized_fragments
    )


def apply_reference_fallback(
    card: dict[str, Any],
    diagnostic: dict[str, Any],
    reference_cards: dict[str, dict[str, Any]],
) -> None:
    """Fill evidenced unresolved descriptions after strict structural matching."""
    provenance: dict[str, Any] = {
        "status": "no_name_match",
        "matched_name": card["name"],
        "filled_fields": [],
        "rejected_fields": {},
    }
    diagnostic["reference_fallback"] = provenance
    if not diagnostic["unresolved_fields"]:
        provenance["status"] = "skipped_complete"
        return

    reference = reference_cards.get(card["name"])
    if reference is None:
        return

    mismatches = [field_name for field_name in REFERENCE_IDENTITY_FIELDS if card.get(field_name) != reference.get(field_name)]
    if mismatches:
        provenance["status"] = "identity_mismatch"
        provenance["mismatched_fields"] = mismatches
        return

    provenance["status"] = "strict_match"
    provenance["reference_uuid"] = reference.get("uuid")
    unresolved = set(diagnostic["unresolved_fields"])
    description_fields = (
        ("description", "dsl_description_fragments", "description_source"),
        ("gold_description", "dsl_gold_description_fragments", "gold_description_source"),
    )
    for field_name, fragment_field, source_field in description_fields:
        if field_name not in unresolved:
            continue
        reference_lines = reference[field_name]
        if not reference_lines:
            provenance["rejected_fields"][field_name] = "reference_empty"
            continue
        fragments = diagnostic[fragment_field]
        normalized_fragments = [normalized_reference_text(fragment) for fragment in fragments]
        if not normalized_fragments or any(not fragment for fragment in normalized_fragments):
            provenance["rejected_fields"][field_name] = "dsl_fragments_empty"
            continue
        if not reference_contains_fragments(reference_lines, fragments):
            provenance["rejected_fields"][field_name] = "dsl_fragment_mismatch"
            continue
        card[field_name] = list(reference_lines)
        diagnostic[source_field] = "reference"
        provenance["filled_fields"].append(field_name)
        unresolved.remove(field_name)

    if "price" in unresolved:
        provenance["rejected_fields"]["price"] = "historical_price_disabled"

    diagnostic["unresolved_fields"] = sorted(unresolved)
    if provenance["filled_fields"]:
        provenance["status"] = "filled"


def write_json(path: Path, value: Any) -> None:
    try:
        serialized = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    except ValueError as error:
        raise AssembleError(f"Cannot serialize non-finite JSON value for {path}: {error}") from error
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(serialized, encoding="utf-8")
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Assemble Star Tavern cards from extracted SC2 resources.")
    parser.add_argument("extracted", nargs="?", type=Path, default=Path(DEFAULT_EXTRACTED), help="Extracted map directory")
    parser.add_argument("--output", type=Path, default=Path(DEFAULT_OUTPUT), help="Output card JSON (an array)")
    parser.add_argument("--diagnostics", type=Path, help="Diagnostic JSON (default: <output-stem>_diagnostics.json)")
    parser.add_argument("--overrides", type=Path, default=Path(DEFAULT_OVERRIDES), help="Auditable dependency/DSL override JSON")
    parser.add_argument("--unit-info", type=Path, default=Path(DEFAULT_UNIT_INFO), help="Supplemental unit names and values JSON")
    parser.add_argument("--reference", type=Path, help="Prior card JSON used only to fill unresolved fields after strict identity matching")
    parser.add_argument("--locale", default="zhCN", help="SC2 locale directory prefix (default: zhCN)")
    parser.add_argument("--uuid-strategy", choices=("order", "name-key", "uuid5"), default="order")
    parser.add_argument("--include-disabled", action="store_true", help="Include packs registered with valid=false")
    parser.add_argument("--include-conditional", action="store_true", help="Include all candidate calls from runtime-conditional packs (not a single game-mode card pool)")
    parser.add_argument("--include-special", action="store_true", help="Include auxiliary cards created outside normal card packs")
    parser.add_argument("--json", action="store_true", help="Print the generation summary as JSON")
    return parser


def run(args: argparse.Namespace) -> int:
    extracted = args.extracted.expanduser().resolve()
    script_path = extracted / "scripts" / "MapScript.galaxy"
    locale_dir = extracted / "text" / f"{args.locale}.SC2Data" / "LocalizedData"
    catalog_path = extracted / "text" / "Base.SC2Data" / "GameData" / "UnitData.xml"
    if not script_path.is_file():
        raise AssembleError(f"Galaxy script does not exist: {script_path}")
    source = script_path.read_text(encoding="utf-8-sig", errors="replace")
    masked = mask_comments(source)
    localization = load_localization(locale_dir)
    override_path = args.overrides.expanduser().resolve() if args.overrides else None
    unit_info_path = args.unit_info.expanduser().resolve() if args.unit_info else None
    reference_path = args.reference.expanduser().resolve() if args.reference else None
    unit_info = load_unit_info(unit_info_path)
    overrides = merge_unit_info(load_overrides(override_path), unit_info)
    reference_cards = load_reference_cards(reference_path)
    reference_sha256 = (
        hashlib.sha256(reference_path.read_bytes()).hexdigest()
        if reference_path is not None
        else None
    )
    catalog = parse_unit_catalog(catalog_path)
    functions = extract_functions(source, masked)
    _, can_add = function_call_graph(functions)
    packs = discover_packs(source, localization)

    raw_cards: list[RawCard] = []
    processed_packs: list[Pack] = []
    for pack in packs:
        if pack.validity == "disabled" and not args.include_disabled:
            continue
        if pack.validity.startswith("conditional:") and not args.include_conditional:
            continue
        processed_packs.append(pack)
        raw_cards.extend(collect_cards(source, functions, pack, can_add, localization))
    if args.include_special:
        core_pack = next((pack for pack in packs if pack.name == "核心"), None)
        if core_pack is None:
            raise AssembleError("Cannot identify the core pack for special cards")
        raw_cards.extend(collect_cards(source, functions, Pack(SPECIAL_CARDS, core_pack.name, "special", False, 0), can_add, localization))

    uuid_orders, overridden_uuid_count = (
        order_uuid_values(raw_cards, overrides)
        if args.uuid_strategy == "order"
        else (list(range(1, len(raw_cards) + 1)), 0)
    )
    cards: list[dict[str, Any]] = []
    card_diagnostics: list[dict[str, Any]] = []
    for registration_order, (uuid_order, raw) in enumerate(zip(uuid_orders, raw_cards), 1):
        card, diagnostic = assemble_card(raw, uuid_order, args.uuid_strategy, localization, catalog, overrides)
        diagnostic["registration_order"] = registration_order
        diagnostic["uuid_source"] = (
            "external_override"
            if overrides["cards"].get(diagnostic["name_key"], {}).get("uuid") == card["uuid"]
            else "registration_order_or_swap"
        ) if args.uuid_strategy == "order" else args.uuid_strategy
        if reference_path is not None:
            apply_reference_fallback(card, diagnostic, reference_cards)
        cards.append(card)
        card_diagnostics.append(diagnostic)

    uuids = [card["uuid"] for card in cards]
    if len(set(uuids)) != len(uuids):
        duplicates = sorted(
            (value for value, count in Counter(uuids).items() if count > 1),
            key=str,
        )
        raise AssembleError(f"UUID strategy {args.uuid_strategy!r} produced duplicates: {duplicates}")

    output = args.output.expanduser().resolve()
    diagnostics_path = (
        args.diagnostics.expanduser().resolve()
        if args.diagnostics
        else output.with_name(f"{output.stem}_diagnostics.json")
    )
    unresolved_counts = Counter(field for item in card_diagnostics for field in item["unresolved_fields"])
    description_sources = Counter(item["description_source"] for item in card_diagnostics)
    gold_description_sources = Counter(item["gold_description_source"] for item in card_diagnostics)
    reference_statuses = Counter(
        item["reference_fallback"]["status"]
        for item in card_diagnostics
        if "reference_fallback" in item
    )
    reference_filled_fields = Counter(
        field_name
        for item in card_diagnostics
        for field_name in item.get("reference_fallback", {}).get("filled_fields", [])
    )
    dsl_fields = [
        (item, field_name, item["dsl_complete"][field_name])
        for item in card_diagnostics
        for field_name in ("description", "gold_description")
        if item["dsl_complete"][field_name] is not None
    ]
    dsl_complete_fields = sum(1 for _, _, complete in dsl_fields if complete)
    dsl_partial_fields = sum(
        1
        for item, field_name, complete in dsl_fields
        if not complete and item[f"dsl_{'gold_' if field_name == 'gold_description' else ''}description_fragments"]
    )
    dsl_failed_fields = len(dsl_fields) - dsl_complete_fields - dsl_partial_fields
    summary = {
        "source": str(script_path),
        "output": str(output),
        "diagnostics": str(diagnostics_path),
        "unit_info": str(unit_info_path) if unit_info_path else None,
        "reference": str(reference_path) if reference_path else None,
        "reference_sha256": reference_sha256,
        "reference_cards": len(reference_cards),
        "reference_status_counts": dict(sorted(reference_statuses.items())),
        "reference_filled_cards": sum(
            1 for item in card_diagnostics if item.get("reference_fallback", {}).get("filled_fields")
        ),
        "reference_filled_field_counts": dict(sorted(reference_filled_fields.items())),
        "supplemental_units": len(unit_info),
        "supplemental_units_with_value": sum(
            1 for values in unit_info.values() if values.get("value") is not None
        ),
        "uuid_strategy": args.uuid_strategy,
        "overridden_uuids": overridden_uuid_count,
        "source_add_card_calls": sum(
            1
            for _, _, call_args in iter_named_calls(masked, {ADD_CARD})
            if len(call_args) == 16 and not call_args[0].lstrip().startswith("text ")
        ),
        "discovered_packs": len(packs),
        "processed_packs": len(processed_packs),
        "cards": len(cards),
        "conditional_cards": sum(1 for item in card_diagnostics if str(item["pack_validity"]).startswith("conditional:")),
        "fully_resolved_cards": sum(1 for item in card_diagnostics if not item["unresolved_fields"]),
        "description_source_counts": dict(sorted(description_sources.items())),
        "gold_description_source_counts": dict(sorted(gold_description_sources.items())),
        "dsl_fallback_attempted_fields": len(dsl_fields),
        "dsl_fallback_complete_fields": dsl_complete_fields,
        "dsl_fallback_partial_fields": dsl_partial_fields,
        "dsl_fallback_failed_fields": dsl_failed_fields,
        "dsl_fallback_complete_cards": sum(
            1
            for item in card_diagnostics
            if any(value is not None for value in item["dsl_complete"].values())
            and all(value for value in item["dsl_complete"].values() if value is not None)
        ),
        "cards_with_dsl_fragments": sum(
            1
            for item in card_diagnostics
            if item["dsl_description_fragments"] or item["dsl_gold_description_fragments"]
        ),
        "unresolved_field_counts": dict(sorted(unresolved_counts.items())),
    }
    write_json(output, cards)
    write_json(diagnostics_path, {"summary": summary, "packs": [asdict(pack) for pack in packs], "cards": card_diagnostics})
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"Generated {len(cards)} cards: {output}")
        print(f"Diagnostics: {diagnostics_path}")
        print(f"Fully resolved: {summary['fully_resolved_cards']}/{len(cards)}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except (AssembleError, OSError, json.JSONDecodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
