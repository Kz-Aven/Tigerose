"""Per-turn connector projection and safe managed CLI execution."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server.connectors.catalog import get_manifest
from server.connectors.dingtalk_operations import (
    describe_dingtalk_command,
    is_in_doubt_failure,
    receipt_from_output,
)
from server.connectors.store import config_dir, load_state
from server.runtime.executor import ToolResult

_META_CHARS = ("|", ">", "<", ";", "$", "`")
_CONNECTOR_ENV = re.compile(r"(?:LARKSUITE_CLI|DWS)_[A-Z0-9_]*=[^\s;&|<>`$]+$")
_HEREDOC = re.compile(
    r"^(?P<header>.*?)\s+<<(?P<quote>['\"]?)(?P<delimiter>[A-Za-z_][A-Za-z0-9_]*)"
    r"(?P=quote)[ \t]*\r?\n(?P<body>.*?)(?:\r?\n)(?P=delimiter)[ \t]*$",
    re.DOTALL,
)
_LEGACY_LARK_WRAPPER = re.compile(
    r"^\s*export\s+LARKSUITE_CLI_CONFIG_DIR=(?:\"[^\"]+\"|'[^']+')\s*;\s*"
    r"LARK=(?:\"[^\"]+\"|'[^']+')\s*;\s*(?:\"\$LARK\"|\$LARK)\s+"
    r"(?P<argv>.+?)\s*(?:2>&1\s*\|\s*sed\s+-n\s+(?:\"[^\"]+\"|'[^']+'))?\s*$"
)


@dataclass(frozen=True)
class ConnectorCli:
    connector_id: str
    executable_name: str
    executable_path: str
    version: str
    skills_dir: str


@dataclass
class ResolvedConnectorSet:
    cli: dict[str, ConnectorCli] = field(default_factory=dict)
    skills: dict[str, Path] = field(default_factory=dict)

    @property
    def executable_names(self) -> tuple[str, ...]:
        return tuple(self.cli)

    def skill_index(self) -> list[dict[str, str]]:
        out = []
        for skill_id, path in sorted(self.skills.items()):
            out.append({"id": skill_id, "name": path.parent.name, "connector_id": skill_id.split(":")[1]})
        return out


def _enabled_connector_ids(meta: dict[str, Any] | None) -> list[str]:
    connectors = dict((meta or {}).get("connectors") or {})
    ids = []
    for connector_id, value in connectors.items():
        enabled = value.get("enabled") if isinstance(value, dict) else value
        if enabled:
            ids.append(str(connector_id))
    return ids


def _version_key(value: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", (value or "").lstrip("v"))
    return tuple(int(part) for part in parts) or (0,)


def _skills_compatible(version: str, minimum: str, maximum: str) -> bool:
    current = _version_key(version)
    return (
        (not minimum or current >= _version_key(minimum))
        and (not maximum or current <= _version_key(maximum))
    )


def resolve_connectors(config_meta: dict[str, Any] | None) -> ResolvedConnectorSet:
    resolved = ResolvedConnectorSet()
    for connector_id in _enabled_connector_ids(config_meta):
        manifest = get_manifest(connector_id)
        state = load_state(connector_id)
        if (
            not manifest
            or not state.get("installed")
            or not state.get("authenticated")
            or state.get("health") == "unhealthy"
        ):
            continue
        executable = str(state.get("executable") or "")
        version = str(state.get("active_version") or "")
        if manifest.cli and manifest.exposed.cli_execution and executable and Path(executable).is_file():
            resolved.cli[manifest.cli.executable] = ConnectorCli(
                connector_id=connector_id,
                executable_name=manifest.cli.executable,
                executable_path=executable,
                version=version,
                skills_dir=str(state.get("skills_dir") or ""),
            )
        skills_dir = Path(str(state.get("skills_dir") or ""))
        prefix = manifest.skills.allowlist_prefix if manifest.skills else ""
        compatible = not manifest.skills or _skills_compatible(
            version,
            manifest.skills.min_cli_version,
            manifest.skills.max_cli_version,
        )
        if compatible and skills_dir.is_dir():
            for skill_md in skills_dir.glob("*/SKILL.md"):
                name = skill_md.parent.name
                if prefix and not name.startswith(prefix):
                    continue
                resolved.skills[f"connector:{connector_id}:skill:{name}"] = skill_md
    return resolved


def _managed_cli_parts(command: str) -> tuple[str, list[str], str | None] | None:
    raw = (command or "").strip()
    if not raw:
        return None
    stdin: str | None = None
    heredoc = _HEREDOC.fullmatch(raw)
    if heredoc:
        raw = heredoc.group("header").strip()
        stdin = heredoc.group("body")
    elif "\n" in raw:
        return None
    if "&" in raw or any(token in raw for token in _META_CHARS):
        return None
    try:
        argv = shlex.split(raw, posix=True)
    except ValueError:
        return None
    while argv and _CONNECTOR_ENV.fullmatch(argv[0]):
        argv.pop(0)
    if not argv:
        return None
    return argv[0], argv[1:], stdin


def _legacy_lark_wrapper_parts(command: str) -> tuple[str, list[str], str | None] | None:
    """Normalize the old generated Lark shell wrapper without executing shell."""
    match = _LEGACY_LARK_WRAPPER.fullmatch((command or "").strip())
    if not match:
        return None
    raw_args = match.group("argv").strip()
    if not raw_args or any(token in raw_args for token in (";", "&", "|", "<", ">", "$", "`")):
        return None
    try:
        return "lark-cli", shlex.split(raw_args, posix=True), None
    except ValueError:
        return None


def _managed_cli_batch_parts(command: str) -> list[tuple[str, list[str], str | None]] | None:
    raw = (command or "").strip()
    if not raw or "\n" in raw or any(token in raw for token in _META_CHARS):
        return None
    try:
        lexer = shlex.shlex(raw, posix=True, punctuation_chars="&")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return None
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token == "&&":
            if not segments[-1]:
                return None
            segments.append([])
        elif token == "&":
            return None
        else:
            segments[-1].append(token)
    if not segments[-1]:
        return None
    parts: list[tuple[str, list[str], str | None]] = []
    for argv in segments:
        while argv and _CONNECTOR_ENV.fullmatch(argv[0]):
            argv.pop(0)
        if not argv:
            return None
        parts.append((argv[0], argv[1:], None))
    return parts


def _managed_cli_sequence(command: str) -> list[tuple[str, list[str], str | None]] | None:
    single = _managed_cli_parts(command)
    if single is not None:
        return [single]
    legacy_lark = _legacy_lark_wrapper_parts(command)
    return [legacy_lark] if legacy_lark is not None else _managed_cli_batch_parts(command)


def is_managed_cli_command(command: str, executable_names: tuple[str, ...] | list[str]) -> bool:
    parts = _managed_cli_sequence(command)
    return parts is not None and all(part[0] in executable_names for part in parts)


def parse_managed_cli(command: str, resolved: ResolvedConnectorSet | None) -> tuple[ConnectorCli, list[str]] | None:
    parsed = _managed_cli_parts(command)
    if resolved is None or parsed is None or parsed[0] not in resolved.cli:
        return None
    return resolved.cli[parsed[0]], parsed[1]


def execute_managed_cli(command: str, resolved: ResolvedConnectorSet | None, *, timeout_seconds: int = 180) -> ToolResult | None:
    parts = _managed_cli_sequence(command)
    if resolved is None or parts is None or any(part[0] not in resolved.cli for part in parts):
        return None
    results: list[ToolResult] = []
    for executable, args, stdin in parts:
        result = _execute_managed_cli_part(resolved.cli[executable], args, timeout_seconds, stdin=stdin)
        results.append(result)
        if result.outcome != "ok":
            return ToolResult(
                "\n\n".join(item.content for item in results),
                result.outcome,
                {**result.metadata, "connector_batch": len(parts) > 1, "completed_commands": len(results)},
            )
    last = results[-1]
    return ToolResult(
        "\n\n".join(item.content for item in results),
        "ok",
        {**last.metadata, "connector_batch": len(parts) > 1, "completed_commands": len(results)},
    )


def _execute_managed_cli_part(
    cli: ConnectorCli,
    args: list[str],
    timeout_seconds: int,
    *,
    stdin: str | None = None,
) -> ToolResult:
    if cli.connector_id == "lark":
        return _execute_lark_cli(cli, args, timeout_seconds, stdin=stdin)
    if cli.connector_id != "dingtalk":
        return ToolResult("连接器 CLI adapter 未实现。", "error", {"connector": cli.connector_id})
    operation = describe_dingtalk_command(args)
    started = time.monotonic()
    metadata = {
        "connector": cli.connector_id,
        "connector_cli": True,
        "argv": args[:20],
        "operation_id": operation.operation_id,
        "operation_effect": operation.effect,
        "goal_role": operation.goal_role,
        "domain": operation.domain,
        "operation": operation.operation,
        "resource_type": operation.resource_type,
        "cli_version": cli.version,
    }
    schema_key = " ".join(args[1:]) if args and args[0] == "schema" else ""
    if schema_key:
        from server.connectors import schema_cache

        cached = schema_cache.get(cli.connector_id, cli.version, schema_key)
        if cached is not None:
            return ToolResult(cached, "ok", {**metadata, "schema_cache": "hit"})
    try:
        result = subprocess.run(
            [cli.executable_path, *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=max(1, min(int(timeout_seconds), 180)),
            check=False,
            env={**os.environ, "DWS_CONFIG_DIR": str(config_dir(cli.connector_id))},
        )
    except subprocess.TimeoutExpired:
        return ToolResult(
            "连接器操作未收到结果，可能已经生效。请先核验已有结果，不要重复执行写入操作。",
            "in_doubt" if operation.effect != "read" else "timeout",
            {**metadata, "no_replay": operation.effect != "read", "duration_ms": int((time.monotonic() - started) * 1000)},
        )
    output = (result.stdout or result.stderr or "").strip()[:50000]
    metadata.update({"exit_code": result.returncode, "duration_ms": int((time.monotonic() - started) * 1000)})
    if result.returncode == 0:
        if schema_key and output:
            from server.connectors import schema_cache

            schema_cache.put(cli.connector_id, cli.version, schema_key, output)
            metadata["schema_cache"] = "miss"
        if operation.acceptance_type:
            receipt = receipt_from_output(operation, output)
            if receipt:
                metadata["acceptance_receipt"] = receipt
        return ToolResult(output or "命令执行成功", "ok", metadata)
    if is_in_doubt_failure(operation, output):
        return ToolResult(
            (output or "连接器 CLI 未确认执行结果") + "\n\n该操作可能已经生效。请先查询核验，禁止重复执行同一写入操作。",
            "in_doubt",
            {**metadata, "no_replay": True, "operation_outcome": "in_doubt"},
        )
    return ToolResult(output or "连接器 CLI 命令失败", "error", metadata)


def _lark_missing_user_scopes(output: str) -> list[str]:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        return []
    error = payload.get("error") if isinstance(payload, dict) else None
    if not isinstance(error, dict) or str(error.get("subtype") or "") != "missing_scope":
        return []
    scopes = error.get("missing_scopes")
    if not isinstance(scopes, list):
        return []
    return [scope for scope in scopes if isinstance(scope, str) and scope]


def _lark_login_scopes(args: list[str]) -> list[str] | None:
    """Return requested scopes for a direct CLI login, if this is one."""
    if args[:2] != ["auth", "login"]:
        return None
    if "--device-code" in args:
        return []
    scopes: list[str] = []
    index = 2
    while index < len(args):
        value = args[index]
        if value == "--scope" and index + 1 < len(args):
            scopes.extend(scope for scope in args[index + 1].split() if scope)
            index += 2
            continue
        if value.startswith("--scope="):
            scopes.extend(scope for scope in value.partition("=")[2].split() if scope)
        index += 1
    return scopes


def _execute_lark_cli(
    cli: ConnectorCli,
    args: list[str],
    timeout_seconds: int,
    *,
    allow_scope_reauthorization: bool = True,
    stdin: str | None = None,
) -> ToolResult:
    if args[:2] in (["config", "init"], ["config", "bind"]) or args[:3] == ["auth", "login", "--device-code"]:
        return ToolResult(
            "飞书应用配置与设备授权由连接器设置页管理，不能通过 Bash 调用。",
            "error",
            {"connector": cli.connector_id, "connector_cli": True, "lifecycle_command": True},
        )
    login_scopes = _lark_login_scopes(args)
    if login_scopes is not None:
        if not login_scopes:
            return ToolResult(
                "飞书授权必须由连接器运行时发起，无法恢复缺少 scope 的登录流程。",
                "error",
                {"connector": cli.connector_id, "connector_cli": True},
            )
        try:
            from server.connectors.manager import authorize_lark_scopes

            authorize_lark_scopes(login_scopes)
        except RuntimeError as exc:
            return ToolResult(
                f"飞书权限补充授权失败：{exc}",
                "error",
                {"connector": cli.connector_id, "connector_cli": True, "missing_scopes": login_scopes},
            )
        return ToolResult(
            "飞书权限已授权并写入 CLI。",
            "ok",
            {"connector": cli.connector_id, "connector_cli": True, "scope_reauthorized": login_scopes},
        )

    started = time.monotonic()
    metadata = {
        "connector": cli.connector_id,
        "connector_cli": True,
        "argv": args[:20],
        "operation_effect": "unknown",
        "goal_role": "unknown",
        "cli_version": cli.version,
    }
    try:
        result = subprocess.run(
            [cli.executable_path, *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=max(1, min(int(timeout_seconds), 180)),
            check=False,
            env={
                **os.environ,
                "LARKSUITE_CLI_CONFIG_DIR": str(config_dir(cli.connector_id)),
                "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
                "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
            },
        )
    except subprocess.TimeoutExpired:
        return ToolResult(
            "飞书 CLI 未在限定时间内返回结果。请先查询核验，避免重复执行可能已生效的操作。",
            "in_doubt",
            {**metadata, "no_replay": True, "duration_ms": int((time.monotonic() - started) * 1000)},
        )
    output = (result.stdout or result.stderr or "").strip()[:50000]
    metadata.update({"exit_code": result.returncode, "duration_ms": int((time.monotonic() - started) * 1000)})
    if result.returncode == 0:
        return ToolResult(output or "命令执行成功", "ok", metadata)
    missing_scopes = _lark_missing_user_scopes(output)
    if missing_scopes and allow_scope_reauthorization:
        try:
            from server.connectors.manager import authorize_lark_scopes

            authorize_lark_scopes(missing_scopes)
        except RuntimeError as exc:
            return ToolResult(f"飞书权限补充授权失败：{exc}", "error", {**metadata, "missing_scopes": missing_scopes})
        retried = _execute_lark_cli(
            cli,
            args,
            timeout_seconds,
            allow_scope_reauthorization=False,
            stdin=stdin,
        )
        retried.metadata["scope_reauthorized"] = missing_scopes
        return retried
    return ToolResult(output or "飞书 CLI 命令失败", "error", metadata)
