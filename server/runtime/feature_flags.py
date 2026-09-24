"""Runtime feature flags for goal/scope/budget rollout."""

from __future__ import annotations

import os
from typing import Final

try:
    from avent_config import cfg_get, get_config
except ImportError:  # pragma: no cover
    cfg_get = None  # type: ignore[assignment,misc]
    get_config = None  # type: ignore[assignment,misc]

KNOWN_FLAGS: Final[tuple[str, ...]] = (
    "agent_mesh_v1",
    "task_intent_v2",
    "typesafe_judgments_v1",
    "server_id_v2",
    "tool_semantics_v2",
    "attempt_ledger_v2",
    "domain_acceptance_v2",
    "budget_guard_v2",
    "durable_workflow_shadow_v0",
    "durable_workflow_v1",
    "goal_workflow_projection_v1",
)

_FLAG_DEPENDENCIES: Final[dict[str, tuple[str, ...]]] = {
    "agent_mesh_v1": (),
    "tool_semantics_v2": (),
    "attempt_ledger_v2": ("tool_semantics_v2",),
    "domain_acceptance_v2": ("attempt_ledger_v2",),
    "server_id_v2": (),
    "budget_guard_v2": (),
    "task_intent_v2": (),
    "typesafe_judgments_v1": (),
    "durable_workflow_shadow_v0": (),
    "durable_workflow_v1": ("durable_workflow_shadow_v0",),
    "goal_workflow_projection_v1": ("durable_workflow_v1",),
}

_DISABLED_BY_DEFAULT: Final[frozenset[str]] = frozenset(
    {
        "durable_workflow_shadow_v0",
        "durable_workflow_v1",
        "goal_workflow_projection_v1",
    }
)


def _env_flag_name(name: str) -> str:
    return f"AVENT_FF_{name.upper()}"


def _coerce_bool(value: object, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def flag_enabled(name: str, *, default: bool = True) -> bool:
    if name not in KNOWN_FLAGS:
        raise KeyError(f"unknown feature flag: {name}")
    if name in _DISABLED_BY_DEFAULT:
        default = False
    env_key = _env_flag_name(name)
    if env_key in os.environ:
        return _coerce_bool(os.environ.get(env_key), default=default)
    if get_config is not None and cfg_get is not None:
        try:
            raw = cfg_get(get_config(), "agent", "feature_flags", name, default=None)
            if raw is not None:
                return _coerce_bool(raw, default=default)
        except Exception:
            pass
    return default


def require_flag_deps() -> None:
    for flag, deps in _FLAG_DEPENDENCIES.items():
        if not flag_enabled(flag):
            continue
        for dep in deps:
            if not flag_enabled(dep):
                raise RuntimeError(
                    f"feature flag {flag!r} is enabled but required dependency "
                    f"{dep!r} is disabled"
                )


def validate_feature_flags_at_run_start() -> None:
    """Call at the start of a run/turn to fail fast on invalid flag combinations."""
    require_flag_deps()
