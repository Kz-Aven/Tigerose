"""Git worktree helpers under .worktrees/."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from avent_paths import data_root, ensure_data_dirs

from server.runtime.tools import board as task_board

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _worktrees_dir() -> Path:
    ensure_data_dirs()
    d = data_root() / ".worktrees"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_git(args: list[str]) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(data_root()),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception as exc:
        return False, str(exc)
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    return proc.returncode == 0, out


def create_worktree(name: str, task_id: str = "", *, expected_scope: str | None = None) -> str:
    if not _NAME_RE.match(name or ""):
        return f"Invalid worktree name: {name}"
    path = _worktrees_dir() / name
    if path.exists():
        return f"Worktree already exists: {name}"
    if task_id:
        task = task_board.get_task(task_id)
        if not task:
            return f"Task not found: {task_id}"
        if expected_scope is not None and task.get("scope_key") != expected_scope:
            return f"Task scope mismatch: {task_id}"
    ok, result = _run_git(["worktree", "add", str(path), "-b", f"wt/{name}", "HEAD"])
    if not ok:
        return f"Failed to create worktree: {result}"
    if task_id:
        task_board.bind_worktree(task_id, name)
    return f"Created worktree {name} at {path}"


def remove_worktree(name: str, discard_changes: bool = False) -> str:
    if not _NAME_RE.match(name or ""):
        return f"Invalid worktree name: {name}"
    path = _worktrees_dir() / name
    if not path.exists():
        return f"Worktree not found: {name}"
    if not discard_changes:
        ok, status = _run_git(["-C", str(path), "status", "--porcelain"])
        if ok and status.strip():
            return (
                "Worktree has uncommitted changes. "
                "Pass discard_changes=true or keep_worktree."
            )
    ok, result = _run_git(["worktree", "remove", str(path), "--force"])
    if not ok:
        return f"Failed to remove worktree: {result}"
    return f"Removed worktree {name}"


def keep_worktree(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        return f"Invalid worktree name: {name}"
    path = _worktrees_dir() / name
    if not path.exists():
        return f"Worktree not found: {name}"
    marker = path / ".avent_keep"
    marker.write_text("keep\n", encoding="utf-8")
    return f"Kept worktree {name}"
