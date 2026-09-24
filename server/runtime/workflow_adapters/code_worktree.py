"""Local, no-shell code workflow executor with a manual merge gate."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Literal

from avent_paths import data_root, ensure_data_dirs
from server.db import workflow_repos
from server.runtime.workflow_runtime import ExecutionResult


def _run(args: list[str], *, cwd: Path, timeout: int = 300) -> tuple[int, str]:
    try:
        process = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return process.returncode, ((process.stdout or "") + (process.stderr or ""))[-20000:]


def _worktree_path(workflow_id: str, task_id: str) -> Path:
    ensure_data_dirs()
    return data_root() / ".workflow-worktrees" / workflow_id / task_id


def _prepare_worktree(task: dict[str, Any]) -> tuple[Path | None, str, str]:
    workspace = Path(str(task.get("workspace_path") or "")).expanduser()
    if not workspace.is_dir():
        return None, "", "workspace_not_found"
    code, base = _run(["git", "rev-parse", "--verify", "HEAD"], cwd=workspace, timeout=30)
    if code != 0:
        return None, "", "workspace_not_git_repository"
    target = _worktree_path(str(task["workflow_id"]), str(task["task_id"]))
    if target.exists():
        return target, base.strip(), ""
    target.parent.mkdir(parents=True, exist_ok=True)
    code, output = _run(["git", "worktree", "add", "--detach", str(target), base.strip()], cwd=workspace, timeout=90)
    if code != 0:
        return None, "", f"worktree_create_failed:{output[:200]}"
    return target, base.strip(), ""


def execute(task: dict[str, Any], attempt: dict[str, Any]) -> ExecutionResult:
    intent = dict(attempt.get("dispatch") or {})
    commands = intent.get("commands")
    if not isinstance(commands, list) or not commands or any(
        not isinstance(command, list) or not command or any(not isinstance(part, str) or "\x00" in part for part in command)
        for command in commands
    ):
        return ExecutionResult("failed", error_code="commands_must_be_nonempty_argument_arrays")
    worktree, base, error = _prepare_worktree(task)
    if worktree is None:
        return ExecutionResult("failed", error_code=error)
    workflow_repos.set_task_worktree(str(task["task_id"]), str(worktree))
    for index, command in enumerate(commands):
        code, output = _run(command, cwd=worktree, timeout=max(1, min(900, int(intent.get("timeout_seconds") or 300))))
        digest = hashlib.sha256(output.encode("utf-8")).hexdigest()
        workflow_repos.record_artifact(str(task["task_id"]), kind="command_log", uri=f"file://{worktree}/.workflow-command-{index}.log", sha256=digest,
                                       metadata={"command": command, "exit_code": code, "output": output})
        if code != 0:
            return ExecutionResult("failed", error_code=f"command_{index}_failed")
    _, diff = _run(["git", "diff", "--binary"], cwd=worktree, timeout=60)
    workflow_repos.record_artifact(str(task["task_id"]), kind="git_diff", uri=f"file://{worktree}",
                                   sha256=hashlib.sha256(diff.encode("utf-8")).hexdigest(),
                                   metadata={"base_revision": base, "worktree_path": str(worktree)})
    return ExecutionResult("succeeded", receipt={"base_revision": base, "worktree_path": str(worktree), "merge_target": str(task.get("merge_target") or "")})


def reconcile(task: dict[str, Any], attempt: dict[str, Any]) -> Literal["succeeded", "absent", "unknown"]:
    receipt = dict(attempt.get("receipt") or {})
    path = Path(str(receipt.get("worktree_path") or task.get("worktree_path") or ""))
    if path.is_dir() and workflow_repos.list_artifacts(str(task["task_id"])):
        return "succeeded"
    return "absent"
