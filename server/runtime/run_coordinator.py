"""Per-session run serialization, cancellation, and result fencing."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from server.db import repos


class RunBusyError(RuntimeError):
    pass


class RunCancelledError(RuntimeError):
    pass


@dataclass
class RunToken:
    run_id: str
    session_id: str
    generation: int
    cancel_event: threading.Event = field(default_factory=threading.Event)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def checkpoint(self) -> None:
        if self.cancelled:
            raise RunCancelledError(f"run cancelled: {self.run_id}")


class SessionRunCoordinator:
    def __init__(self) -> None:
        self._guard = threading.RLock()
        self._locks: dict[str, threading.Lock] = {}
        self._active: dict[str, RunToken] = {}
        self._queued: dict[str, list[str]] = {}
        self._tokens: dict[str, RunToken] = {}
        self._cancelled_queued: set[str] = set()
        self._generation: dict[str, int] = {}

    def _drop_queued_run(self, session_id: str, run_id: str) -> bool:
        """Remove run_id from the in-memory queue. Caller must hold _guard."""
        queued = self._queued.get(session_id) or []
        if run_id not in queued:
            return False
        queued.remove(run_id)
        if queued:
            self._queued[session_id] = queued
        else:
            self._queued.pop(session_id, None)
        return True

    def enqueue(self, session_id: str, *, agent_id: str = "") -> str:
        run_id = f"run_{uuid.uuid4().hex[:16]}"
        with self._guard:
            if session_id in self._active or self._queued.get(session_id):
                raise RunBusyError(f"session already has a queued or active run: {session_id}")
            self._queued.setdefault(session_id, []).append(run_id)
        try:
            repos.create_runtime_run(run_id, session_id, agent_id=agent_id, status="queued")
        except Exception:
            with self._guard:
                self._drop_queued_run(session_id, run_id)
            raise
        return run_id

    def begin(self, session_id: str, *, run_id: str | None = None) -> RunToken:
        with self._guard:
            lock = self._locks.setdefault(session_id, threading.Lock())
        acquired = lock.acquire(blocking=False)
        if not acquired:
            # Absorb the brief window where finish() has cleared _active but
            # has not released the session lock yet.
            for _ in range(5):
                time.sleep(0.02)
                if lock.acquire(blocking=False):
                    acquired = True
                    break
        if not acquired:
            raise RunBusyError(f"session already has an active run: {session_id}")
        try:
            with self._guard:
                if run_id is None:
                    queued = self._queued.get(session_id) or []
                    run_id = queued.pop(0) if queued else f"run_{uuid.uuid4().hex[:16]}"
                    if not queued:
                        self._queued.pop(session_id, None)
                else:
                    self._drop_queued_run(session_id, run_id)
                generation = self._generation.get(session_id, 0)
                token = RunToken(run_id=run_id, session_id=session_id, generation=generation)
                if run_id in self._cancelled_queued:
                    token.cancel_event.set()
                    self._cancelled_queued.discard(run_id)
                self._active[session_id] = token
                self._tokens[run_id] = token
            repos.upsert_runtime_run(run_id, session_id, status="running")
            return token
        except Exception:
            with self._guard:
                self._active.pop(session_id, None)
                self._tokens.pop(run_id, None)
            lock.release()
            raise

    def finish(self, token: RunToken, status: str, *, error: str = "") -> None:
        # Drop busy markers and release the session lock in one critical
        # section so enqueue/begin cannot observe "free _active + held lock".
        with self._guard:
            active = self._active.get(token.session_id)
            if active is token:
                self._active.pop(token.session_id, None)
                lock = self._locks.get(token.session_id)
                if lock and lock.locked():
                    lock.release()
        repos.finish_runtime_run(token.run_id, status=status, error=error)

    def cancel(self, run_id: str) -> bool:
        """Cancel a run.

        Queued runs are removed from the in-memory queue immediately so the
        session does not stay permanently busy after a rolled-back enqueue
        (e.g. /goal resume with no suspended goal). Late workers that still
        hold the run_id are fenced via ``_cancelled_queued``.
        """
        finish_queued = False
        with self._guard:
            token = self._tokens.get(run_id)
            if token:
                token.cancel_event.set()
            for sid, queued in list(self._queued.items()):
                if run_id in queued:
                    self._drop_queued_run(sid, run_id)
                    self._cancelled_queued.add(run_id)
                    finish_queued = True
                    break
        if finish_queued:
            repos.finish_runtime_run(run_id, status="cancelled")
            return True
        if token:
            repos.mark_runtime_run_cancel_requested(run_id)
            return True
        row = repos.get_runtime_run(run_id)
        if row and row.get("status") in {"queued", "running", "cancel_requested"}:
            repos.mark_runtime_run_cancel_requested(run_id)
            return True
        return False

    def cancel_session(self, session_id: str, *, wait_s: float = 5.0) -> None:
        with self._guard:
            token = self._active.get(session_id)
            queued = list(self._queued.pop(session_id, []) or [])
            self._cancelled_queued.update(queued)
            self._generation[session_id] = self._generation.get(session_id, 0) + 1
            if token:
                token.cancel_event.set()
        for run_id in queued:
            repos.finish_runtime_run(run_id, status="cancelled")
        if token:
            repos.mark_runtime_run_cancel_requested(token.run_id)
            deadline = time.monotonic() + max(0.0, wait_s)
            while time.monotonic() < deadline:
                with self._guard:
                    if self._active.get(session_id) is not token:
                        break
                time.sleep(0.02)

    def can_commit(self, token: RunToken) -> bool:
        with self._guard:
            return (
                not token.cancelled
                and self._active.get(token.session_id) is token
                and self._generation.get(token.session_id, 0) == token.generation
            )

    def status(self, run_id: str) -> dict[str, Any] | None:
        return repos.get_runtime_run(run_id)

    def is_busy(self, session_id: str) -> bool:
        with self._guard:
            return bool(
                session_id in self._active
                or self._queued.get(session_id)
            )


coordinator = SessionRunCoordinator()


def suspend_interrupted_goals(session_manager: Any) -> int:
    """Fence goals whose owning process died before completing its run."""
    changed = 0
    for meta in session_manager.list_sessions(limit=10000, include_archived=True):
        state = session_manager.load(meta.session_id)
        if not state:
            continue
        goal = state.context.get("goal") if isinstance(state.context, dict) else None
        if not isinstance(goal, dict) or goal.get("status") != "active":
            continue
        run_id = str(goal.get("run_id") or "")
        run = repos.get_runtime_run(run_id) if run_id else None
        if run_id and run and run.get("status") == "interrupted":
            goal["status"] = "suspended"
            goal["updated_at"] = time.time()
            session_manager.save(state, expected_version=state.version, retries=5)
            changed += 1
    return changed
