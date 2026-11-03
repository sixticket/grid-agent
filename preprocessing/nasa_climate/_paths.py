"""Separate versioned code from an external, untracked research data root."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

RELEASE_ROOT = Path(__file__).resolve().parents[2]


def data_root(value: Path | str | None = None) -> Path:
    return Path(value or os.environ.get("GRID_DATA_ROOT") or RELEASE_ROOT / "data").expanduser().resolve()


def add_data_root(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-root", type=Path, help="External root containing experiment/, prediction_local/, corrections/, nasa_climate/ (default: GRID_DATA_ROOT or repository/data)")
    parser.add_argument("--plan", action="store_true", help="Print selected paths and operations without reading inputs, downloading, or writing files")


def reference(path: Path | str, root: Path) -> str:
    """Root-qualified relative references keep newly generated provenance movable."""
    path = Path(path).resolve()
    for prefix, base in (("data://", root), ("repo://", RELEASE_ROOT)):
        if path.is_relative_to(base):
            return prefix + path.relative_to(base).as_posix()
    raise ValueError(f"Provenance path outside data and source roots: {path}")


def resolve_reference(value: Path | str, root: Path) -> Path:
    value = str(value)
    for prefix, base in (("data://", root), ("repo://", RELEASE_ROOT)):
        if value.startswith(prefix):
            path = (base / value[len(prefix):]).resolve()
            if not path.is_relative_to(base):
                raise ValueError(f"Provenance reference escapes root: {value}")
            return path
    # Old provenance is accepted only when it still names an allowed location.
    # Absolute research-machine paths are never guessed or silently rewritten.
    path = Path(value).expanduser().resolve()
    if not any(path.is_relative_to(base) for base in (root, RELEASE_ROOT)):
        raise ValueError(f"Provenance path outside data and source roots: {value}")
    return path


def selection_json(args) -> dict:
    return {key: str(value) if isinstance(value, Path) else value for key, value in vars(args).items()}
