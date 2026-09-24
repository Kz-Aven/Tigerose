"""Lightweight migration journal for MCP server_id and ledger dual-write checkpoints."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from avent_paths import data_root

_JOURNAL_NAME = "migration_journal.jsonl"


def journal_path() -> Path:
    root = data_root() / "migrations"
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root / _JOURNAL_NAME


def append_journal_event(
    step_id: str,
    stage: str,
    *,
    payload: dict[str, Any] | None = None,
    checksum: str = "",
) -> None:
    """Append an idempotent migration step record (prepared → … → committed)."""
    event = {
        "ts": time.time(),
        "step_id": step_id,
        "stage": stage,
        "checksum": checksum,
        "payload": payload or {},
    }
    path = journal_path()
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def latest_stage(step_id: str) -> str | None:
    path = journal_path()
    if not path.is_file():
        return None
    latest: str | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("step_id") == step_id:
            latest = str(event.get("stage") or "")
    return latest


def unfinished_steps() -> list[str]:
    """Return step_ids whose latest stage is not committed/rolled_back."""
    path = journal_path()
    if not path.is_file():
        return []
    latest: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        sid = str(event.get("step_id") or "")
        if sid:
            latest[sid] = str(event.get("stage") or "")
    return [
        sid
        for sid, stage in latest.items()
        if stage not in {"committed", "rolled_back"}
    ]
