#!/usr/bin/env python3
"""Extract text data and Galaxy scripts from a StarCraft II MPQ map."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence

import mpyq

try:
    from .paths import EXTRACTED_DIR
except ImportError:  # Allows running this module directly as a script.
    from paths import EXTRACTED_DIR  # type: ignore[no-redef]

MPQ_MAGIC = b"MPQ\x1a"
SCRIPT_SUFFIXES = {".galaxy", ".galscript"}
TEXT_SUFFIXES = {
    ".sc2layout",
    ".sc2style",
    ".txt",
    ".version",
    ".xml",
}
# These standard SC2 component files contain textual XML despite having no suffix.
TEXT_COMPONENTS = {
    "documentheader",
    "documentinfo",
    "mapinfo",
    "objects",
    "regions",
}
SPECIAL_TEXT_MEMBERS = {"(listfile)"}


class UnpackError(RuntimeError):
    """An error that can be shown directly to the CLI user."""


@dataclass(frozen=True)
class ExtractedMember:
    archive_path: str
    output_path: str
    category: str
    size: int
    sha256: str
    status: str


@dataclass(frozen=True)
class ExtractionResult:
    source: str
    source_size: int
    source_sha256: str
    mpq_format_version: int
    listed_members: int
    text_files: int
    script_files: int
    extracted_bytes: int
    output_dir: str
    members: list[ExtractedMember]


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest_file(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    size = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            size += len(chunk)
            hasher.update(chunk)
    return hasher.hexdigest(), size


def decode_member_name(raw_name: bytes | str) -> str:
    if isinstance(raw_name, str):
        return raw_name
    try:
        return raw_name.decode("utf-8")
    except UnicodeDecodeError as error:
        raise UnpackError("MPQ listfile contains a non-UTF-8 member name") from error


def member_category(member_name: str) -> str | None:
    normalized = member_name.replace("\\", "/")
    suffix = PurePosixPath(normalized).suffix.casefold()
    basename = PurePosixPath(normalized).name.casefold()
    if suffix in SCRIPT_SUFFIXES:
        return "scripts"
    if suffix in TEXT_SUFFIXES or basename in TEXT_COMPONENTS:
        return "text"
    if normalized.casefold() in SPECIAL_TEXT_MEMBERS:
        return "text"
    return None


def safe_relative_path(member_name: str) -> Path:
    normalized = member_name.replace("\\", "/")
    pure_path = PurePosixPath(normalized)
    if (
        not normalized
        or pure_path.is_absolute()
        or any(part in {"", ".", ".."} for part in pure_path.parts)
        or ":" in pure_path.parts[0]
    ):
        raise UnpackError(f"Unsafe MPQ member path: {member_name!r}")
    return Path(*pure_path.parts)


def write_atomically(path: Path, data: bytes, *, force: bool) -> str:
    if path.exists():
        if not path.is_file():
            raise UnpackError(f"Output path is not a file: {path}")
        if digest_file(path)[0] == digest_bytes(data):
            return "unchanged"
        if not force:
            raise UnpackError(f"Output file already exists with different content: {path}")

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        return "written"
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def archive_members(archive: mpyq.MPQArchive) -> list[tuple[bytes | str, str]]:
    members = [(raw_name, decode_member_name(raw_name)) for raw_name in archive.files]
    # mpyq omits the listfile itself from archive.files, but it is useful metadata.
    if not any(name == "(listfile)" for _, name in members):
        members.append((b"(listfile)", "(listfile)"))
    return members


def extract_map(source: Path, output_dir: Path, *, force: bool) -> ExtractionResult:
    if not source.is_file():
        raise UnpackError(f"Map file does not exist: {source}")
    with source.open("rb") as stream:
        if stream.read(len(MPQ_MAGIC)) != MPQ_MAGIC:
            raise UnpackError(f"Input is not an MPQ archive: {source}")

    source_sha256, source_size = digest_file(source)
    archive = None
    try:
        archive = mpyq.MPQArchive(str(source))
        members = archive_members(archive)
        extracted: list[ExtractedMember] = []
        seen_outputs: set[str] = set()

        for raw_member_name, member_name in members:
            category = member_category(member_name)
            if category is None:
                continue

            relative_path = safe_relative_path(member_name)
            output_path = output_dir / category / relative_path
            output_key = str(output_path.resolve()).casefold()
            if output_key in seen_outputs:
                raise UnpackError(f"Case-insensitive output collision: {member_name}")
            seen_outputs.add(output_key)

            try:
                data = archive.read_file(raw_member_name)
            except (NotImplementedError, RuntimeError, ValueError) as error:
                raise UnpackError(f"Failed to decompress {member_name}: {error}") from error
            if data is None:
                raise UnpackError(f"Listed MPQ member could not be read: {member_name}")

            status = write_atomically(output_path, data, force=force)
            extracted.append(
                ExtractedMember(
                    archive_path=member_name,
                    output_path=str(output_path),
                    category=category,
                    size=len(data),
                    sha256=digest_bytes(data),
                    status=status,
                )
            )

        result = ExtractionResult(
            source=str(source),
            source_size=source_size,
            source_sha256=source_sha256,
            mpq_format_version=int(archive.header["format_version"]),
            listed_members=len(archive.files),
            text_files=sum(member.category == "text" for member in extracted),
            script_files=sum(member.category == "scripts" for member in extracted),
            extracted_bytes=sum(member.size for member in extracted),
            output_dir=str(output_dir),
            members=extracted,
        )
    except UnpackError:
        raise
    except (OSError, struct.error, ValueError) as error:
        raise UnpackError(f"Failed to read MPQ archive: {error}") from error
    finally:
        if archive is not None:
            archive.file.close()

    manifest_path = output_dir / "extraction_manifest.json"
    manifest_data = json.dumps(
        asdict(result), ensure_ascii=False, indent=2
    ).encode("utf-8") + b"\n"
    # The manifest is tool-managed and includes per-run written/unchanged statuses.
    write_atomically(manifest_path, manifest_data, force=True)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract textual data and Galaxy scripts from an SC2Map MPQ archive."
    )
    parser.add_argument("map", type=Path, help="Input .SC2Map file")
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Output directory (default: artifacts/extracted/<map-name>_extracted)",
    )
    parser.add_argument(
        "--force", action="store_true", help="Replace existing files whose content differs"
    )
    parser.add_argument("--json", action="store_true", help="Print the summary as JSON")
    return parser


def run(args: argparse.Namespace) -> int:
    source = args.map.expanduser().resolve()
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else EXTRACTED_DIR / f"{source.stem}_extracted"
    )
    result = extract_map(source, output_dir, force=args.force)
    summary = {
        key: value
        for key, value in asdict(result).items()
        if key != "members"
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print("Extraction complete")
        print(f"Source: {result.source}")
        print(f"MPQ format: v{result.mpq_format_version}")
        print(f"Listed members: {result.listed_members}")
        print(f"Text files: {result.text_files}")
        print(f"Galaxy scripts: {result.script_files}")
        print(f"Extracted bytes: {result.extracted_bytes}")
        print(f"Output: {result.output_dir}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except (UnpackError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
