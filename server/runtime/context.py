"""Per-turn execution context for server runtime tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class TurnContext:
    template_id: str
    scope_key: str
    session_id: str
    cwd: Path
    surface: str
    trusted_roots: tuple[Path, ...] = ()
    group_id: str | None = None
    instance_id: str | None = None
    enabled_skill_ids: list[str] = field(default_factory=list)
    connector_skills: dict[str, Path] = field(default_factory=dict)
    connector_set: Any = None
    session_manager: Any = None
    session_state: Any = None
    run_id: str = ""
    mesh_task_id: str = ""
    cancel_token: Any = None
    user_hooks: list[dict[str, Any]] = field(default_factory=list)
    # Optional hooks for cron/teammate side-effects
    publish: Any = None  # callable(channel, event_type, data)

    @property
    def board_scope(self) -> str:
        if self.group_id:
            return f"group:{self.group_id}"
        return f"assistant:{self.template_id}"

    @property
    def sse_channel(self) -> str:
        if self.group_id:
            return f"group:{self.group_id}"
        return f"assistant:{self.template_id}"
