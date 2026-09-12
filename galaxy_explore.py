#!/usr/bin/env python3
"""Read Galaxy map script identifiers that the SC2 editor hex-encoded.

The editor rewrites non-ASCII trigger/function/variable names as the uppercase
hex of their UTF-8 bytes (``gf_E5AD90...``).  This helper decodes them so the
description DSL can be read, and prints function bodies by Chinese name.

Examples:
    uv run python galaxy_explore.py --list-functions 子特效字符串
    uv run python galaxy_explore.py --function 根据特效字符串生成描述文本
    uv run python galaxy_explore.py --readable artifacts/MapScript.readable.galaxy
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Sequence

try:
    from .paths import DEFAULT_EXTRACTED
except ImportError:  # Allows running this module directly as a script.
    from paths import DEFAULT_EXTRACTED  # type: ignore[no-redef]

# gf_/gv_/gt_/ge_/lv_/lp_ prefixes are Galaxy scopes; the rest may be hex bytes.
# Parameter names arrive with a lower-cased first nibble (lp_e5AD97... for 字符),
# so the leading byte is matched case-insensitively.
IDENTIFIER = re.compile(r"\b(?:lib\d+_)?(?:g[ftve]|l[vp])_[0-9A-Za-z_]*")
PREFIXES = ("gf_", "gv_", "gt_", "ge_", "lv_", "lp_")


HEX_RUN = re.compile(r"[0-9A-F]{6,}")


def decode_hex_run(run: str) -> str | None:
    """Decode a hex run, tolerating literal ASCII glued to its tail.

    The editor encodes each non-ASCII character and leaves ASCII alone, so
    ``作用位置A`` becomes ``E4BD9CE794A8E4BD8DE7BDAEA``: a hex run plus a stray
    ``A``.  Trailing characters are peeled off until the rest decodes.
    """
    for tail_length in range(0, 4):
        head = run[: len(run) - tail_length] if tail_length else run
        tail = run[len(run) - tail_length :] if tail_length else ""
        if not head or len(head) % 2:
            continue
        try:
            text = bytes.fromhex(head).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        # An all-ASCII decode is almost always a coincidence, not an encoding.
        if any(ord(char) > 0x7F for char in text):
            return text + tail
    return None


def decode_hex_body(body: str) -> str | None:
    if not body:
        return None
    # Parameter names arrive with a lower-cased leading character.
    candidates = [body]
    if body[0].islower() and body[0] in "abcdef":
        candidates.append(body[0].upper() + body[1:])
    for candidate in candidates:
        decoded = HEX_RUN.sub(lambda match: decode_hex_run(match.group(0)) or match.group(0), candidate)
        if decoded != candidate:
            return decoded
    return None


def decode_identifier(name: str) -> str:
    """Decode ``gf_<hex>`` and ``ge_<hex>_<hex>`` style identifiers."""
    for prefix in PREFIXES:
        if not name.startswith(prefix):
            continue
        segments = name[len(prefix) :].split("_")
        decoded = [decode_hex_body(segment) or segment for segment in segments]
        return prefix + "_".join(decoded)
    return name


# Variables must start lower case in Galaxy, so the editor lower-cases the first
# encoded character of gv_/lv_/lp_ names (but leaves gf_/ge_/gt_/gs_ alone).
LOWER_FIRST_PREFIXES = ("gv_", "lv_", "lp_")


def encode_identifier(name: str) -> str:
    """Inverse of :func:`decode_identifier`: hex-encode each non-ASCII character."""
    prefix = ""
    for candidate in PREFIXES:
        if name.startswith(candidate):
            prefix, name = candidate, name[len(candidate) :]
            break
    if not name:
        return prefix
    encoded = "".join(
        char if ord(char) < 0x80 else char.encode("utf-8").hex().upper() for char in name
    )
    if prefix in LOWER_FIRST_PREFIXES and ord(name[0]) > 0x7F:
        encoded = encoded[0].lower() + encoded[1:]
    return prefix + encoded


LIBRARY_PREFIX = re.compile(r"^lib\d+_")


def readable(source: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(0)
        library = LIBRARY_PREFIX.match(name)
        if library is None:
            return decode_identifier(name)
        return library.group(0) + decode_identifier(name[library.end() :])

    return IDENTIFIER.sub(replace, source)


def find_function(source: str, name: str) -> tuple[str, str] | None:
    """Return (declaration, body) for a function whose name may be Chinese."""
    for candidate in (name, encode_identifier(name), f"gf_{encode_identifier(name)}"):
        pattern = re.compile(
            r"^[ \t]*(?:static\s+)?(?:void|bool|int|fixed|text|string|trigger|point|unit|order)\s+"
            rf"{re.escape(candidate)}\s*\(",
            re.MULTILINE,
        )
        for match in pattern.finditer(source):
            opening = source.index("(", match.start())
            depth = 0
            signature_end = None
            for index in range(opening, len(source)):
                if source[index] == "(":
                    depth += 1
                elif source[index] == ")":
                    depth -= 1
                    if depth == 0:
                        signature_end = index
                        break
            if signature_end is None:
                continue
            cursor = signature_end + 1
            while cursor < len(source) and source[cursor].isspace():
                cursor += 1
            # Galaxy emits forward declarations first; only a '{' means a body.
            if cursor >= len(source) or source[cursor] != "{":
                continue
            depth = 0
            for index in range(cursor, len(source)):
                if source[index] == "{":
                    depth += 1
                elif source[index] == "}":
                    depth -= 1
                    if depth == 0:
                        return source[match.start() : cursor].strip(), source[cursor : index + 1]
    return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--script",
        type=Path,
        default=DEFAULT_EXTRACTED / "scripts" / "MapScript.galaxy",
        help="Galaxy script to read",
    )
    parser.add_argument("--function", help="Print one function body (Chinese or raw name)")
    parser.add_argument("--list-functions", metavar="SUBSTRING", help="List function names containing SUBSTRING")
    parser.add_argument("--decode", metavar="NAME", help="Decode a single identifier")
    parser.add_argument("--encode", metavar="NAME", help="Encode a single identifier")
    parser.add_argument("--readable", type=Path, metavar="OUT", help="Write a fully decoded copy of the script")
    parser.add_argument("--grep", metavar="REGEX", help="Print decoded lines matching REGEX")
    parser.add_argument("--context", type=int, default=0, help="Lines of context for --grep")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.decode:
        print(decode_identifier(args.decode))
        return 0
    if args.encode:
        print(encode_identifier(args.encode))
        return 0
    source = args.script.read_text(encoding="utf-8-sig", errors="replace")
    if args.readable:
        args.readable.parent.mkdir(parents=True, exist_ok=True)
        args.readable.write_text(readable(source), encoding="utf-8")
        print(f"Wrote {args.readable}")
        return 0
    if args.list_functions:
        pattern = re.compile(
            r"\b(?:void|bool|int|fixed|text|string|trigger|point|unit|order)\s+([A-Za-z_][A-Za-z0-9_]*)\s*\("
        )
        seen: set[str] = set()
        for match in pattern.finditer(source):
            decoded = decode_identifier(match.group(1))
            if args.list_functions in decoded and decoded not in seen:
                seen.add(decoded)
                print(f"{decoded}\t{match.group(1)}")
        return 0
    if args.function:
        found = find_function(source, args.function)
        if found is None:
            print(f"Function not found: {args.function}", file=sys.stderr)
            return 2
        signature, body = found
        print(readable(signature))
        print(readable(body))
        return 0
    if args.grep:
        pattern = re.compile(args.grep)
        lines = readable(source).splitlines()
        for index, line in enumerate(lines):
            if pattern.search(line):
                low = max(0, index - args.context)
                high = min(len(lines), index + args.context + 1)
                for offset in range(low, high):
                    print(f"{offset + 1}:{lines[offset]}")
                if args.context:
                    print("--")
        return 0
    build_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
