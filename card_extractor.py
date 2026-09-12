#!/usr/bin/env python3
"""Extract Star Tavern card data by running the map's own registration code.

``star_tavern_cards.py`` reconstructs cards by pattern-matching the Galaxy source,
which cannot reproduce descriptions: the map builds those at runtime from a flat
"special string".  This extractor instead interprets the map script
(:mod:`galaxy_interp`), lets ``gf_AddCardModule`` populate the real card template
table, and reads the finished templates back out.

Pipeline (mirrors the map's own boot order):
    1. ``InitGlobals``                       - global constants and defaults
    2. ``gf_初始化特殊词条``                   - the special-keyword table
    3. every ``gt_*_Init`` that calls ``CardPackInit`` - registers card packs
    4. per pack: ``gv_LoadingCardPack = n`` then run the pack trigger
    5. ``gf_初始化卡牌设计（补充）``            - sorting, special and support cards
    6. read ``gv_已设计的卡牌模板[1..n]``
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from galaxy_explore import decode_identifier, encode_identifier
from galaxy_interp import Array, GalaxyError, Interpreter, Program, Struct, Trigger
from paths import (
    DEFAULT_CARD_DIAGNOSTICS,
    DEFAULT_CARD_OUTPUT,
    DEFAULT_EXTRACTED,
    DEFAULT_OVERRIDES,
    DEFAULT_UNIT_INFO,
    REFERENCE_CARD_JSON,
)
from star_tavern_cards import (
    AssembleError,
    UnitCatalogEntry,
    load_localization,
    load_overrides,
    load_unit_info,
    merge_unit_info,
    parse_unit_catalog,
    resolved_costs,
    split_description,
)

# Map identifiers this extractor drives directly (written in readable form).
INIT_KEYWORDS = "gf_初始化特殊词条"
# Upgrade names are referenced by description text ("获得<轨道空降>升级").
INIT_UPGRADES = "gf_初始化卡牌升级"
INIT_SUPPLEMENT = "gf_初始化卡牌设计（补充）"
ADD_CARD = "gf_AddCardModule"
CARD_TEMPLATES = "gv_已设计的卡牌模板"
CARD_TEMPLATE_COUNT = "gv_已设计的卡牌模板数量"
CARD_TEMPLATE_EXTRA = "gv_已设计的卡牌模板的辅助信息"
REGULAR_TEMPLATE_END = "gv_常规模板索引结束"
PACK_TRIGGER_TABLE = "cardPackInitTriggers"
LOADING_PACK = "gv_LoadingCardPack"
PACK_COUNT = "gv_CardPackNumber"
NORMAL_PACK_END = "gv_NormalCardPackIndexEnd"
IDENTIFIER_SUPPORT_CARD = "ge_卡牌识别符_辅助卡"
IDENTIFIER_NO_POOL = "ge_卡牌识别符_无法进入卡池"
SUPPORT_RANGE_START = "gv_辅助卡模板索引开始"
NOVA_SUPPORT_COUNT = "gv_诺娃可以抽取前几张辅助卡"
KEYWORD_TABLE = "gv_所有设计的特殊词条"
KEYWORD_COUNT = "gv_已设计的特殊词条数量"
KEYWORD_NAME_FIELD = "lv_特殊功能名称"
KEYWORD_CODE_FIELD = "lv_特殊功能字符代号"
PACK_FIELD = "lv_卡牌所属拓展包"

# Lobby attributes the map reads while registering cards.  Attribute 4 is the
# game-mode switch: "0001" is 新手模式 (core pack only, no expansions), "0002" is
# the standard mode.  ``gf_核心中立初始化`` registers 虚空大军 and 黑暗预兆 only
# under "0002", so leaving the attribute unset silently drops two 核心中立 cards.
# This is the only attribute that gates a ``gf_AddCardModule`` call in v4.6.1.7.
STANDARD_MODE_ATTRIBUTES = {"[bnet:local/0.0/178523]4": "0002"}

RACE_GLOBALS = {
    "gv__Terran": ("terran", "人族"),
    "gv__Zerg": ("zerg", "虫族"),
    "gv__Protoss": ("protoss", "神族"),
    "gv__Void": ("neutral", "中立"),
}

# Cards registered after the regular packs carry these source labels instead of
# "<pack><race>", matching the curated snapshot in data/.
SOURCE_SUPPORT_CARD = "辅助卡"
SOURCE_SPECIAL = "特殊"
# Extra label for cards the map can never put in the shop / discover pool, so
# downstream code does not have to infer it from a star level or a name list.
# Three map facts produce it (see CardExtractor._pool_reachable):
#   * ge_卡牌识别符_无法进入卡池 - the map says so outright (不法之徒 / 母舰核心)
#   * 星级 > 6                  - above the highest tavern level (the four
#                                 "只用于沙盒模式测试" cards, incl. 挂件仓库)
#   * a 辅助卡 outside the discoverable window - only handed out by a specific
#                                 effect or hero skill, never discovered
#                                 (引导核弹 / 冷钱包 / 矿簇 / 战士的财宝 / 核弹天劫)
SOURCE_NO_POOL = "不进卡池"
# The tavern tops out at level 6; anything above it exists for sandbox testing.
MAX_TAVERN_LEVEL = 6
# The core pack is split per race ("核心人族"); expansions are not.
CORE_PACK_NAME = "核心"
# The contest-champion (特典卡) pack ships enabled and unfilterable, so its cards
# carry no expansion source (an empty list means "always in the pool" downstream).
# Its trigger shuffles the pack and enables exactly one card per game, stamping
# ge_卡牌识别符_无法进入卡池 on the other six - a per-game roll, not card data.
CHAMPION_PACK_NAME = "比赛冠军"
PACKS_WITHOUT_SOURCE = {CHAMPION_PACK_NAME}

# Tags describe card state, so they come from what the card actually says or is,
# not from the keyword icons on its left edge (those also flag mechanics such as
# 任务/集结, and the icon set disagrees with the card text on several cards).
#
# 1. the card subclass, for the two membership tags
SUBCLASS_TAGS = {
    "ge_卡牌子类_原始虫群": "属于原始虫群",
    "ge_卡牌子类_埃蒙": "具有虚空投影",
}
# 2. description lines that are nothing but a keyword statement
STANDALONE_TAGS = ("具有黑暗容器", "能够定点部署", "拥有卵鞘", "无法三连", "辅助卡")
# 3. keywords that prefix an effect line ("灵能：获得…"). 唯一 is deliberately not
#    tagged: it marks an effect, not a card state, and the curated snapshot only
#    ever tagged one card with it.
PREFIX_TAGS = ("灵能",)

COLOR_MARKUP = re.compile(r"</?c(?:\s+val=\"[^\"]*\")?>")


class ExtractError(RuntimeError):
    """A user-facing extraction failure."""


@dataclass
class Pack:
    index: int
    trigger: str
    name: str
    normal: bool
    size: int


@dataclass
class ExtractionDiagnostics:
    init_failures: list[str] = field(default_factory=list)
    engine_constants: list[str] = field(default_factory=list)
    missing_unit_names: list[str] = field(default_factory=list)
    pack_failures: dict[str, str] = field(default_factory=dict)
    # Packs CardPackInit rejected (valid=false): hard-disabled content plus the
    # team-mode / PvE packs, which only register in those game modes.
    skipped_packs: list[str] = field(default_factory=list)
    unresolved_unit_costs: dict[str, list[str]] = field(default_factory=dict)
    cards: list[dict[str, Any]] = field(default_factory=list)


def strip_color(value: str) -> str:
    return COLOR_MARKUP.sub("", value)


class CardExtractor:
    def __init__(
        self,
        extracted: Path,
        *,
        locale: str = "zhCN",
        unit_info_path: Path | None = None,
        overrides_path: Path | None = None,
    ):
        script = extracted / "scripts" / "MapScript.galaxy"
        locale_dir = extracted / "text" / f"{locale}.SC2Data" / "LocalizedData"
        catalog_path = extracted / "text" / "Base.SC2Data" / "GameData" / "UnitData.xml"
        if not script.is_file():
            raise ExtractError(f"Galaxy script does not exist: {script}")
        self.script_path = script
        self.localization = load_localization(locale_dir)
        self.catalog: dict[str, UnitCatalogEntry] = parse_unit_catalog(catalog_path)
        unit_info = load_unit_info(unit_info_path)
        self.overrides = merge_unit_info(load_overrides(overrides_path), unit_info)
        self.unit_values = {
            unit_id: values.get("value")
            for unit_id, values in self.overrides["units"].items()
        }
        unit_names = {
            unit_id: values["name"]
            for unit_id, values in self.overrides["units"].items()
            if values.get("name")
        }
        self.program = Program(script.read_text(encoding="utf-8-sig", errors="replace"))
        self.interpreter = Interpreter(
            self.program,
            localization=self.localization,
            unit_names=unit_names,
            unit_costs=lambda unit_id: resolved_costs(unit_id, self.catalog),
            game_attribute_values=STANDARD_MODE_ATTRIBUTES,
        )
        self.diagnostics = ExtractionDiagnostics()
        self.packs: list[Pack] = []
        self._install_hooks()

    # -- naming helpers ---------------------------------------------------
    def raw(self, readable_name: str) -> str:
        """Translate a readable identifier into the编译后 hex form."""
        encoded = encode_identifier(readable_name)
        if encoded in self.program.functions or encoded in self.program.globals:
            return encoded
        if encoded in self.program.constants:
            return encoded
        if readable_name in self.program.functions or readable_name in self.program.globals:
            return readable_name
        raise ExtractError(f"Map script has no identifier named {readable_name!r} ({encoded})")

    @staticmethod
    def field(struct: Struct, readable_name: str) -> Any:
        """Read a struct field written in readable form (fields are hex too)."""
        if readable_name in struct.fields:
            return struct.get(readable_name)
        return struct.get(encode_identifier(readable_name))

    def global_value(self, readable_name: str) -> Any:
        return self.interpreter.globals.values[self.raw(readable_name)]

    def set_global(self, readable_name: str, value: Any) -> None:
        self.interpreter.globals.values[self.raw(readable_name)] = value

    def call(self, readable_name: str, arguments: Sequence[Any] = ()) -> Any:
        return self.interpreter.call_function(self.raw(readable_name), list(arguments))

    # -- hooks ------------------------------------------------------------
    def _install_hooks(self) -> None:
        interpreter = self.interpreter

        def card_pack_init(
            trigger: Any,
            name: Any,
            icon35: Any,
            icon50: Any,
            size: Any,
            valid: Any,
            normal: Any,
            readme: Any,
        ) -> None:
            """Replicate CardPackInit without the dialog-building side effects."""
            if not interpreter.truthy(valid):
                self.diagnostics.skipped_packs.append(interpreter.as_text(name))
                return
            index = interpreter.as_int(self.global_value(PACK_COUNT)) + 1
            self.set_global(PACK_COUNT, index)
            table = self.global_value(PACK_TRIGGER_TABLE)
            if not isinstance(table, Array):
                raise ExtractError(f"{PACK_TRIGGER_TABLE} is not an array")
            table.set(index, trigger)
            if interpreter.truthy(normal):
                self.set_global(NORMAL_PACK_END, index)
            self.packs.append(
                Pack(
                    index=index,
                    trigger=trigger.name if isinstance(trigger, Trigger) else str(trigger),
                    name=interpreter.as_text(name),
                    normal=bool(interpreter.truthy(normal)),
                    size=interpreter.as_int(size),
                )
            )

        interpreter.hooks["CardPackInit"] = card_pack_init
        # Pack browser UI and per-pack sounds are irrelevant to card data.
        interpreter.hooks[self.raw("gf_CreateCardPack")] = lambda *_: None

        def trigger_execute(trigger: Any, *_: Any) -> None:
            if not isinstance(trigger, Trigger):
                raise ExtractError(f"TriggerExecute received {trigger!r}")
            self.interpreter.call_function(trigger.name, [True, True])

        interpreter.natives["TriggerExecute"] = trigger_execute

    # -- boot -------------------------------------------------------------
    def boot(self) -> None:
        self.diagnostics.init_failures = self.interpreter.run_global_initialization()
        # Trigger globals normally get their function through InitTriggers.
        for name, declaration in self.program.globals.items():
            if declaration.type_name != "trigger":
                continue
            function_name = f"{name}_Func"
            if function_name in self.program.functions:
                self.interpreter.globals.values[name] = Trigger(function_name)
        for initializer in (INIT_KEYWORDS, INIT_UPGRADES):
            try:
                self.call(initializer)
            except GalaxyError as error:
                self.diagnostics.init_failures.append(f"{initializer}: {type(error).__name__}: {error}")

    def register_packs(self) -> None:
        pattern = re.compile(r"\bCardPackInit\s*\(")
        for name, definition in self.program.functions.items():
            if not name.endswith("_Init") or not pattern.search(definition.body_source):
                continue
            try:
                self.interpreter.call_function(name, [])
            except GalaxyError as error:
                self.diagnostics.pack_failures[decode_identifier(name)] = f"{type(error).__name__}: {error}"
        if not self.packs:
            raise ExtractError("No card packs were registered")

    def load_cards(self) -> None:
        for pack in self.packs:
            self.set_global(LOADING_PACK, pack.index)
            try:
                self.interpreter.call_function(pack.trigger, [True, True])
            except GalaxyError as error:
                self.diagnostics.pack_failures[pack.name] = f"{type(error).__name__}: {error}"
        self.call(INIT_SUPPLEMENT)

    # -- reading templates ------------------------------------------------
    def keyword_letters(self) -> dict[str, str]:
        """Map keyword letter -> keyword name, for the diagnostics report."""
        table = self.global_value(KEYWORD_TABLE)
        count = self.interpreter.as_int(self.global_value(KEYWORD_COUNT))
        letters: dict[str, str] = {}
        for index in range(1, count + 1):
            entry = table.get(index)
            if not isinstance(entry, Struct):
                continue
            name = strip_color(self.interpreter.as_text(self.field(entry, KEYWORD_NAME_FIELD))).strip()
            code = self.interpreter.as_text(self.field(entry, KEYWORD_CODE_FIELD))
            if code:
                letters[code] = name
        return letters

    def card_tags(self, race_key: str, subclass: Any, lines: Sequence[str]) -> list[str]:
        tags = [race_key]
        for constant, tag in SUBCLASS_TAGS.items():
            if self.interpreter.globals.values.get(self.raw(constant)) == subclass and tag not in tags:
                tags.append(tag)
        for line in lines:
            plain = strip_color(line).strip()
            if plain in STANDALONE_TAGS and plain not in tags:
                tags.append(plain)
                continue
            keyword = plain.split("：", 1)[0]
            if keyword in PREFIX_TAGS and keyword != plain and keyword not in tags:
                tags.append(keyword)
        return tags

    def race_of(self, value: Any) -> tuple[str, str]:
        for global_name, race in RACE_GLOBALS.items():
            if self.interpreter.globals.values.get(global_name) == value:
                return race
        raise ExtractError(f"Unknown race value {value!r}")

    def unit_price(self, unit_id: str) -> float | None:
        override = self.unit_values.get(unit_id)
        if override is not None:
            return float(override)
        costs = resolved_costs(unit_id, self.catalog)
        if costs is None:
            return None
        return costs.get("Minerals", 0.0) + costs.get("Vespene", 0.0)

    def support_discover_window(self) -> range:
        """Template indices a generic "发现一张辅助卡" may return.

        ``gf_SpecialCardsInit`` brackets the discoverable support cards with
        ``gv_辅助卡模板索引开始``, and both draw sites (诺娃 / 无尽虫群) read
        ``开始 .. 开始 + gv_诺娃可以抽取前几张辅助卡 - 1``.  Support cards
        registered outside that window are handed out by name instead.

        These indices are safe to compare against: ``gf_初始化卡牌设计（补充）``
        sorts the regular templates *before* ``gf_SpecialCardsInit`` appends the
        support cards, so support indices are already final.  Ranges recorded
        during a pack trigger (比赛冠军 etc.) are not - the sort moves those.
        """
        start = self.interpreter.as_int(self.global_value(SUPPORT_RANGE_START))
        count = self.interpreter.as_int(self.global_value(NOVA_SUPPORT_COUNT))
        return range(start, start + count)

    def cards(self) -> list[dict[str, Any]]:
        interpreter = self.interpreter
        templates = self.global_value(CARD_TEMPLATES)
        extra = self.global_value(CARD_TEMPLATE_EXTRA)
        total = interpreter.as_int(self.global_value(CARD_TEMPLATE_COUNT))
        regular_end = interpreter.as_int(self.global_value(REGULAR_TEMPLATE_END))
        support_identifier = self.interpreter.globals.values[self.raw(IDENTIFIER_SUPPORT_CARD)]
        no_pool_identifier = self.interpreter.globals.values[self.raw(IDENTIFIER_NO_POOL)]
        support_window = self.support_discover_window()
        keyword_letters = self.keyword_letters()
        packs_by_index = {pack.index: pack for pack in self.packs}

        cards: list[dict[str, Any]] = []
        for index in range(1, total + 1):
            template = templates.get(index)
            if not isinstance(template, Struct):
                raise ExtractError(f"Card template {index} is missing")
            name = strip_color(interpreter.as_text(self.field(template, "lv_name"))).strip()
            race_key, race_suffix = self.race_of(self.field(template, "lv_race"))
            identifier = self.field(template, "lv_Identify")
            unit_total = interpreter.as_int(self.field(template, "lv_units"))
            unit_types = self.field(template, "lv_unitType")

            # Support cards ("辅助卡") are consumed on deployment and never fight;
            # their unit slots only hold the box/label props that draw the card.
            is_support_card = identifier == support_identifier
            unit_ids: Counter[str] = Counter()
            for slot in range(1, 0 if is_support_card else unit_total + 1):
                unit_id = unit_types.get(slot)
                if unit_id:
                    unit_ids[unit_id] += 1

            units: Counter[str] = Counter()
            price = 0.0
            unresolved_costs: list[str] = []
            for unit_id, count in unit_ids.items():
                display = interpreter.natives["UnitTypeGetName"](unit_id)
                units[strip_color(display)] += count
                value = self.unit_price(unit_id)
                if value is None:
                    unresolved_costs.append(unit_id)
                    continue
                price += count * value
            if unresolved_costs:
                self.diagnostics.unresolved_unit_costs[name] = sorted(unresolved_costs)

            sources: list[str] = []
            pack_name = ""
            if index > regular_end:
                sources = [SOURCE_SUPPORT_CARD if is_support_card else SOURCE_SPECIAL]
            else:
                pack_index = interpreter.as_int(self.field(extra.get(index), PACK_FIELD))
                pack = packs_by_index.get(pack_index)
                if pack is None:
                    sources = ["未知"]
                else:
                    pack_name = pack.name
                    if pack.name in PACKS_WITHOUT_SOURCE:
                        sources = []
                    elif pack.name == CORE_PACK_NAME:
                        sources = [f"{pack.name}{race_suffix}"]
                    else:
                        sources = [pack.name]

            level = interpreter.as_int(self.field(template, "lv_star"))
            # 比赛冠军 is excused from the identifier test: its trigger rolls one
            # card in and stamps 无法进入卡池 on the rest, so the flag we read is
            # this run's dice, not a property of the card.
            says_no_pool = identifier == no_pool_identifier and pack_name != CHAMPION_PACK_NAME
            if (
                says_no_pool
                or level > MAX_TAVERN_LEVEL
                or (is_support_card and index not in support_window)
            ):
                sources.append(SOURCE_NO_POOL)

            description = split_description(interpreter.as_text(self.field(template, "lv_description")))
            gold_description = split_description(interpreter.as_text(self.field(template, "lv_description2")))
            introduction = interpreter.as_text(self.field(template, "lv_introductionString"))
            tags = self.card_tags(race_key, self.field(template, "lv_Identify2"), [*description, *gold_description])
            cards.append(
                {
                    "name": name,
                    "level": level,
                    "description": description,
                    "gold_description": gold_description,
                    "gold_tags": list(tags),
                    "units": dict(units),
                    "uuid": index - 1,
                    "race": race_key,
                    "price": None if unresolved_costs or is_support_card else float(price),
                    "tags": tags,
                    "source": sources,
                }
            )
            self.diagnostics.cards.append(
                {
                    "uuid": index - 1,
                    "name": name,
                    "special_string": interpreter.as_text(self.field(template, "lv_specialString")),
                    "introduction_string": introduction,
                    "keywords": [keyword_letters.get(letter, letter) for letter in introduction],
                    "identifier": decode_identifier(self._identifier_name(identifier)),
                    "unit_ids": dict(unit_ids),
                    "unresolved_unit_costs": sorted(unresolved_costs),
                    # Sum over the units that do have a price, so a missing value
                    # can be derived from a known card total.
                    "priced_unit_total": float(price),
                    "source": sources,
                }
            )
        self.diagnostics.engine_constants = sorted(self.interpreter.engine_constants)
        self.diagnostics.missing_unit_names = sorted(self.interpreter.missing_unit_names)
        return cards

    def _identifier_name(self, value: Any) -> str:
        """Name the ge_卡牌识别符_* constant holding `value`, for diagnostics."""
        prefix = encode_identifier("ge_卡牌识别符_")
        for name in self.program.constants:
            if name.startswith(prefix) and self.interpreter.globals.values.get(name) == value:
                return name
        return str(value)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def write_json(path: Path, value: Any) -> None:
    serialized = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(serialized, encoding="utf-8")
    temporary.replace(path)


COMPARED_FIELDS = ("level", "race", "price", "units", "description", "gold_description", "tags", "source")
DIGITS = re.compile(r"\d+")


def wording_only(lines: Sequence[str], other: Sequence[str]) -> bool:
    """True when two description blocks differ only in colours and numbers.

    Those two axes move with every balance patch and palette tweak, so telling
    them apart from structural differences shows whether the renderer is right.
    """
    if len(lines) != len(other):
        return False
    return all(
        DIGITS.sub("#", strip_color(left)) == DIGITS.sub("#", strip_color(right))
        for left, right in zip(lines, other)
    )


def compare_with_reference(cards: list[dict[str, Any]], reference_path: Path) -> dict[str, Any]:
    """Diff against a curated snapshot. Informational only: nothing is merged."""
    reference = json.loads(reference_path.read_text(encoding="utf-8"))
    by_name = {card["name"]: card for card in reference}
    mismatches: dict[str, list[str]] = {}
    cosmetic = 0
    for card in cards:
        other = by_name.get(card["name"])
        if other is None:
            continue
        differing = [name for name in COMPARED_FIELDS if card[name] != other[name]]
        if not differing:
            continue
        mismatches[card["name"]] = differing
        if set(differing) <= {"description", "gold_description"} and all(
            wording_only(card[field], other[field]) for field in differing
        ):
            cosmetic += 1
    extracted_names = {card["name"] for card in cards}
    shared = len(extracted_names & set(by_name))
    return {
        "reference": str(reference_path),
        "reference_cards": len(reference),
        "extracted_cards": len(cards),
        "shared_cards": shared,
        "only_in_reference": sorted(set(by_name) - extracted_names),
        "only_in_extraction": sorted(extracted_names - set(by_name)),
        "field_mismatch_counts": dict(
            sorted(Counter(field for names in mismatches.values() for field in names).items())
        ),
        "cards_identical": shared - len(mismatches),
        "cards_differing_only_in_colours_and_numbers": cosmetic,
        # The snapshot numbers cards from a larger, older extraction (ids up to
        # 195 for 154 cards), so uuids are not comparable across map versions.
        "uuid_comparable": False,
        "mismatches": mismatches,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("extracted", nargs="?", type=Path, default=DEFAULT_EXTRACTED, help="Extracted map directory")
    parser.add_argument("--output", type=Path, default=DEFAULT_CARD_OUTPUT, help="Output card JSON array")
    parser.add_argument(
        "--diagnostics",
        type=Path,
        default=DEFAULT_CARD_DIAGNOSTICS,
        help="Diagnostics JSON (extraction audit trail)",
    )
    parser.add_argument("--unit-info", type=Path, default=DEFAULT_UNIT_INFO, help="Supplemental unit names/values")
    parser.add_argument("--overrides", type=Path, default=DEFAULT_OVERRIDES, help="Auditable override JSON")
    parser.add_argument("--locale", default="zhCN", help="Localization directory prefix (default: zhCN)")
    parser.add_argument(
        "--reference",
        type=Path,
        default=REFERENCE_CARD_JSON,
        help="Curated snapshot to compare against (comparison only, never merged)",
    )
    parser.add_argument("--no-compare", action="store_true", help="Skip the reference comparison")
    parser.add_argument("--json", action="store_true", help="Print the summary as JSON")
    return parser


def run(args: argparse.Namespace) -> int:
    extractor = CardExtractor(
        args.extracted.expanduser().resolve(),
        locale=args.locale,
        unit_info_path=args.unit_info.expanduser().resolve() if args.unit_info else None,
        overrides_path=args.overrides.expanduser().resolve() if args.overrides else None,
    )
    extractor.boot()
    extractor.register_packs()
    extractor.load_cards()
    cards = extractor.cards()

    output = args.output.expanduser().resolve()
    diagnostics_path = (
        args.diagnostics.expanduser().resolve()
        if args.diagnostics
        else output.with_name(f"{output.stem}_diagnostics.json")
    )
    summary: dict[str, Any] = {
        "source": str(extractor.script_path),
        "output": str(output),
        "diagnostics": str(diagnostics_path),
        "packs": [pack.name for pack in extractor.packs],
        "skipped_packs": extractor.diagnostics.skipped_packs,
        "cards": len(cards),
        "cards_with_description": sum(1 for card in cards if card["description"]),
        "cards_without_price": sum(1 for card in cards if card["price"] is None),
        "cards_not_in_pool": sorted(
            card["name"] for card in cards if SOURCE_NO_POOL in card["source"]
        ),
        "init_failures": len(extractor.diagnostics.init_failures),
        "pack_failures": extractor.diagnostics.pack_failures,
        "missing_unit_names": extractor.diagnostics.missing_unit_names,
    }
    if not args.no_compare and args.reference and args.reference.is_file():
        summary["comparison"] = compare_with_reference(cards, args.reference.expanduser().resolve())

    write_json(output, cards)
    write_json(
        diagnostics_path,
        {
            "summary": summary,
            "packs": [vars(pack) for pack in extractor.packs],
            "init_failures": extractor.diagnostics.init_failures,
            "engine_constants": extractor.diagnostics.engine_constants,
            "unresolved_unit_costs": extractor.diagnostics.unresolved_unit_costs,
            "cards": extractor.diagnostics.cards,
        },
    )
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(f"Extracted {len(cards)} cards -> {output}")
        print(f"Diagnostics -> {diagnostics_path}")
        comparison = summary.get("comparison")
        if comparison:
            print(
                f"Reference match: {comparison['cards_identical']}/{comparison['shared_cards']} "
                f"shared cards identical, "
                f"{comparison['cards_differing_only_in_colours_and_numbers']} differ only in "
                f"colours/numbers"
            )
            if comparison["field_mismatch_counts"]:
                print(f"Field mismatches: {comparison['field_mismatch_counts']}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except (ExtractError, AssembleError, GalaxyError, OSError, json.JSONDecodeError) as error:
        print(f"Error: {type(error).__name__}: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
