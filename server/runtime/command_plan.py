"""Classify shell commands before they enter the permission gate."""

from __future__ import annotations

import shlex
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

CommandRisk = Literal["verified_read", "unknown", "mutation", "high_risk"]

_SHELL_BLOCKERS = {";", "|", "||", "&", "<", ">", ">>", "<<"}
_READ_ONLY_PROGRAMS = {
    "ls", "pwd", "cat", "head", "tail", "wc", "file", "stat", "which",
    "type", "dirname", "basename", "rg", "grep", "find",
}
_HIGH_RISK_PROGRAMS = {
    "rm", "sudo", "shutdown", "reboot", "mkfs", "dd", "kill", "pkill",
    "curl", "wget", "ssh", "scp", "rsync", "chmod", "chown",
}
_MUTATION_PROGRAMS = {"touch", "mkdir", "cp", "mv", "truncate", "tee"}
_GIT_READ_ONLY = {"status", "log", "diff", "branch", "remote", "rev-parse", "show"}
_GIT_HIGH_RISK = {"push", "reset", "clean", "commit", "checkout", "switch", "merge", "rebase", "restore"}
_GIT_MUTATION = {"add", "mv", "rm", "stash", "tag"}


@dataclass(frozen=True)
class CommandPlan:
    command: str
    argv_groups: tuple[tuple[str, ...], ...]
    risk: CommandRisk

    @property
    def grant_group(self) -> str:
        return {
            "unknown": "command.unknown",
            "mutation": "command.mutation",
        }.get(self.risk, "")

    @property
    def can_run_without_shell(self) -> bool:
        return self.risk == "verified_read" and bool(self.argv_groups)


def classify_bash_command(command: str) -> CommandPlan:
    """Return a conservative command plan.

    ``verified_read`` is deliberately narrow: every segment must be known
    read-only, contain no shell expansion, and reference only relative paths.
    Everything else remains visible to the user through the permission flow.
    """
    raw = (command or "").strip()
    groups = _split_and_parse(raw)
    if not groups:
        return CommandPlan(raw, (), "unknown")
    risks = [_argv_risk(argv) for argv in groups]
    if "high_risk" in risks:
        risk: CommandRisk = "high_risk"
    elif "mutation" in risks:
        risk = "mutation"
    elif all(item == "verified_read" for item in risks):
        risk = "verified_read"
    else:
        risk = "unknown"
    return CommandPlan(raw, tuple(tuple(item) for item in groups), risk)


def _split_and_parse(command: str) -> list[list[str]]:
    if not command or "\n" in command or "`" in command or "$" in command:
        return []
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        return []
    groups: list[list[str]] = [[]]
    for token in tokens:
        if token == "&&":
            if not groups[-1]:
                return []
            groups.append([])
        elif token in _SHELL_BLOCKERS or token.startswith(("<", ">", "|", ";", "&")):
            return []
        else:
            groups[-1].append(token)
    if not groups or any(not group for group in groups):
        return []
    return groups


def _argv_risk(argv: list[str]) -> CommandRisk:
    program = argv[0]
    if program in _HIGH_RISK_PROGRAMS:
        return "high_risk"
    if program in _MUTATION_PROGRAMS:
        return "mutation"
    if program == "git":
        if len(argv) < 2:
            return "unknown"
        subcommand = argv[1]
        if subcommand in _GIT_HIGH_RISK:
            return "high_risk"
        if subcommand in _GIT_MUTATION:
            return "mutation"
        if subcommand not in _GIT_READ_ONLY or "--ext-diff" in argv:
            return "unknown"
    elif program not in _READ_ONLY_PROGRAMS:
        return "unknown"
    if any(_is_external_or_expanding_argument(arg) for arg in argv[1:]):
        return "unknown"
    if program == "find" and any(arg in {"-exec", "-execdir", "-delete"} for arg in argv[1:]):
        return "high_risk"
    if program == "sed" and any(arg.startswith("-i") for arg in argv[1:]):
        return "mutation"
    return "verified_read"


def _is_external_or_expanding_argument(arg: str) -> bool:
    if arg.startswith("/") or arg == ".." or arg.startswith("../"):
        return True
    return any(marker in arg for marker in ("$", "`", "~"))


def task_command_authorization(
    context: dict[str, Any],
    *,
    workspace: Path,
    continuation: bool,
) -> dict[str, Any]:
    """Return the session-persisted grants for the active user task."""
    now = time.time()
    current = context.get("command_authorization")
    workspace_value = str(workspace.resolve())
    valid = (
        continuation
        and isinstance(current, dict)
        and current.get("workspace") == workspace_value
        and float(current.get("expires_at") or 0) > now
    )
    if not valid:
        current = {
            "task_id": f"command_task_{uuid.uuid4().hex[:12]}",
            "workspace": workspace_value,
            "grants": [],
            "created_at": now,
            "expires_at": now + 3600,
        }
        context["command_authorization"] = current
    return current


def has_task_grant(context: dict[str, Any], group: str) -> bool:
    return bool(group) and group in set(context.get("grants") or []) and float(
        context.get("expires_at") or 0
    ) > time.time()


def grant_task_group(context: dict[str, Any], group: str) -> None:
    if not group:
        return
    grants = [str(item) for item in context.get("grants") or [] if str(item)]
    if group not in grants:
        grants.append(group)
    context["grants"] = grants

