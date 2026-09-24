"""In-memory budget guard with reserve/commit/release accounting."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

try:
    from avent_config import cfg_get, get_config
except ImportError:  # pragma: no cover
    cfg_get = None  # type: ignore[assignment,misc]
    get_config = None  # type: ignore[assignment,misc]

BudgetKind = Literal["llm_round", "tool_call", "diagnostic", "write_attempt"]
BudgetProfile = Literal["bootstrap", "one_shot", "durable_goal"]

_DEFAULT_LIMITS: dict[str, dict[str, float | int]] = {
    "bootstrap": {
        "soft_rounds": 0,
        "hard_rounds": 1,
        "soft_tool_calls": 0,
        "hard_tool_calls": 0,
        "max_diagnostics": 0,
        "write_attempts": 0,
        "timeout_s": 10,
    },
    "one_shot": {
        "soft_rounds": 24,
        "hard_rounds": 60,
        "soft_tool_calls": 40,
        "hard_tool_calls": 80,
        "max_diagnostics": 12,
        "write_attempts": 12,
        "timeout_s": 900,
    },
    "durable_goal": {
        "soft_rounds": 80,
        "hard_rounds": 160,
        "soft_tool_calls": 150,
        "hard_tool_calls": 300,
        "max_diagnostics": 24,
        "write_attempts": 50,
        "timeout_s": 7200,
    },
}

_KIND_TO_LIMIT_KEY: dict[BudgetKind, tuple[str, str]] = {
    "llm_round": ("soft_rounds", "hard_rounds"),
    "tool_call": ("soft_tool_calls", "hard_tool_calls"),
    "diagnostic": ("max_diagnostics", "max_diagnostics"),
    "write_attempt": ("write_attempts", "write_attempts"),
}

_store_lock = threading.Lock()
_budgets: dict[str, "BudgetState"] = {}
_reservations: dict[str, dict[str, Any]] = {}


@dataclass
class BudgetLimits:
    soft_rounds: int = 24
    hard_rounds: int = 60
    soft_tool_calls: int = 40
    hard_tool_calls: int = 80
    max_diagnostics: int = 12
    write_attempts: int = 12
    timeout_s: float = 900.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "soft_rounds": self.soft_rounds,
            "hard_rounds": self.hard_rounds,
            "soft_tool_calls": self.soft_tool_calls,
            "hard_tool_calls": self.hard_tool_calls,
            "max_diagnostics": self.max_diagnostics,
            "write_attempts": self.write_attempts,
            "timeout_s": self.timeout_s,
        }


@dataclass
class BudgetState:
    root_budget_id: str
    profile: BudgetProfile
    limits_snapshot: dict[str, float | int]
    consumed: dict[str, float] = field(default_factory=dict)
    reserved: dict[str, float] = field(default_factory=dict)
    active_execution_ms: int = 0
    version: int = 1
    started_at: float = field(default_factory=time.time)

    def _total(self, kind: BudgetKind) -> float:
        return float(self.consumed.get(kind, 0.0) + self.reserved.get(kind, 0.0))


def load_budget_limits_from_config() -> dict[str, BudgetLimits]:
    profiles: dict[str, BudgetLimits] = {}
    for profile, defaults in _DEFAULT_LIMITS.items():
        profiles[profile] = BudgetLimits(
            soft_rounds=int(defaults["soft_rounds"]),
            hard_rounds=int(defaults["hard_rounds"]),
            soft_tool_calls=int(defaults["soft_tool_calls"]),
            hard_tool_calls=int(defaults["hard_tool_calls"]),
            max_diagnostics=int(defaults["max_diagnostics"]),
            write_attempts=int(defaults["write_attempts"]),
            timeout_s=float(defaults["timeout_s"]),
        )

    if get_config is None or cfg_get is None:
        return profiles

    try:
        cfg = get_config()
        raw_profiles = cfg_get(cfg, "agent", "budgets", default={}) or {}
        if not isinstance(raw_profiles, dict):
            return profiles
        for profile_name, raw_limits in raw_profiles.items():
            if profile_name not in profiles or not isinstance(raw_limits, dict):
                continue
            base = profiles[profile_name]
            profiles[profile_name] = BudgetLimits(
                soft_rounds=int(raw_limits.get("soft_rounds", base.soft_rounds)),
                hard_rounds=int(raw_limits.get("hard_rounds", base.hard_rounds)),
                soft_tool_calls=int(
                    raw_limits.get("soft_tool_calls", base.soft_tool_calls)
                ),
                hard_tool_calls=int(
                    raw_limits.get("hard_tool_calls", base.hard_tool_calls)
                ),
                max_diagnostics=int(
                    raw_limits.get("max_diagnostics", base.max_diagnostics)
                ),
                write_attempts=int(
                    raw_limits.get("write_attempts", base.write_attempts)
                ),
                timeout_s=float(raw_limits.get("timeout_s", base.timeout_s)),
            )
    except Exception:
        pass
    return profiles


def _limits_for_profile(profile: BudgetProfile) -> dict[str, float | int]:
    return load_budget_limits_from_config()[profile].as_dict()


def create_budget(profile: BudgetProfile) -> BudgetState:
    root_budget_id = f"budget_{uuid.uuid4().hex[:16]}"
    state = BudgetState(
        root_budget_id=root_budget_id,
        profile=profile,
        limits_snapshot=_limits_for_profile(profile),
    )
    with _store_lock:
        _budgets[root_budget_id] = state
    _persist_budget(state)
    return state


def load_budget(root_budget_id: str) -> BudgetState | None:
    """Load budget from memory, falling back to SQLite across process restarts."""
    with _store_lock:
        cached = _budgets.get(root_budget_id)
        if cached is not None:
            return cached
    try:
        from server.db import repos

        row = repos.get_runtime_budget(root_budget_id)
    except Exception:
        return None
    if not row:
        return None
    state = BudgetState(
        root_budget_id=root_budget_id,
        profile=str(row.get("profile") or "one_shot"),  # type: ignore[arg-type]
        limits_snapshot=dict(row.get("limits") or {}),
        consumed={str(k): float(v) for k, v in dict(row.get("consumed") or {}).items()},
        reserved={str(k): float(v) for k, v in dict(row.get("reserved") or {}).items()},
        active_execution_ms=int(row.get("active_execution_ms") or 0),
        version=int(row.get("version") or 1),
    )
    with _store_lock:
        _budgets[root_budget_id] = state
    return state


def _persist_budget(budget: BudgetState) -> None:
    try:
        from server.db import repos

        repos.upsert_runtime_budget(
            budget.root_budget_id,
            profile=budget.profile,
            limits=budget.limits_snapshot,
            consumed=budget.consumed,
            reserved=budget.reserved,
            active_execution_ms=budget.active_execution_ms,
            version=budget.version,
        )
    except Exception:
        pass


def _get_budget(root_budget_id: str) -> BudgetState:
    with _store_lock:
        budget = _budgets.get(root_budget_id)
    if budget is None:
        loaded = load_budget(root_budget_id)
        if loaded is None:
            raise KeyError(f"budget not found: {root_budget_id}")
        return loaded
    return budget


def upgrade_profile(budget: BudgetState, new_profile: BudgetProfile) -> BudgetState:
    with _store_lock:
        stored = _budgets.get(budget.root_budget_id)
        if stored is None:
            raise KeyError(f"budget not found: {budget.root_budget_id}")
        stored.profile = new_profile
        stored.limits_snapshot = _limits_for_profile(new_profile)
        stored.version += 1
        snapshot = stored
    _persist_budget(snapshot)
    return snapshot


def _limit_value(limits: dict[str, float | int], key: str) -> float:
    return float(limits.get(key, 0))


def reserve(
    root_id: str,
    reservation_id: str,
    kind: BudgetKind,
    amount: float = 1.0,
) -> bool:
    amount = max(0.0, float(amount))
    try:
        _get_budget(root_id)
    except KeyError:
        return False
    with _store_lock:
        existing = _reservations.get(reservation_id)
        if existing is not None:
            return bool(existing.get("accepted"))

        budget = _budgets.get(root_id)
        if budget is None:
            return False

        _soft_key, hard_key = _KIND_TO_LIMIT_KEY[kind]
        hard_limit = _limit_value(budget.limits_snapshot, hard_key)
        projected = budget._total(kind) + amount
        accepted = projected <= hard_limit
        _reservations[reservation_id] = {
            "root_id": root_id,
            "kind": kind,
            "amount": amount,
            "accepted": accepted,
        }
        if accepted:
            budget.reserved[kind] = budget.reserved.get(kind, 0.0) + amount
            budget.version += 1
            snapshot = budget
        else:
            snapshot = None
    if accepted and snapshot is not None:
        try:
            from server.db import repos

            repos.upsert_budget_reservation(
                reservation_id,
                root_budget_id=root_id,
                kind=kind,
                amount=amount,
                status="reserved",
            )
        except Exception:
            pass
        _persist_budget(snapshot)
    return accepted


def commit(reservation_id: str, actual: float | None = None) -> None:
    with _store_lock:
        reservation = _reservations.get(reservation_id)
        if reservation is None or not reservation.get("accepted"):
            return
        root_id = str(reservation["root_id"])
        kind: BudgetKind = reservation["kind"]
        reserved_amount = float(reservation.get("amount") or 0.0)
        actual_amount = reserved_amount if actual is None else max(0.0, float(actual))

        budget = _budgets.get(root_id)
        if budget is None:
            return
        budget.reserved[kind] = max(0.0, budget.reserved.get(kind, 0.0) - reserved_amount)
        budget.consumed[kind] = budget.consumed.get(kind, 0.0) + actual_amount
        budget.version += 1
        del _reservations[reservation_id]
        snapshot = budget
    try:
        from server.db import repos

        repos.delete_budget_reservation(reservation_id)
    except Exception:
        pass
    _persist_budget(snapshot)


def release(reservation_id: str) -> None:
    with _store_lock:
        reservation = _reservations.get(reservation_id)
        if reservation is None or not reservation.get("accepted"):
            _reservations.pop(reservation_id, None)
            return
        root_id = str(reservation["root_id"])
        kind: BudgetKind = reservation["kind"]
        amount = float(reservation.get("amount") or 0.0)
        budget = _budgets.get(root_id)
        if budget is not None:
            budget.reserved[kind] = max(0.0, budget.reserved.get(kind, 0.0) - amount)
            budget.version += 1
            snapshot = budget
        else:
            snapshot = None
        del _reservations[reservation_id]
    try:
        from server.db import repos

        repos.delete_budget_reservation(reservation_id)
    except Exception:
        pass
    if snapshot is not None:
        _persist_budget(snapshot)


def check_soft(budget: BudgetState, kind: BudgetKind) -> str | None:
    soft_key, hard_key = _KIND_TO_LIMIT_KEY[kind]
    total = budget._total(kind)
    soft_limit = _limit_value(budget.limits_snapshot, soft_key)
    hard_limit = _limit_value(budget.limits_snapshot, hard_key)
    if total >= hard_limit:
        return "hard"
    if total >= soft_limit:
        return "soft"
    return None


def check_hard(budget: BudgetState, kind: BudgetKind) -> str | None:
    _, hard_key = _KIND_TO_LIMIT_KEY[kind]
    total = budget._total(kind)
    hard_limit = _limit_value(budget.limits_snapshot, hard_key)
    if total >= hard_limit:
        return "hard"
    return None


def remaining_timeout_s(budget: BudgetState) -> float:
    timeout_s = _limit_value(budget.limits_snapshot, "timeout_s")
    elapsed = time.time() - budget.started_at
    return max(0.0, timeout_s - elapsed)
