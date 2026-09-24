"""Resolve code vs data roots for CLI, server, and macOS app.

When ``TIGEROSE_HOME`` (preferred) or ``AVENT_HOME`` (compat) is set (macOS app),
mutable data lives there while packaged code (server/, skills/, plugins/) stays
under the install/code root.

When unset, both roots are the repository root (CLI / start-dev.sh).

Module name ``avent_paths`` is kept for import compatibility; see also
``avent_config`` / tigerose_config comments in operational docs.
"""

from __future__ import annotations

import os
from pathlib import Path

_CODE_ROOT: Path | None = None
_DATA_ROOT: Path | None = None


def code_root() -> Path:
    """Directory that contains ``server/``, ``skills/``, ``avent_config.py``."""
    global _CODE_ROOT
    if _CODE_ROOT is None:
        _CODE_ROOT = Path(__file__).resolve().parent
    return _CODE_ROOT


def data_root() -> Path:
    """Mutable home: config, .env, data/, .sessions/, .memory/, .worktrees/."""
    global _DATA_ROOT
    if _DATA_ROOT is None:
        raw = (os.environ.get("TIGEROSE_HOME") or os.environ.get("AVENT_HOME") or "").strip()
        if raw:
            _DATA_ROOT = Path(raw).expanduser().resolve()
        else:
            _DATA_ROOT = code_root()
    return _DATA_ROOT


def ensure_data_dirs() -> Path:
    """Create standard Application Support / repo data directories."""
    root = data_root()
    for rel in (
        "data",
        "data/uploads",
        ".sessions",
        ".memory",
        ".worktrees",
        "logs",
        "skills",
        "plugins",
        "archives",
        "runs",
    ):
        (root / rel).mkdir(parents=True, exist_ok=True)
    return root


def user_skills_dir() -> Path:
    return data_root() / "skills"


def user_plugins_dir() -> Path:
    return data_root() / "plugins"


def user_mcp_config_path() -> Path:
    return data_root() / "mcp.json"


def connectors_dir() -> Path:
    """Managed connector installs and non-secret state."""
    return data_root() / "connectors"


def project_agent_dir(workspace: Path | str | None) -> Path | None:
    if not workspace:
        return None
    p = Path(workspace).expanduser()
    try:
        p = p.resolve()
    except OSError:
        return None
    return p / ".agent"


def reset_path_cache() -> None:
    """Test helper: clear cached roots after env changes."""
    global _CODE_ROOT, _DATA_ROOT
    _CODE_ROOT = None
    _DATA_ROOT = None
