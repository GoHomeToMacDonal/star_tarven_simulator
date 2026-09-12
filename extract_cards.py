#!/usr/bin/env python3
"""Run the whole SC2Map -> card JSON pipeline with one command.

    uv run python extract_cards.py

Unpacks the map (skipping members whose content is unchanged) and then extracts
the card table. Both steps are also usable on their own:

    uv run python sc2map_unpacker.py data/maps/<map>.SC2Map
    uv run python card_extractor.py
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

import card_extractor
import sc2map_unpacker
from paths import DEFAULT_EXTRACTED, DEFAULT_MAP


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--map", type=Path, default=DEFAULT_MAP, help="Input .SC2Map file")
    parser.add_argument("--extracted", type=Path, default=DEFAULT_EXTRACTED, help="Where to unpack the map")
    parser.add_argument("--skip-unpack", action="store_true", help="Reuse an existing unpacked map")
    parser.add_argument("--json", action="store_true", help="Print the extraction summary as JSON")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.skip_unpack:
        status = sc2map_unpacker.main([str(args.map), "--output-dir", str(args.extracted), "--force"])
        if status != 0:
            return status
    return card_extractor.main([str(args.extracted), *(["--json"] if args.json else [])])


if __name__ == "__main__":
    raise SystemExit(main())
