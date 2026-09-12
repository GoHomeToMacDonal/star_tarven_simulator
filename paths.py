#!/usr/bin/env python3
"""Shared filesystem locations for the SC2Map -> card JSON extraction pipeline.

`sc2map_unpacker.py` and `star_tavern_cards.py` import these constants, so the
whole pipeline can run with no arguments from the repository root.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

DATA_DIR = REPO_ROOT / "data"
MAPS_DIR = DATA_DIR / "maps"

# Generated content lives outside data/ so the curated snapshots stay untouched.
ARTIFACTS_DIR = REPO_ROOT / "artifacts"
EXTRACTED_DIR = ARTIFACTS_DIR / "extracted"

# The map shipped with the repository. Bump these when a newer map is added.
MAP_NAME = "星际酒馆正式版-v4.6.1.7"
MAP_VERSION = "v4.6.1.7"
DEFAULT_MAP = MAPS_DIR / f"{MAP_NAME}.SC2Map"

# sc2map_unpacker.py writes here by default (<map-stem>_extracted).
DEFAULT_EXTRACTED = EXTRACTED_DIR / f"{MAP_NAME}_extracted"

# Card data is a deliverable, so it lands in data/ next to the older snapshots;
# the machine-readable audit trail stays with the other build artifacts.
DEFAULT_CARD_OUTPUT = DATA_DIR / f"{MAP_VERSION}_card.json"
DEFAULT_CARD_DIAGNOSTICS = ARTIFACTS_DIR / "cards" / f"{MAP_VERSION}_card_diagnostics.json"
DEFAULT_OVERRIDES = MAPS_DIR / "card_overrides.json"
DEFAULT_UNIT_INFO = MAPS_DIR / "unit_info.json"

# Curated snapshot used as the shape reference / optional fallback source.
REFERENCE_CARD_JSON = DATA_DIR / "v20260826_card.json"

__all__ = [
    "ARTIFACTS_DIR",
    "DATA_DIR",
    "DEFAULT_CARD_DIAGNOSTICS",
    "DEFAULT_CARD_OUTPUT",
    "DEFAULT_EXTRACTED",
    "DEFAULT_MAP",
    "DEFAULT_OVERRIDES",
    "DEFAULT_UNIT_INFO",
    "EXTRACTED_DIR",
    "MAPS_DIR",
    "MAP_NAME",
    "MAP_VERSION",
    "REFERENCE_CARD_JSON",
    "REPO_ROOT",
]
