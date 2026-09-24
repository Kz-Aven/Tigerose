"""Atomic, non-secret connector state storage."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from avent_paths import connectors_dir


def connector_root(connector_id: str) -> Path:
    return connectors_dir() / connector_id


def installs_root(connector_id: str) -> Path:
    return connector_root(connector_id) / "installs"


def config_dir(connector_id: str) -> Path:
    return connector_root(connector_id) / "config"


def state_path(connector_id: str) -> Path:
    return connector_root(connector_id) / "state.json"


def load_state(connector_id: str) -> dict[str, Any]:
    try:
        data = json.loads(state_path(connector_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(connector_id: str, state: dict[str, Any]) -> None:
    target = state_path(connector_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix="state-", suffix=".json", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
