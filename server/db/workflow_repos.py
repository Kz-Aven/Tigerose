"""Transactional persistence for the durable workflow runtime.

The tables in this module deliberately do not reuse the session task board.  A
workflow is the recoverable execution record; Goal and transcript data are
projections that may be rebuilt from its event stream.
"""

from __future__ import annotations

import json
import hashlib
import time
import uuid
from typing import Any

from .connection import get_connection


TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "cancelled"})
RUNNABLE_TASK_STATUSES = frozenset({"ready", "retry_wait"})


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _now() -> float:
    return time.time()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _row(row) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def _hydrate_workflow(row) -> dict[str, Any] | None:
    data = _row(row)
    if data is not None:
        data["input_snapshot"] = _loads(data.pop("input_snapshot_json", "{}"), {})
    return data


def _hydrate_task(row) -> dict[str, Any] | None:
    data = _row(row)
    if data is not None:
        data["intent"] = _loads(data.pop("intent_json", "{}"), {})
        data["retry_policy"] = _loads(data.pop("retry_policy_json", "{}"), {})
    return data


def _hydrate_attempt(row) -> dict[str, Any] | None:
    data = _row(row)
    if data is not None:
        data["dispatch"] = _loads(data.pop("dispatch_json", "{}"), {})
        data["receipt"] = _loads(data.pop("receipt_json", "{}"), {})
    return data


def _hydrate_event(row) -> dict[str, Any] | None:
    data = _row(row)
    if data is not None:
        data["payload"] = _loads(data.pop("payload_json", "{}"), {})
    return data


def _hydrate_outbox(row) -> dict[str, Any] | None:
    data = _row(row)
    if data is not None:
        data["payload"] = _loads(data.pop("payload_json", "{}"), {})
    return data


def _begin(conn) -> None:
    conn.execute("BEGIN IMMEDIATE")


def _next_event_sequence(conn, workflow_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM workflow_events WHERE workflow_id = ?",
        (workflow_id,),
    ).fetchone()
    return int(row[0])


def _append_event_tx(
    conn,
    *,
    workflow_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    task_id: str | None = None,
    attempt_id: str | None = None,
    dedupe_key: str = "",
    now: float | None = None,
) -> dict[str, Any]:
    if dedupe_key:
        existing = conn.execute(
            "SELECT * FROM workflow_events WHERE workflow_id = ? AND dedupe_key = ?",
            (workflow_id, dedupe_key),
        ).fetchone()
        if existing is not None:
            return _hydrate_event(existing) or {}
    event_id = _id("wev")
    sequence = _next_event_sequence(conn, workflow_id)
    created_at = _now() if now is None else now
    conn.execute(
        """INSERT INTO workflow_events
           (event_id, workflow_id, task_id, attempt_id, sequence, type, dedupe_key,
            payload_json, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id,
            workflow_id,
            task_id,
            attempt_id,
            sequence,
            event_type,
            dedupe_key,
            _json(payload or {}),
            created_at,
        ),
    )
    return {
        "event_id": event_id,
        "workflow_id": workflow_id,
        "task_id": task_id,
        "attempt_id": attempt_id,
        "sequence": sequence,
        "type": event_type,
        "dedupe_key": dedupe_key,
        "payload": payload or {},
        "created_at": created_at,
    }


def append_event(
    workflow_id: str,
    event_type: str,
    *,
    payload: dict[str, Any] | None = None,
    task_id: str | None = None,
    attempt_id: str | None = None,
    dedupe_key: str = "",
) -> dict[str, Any]:
    """Append an immutable event, returning an earlier one for the same key."""
    conn = get_connection()
    try:
        _begin(conn)
        event = _append_event_tx(
            conn,
            workflow_id=workflow_id,
            event_type=event_type,
            payload=payload,
            task_id=task_id,
            attempt_id=attempt_id,
            dedupe_key=dedupe_key,
        )
        conn.commit()
        return event
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_workflow(
    *,
    kind: str,
    owner_session_id: str,
    title: str,
    intent: dict[str, Any] | None = None,
    executor_kind: str = "agent",
    acceptance_policy: str = "goal",
    root_budget_id: str = "",
    legacy_source: str = "",
    legacy_id: str = "",
    workflow_id: str | None = None,
    task_id: str | None = None,
) -> dict[str, Any]:
    """Create a workflow and its root task in one transaction.

    ``legacy_source``/``legacy_id`` gives V0 shadow writes a stable, idempotent
    identity.  Existing links return their original workflow instead of
    creating a second root task.
    """
    conn = get_connection()
    now = _now()
    try:
        _begin(conn)
        if legacy_source and legacy_id:
            existing = conn.execute(
                "SELECT * FROM workflow_runs WHERE legacy_source = ? AND legacy_id = ?",
                (legacy_source, legacy_id),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return get_workflow(str(existing["workflow_id"])) or {}
        wid = workflow_id or _id("wf")
        tid = task_id or _id("wtask")
        conn.execute(
            """INSERT INTO workflow_runs
               (workflow_id, revision, kind, status, owner_session_id, root_budget_id,
                input_snapshot_json, legacy_source, legacy_id, created_at, updated_at)
               VALUES (?, 1, ?, 'active', ?, ?, ?, ?, ?, ?, ?)""",
            (wid, kind, owner_session_id, root_budget_id, _json(intent or {}), legacy_source, legacy_id, now, now),
        )
        conn.execute(
            """INSERT INTO workflow_tasks
               (task_id, workflow_id, revision, title, intent_json, status, priority,
                executor_kind, acceptance_policy, next_run_at, created_at, updated_at)
               VALUES (?, ?, 1, ?, ?, 'ready', 0, ?, ?, ?, ?, ?)""",
            (tid, wid, title, _json(intent or {}), executor_kind, acceptance_policy, now, now, now),
        )
        _append_event_tx(
            conn,
            workflow_id=wid,
            task_id=tid,
            event_type="WorkflowCreated",
            payload={"kind": kind, "title": title, "owner_session_id": owner_session_id},
            dedupe_key="workflow-created",
            now=now,
        )
        conn.commit()
        return get_workflow(wid) or {}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_workflow(workflow_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        return _hydrate_workflow(conn.execute("SELECT * FROM workflow_runs WHERE workflow_id = ?", (workflow_id,)).fetchone())
    finally:
        conn.close()


def get_workflow_for_scope(workflow_id: str, owner_session_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        return _hydrate_workflow(
            conn.execute(
                "SELECT * FROM workflow_runs WHERE workflow_id = ? AND owner_session_id = ?",
                (workflow_id, owner_session_id),
            ).fetchone()
        )
    finally:
        conn.close()


def get_root_task(workflow_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        return _hydrate_task(
            conn.execute(
                "SELECT * FROM workflow_tasks WHERE workflow_id = ? ORDER BY created_at, task_id LIMIT 1",
                (workflow_id,),
            ).fetchone()
        )
    finally:
        conn.close()


def get_task(task_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        return _hydrate_task(conn.execute("SELECT * FROM workflow_tasks WHERE task_id = ?", (task_id,)).fetchone())
    finally:
        conn.close()


def list_tasks(workflow_id: str) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        return [
            _hydrate_task(row) or {}
            for row in conn.execute(
                "SELECT * FROM workflow_tasks WHERE workflow_id = ? ORDER BY priority DESC, created_at, task_id",
                (workflow_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


def create_task(
    workflow_id: str,
    *,
    title: str,
    intent: dict[str, Any] | None = None,
    executor_kind: str = "manual",
    acceptance_policy: str = "manual",
    priority: int = 0,
    depends_on: list[tuple[str, str]] | None = None,
    parent_task_id: str = "",
    retry_policy: dict[str, Any] | None = None,
    workspace_path: str = "",
    merge_target: str = "",
) -> dict[str, Any]:
    """Create a task and its dependency declarations atomically."""
    conn = get_connection()
    now = _now()
    try:
        _begin(conn)
        workflow = conn.execute(
            "SELECT status FROM workflow_runs WHERE workflow_id=?", (workflow_id,)
        ).fetchone()
        if workflow is None or workflow["status"] != "active":
            raise ValueError("workflow is not active")
        task_id = _id("wtask")
        conn.execute(
            """INSERT INTO workflow_tasks
               (task_id, workflow_id, revision, title, intent_json, status, priority,
                executor_kind, acceptance_policy, next_run_at, parent_task_id, retry_policy_json,
                workspace_path, merge_target, created_at, updated_at)
               VALUES (?, ?, 1, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (task_id, workflow_id, title, _json(intent or {}), priority, executor_kind, acceptance_policy,
             now, parent_task_id, _json(retry_policy or {}), workspace_path, merge_target, now, now),
        )
        for dependency_id, kind in depends_on or []:
            if kind not in {"hard", "soft"}:
                raise ValueError("dependency kind must be hard or soft")
            dependency = conn.execute(
                "SELECT workflow_id FROM workflow_tasks WHERE task_id=?", (dependency_id,)
            ).fetchone()
            if dependency is None or dependency["workflow_id"] != workflow_id:
                raise ValueError("workflow task dependency must be in the same workflow")
            if dependency_id == task_id:
                raise ValueError("workflow task cannot depend on itself")
            conn.execute(
                "INSERT INTO workflow_task_deps (task_id, depends_on_task_id, dependency_kind) VALUES (?, ?, ?)",
                (task_id, dependency_id, kind),
            )
        _append_event_tx(
            conn,
            workflow_id=workflow_id,
            task_id=task_id,
            event_type="TaskCreated",
            payload={"title": title, "executor_kind": executor_kind, "parent_task_id": parent_task_id},
            now=now,
        )
        conn.commit()
        return get_task(task_id) or {}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_attempts(task_id: str) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        return [
            _hydrate_attempt(row) or {}
            for row in conn.execute(
                "SELECT * FROM workflow_attempts WHERE task_id = ? ORDER BY sequence", (task_id,)
            ).fetchall()
        ]
    finally:
        conn.close()


def get_attempt(attempt_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        return _hydrate_attempt(
            conn.execute("SELECT * FROM workflow_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        )
    finally:
        conn.close()


def list_events(workflow_id: str, *, after_sequence: int = 0) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        return [
            _hydrate_event(row) or {}
            for row in conn.execute(
                "SELECT * FROM workflow_events WHERE workflow_id = ? AND sequence > ? ORDER BY sequence",
                (workflow_id, after_sequence),
            ).fetchall()
        ]
    finally:
        conn.close()


def workflow_projection(workflow_id: str) -> dict[str, Any] | None:
    workflow = get_workflow(workflow_id)
    if workflow is None:
        return None
    tasks = list_tasks(workflow_id)
    for task in tasks:
        task["attempts"] = list_attempts(str(task["task_id"]))
        task["lease"] = get_lease(str(task["task_id"]))
        task["artifacts"] = list_artifacts(str(task["task_id"]))
    workflow["tasks"] = tasks
    workflow["events"] = list_events(workflow_id)
    workflow["milestones"] = list_milestones(workflow_id)
    workflow["merge_gates"] = list_merge_gates(workflow_id)
    return workflow


def list_workflows(owner_session_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM workflow_runs WHERE owner_session_id=? ORDER BY updated_at DESC LIMIT ?",
            (owner_session_id, max(1, min(limit, 100))),
        ).fetchall()
        return [_hydrate_workflow(row) or {} for row in rows]
    finally:
        conn.close()


def reap_dependency_blocks(*, now: float | None = None) -> int:
    """Project terminal hard-dependency failures before workers claim work."""
    now = _now() if now is None else now
    conn = get_connection()
    try:
        _begin(conn)
        rows = conn.execute(
            """SELECT DISTINCT t.task_id, t.workflow_id, dep.task_id AS dependency_id, dep.status AS dependency_status
               FROM workflow_tasks t
               JOIN workflow_task_deps d ON d.task_id=t.task_id AND d.dependency_kind='hard'
               JOIN workflow_tasks dep ON dep.task_id=d.depends_on_task_id
               WHERE t.status IN ('ready', 'retry_wait') AND dep.status IN ('failed', 'cancelled', 'blocked_by_dependency')"""
        ).fetchall()
        for row in rows:
            reason = f"{row['dependency_id']}:{row['dependency_status']}"
            conn.execute(
                "UPDATE workflow_tasks SET status='blocked_by_dependency', blocked_reason=?, next_run_at=NULL, updated_at=? WHERE task_id=?",
                (reason, now, row["task_id"]),
            )
            _append_event_tx(conn, workflow_id=str(row["workflow_id"]), task_id=str(row["task_id"]),
                             event_type="TaskBlockedByDependency", payload={"reason": reason}, now=now)
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_lease(task_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        return _row(conn.execute("SELECT * FROM workflow_leases WHERE task_id = ?", (task_id,)).fetchone())
    finally:
        conn.close()


def claim_next_task(
    owner_id: str,
    *,
    lease_seconds: float = 30.0,
    now: float | None = None,
    executor_kinds: set[str] | None = None,
) -> dict[str, Any] | None:
    """Atomically claim one runnable task whose hard dependencies are closed."""
    now = _now() if now is None else now
    conn = get_connection()
    try:
        _begin(conn)
        executor_filter = ""
        params: list[Any] = [now, now]
        if executor_kinds is not None:
            if not executor_kinds:
                conn.commit()
                return None
            placeholders = ", ".join("?" for _ in executor_kinds)
            executor_filter = f" AND t.executor_kind IN ({placeholders})"
            params.extend(sorted(executor_kinds))
        row = conn.execute(
            """SELECT t.* FROM workflow_tasks t
               JOIN workflow_runs w ON w.workflow_id = t.workflow_id
               LEFT JOIN workflow_leases l ON l.task_id = t.task_id
               WHERE t.status IN ('ready', 'retry_wait')
                 AND t.executor_kind NOT IN ('goal_projection', 'manual')
                 AND (t.next_run_at IS NULL OR t.next_run_at <= ?)
                 AND w.status = 'active'
                 AND (l.task_id IS NULL OR l.expires_at <= ?)
                 AND NOT EXISTS (
                   SELECT 1 FROM workflow_task_deps d
                   JOIN workflow_tasks dep ON dep.task_id = d.depends_on_task_id
                   WHERE d.task_id = t.task_id
                     AND d.dependency_kind = 'hard'
                     AND dep.status != 'completed'
                 )
               """ + executor_filter + """
               ORDER BY t.priority DESC, t.next_run_at ASC, t.created_at ASC
               LIMIT 1""",
            params,
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        task_id = str(row["task_id"])
        old = conn.execute(
            "SELECT fencing_token FROM workflow_leases WHERE task_id = ?", (task_id,)
        ).fetchone()
        fence = int(old[0]) + 1 if old is not None else 1
        token = _id("lease")
        conn.execute(
            """INSERT INTO workflow_leases
               (task_id, owner_id, lease_token, fencing_token, expires_at, heartbeat_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(task_id) DO UPDATE SET owner_id=excluded.owner_id,
                 lease_token=excluded.lease_token, fencing_token=excluded.fencing_token,
                 expires_at=excluded.expires_at, heartbeat_at=excluded.heartbeat_at""",
            (task_id, owner_id, token, fence, now + lease_seconds, now),
        )
        conn.execute(
            "UPDATE workflow_tasks SET status = 'running', updated_at = ? WHERE task_id = ?",
            (now, task_id),
        )
        _append_event_tx(
            conn,
            workflow_id=str(row["workflow_id"]),
            task_id=task_id,
            event_type="TaskLeaseClaimed",
            payload={"owner_id": owner_id, "fencing_token": fence},
            now=now,
        )
        conn.commit()
        task = _hydrate_task(row) or {}
        task["status"] = "running"
        task["lease"] = {"task_id": task_id, "owner_id": owner_id, "lease_token": token, "fencing_token": fence, "expires_at": now + lease_seconds, "heartbeat_at": now}
        return task
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def renew_lease(task_id: str, lease_token: str, fencing_token: int, *, lease_seconds: float = 30.0, now: float | None = None) -> bool:
    now = _now() if now is None else now
    conn = get_connection()
    try:
        cur = conn.execute(
            """UPDATE workflow_leases SET expires_at = ?, heartbeat_at = ?
               WHERE task_id = ? AND lease_token = ? AND fencing_token = ? AND expires_at > ?""",
            (now + lease_seconds, now, task_id, lease_token, fencing_token, now),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def expire_leases(*, now: float | None = None) -> int:
    """Release expired leases without assigning a terminal task outcome."""
    now = _now() if now is None else now
    conn = get_connection()
    try:
        _begin(conn)
        rows = conn.execute(
            """SELECT t.task_id, t.workflow_id FROM workflow_tasks t
               JOIN workflow_leases l ON l.task_id = t.task_id
               WHERE l.expires_at <= ? AND t.status = 'running'""",
            (now,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE workflow_tasks SET status = 'ready', next_run_at = ?, updated_at = ? WHERE task_id = ?",
                (now, now, row["task_id"]),
            )
            _append_event_tx(
                conn,
                workflow_id=str(row["workflow_id"]),
                task_id=str(row["task_id"]),
                event_type="TaskLeaseExpired",
                payload={},
                now=now,
            )
        # Retain expired lease rows so the next owner receives a higher fence.
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _lease_is_current(conn, task_id: str, lease_token: str, fencing_token: int, now: float) -> bool:
    return bool(conn.execute(
        """SELECT 1 FROM workflow_leases WHERE task_id = ? AND lease_token = ?
           AND fencing_token = ? AND expires_at > ?""",
        (task_id, lease_token, fencing_token, now),
    ).fetchone())


def prepare_attempt(
    task_id: str,
    *,
    lease_token: str,
    fencing_token: int,
    dispatch: dict[str, Any],
    idempotency_key: str = "",
    outbox_kind: str = "provider_dispatch",
    now: float | None = None,
) -> dict[str, Any] | None:
    """Persist a frozen attempt and dispatch outbox item before any provider call."""
    now = _now() if now is None else now
    conn = get_connection()
    try:
        _begin(conn)
        task = conn.execute("SELECT * FROM workflow_tasks WHERE task_id = ?", (task_id,)).fetchone()
        if task is None or not _lease_is_current(conn, task_id, lease_token, fencing_token, now):
            conn.rollback()
            return None
        # A crash before dispatch leaves a committed pending outbox. Reuse that
        # frozen attempt and its idempotency key; creating another one could
        # turn a safe resume into a duplicate external mutation.
        pending = conn.execute(
            """SELECT a.* FROM workflow_attempts a
               JOIN workflow_outbox o ON o.attempt_id = a.attempt_id
               WHERE a.task_id = ? AND a.status = 'dispatching' AND o.kind = 'provider_dispatch'
                 AND o.status = 'pending'
               ORDER BY a.sequence LIMIT 1""",
            (task_id,),
        ).fetchone()
        if pending is not None:
            conn.commit()
            return _hydrate_attempt(pending)
        sequence = int(conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 FROM workflow_attempts WHERE task_id = ?", (task_id,)
        ).fetchone()[0])
        key = idempotency_key or f"{task_id}:r{int(task['revision'])}:a{sequence}"
        existing = conn.execute(
            "SELECT * FROM workflow_attempts WHERE task_id = ? AND idempotency_key = ?", (task_id, key)
        ).fetchone()
        if existing is not None:
            conn.commit()
            return _hydrate_attempt(existing)
        attempt_id = _id("wattempt")
        conn.execute(
            """INSERT INTO workflow_attempts
               (attempt_id, task_id, sequence, status, intent_revision, idempotency_key,
                dispatch_json, receipt_json, created_at, updated_at)
               VALUES (?, ?, ?, 'dispatching', ?, ?, ?, '{}', ?, ?)""",
            (attempt_id, task_id, sequence, int(task["revision"]), key, _json(dispatch), now, now),
        )
        event = _append_event_tx(
            conn,
            workflow_id=str(task["workflow_id"]),
            task_id=task_id,
            attempt_id=attempt_id,
            event_type="AttemptPrepared",
            payload={"sequence": sequence, "idempotency_key": key},
            now=now,
        )
        outbox_id = _id("wout")
        conn.execute(
            """INSERT INTO workflow_outbox
               (outbox_id, event_id, workflow_id, attempt_id, kind, payload_json, status,
                attempts, available_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)""",
            (outbox_id, event["event_id"], task["workflow_id"], attempt_id, outbox_kind, _json(dispatch), now, now, now),
        )
        conn.commit()
        return {"attempt_id": attempt_id, "task_id": task_id, "sequence": sequence, "status": "dispatching", "intent_revision": int(task["revision"]), "idempotency_key": key, "dispatch": dispatch, "receipt": {}, "outbox_id": outbox_id}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_task_waiting_for_budget(
    task_id: str,
    *,
    lease_token: str,
    fencing_token: int,
    reason: str = "budget_exhausted",
) -> bool:
    now = _now()
    conn = get_connection()
    try:
        _begin(conn)
        task = conn.execute("SELECT workflow_id FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None or not _lease_is_current(conn, task_id, lease_token, fencing_token, now):
            conn.rollback()
            return False
        conn.execute("UPDATE workflow_tasks SET status='waiting_for_budget', updated_at=? WHERE task_id=?", (now, task_id))
        conn.execute("UPDATE workflow_leases SET expires_at=? WHERE task_id=? AND lease_token=? AND fencing_token=?", (now, task_id, lease_token, fencing_token))
        _append_event_tx(conn, workflow_id=str(task["workflow_id"]), task_id=task_id, event_type="TaskWaitingForBudget", payload={"reason": reason}, now=now)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _transition_attempt_tx(
    conn,
    *,
    attempt_id: str,
    task_status: str,
    attempt_status: str,
    receipt: dict[str, Any] | None,
    error_code: str = "",
    lease_token: str,
    fencing_token: int,
    event_type: str,
    payload: dict[str, Any] | None = None,
    now: float,
) -> bool:
    row = conn.execute(
        """SELECT a.*, t.workflow_id FROM workflow_attempts a
           JOIN workflow_tasks t ON t.task_id = a.task_id WHERE a.attempt_id = ?""",
        (attempt_id,),
    ).fetchone()
    if row is None or not _lease_is_current(conn, str(row["task_id"]), lease_token, fencing_token, now):
        return False
    cur = conn.execute(
        """UPDATE workflow_attempts SET status = ?, receipt_json = ?, error_code = ?, updated_at = ?
           WHERE attempt_id = ? AND status = 'dispatching'""",
        (attempt_status, _json(receipt or {}), error_code, now, attempt_id),
    )
    if cur.rowcount != 1:
        return False
    conn.execute(
        "UPDATE workflow_tasks SET status = ?, next_run_at = NULL, updated_at = ? WHERE task_id = ?",
        (task_status, now, row["task_id"]),
    )
    if task_status in TERMINAL_TASK_STATUSES:
        conn.execute(
            "UPDATE workflow_leases SET expires_at = ? WHERE task_id = ? AND lease_token = ? AND fencing_token = ?",
            (now, row["task_id"], lease_token, fencing_token),
        )
    _append_event_tx(
        conn,
        workflow_id=str(row["workflow_id"]),
        task_id=str(row["task_id"]),
        attempt_id=attempt_id,
        event_type=event_type,
        payload=payload or {"status": attempt_status},
        now=now,
    )
    return True


def complete_attempt(attempt_id: str, *, lease_token: str, fencing_token: int, receipt: dict[str, Any] | None = None, now: float | None = None) -> bool:
    now = _now() if now is None else now
    conn = get_connection()
    try:
        _begin(conn)
        ok = _transition_attempt_tx(conn, attempt_id=attempt_id, task_status="completed", attempt_status="succeeded", receipt=receipt, lease_token=lease_token, fencing_token=fencing_token, event_type="AttemptSucceeded", payload={"receipt": receipt or {}}, now=now)
        workflow_id = ""
        if ok:
            row = conn.execute("SELECT t.workflow_id FROM workflow_attempts a JOIN workflow_tasks t ON t.task_id=a.task_id WHERE a.attempt_id=?", (attempt_id,)).fetchone()
            workflow_id = str(row[0]) if row else ""
        conn.commit()
        if workflow_id:
            evaluate_milestones(workflow_id, now=now)
        return ok
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def fail_attempt(attempt_id: str, *, lease_token: str, fencing_token: int, retry_at: float | None = None, error_code: str = "", now: float | None = None) -> bool:
    now = _now() if now is None else now
    conn = get_connection()
    try:
        _begin(conn)
        task_status = "retry_wait" if retry_at is not None else "failed"
        ok = _transition_attempt_tx(conn, attempt_id=attempt_id, task_status=task_status, attempt_status="failed", receipt=None, error_code=error_code, lease_token=lease_token, fencing_token=fencing_token, event_type="AttemptFailed", payload={"retry_at": retry_at, "error_code": error_code}, now=now)
        if ok and retry_at is not None:
            attempt = conn.execute("SELECT task_id FROM workflow_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
            conn.execute("UPDATE workflow_tasks SET next_run_at = ? WHERE task_id = ?", (retry_at, attempt[0]))
            conn.execute("UPDATE workflow_leases SET expires_at = ? WHERE task_id = ? AND lease_token = ? AND fencing_token = ?", (now, attempt[0], lease_token, fencing_token))
        conn.commit()
        return ok
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def schedule_retry(
    attempt_id: str,
    *,
    lease_token: str,
    fencing_token: int,
    error_code: str = "",
    now: float | None = None,
) -> bool:
    """Apply the task's declared bounded exponential retry policy.

    A provider can request a retry, but only this persisted policy decides whether
    the task is retried.  This keeps restart behavior independent of worker RAM.
    """
    now = _now() if now is None else now
    conn = get_connection()
    try:
        _begin(conn)
        row = conn.execute(
            """SELECT a.task_id, a.sequence, t.workflow_id, t.retry_policy_json
               FROM workflow_attempts a JOIN workflow_tasks t ON t.task_id=a.task_id
               WHERE a.attempt_id=?""",
            (attempt_id,),
        ).fetchone()
        if row is None or not _lease_is_current(conn, str(row["task_id"]), lease_token, fencing_token, now):
            conn.rollback()
            return False
        policy = _loads(row["retry_policy_json"], {})
        max_attempts = max(1, int(policy.get("max_attempts", 1)))
        retryable_codes = policy.get("retryable_codes", [])
        allowed = not retryable_codes or error_code in {str(code) for code in retryable_codes}
        if int(row["sequence"]) >= max_attempts or not allowed:
            ok = _transition_attempt_tx(conn, attempt_id=attempt_id, task_status="failed", attempt_status="failed",
                receipt=None, error_code=error_code, lease_token=lease_token, fencing_token=fencing_token,
                event_type="AttemptFailed", payload={"error_code": error_code, "retry_exhausted": True}, now=now)
            conn.commit()
            return ok
        base = max(0.1, float(policy.get("base_delay_s", 1.0)))
        cap = max(base, float(policy.get("max_delay_s", 300.0)))
        # Stable jitter makes a recovered task use the same schedule everywhere.
        seed = int(hashlib.sha256(attempt_id.encode("utf-8")).hexdigest()[:8], 16) / 0xFFFFFFFF
        delay = min(cap, base * (2 ** max(0, int(row["sequence"]) - 1))) * (0.8 + seed * 0.4)
        retry_at = now + delay
        ok = _transition_attempt_tx(conn, attempt_id=attempt_id, task_status="retry_wait", attempt_status="failed",
            receipt=None, error_code=error_code, lease_token=lease_token, fencing_token=fencing_token,
            event_type="AttemptRetryScheduled", payload={"error_code": error_code, "retry_at": retry_at}, now=now)
        if ok:
            conn.execute("UPDATE workflow_tasks SET next_run_at=? WHERE task_id=?", (retry_at, row["task_id"]))
            conn.execute("UPDATE workflow_leases SET expires_at=? WHERE task_id=? AND lease_token=? AND fencing_token=?",
                         (now, row["task_id"], lease_token, fencing_token))
        conn.commit()
        return ok
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_attempt_in_doubt(attempt_id: str, *, lease_token: str, fencing_token: int, receipt: dict[str, Any] | None = None, now: float | None = None) -> bool:
    now = _now() if now is None else now
    conn = get_connection()
    try:
        _begin(conn)
        ok = _transition_attempt_tx(conn, attempt_id=attempt_id, task_status="reconciling", attempt_status="in_doubt", receipt=receipt, lease_token=lease_token, fencing_token=fencing_token, event_type="AttemptInDoubt", payload={"receipt": receipt or {}}, now=now)
        if ok:
            row = conn.execute("SELECT a.task_id, t.workflow_id FROM workflow_attempts a JOIN workflow_tasks t ON t.task_id=a.task_id WHERE a.attempt_id=?", (attempt_id,)).fetchone()
            event = _append_event_tx(conn, workflow_id=str(row["workflow_id"]), task_id=str(row["task_id"]), attempt_id=attempt_id, event_type="ReconciliationRequested", payload={}, now=now)
            conn.execute(
                """INSERT INTO workflow_outbox (outbox_id, event_id, workflow_id, attempt_id, kind,
                   payload_json, status, attempts, available_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'reconcile_request', '{}', 'pending', 0, ?, ?, ?)""",
                (_id("wout"), event["event_id"], row["workflow_id"], attempt_id, now, now, now),
            )
        conn.commit()
        return ok
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def record_reconciliation(attempt_id: str, *, outcome: str, actor: str = "scheduler", evidence: dict[str, Any] | None = None, now: float | None = None) -> dict[str, Any] | None:
    """Apply a reconciler result. Unknown results always require a human decision."""
    if outcome not in {"succeeded", "absent", "unknown"}:
        raise ValueError("invalid reconciliation outcome")
    now = _now() if now is None else now
    conn = get_connection()
    try:
        _begin(conn)
        row = conn.execute(
            """SELECT a.*, t.workflow_id, t.revision, w.owner_session_id FROM workflow_attempts a
               JOIN workflow_tasks t ON t.task_id=a.task_id
               JOIN workflow_runs w ON w.workflow_id=t.workflow_id WHERE a.attempt_id=?""",
            (attempt_id,),
        ).fetchone()
        if row is None or row["status"] != "in_doubt":
            conn.rollback()
            return None
        if outcome == "succeeded":
            conn.execute("UPDATE workflow_attempts SET status='reconciled_succeeded', receipt_json=?, updated_at=? WHERE attempt_id=?", (_json(evidence or {}), now, attempt_id))
            conn.execute("UPDATE workflow_tasks SET status='completed', updated_at=? WHERE task_id=?", (now, row["task_id"]))
            event_type = "AttemptReconciledSucceeded"
            result: dict[str, Any] = {"status": "completed"}
        elif outcome == "absent":
            conn.execute("UPDATE workflow_attempts SET status='reconciled_absent', receipt_json=?, updated_at=? WHERE attempt_id=?", (_json(evidence or {}), now, attempt_id))
            conn.execute("UPDATE workflow_tasks SET status='ready', next_run_at=?, updated_at=? WHERE task_id=?", (now, now, row["task_id"]))
            event_type = "AttemptReconciledAbsent"
            result = {"status": "ready"}
        else:
            conn.execute("UPDATE workflow_tasks SET status='waiting_for_user', updated_at=? WHERE task_id=?", (now, row["task_id"]))
            action_id = _id("pact")
            conn.execute(
                """INSERT INTO pending_actions
                   (action_id, kind, status, session_id, objective_id, objective_revision, attempt_id,
                    tool_name, frozen_args_digest, continuation_ref, run_id, created_at, updated_at,
                    workflow_id, task_id, decision_type, decision_payload_json)
                   VALUES (?, 'workflow_reconciliation', 'pending', ?, '', ?, ?, '', '', '', '', ?, ?, ?, ?, 'reconciliation', ?)""",
                (action_id, row["owner_session_id"], row["revision"], attempt_id, now, now, row["workflow_id"], row["task_id"], _json({"evidence": evidence or {}, "actor": actor})),
            )
            event_type = "ReconciliationUnknown"
            result = {"status": "waiting_for_user", "pending_action_id": action_id}
        _append_event_tx(conn, workflow_id=str(row["workflow_id"]), task_id=str(row["task_id"]), attempt_id=attempt_id, event_type=event_type, payload={"actor": actor, "evidence": evidence or {}}, now=now)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def decide_pending_action(action_id: str, *, owner_session_id: str, decision: str, actor: str, now: float | None = None) -> bool:
    """Apply one of the three explicitly allowed reconciliation decisions."""
    if decision not in {"confirm_succeeded", "retry_confirmed_absent", "cancel"}:
        raise ValueError("invalid workflow decision")
    now = _now() if now is None else now
    conn = get_connection()
    try:
        _begin(conn)
        action = conn.execute(
            "SELECT * FROM pending_actions WHERE action_id=? AND session_id=? AND status='pending'",
            (action_id, owner_session_id),
        ).fetchone()
        if action is None or not action["workflow_id"] or not action["task_id"]:
            conn.rollback()
            return False
        task = conn.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (action["task_id"],)).fetchone()
        if task is None or task["status"] != "waiting_for_user":
            conn.rollback()
            return False
        attempt_id = str(action["attempt_id"] or "")
        if decision == "confirm_succeeded":
            conn.execute("UPDATE workflow_attempts SET status='reconciled_succeeded', updated_at=? WHERE attempt_id=? AND status='in_doubt'", (now, attempt_id))
            conn.execute("UPDATE workflow_tasks SET status='completed', updated_at=? WHERE task_id=?", (now, task["task_id"]))
        elif decision == "retry_confirmed_absent":
            conn.execute("UPDATE workflow_attempts SET status='reconciled_absent', updated_at=? WHERE attempt_id=? AND status='in_doubt'", (now, attempt_id))
            conn.execute("UPDATE workflow_tasks SET status='ready', next_run_at=?, updated_at=? WHERE task_id=?", (now, now, task["task_id"]))
        else:
            conn.execute("UPDATE workflow_attempts SET status='abandoned', updated_at=? WHERE attempt_id=? AND status='in_doubt'", (now, attempt_id))
            conn.execute("UPDATE workflow_tasks SET status='cancelled', updated_at=? WHERE task_id=?", (now, task["task_id"]))
        conn.execute("UPDATE pending_actions SET status='decided', decided_at=?, updated_at=?, resolved_by=? WHERE action_id=?", (now, now, actor, action_id))
        _append_event_tx(conn, workflow_id=str(action["workflow_id"]), task_id=str(task["task_id"]), attempt_id=attempt_id or None, event_type="PendingActionDecided", payload={"decision": decision, "actor": actor}, now=now)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def next_outbox_item(*, now: float | None = None) -> dict[str, Any] | None:
    now = _now() if now is None else now
    conn = get_connection()
    try:
        _begin(conn)
        row = conn.execute(
            "SELECT * FROM workflow_outbox WHERE status='pending' AND kind NOT LIKE 'mesh.%' AND available_at <= ? ORDER BY created_at LIMIT 1",
            (now,),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute("UPDATE workflow_outbox SET status='dispatching', attempts=attempts+1, updated_at=? WHERE outbox_id=? AND status='pending'", (now, row["outbox_id"]))
        conn.commit()
        data = _hydrate_outbox(row) or {}
        data["status"] = "dispatching"
        data["attempts"] = int(data.get("attempts") or 0) + 1
        return data
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def recover_stale_dispatches(*, now: float | None = None) -> int:
    """Turn expired provider dispatches into reconciliation, never a replay."""
    now = _now() if now is None else now
    conn = get_connection()
    try:
        _begin(conn)
        rows = conn.execute(
            """SELECT a.attempt_id, a.task_id, t.workflow_id
               FROM workflow_attempts a
               JOIN workflow_tasks t ON t.task_id=a.task_id
               JOIN workflow_leases l ON l.task_id=t.task_id
               JOIN workflow_outbox o ON o.attempt_id=a.attempt_id
               WHERE a.status='dispatching' AND o.kind='provider_dispatch'
                 AND o.status='dispatching' AND l.expires_at <= ?""",
            (now,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE workflow_attempts SET status='in_doubt', error_code='stale_dispatch', updated_at=? WHERE attempt_id=?",
                (now, row["attempt_id"]),
            )
            conn.execute(
                "UPDATE workflow_tasks SET status='reconciling', updated_at=? WHERE task_id=?",
                (now, row["task_id"]),
            )
            event = _append_event_tx(
                conn,
                workflow_id=str(row["workflow_id"]),
                task_id=str(row["task_id"]),
                attempt_id=str(row["attempt_id"]),
                event_type="AttemptInDoubt",
                payload={"reason": "stale_dispatch"},
                now=now,
            )
            conn.execute(
                """INSERT INTO workflow_outbox (outbox_id, event_id, workflow_id, attempt_id, kind,
                   payload_json, status, attempts, available_at, created_at, updated_at)
                   VALUES (?, ?, ?, ?, 'reconcile_request', '{}', 'pending', 0, ?, ?, ?)""",
                (_id("wout"), event["event_id"], row["workflow_id"], row["attempt_id"], now, now, now),
            )
        # Retain expired tokens for monotonic fencing on the next claim.
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finish_outbox_item(outbox_id: str, *, status: str = "delivered", retry_at: float | None = None) -> bool:
    if status not in {"delivered", "pending", "failed"}:
        raise ValueError("invalid outbox status")
    now = _now()
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE workflow_outbox SET status=?, available_at=?, updated_at=? WHERE outbox_id=? AND status='dispatching'",
            (status, retry_at if retry_at is not None else now, now, outbox_id),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


_BUDGET_LIMIT_KEY = {
    "llm_round": "hard_rounds",
    "tool_call": "hard_tool_calls",
    "diagnostic": "max_diagnostics",
    "write_attempt": "write_attempts",
}


def reserve_budget(
    root_budget_id: str,
    reservation_id: str,
    kind: str,
    amount: float = 1.0,
) -> bool:
    """Atomically reserve a workflow budget across processes and restarts."""
    if kind not in _BUDGET_LIMIT_KEY:
        raise ValueError("unknown budget kind")
    amount = max(0.0, float(amount))
    conn = get_connection()
    now = _now()
    try:
        _begin(conn)
        existing = conn.execute(
            "SELECT status FROM budget_reservations WHERE reservation_id=?", (reservation_id,)
        ).fetchone()
        if existing is not None:
            conn.commit()
            return existing["status"] == "reserved"
        budget = conn.execute(
            "SELECT * FROM runtime_budgets WHERE root_budget_id=?", (root_budget_id,)
        ).fetchone()
        if budget is None:
            conn.rollback()
            return False
        limits = _loads(budget["limits_json"], {})
        consumed = _loads(budget["consumed_json"], {})
        reserved = _loads(budget["reserved_json"], {})
        key = _BUDGET_LIMIT_KEY[kind]
        limit = float(limits.get(key, 0))
        total = float(consumed.get(kind, 0)) + float(reserved.get(kind, 0)) + amount
        if total > limit:
            conn.commit()
            return False
        reserved[kind] = float(reserved.get(kind, 0)) + amount
        conn.execute(
            "UPDATE runtime_budgets SET reserved_json=?, version=version+1, updated_at=? WHERE root_budget_id=?",
            (_json(reserved), now, root_budget_id),
        )
        conn.execute(
            """INSERT INTO budget_reservations
               (reservation_id, root_budget_id, kind, amount, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'reserved', ?, ?)""",
            (reservation_id, root_budget_id, kind, amount, now, now),
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def settle_budget_reservation(reservation_id: str, *, commit: bool, actual: float | None = None) -> bool:
    """Commit consumed budget or release the reservation in one transaction."""
    conn = get_connection()
    now = _now()
    try:
        _begin(conn)
        reservation = conn.execute(
            "SELECT * FROM budget_reservations WHERE reservation_id=? AND status='reserved'",
            (reservation_id,),
        ).fetchone()
        if reservation is None:
            conn.commit()
            return False
        budget = conn.execute(
            "SELECT * FROM runtime_budgets WHERE root_budget_id=?", (reservation["root_budget_id"],)
        ).fetchone()
        if budget is None:
            raise RuntimeError("budget reservation has no budget")
        reserved = _loads(budget["reserved_json"], {})
        consumed = _loads(budget["consumed_json"], {})
        kind = str(reservation["kind"])
        amount = float(reservation["amount"])
        reserved[kind] = max(0.0, float(reserved.get(kind, 0)) - amount)
        if commit:
            consumed[kind] = float(consumed.get(kind, 0)) + (amount if actual is None else max(0.0, float(actual)))
        conn.execute(
            "UPDATE runtime_budgets SET reserved_json=?, consumed_json=?, version=version+1, updated_at=? WHERE root_budget_id=?",
            (_json(reserved), _json(consumed), now, reservation["root_budget_id"]),
        )
        conn.execute("UPDATE budget_reservations SET status=?, updated_at=? WHERE reservation_id=?", ("committed" if commit else "released", now, reservation_id))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def save_context_pack(task_id: str, *, revision: int, summary: str, open_items: list[Any], artifact_refs: list[Any], source_event_seq: int) -> dict[str, Any]:
    conn = get_connection()
    now = _now()
    try:
        _begin(conn)
        task = conn.execute("SELECT workflow_id FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            raise KeyError(f"unknown workflow task: {task_id}")
        latest = int(conn.execute("SELECT COALESCE(MAX(sequence), 0) FROM workflow_events WHERE workflow_id=?", (task["workflow_id"],)).fetchone()[0])
        if source_event_seq > latest:
            raise ValueError("context pack cannot reference a future event")
        pack_id = _id("wctx")
        conn.execute(
            """INSERT INTO workflow_context_packs
               (pack_id, task_id, revision, summary, open_items_json, artifact_refs_json, source_event_seq, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(task_id, revision, source_event_seq) DO UPDATE SET
                 summary=excluded.summary, open_items_json=excluded.open_items_json,
                 artifact_refs_json=excluded.artifact_refs_json""",
            (pack_id, task_id, revision, summary[:20000], _json(open_items), _json(artifact_refs), source_event_seq, now),
        )
        stored = conn.execute(
            "SELECT pack_id, created_at FROM workflow_context_packs WHERE task_id=? AND revision=? AND source_event_seq=?",
            (task_id, revision, source_event_seq),
        ).fetchone()
        _append_event_tx(conn, workflow_id=str(task["workflow_id"]), task_id=task_id, event_type="ContextPackSaved", payload={"revision": revision, "source_event_seq": source_event_seq}, dedupe_key=f"context:{task_id}:{revision}:{source_event_seq}", now=now)
        conn.commit()
        return {"pack_id": stored["pack_id"], "task_id": task_id, "revision": revision, "summary": summary[:20000], "open_items": open_items, "artifact_refs": artifact_refs, "source_event_seq": source_event_seq, "created_at": stored["created_at"]}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def latest_context_pack(task_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM workflow_context_packs WHERE task_id=? ORDER BY source_event_seq DESC, created_at DESC LIMIT 1", (task_id,)).fetchone()
        data = _row(row)
        if data is not None:
            data["open_items"] = _loads(data.pop("open_items_json", "[]"), [])
            data["artifact_refs"] = _loads(data.pop("artifact_refs_json", "[]"), [])
        return data
    finally:
        conn.close()


def record_artifact(
    task_id: str,
    *,
    kind: str,
    uri: str = "",
    sha256: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    conn = get_connection()
    now = _now()
    artifact_id = _id("wart")
    try:
        _begin(conn)
        task = conn.execute("SELECT workflow_id FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            raise KeyError(f"unknown workflow task: {task_id}")
        existing = conn.execute(
            "SELECT * FROM workflow_artifacts WHERE task_id=? AND kind=? AND uri=? AND sha256=?",
            (task_id, kind, uri, sha256),
        ).fetchone()
        if existing is not None:
            conn.commit()
            data = _row(existing) or {}
            data["metadata"] = _loads(data.pop("metadata_json", "{}"), {})
            return data
        conn.execute(
            """INSERT INTO workflow_artifacts
               (artifact_id, task_id, kind, uri, sha256, metadata_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (artifact_id, task_id, kind, uri, sha256, _json(metadata or {}), now),
        )
        _append_event_tx(
            conn,
            workflow_id=str(task["workflow_id"]),
            task_id=task_id,
            event_type="ArtifactRecorded",
            payload={"artifact_id": artifact_id, "kind": kind, "uri": uri, "sha256": sha256},
            now=now,
        )
        workflow_id = str(task["workflow_id"])
        conn.commit()
        evaluate_milestones(workflow_id, now=now)
        return {"artifact_id": artifact_id, "task_id": task_id, "kind": kind, "uri": uri, "sha256": sha256, "metadata": metadata or {}, "created_at": now}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_artifacts(task_id: str) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        out = []
        for row in conn.execute(
            "SELECT * FROM workflow_artifacts WHERE task_id=? ORDER BY created_at, artifact_id", (task_id,)
        ).fetchall():
            data = _row(row) or {}
            data["metadata"] = _loads(data.pop("metadata_json", "{}"), {})
            out.append(data)
        return out
    finally:
        conn.close()


def create_milestone(
    workflow_id: str,
    *,
    title: str,
    task_ids: list[str],
    required_task_ids: set[str] | None = None,
) -> dict[str, Any]:
    if not task_ids:
        raise ValueError("milestone requires tasks")
    conn = get_connection()
    now = _now()
    try:
        _begin(conn)
        count = conn.execute(
            "SELECT COUNT(*) FROM workflow_tasks WHERE workflow_id=? AND task_id IN (%s)" % ",".join("?" * len(task_ids)),
            [workflow_id, *task_ids],
        ).fetchone()[0]
        if int(count) != len(set(task_ids)):
            raise ValueError("milestone tasks must belong to the workflow")
        milestone_id = _id("wmile")
        conn.execute("INSERT INTO workflow_milestones (milestone_id, workflow_id, title, created_at) VALUES (?, ?, ?, ?)",
                     (milestone_id, workflow_id, title, now))
        required = required_task_ids if required_task_ids is not None else set(task_ids)
        conn.executemany("INSERT INTO workflow_milestone_tasks (milestone_id, task_id, required) VALUES (?, ?, ?)",
                         [(milestone_id, task_id, int(task_id in required)) for task_id in task_ids])
        _append_event_tx(conn, workflow_id=workflow_id, event_type="MilestoneCreated",
                         payload={"milestone_id": milestone_id, "title": title, "task_ids": task_ids}, now=now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    evaluate_milestones(workflow_id)
    return get_milestone(milestone_id) or {}


def get_milestone(milestone_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM workflow_milestones WHERE milestone_id=?", (milestone_id,)).fetchone()
        data = _row(row)
        if data is not None:
            data["tasks"] = [_row(item) or {} for item in conn.execute(
                "SELECT mt.task_id, mt.required, t.status, t.acceptance_policy FROM workflow_milestone_tasks mt JOIN workflow_tasks t ON t.task_id=mt.task_id WHERE mt.milestone_id=?",
                (milestone_id,),
            ).fetchall()]
        return data
    finally:
        conn.close()


def list_milestones(workflow_id: str) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        ids = [row[0] for row in conn.execute("SELECT milestone_id FROM workflow_milestones WHERE workflow_id=? ORDER BY created_at", (workflow_id,)).fetchall()]
    finally:
        conn.close()
    return [item for item in (get_milestone(mid) for mid in ids) if item is not None]


def evaluate_milestones(workflow_id: str, *, now: float | None = None) -> int:
    now = _now() if now is None else now
    conn = get_connection()
    try:
        _begin(conn)
        rows = conn.execute("SELECT milestone_id FROM workflow_milestones WHERE workflow_id=? AND status='pending'", (workflow_id,)).fetchall()
        accepted = 0
        for row in rows:
            invalid = conn.execute(
                """SELECT 1 FROM workflow_milestone_tasks mt JOIN workflow_tasks t ON t.task_id=mt.task_id
                   WHERE mt.milestone_id=? AND mt.required=1 AND (
                     t.status!='completed' OR (t.acceptance_policy='artifact_required' AND NOT EXISTS (
                       SELECT 1 FROM workflow_artifacts a WHERE a.task_id=t.task_id AND a.sha256!=''
                     ))
                   ) LIMIT 1""",
                (row["milestone_id"],),
            ).fetchone()
            if invalid is None:
                conn.execute("UPDATE workflow_milestones SET status='accepted', accepted_at=? WHERE milestone_id=?", (now, row["milestone_id"]))
                _append_event_tx(conn, workflow_id=workflow_id, event_type="MilestoneAccepted",
                                 payload={"milestone_id": row["milestone_id"]}, now=now)
                accepted += 1
        conn.commit()
        return accepted
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def revise_task(task_id: str, *, intent: dict[str, Any], actor: str, now: float | None = None) -> dict[str, Any] | None:
    """Fence one task and every unfinished downstream hard dependent."""
    now = _now() if now is None else now
    conn = get_connection()
    try:
        _begin(conn)
        root = conn.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
        if root is None:
            conn.rollback()
            return None
        affected = conn.execute(
            """WITH RECURSIVE downstream(task_id) AS (
                 SELECT ? UNION SELECT d.task_id FROM workflow_task_deps d
                 JOIN downstream x ON d.depends_on_task_id=x.task_id WHERE d.dependency_kind='hard'
               ) SELECT task_id FROM downstream""",
            (task_id,),
        ).fetchall()
        ids = [str(row[0]) for row in affected]
        for current_id in ids:
            task = conn.execute("SELECT * FROM workflow_tasks WHERE task_id=?", (current_id,)).fetchone()
            if task is None or task["status"] == "completed":
                continue
            next_intent = intent if current_id == task_id else _loads(task["intent_json"], {})
            conn.execute("UPDATE workflow_tasks SET revision=revision+1, intent_json=?, status='ready', blocked_reason='', next_run_at=?, updated_at=? WHERE task_id=?",
                         (_json(next_intent), now, now, current_id))
            conn.execute("UPDATE workflow_leases SET expires_at=? WHERE task_id=?", (now, current_id))
            _append_event_tx(conn, workflow_id=str(task["workflow_id"]), task_id=current_id,
                             event_type="TaskRevised", payload={"actor": actor, "root_task_id": task_id}, now=now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_task(task_id)


def create_merge_gate(task_id: str, *, merge_target: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    conn = get_connection()
    now = _now()
    try:
        _begin(conn)
        task = conn.execute("SELECT workflow_id FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            raise KeyError(task_id)
        existing = conn.execute("SELECT * FROM workflow_merge_gates WHERE task_id=?", (task_id,)).fetchone()
        if existing is not None:
            conn.commit()
            data = _row(existing) or {}
            data["metadata"] = _loads(data.pop("metadata_json", "{}"), {})
            return data
        gate_id = _id("wmerge")
        conn.execute("INSERT INTO workflow_merge_gates (gate_id, workflow_id, task_id, merge_target, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                     (gate_id, task["workflow_id"], task_id, merge_target, _json(metadata or {}), now))
        _append_event_tx(conn, workflow_id=str(task["workflow_id"]), task_id=task_id, event_type="MergeGateCreated",
                         payload={"gate_id": gate_id, "merge_target": merge_target}, now=now)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return get_merge_gate(gate_id) or {}


def get_merge_gate(gate_id: str) -> dict[str, Any] | None:
    conn = get_connection()
    try:
        data = _row(conn.execute("SELECT * FROM workflow_merge_gates WHERE gate_id=?", (gate_id,)).fetchone())
        if data is not None:
            data["metadata"] = _loads(data.pop("metadata_json", "{}"), {})
        return data
    finally:
        conn.close()


def list_merge_gates(workflow_id: str) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        ids = [row[0] for row in conn.execute("SELECT gate_id FROM workflow_merge_gates WHERE workflow_id=? ORDER BY created_at", (workflow_id,)).fetchall()]
    finally:
        conn.close()
    return [item for item in (get_merge_gate(gate_id) for gate_id in ids) if item is not None]


def decide_merge_gate(gate_id: str, *, owner_session_id: str, approved: bool, actor: str, now: float | None = None) -> bool:
    now = _now() if now is None else now
    conn = get_connection()
    try:
        _begin(conn)
        gate = conn.execute("SELECT g.*, w.owner_session_id FROM workflow_merge_gates g JOIN workflow_runs w ON w.workflow_id=g.workflow_id WHERE g.gate_id=?", (gate_id,)).fetchone()
        if gate is None or gate["owner_session_id"] != owner_session_id or gate["status"] != "pending":
            conn.rollback()
            return False
        status = "approved" if approved else "rejected"
        conn.execute("UPDATE workflow_merge_gates SET status=?, decision_by=?, decision_at=? WHERE gate_id=?", (status, actor, now, gate_id))
        conn.execute("UPDATE workflow_tasks SET status=?, updated_at=? WHERE task_id=? AND status='waiting_for_merge'", ("completed" if approved else "waiting_for_user", now, gate["task_id"]))
        _append_event_tx(conn, workflow_id=str(gate["workflow_id"]), task_id=str(gate["task_id"]), event_type="MergeGateDecided",
                         payload={"gate_id": gate_id, "approved": approved, "actor": actor}, now=now)
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def mark_task_waiting_for_merge(task_id: str, *, merge_target: str, receipt: dict[str, Any] | None = None) -> dict[str, Any]:
    """Create the manual-only merge gate after code validation succeeds."""
    conn = get_connection()
    now = _now()
    try:
        _begin(conn)
        task = conn.execute("SELECT workflow_id FROM workflow_tasks WHERE task_id=?", (task_id,)).fetchone()
        if task is None:
            raise KeyError(task_id)
        conn.execute("UPDATE workflow_tasks SET status='waiting_for_merge', merge_target=?, updated_at=? WHERE task_id=? AND status='completed'",
                     (merge_target, now, task_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return create_merge_gate(task_id, merge_target=merge_target, metadata=receipt or {})


def set_task_worktree(task_id: str, worktree_path: str) -> None:
    conn = get_connection()
    try:
        conn.execute("UPDATE workflow_tasks SET worktree_path=?, updated_at=? WHERE task_id=?", (worktree_path, _now(), task_id))
        conn.commit()
    finally:
        conn.close()


def project_goal_bindings(
    workflow_id: str,
    task_id: str,
    *,
    bindings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Persist the Stop Gate's binding snapshot as a durable projection."""
    return append_event(
        workflow_id,
        "GoalBindingsProjected",
        task_id=task_id,
        payload={"bindings": bindings},
    )


def latest_goal_bindings(workflow_id: str) -> list[dict[str, Any]] | None:
    conn = get_connection()
    try:
        row = conn.execute(
            """SELECT payload_json FROM workflow_events
               WHERE workflow_id = ? AND type = 'GoalBindingsProjected'
               ORDER BY sequence DESC LIMIT 1""",
            (workflow_id,),
        ).fetchone()
        if row is None:
            return None
        payload = _loads(row[0], {})
        bindings = payload.get("bindings") if isinstance(payload, dict) else None
        return bindings if isinstance(bindings, list) else []
    finally:
        conn.close()


def goal_evidence(workflow_id: str) -> list[dict[str, Any]]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT payload_json FROM workflow_events
               WHERE workflow_id = ? AND type = 'GoalEvidence' ORDER BY sequence""",
            (workflow_id,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            payload = _loads(row[0], {})
            evidence = payload.get("evidence") if isinstance(payload, dict) else None
            if isinstance(evidence, dict):
                out.append(evidence)
        return out
    finally:
        conn.close()


def project_goal_terminal(
    workflow_id: str,
    task_id: str,
    *,
    goal_status: str,
    reason: str = "",
) -> bool:
    """Update the root Goal projection; normal workers cannot perform this write."""
    task_status = {
        "achieved": "completed",
        "cancelled": "cancelled",
        "suspended": "suspended",
        "cleared": "cancelled",
    }.get(goal_status)
    if task_status is None:
        return False
    workflow_status = "completed" if task_status == "completed" else task_status
    now = _now()
    conn = get_connection()
    try:
        _begin(conn)
        task = conn.execute(
            "SELECT workflow_id FROM workflow_tasks WHERE task_id = ? AND workflow_id = ?",
            (task_id, workflow_id),
        ).fetchone()
        if task is None:
            conn.rollback()
            return False
        conn.execute(
            "UPDATE workflow_tasks SET status=?, updated_at=? WHERE task_id=?",
            (task_status, now, task_id),
        )
        conn.execute(
            "UPDATE workflow_runs SET status=?, updated_at=?, terminal_at=? WHERE workflow_id=?",
            (workflow_status, now, now, workflow_id),
        )
        _append_event_tx(
            conn,
            workflow_id=workflow_id,
            task_id=task_id,
            event_type="GoalTerminalProjected",
            payload={"goal_status": goal_status, "reason": reason[:1000]},
            now=now,
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def reopen_goal_projection(workflow_id: str, task_id: str) -> bool:
    """Resume a suspended legacy Goal projection without replaying an attempt."""
    now = _now()
    conn = get_connection()
    try:
        _begin(conn)
        task = conn.execute(
            "SELECT 1 FROM workflow_tasks WHERE task_id=? AND workflow_id=?",
            (task_id, workflow_id),
        ).fetchone()
        if task is None:
            conn.rollback()
            return False
        conn.execute(
            "UPDATE workflow_tasks SET status='ready', next_run_at=?, updated_at=? WHERE task_id=?",
            (now, now, task_id),
        )
        conn.execute(
            "UPDATE workflow_runs SET status='active', terminal_at=NULL, updated_at=? WHERE workflow_id=?",
            (now, workflow_id),
        )
        _append_event_tx(
            conn,
            workflow_id=workflow_id,
            task_id=task_id,
            event_type="GoalResumedProjected",
            payload={},
            now=now,
        )
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
