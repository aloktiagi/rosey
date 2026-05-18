"""Single source of truth for filesystem paths used by the agent.

Centralizes the `MEMORY_ROOT` env-var convention so callers don't
re-derive it (and don't silently disagree about whether `/data/memories`
or `/data` was passed in).
"""

from __future__ import annotations

import os
from pathlib import Path


def memories_dir() -> Path:
    """Resolve the memory directory.

    `MEMORY_ROOT` may be either the parent (e.g. `/data`) or the memories
    dir itself (e.g. `/data/memories`). This helper accepts both shapes.
    """
    base = os.environ.get("MEMORY_ROOT", "./memories")
    p = Path(base)
    return p if p.name == "memories" else p / "memories"
