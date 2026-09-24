"""Persistent task board keyed by scope_key (SQLite runtime_tasks)."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from server.db.connection import get_connection


def _id() -> str:
    return f"rtask_{uuid.uuid4().hex[:10]}"


def _now() -> float:
    return time.time()


def _row(r) -> dict[str, Any]:
    d = dict(r)
    try:
        d["blocked_by"] = json.loads(d.get("blocked_by") or "[]")
    except Exception:
        d["blocked_by"] = []
    return d


def create_task(
    scope_key: str,
    subject: str,
    *,
    description: str = "",
    blocked_by: list[str] | None = None,
    goal_id: str = "",
    run_id: str = "",
) -> dict:
    tid = _id()
    now = _now()
    blocked_by = blocked_by or []
    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO runtime_tasks
               (task_id, scope_key, title, description, owner, status, blocked_by, worktree, created_at, updated_at)
               VALUES (?, ?, ?, ?, '', 'pending', ?, NULL, ?, ?)""",
            (tid, scope_key, subject, description, json.dumps(blocked_by), now, now),
        )
        if goal_id:
            conn.execute(
                """INSERT OR IGNORE INTO runtime_goal_tasks
                   (goal_id, task_id, scope_key, run_id, bound_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (goal_id, tid, scope_key, run_id, now),
            )
        conn.commit()
    finally:
        conn.close()
    return {
        "task_id": tid,
        "scope_key": scope_key,
        "title": subject,
        "description": description,
        "owner": "",
        "status": "pending",
        "blocked_by": blocked_by,
        "worktree": None,
        "created_at": now,
        "updated_at": now,
    }


def list_tasks(scope_key: str) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """SELECT * FROM runtime_tasks
               WHERE scope_key = ? AND status != 'archived'
               ORDER BY created_at ASC""",
            (scope_key,),
        ).fetchall()
        return [_row(r) for r in rows]
    finally:
        conn.close()


def get_task(task_id: str) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM runtime_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return _row(row) if row else None
    finally:
        conn.close()


def _set_status(
    task_id: str,
    status: str,
    *,
    expected_scope: str | None = None,
    goal_id: str = "",
    run_id: str = "",
    **extra,
) -> dict | None:
    now = _now()
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM runtime_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        task = _row(row) if row else None
        if not task:
            return None
        if expected_scope is not None and task.get("scope_key") != expected_scope:
            raise PermissionError(
                f"Task scope mismatch: expected {expected_scope}, found {task.get('scope_key')}"
            )
        fields = ["status = ?", "updated_at = ?"]
        vals: list[Any] = [status, now]
        if "owner" in extra:
            fields.append("owner = ?")
            vals.append(extra["owner"])
        if "worktree" in extra:
            fields.append("worktree = ?")
            vals.append(extra["worktree"])
        if "description_append" in extra and extra["description_append"]:
            fields.append("description = ?")
            vals.append((task.get("description") or "") + "\n" + extra["description_append"])
        vals.append(task_id)
        conn.execute(
            f"UPDATE runtime_tasks SET {', '.join(fields)} WHERE task_id = ?",
            vals,
        )
        if goal_id:
            conn.execute(
                """INSERT OR IGNORE INTO runtime_goal_tasks
                   (goal_id, task_id, scope_key, run_id, bound_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (goal_id, task_id, task["scope_key"], run_id, now),
            )
        conn.commit()
    finally:
        conn.close()
    return get_task(task_id)


def claim_task(
    task_id: str,
    owner: str = "agent",
    *,
    expected_scope: str | None = None,
    goal_id: str = "",
    run_id: str = "",
) -> str:
    task = get_task(task_id)
    if not task:
        return f"Task not found: {task_id}"
    if expected_scope is not None and task.get("scope_key") != expected_scope:
        return f"Task scope mismatch: expected {expected_scope}, found {task.get('scope_key')}"
    if task["status"] == "in_progress" and task.get("owner") and task["owner"] != owner:
        return f"Task already claimed by {task['owner']}"
    if task["status"] not in ("pending", "in_progress"):
        return f"Cannot claim task in status {task['status']}"
    for dep in task.get("blocked_by") or []:
        dep_t = get_task(dep)
        if dep_t and dep_t["status"] != "completed":
            return f"Blocked by incomplete task {dep}"
    _set_status(
        task_id,
        "in_progress",
        owner=owner,
        expected_scope=expected_scope,
        goal_id=goal_id,
        run_id=run_id,
    )
    return f"Claimed {task_id} ({task['title']})"


def complete_task(task_id: str, *, expected_scope: str | None = None) -> str:
    task = get_task(task_id)
    if not task:
        return f"Task not found: {task_id}"
    if expected_scope is not None and task.get("scope_key") != expected_scope:
        return f"Task scope mismatch: expected {expected_scope}, found {task.get('scope_key')}"
    if task["status"] != "in_progress":
        return f"Task is {task['status']}, expected in_progress"
    _set_status(task_id, "completed", expected_scope=expected_scope)
    return f"Completed {task_id}"


def fail_task(task_id: str, reason: str = "", *, expected_scope: str | None = None) -> str:
    task = get_task(task_id)
    if not task:
        return f"Task not found: {task_id}"
    if expected_scope is not None and task.get("scope_key") != expected_scope:
        return f"Task scope mismatch: expected {expected_scope}, found {task.get('scope_key')}"
    _set_status(
        task_id,
        "failed",
        expected_scope=expected_scope,
        description_append=f"[failed] {reason}" if reason else "",
    )
    return f"Failed {task_id}"


def cancel_task(task_id: str, reason: str = "", *, expected_scope: str | None = None) -> str:
    task = get_task(task_id)
    if not task:
        return f"Task not found: {task_id}"
    if expected_scope is not None and task.get("scope_key") != expected_scope:
        return f"Task scope mismatch: expected {expected_scope}, found {task.get('scope_key')}"
    _set_status(
        task_id,
        "cancelled",
        expected_scope=expected_scope,
        description_append=f"[cancelled] {reason}" if reason else "",
    )
    return f"Cancelled {task_id}"


def release_task(task_id: str, *, expected_scope: str | None = None) -> str:
    task = get_task(task_id)
    if not task:
        return f"Task not found: {task_id}"
    if expected_scope is not None and task.get("scope_key") != expected_scope:
        return f"Task scope mismatch: expected {expected_scope}, found {task.get('scope_key')}"
    if task["status"] != "in_progress":
        return f"Task is {task['status']}, expected in_progress"
    _set_status(task_id, "pending", owner="", expected_scope=expected_scope)
    return f"Released {task_id}"


def delete_task(task_id: str, *, expected_scope: str | None = None) -> str:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT scope_key FROM runtime_tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            return f"Task not found: {task_id}"
        if expected_scope is not None and str(row[0]) != expected_scope:
            return f"Task scope mismatch: expected {expected_scope}, found {row[0]}"
        cur = conn.execute("DELETE FROM runtime_tasks WHERE task_id = ?", (task_id,))
        conn.commit()
        return f"Deleted {task_id}"
    finally:
        conn.close()


def archive_completed(scope_key: str, older_than_days: int = 7) -> str:
    cutoff = _now() - max(0, older_than_days) * 86400
    conn = get_connection()
    try:
        cur = conn.execute(
            """UPDATE runtime_tasks SET status = 'archived', updated_at = ?
               WHERE scope_key = ? AND status = 'completed' AND updated_at < ?""",
            (_now(), scope_key, cutoff),
        )
        conn.commit()
        n = cur.rowcount
        return f"Archived {n} completed task(s)" if n else "No tasks to archive"
    finally:
        conn.close()


def bind_worktree(task_id: str, worktree: str) -> None:
    task = get_task(task_id)
    if not task:
        return
    _set_status(task_id, task["status"], worktree=worktree)


def format_list(scope_key: str) -> str:
    tasks = list_tasks(scope_key)
    if not tasks:
        return "No active tasks — create_task for new work."
    lines = []
    for t in tasks:
        owner = t.get("owner") or "-"
        wt = t.get("worktree") or "-"
        lines.append(
            f"  {t['task_id']}: [{t['status']}] {t['title']} (owner={owner}, worktree={wt})"
        )
    return "\n".join(lines)


def format_one(task_id: str) -> str:
    t = get_task(task_id)
    if not t:
        return f"Task not found: {task_id}"
    return json.dumps(t, ensure_ascii=False, indent=2)
