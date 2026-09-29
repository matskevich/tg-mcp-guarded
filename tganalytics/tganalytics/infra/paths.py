"""Cwd-independent locations for on-disk state.

MCP clients start the servers from whatever directory they like, so a
cwd-relative default silently forks shared state: two copies of the anti-spam
counters mean the daily quota is enforced twice over, and two copies of the
session registry mean the session guard cannot see the other process at all.
Every state path therefore resolves against the repository, never the cwd.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def data_root() -> Path:
    """Root of the on-disk state tree (`TG_DATA_DIR`, else `<repo>/data`)."""
    raw = os.environ.get("TG_DATA_DIR", "").strip()
    if raw:
        path = Path(raw).expanduser()
        return (path if path.is_absolute() else PROJECT_ROOT / path).resolve()
    return (PROJECT_ROOT / "data").resolve()


def data_path(*parts: str) -> Path:
    """Absolute path inside the state tree."""
    return data_root().joinpath(*parts)


def resolve_state_path(raw: str, *default_parts: str) -> Path:
    """Resolve a configured state path; relative values follow the repo, not the cwd."""
    value = str(raw or "").strip()
    if not value:
        return data_path(*default_parts)
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (PROJECT_ROOT / path).resolve()
