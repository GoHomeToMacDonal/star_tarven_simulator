"""描述列表 -> EventHandler 模板。

流程（对 ``description`` 与 ``gold_description`` 各跑一遍）：

1. 逐行归一化，并把"任务:"行与紧邻的"奖励:"行合并成一条。
2. 被动 tag 声明（:func:`cards.is_passive`）跳过。
3. 用 :func:`cards.resolve` 解析为 ``(event_name, handler)``；``唯一:`` 前缀标记 ``unique``。
4. 解析不出的记入 ``unhandled``，供覆盖率报告使用。
"""

from __future__ import annotations

from typing import List, Tuple

from star_tarven_simulator.cards import is_passive, resolve
from star_tarven_simulator.parsing.text import extract_colors, normalize
from star_tarven_simulator.simulator.event_handler import EventHandler


def _pair_task_reward(lines: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """把 (normalized, raw) 列表里的 '任务:' 与紧邻 '奖励:' 合并。"""
    merged: List[Tuple[str, str]] = []
    i = 0
    while i < len(lines):
        norm, raw = lines[i]
        if (
            norm.startswith("任务:")
            and i + 1 < len(lines)
            and lines[i + 1][0].startswith("奖励:")
        ):
            next_norm, next_raw = lines[i + 1]
            merged.append((f"{norm} {next_norm}", f"{raw} {next_raw}"))
            i += 2
        else:
            merged.append((norm, raw))
            i += 1
    return merged


def _merge_continuations(lines: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """把括号说明等"续行"并入上一条子句。

    新版数据里，UI 说明（如 ``(F8可查看所有精英单位)``）常被切成独立 JSON 片段，
    它们以 ``(`` 开头，语义上属于上一条能力，应合并回去。
    """
    merged: List[Tuple[str, str]] = []
    for norm, raw in lines:
        if norm.startswith("(") and merged:
            prev_norm, prev_raw = merged[-1]
            merged[-1] = (prev_norm + norm, prev_raw + raw)
        else:
            merged.append((norm, raw))
    return merged


def prepared_lines(descriptions: List[str]) -> List[Tuple[str, str]]:
    """返回经过续行合并与任务/奖励配对后的 ``(normalized, raw)`` 行。"""
    lines = [(normalize(raw), raw) for raw in descriptions]
    lines = _merge_continuations(lines)
    lines = _pair_task_reward(lines)
    return lines


def parse_descriptions(descriptions: List[str]) -> Tuple[List[EventHandler], List[str]]:
    """返回 ``(event_handlers, unhandled_texts)``。"""
    lines = prepared_lines(descriptions)

    handlers: List[EventHandler] = []
    unhandled: List[str] = []

    for norm, raw in lines:
        if not norm:
            continue
        if is_passive(norm):
            continue

        colors = extract_colors(raw)
        resolved = resolve(norm, colors)
        if resolved is None:
            unhandled.append(norm)
            continue

        event_names, handler = resolved
        if not isinstance(event_names, list):
            event_names = [event_names]

        unique = norm.startswith("唯一:")
        for event_name in event_names:
            handlers.append(
                EventHandler(None, None, norm, handler, event_name, unique=unique)
            )

    return handlers, unhandled


def parse_card(card) -> Tuple[List[str], List[str]]:
    """就地填充 ``card.event_handlers`` / ``card.gold_event_handlers``。

    返回 ``(unhandled_normal, unhandled_gold)``。
    """
    card.event_handlers, unhandled_normal = parse_descriptions(card.description)
    card.gold_event_handlers, unhandled_gold = parse_descriptions(card.gold_description)
    return unhandled_normal, unhandled_gold
