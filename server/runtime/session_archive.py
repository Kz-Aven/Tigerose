"""Immutable session archives written before clear/delete."""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from avent_paths import data_root, ensure_data_dirs


def archives_root() -> Path:
    ensure_data_dirs()
    root = data_root() / "archives"
    root.mkdir(parents=True, exist_ok=True)
    return root


def archive_session_snapshot(
    *,
    session_id: str,
    template_id: str = "",
    actor: str = "",
    reason: str = "",
    sqlite_messages: list[dict[str, Any]] | None = None,
    state_payload: dict[str, Any] | None = None,
) -> Path:
    """Write an immutable snapshot; returns archive directory path."""
    ts = time.strftime("%Y%m%dT%H%M%S")
    dest = archives_root() / session_id / f"{ts}_{int(time.time())}"
    dest.mkdir(parents=True, exist_ok=False)
    meta = {
        "session_id": session_id,
        "template_id": template_id,
        "actor": actor,
        "reason": reason,
        "archived_at": time.time(),
    }
    (dest / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (dest / "messages.json").write_text(
        json.dumps(sqlite_messages or [], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    if state_payload is not None:
        (dest / "state.json").write_text(
            json.dumps(state_payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    # Copy live session dir files if present (best-effort).
    live = data_root() / ".sessions" / session_id
    if live.is_dir():
        copy_dest = dest / "session_files"
        shutil.copytree(live, copy_dest, dirs_exist_ok=True)
    return dest
