"""Configured filesystem roots shared by every local assistant."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from avent_config import cfg_get, get_config


def configured_trusted_roots(cfg: dict[str, Any] | None = None) -> tuple[Path, ...]:
    """Return normalized absolute roots from ``agent.trusted_data_roots``."""
    cfg = cfg or get_config()
    raw_roots = cfg_get(cfg, "agent", "trusted_data_roots", default=[])
    if not isinstance(raw_roots, (list, tuple)):
        return ()
    roots: list[Path] = []
    for raw in raw_roots:
        if not isinstance(raw, str) or not raw.strip():
            continue
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            continue
        try:
            root = candidate.resolve()
        except OSError:
            continue
        if root not in roots:
            roots.append(root)
    return tuple(roots)
