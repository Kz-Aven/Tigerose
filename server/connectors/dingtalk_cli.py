"""Managed DingTalk Workspace CLI connector.

The CLI owns OAuth credentials (normally in macOS Keychain). This module only
starts the binary with an argv list and exposes sanitized connection metadata.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server.connectors.dingtalk_operations import (
    describe_dingtalk_command,
    is_in_doubt_failure,
    receipt_from_output,
)
from server.runtime.executor import ToolResult

_MAX_OUTPUT = 12_000
_DEFAULT_TIMEOUT = 60
_lock = threading.Lock()
_operation = ""
_operation_error = ""


@dataclass(frozen=True)
class DwsStatus:
    executable: str = ""
    installed: bool = False
    connected: bool = False
    state: str = "not_installed"
    account_name: str = ""
    corp_name: str = ""
    profile: str = ""
    message: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "installed": self.installed,
            "connected": self.connected,
            "state": self.state,
            "account_name": self.account_name,
            "corp_name": self.corp_name,
            "message": self.message,
        }


def _bounded(text: str) -> str:
    value = (text or "").strip()
    if len(value) > _MAX_OUTPUT:
        value = value[:_MAX_OUTPUT] + "\n…(output truncated)"
    return value


def _find_executable(name: str) -> str:
    found = shutil.which(name)
    if found:
        return str(Path(found).resolve())
    for candidate in (
        Path("/opt/homebrew/bin") / name,
        Path("/usr/local/bin") / name,
        Path.home() / ".npm-global/bin" / name,
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    return ""


def _find_dws() -> str:
    found = _find_executable("dws")
    if found:
        return found
    npm = _find_executable("npm")
    if not npm:
        return ""
    try:
        prefix = subprocess.run(
            [npm, "prefix", "-g"], capture_output=True, text=True, timeout=10, check=False
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    for candidate in (Path(prefix) / "bin" / "dws", Path(prefix) / "dws"):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    return ""


def _status_for(executable: str) -> DwsStatus:
    try:
        result = subprocess.run(
            [executable, "auth", "status", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        payload = json.loads(result.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return DwsStatus(executable=executable, installed=True, state="auth_required")
    connected = bool(payload.get("authenticated"))
    profile = ":".join(
        value for value in (str(payload.get("corp_id") or ""), str(payload.get("user_id") or "")) if value
    )
    return DwsStatus(
        executable=executable,
        installed=True,
        connected=connected,
        state="connected" if connected else "auth_required",
        account_name=str(payload.get("user_name") or ""),
        corp_name=str(payload.get("corp_name") or ""),
        profile=profile,
        message=str(payload.get("message") or ""),
    )


def status() -> DwsStatus:
    executable = _find_dws()
    if not executable:
        with _lock:
            if _operation:
                return DwsStatus(state=_operation)
            if _operation_error:
                return DwsStatus(state="failed", message=_operation_error)
        return DwsStatus()
    current = _status_for(executable)
    with _lock:
        if _operation == "installing":
            return DwsStatus(executable=executable, installed=True, state="installing")
        if _operation == "authorizing" and not current.connected:
            return DwsStatus(executable=executable, installed=True, state="authorizing")
        if _operation_error and not current.connected:
            return DwsStatus(
                executable=executable,
                installed=True,
                state="failed",
                message=_operation_error,
            )
    return current


def is_connected() -> bool:
    return status().connected


def _set_operation(value: str, error: str = "") -> None:
    global _operation, _operation_error
    with _lock:
        _operation = value
        _operation_error = error


def _connect_worker() -> None:
    executable = _find_dws()
    if not executable:
        npm = _find_executable("npm")
        if not npm:
            _set_operation("", "未找到 npm，无法安装钉钉 CLI")
            return
        _set_operation("installing")
        try:
            installed = subprocess.run(
                [npm, "install", "-g", "dingtalk-workspace-cli", "--registry=https://registry.npmmirror.com"],
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _set_operation("", f"钉钉 CLI 安装失败：{exc}")
            return
        executable = _find_dws()
        if installed.returncode != 0 or not executable:
            detail = _bounded(installed.stderr or installed.stdout) or "未找到 dws 可执行文件"
            _set_operation("", f"钉钉 CLI 安装失败：{detail}")
            return
    _set_operation("authorizing")
    try:
        process = subprocess.Popen([executable, "auth", "login"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        process.wait(timeout=600)
    except subprocess.TimeoutExpired:
        _set_operation("", "浏览器授权超时，请重试")
        return
    except OSError as exc:
        _set_operation("", f"无法启动钉钉授权：{exc}")
        return
    final = _status_for(executable)
    _set_operation("", "" if final.connected else (final.message or "授权未完成，请重试"))


def start_connect() -> DwsStatus:
    current = status()
    if current.connected:
        return current
    with _lock:
        if _operation:
            return current
    thread = threading.Thread(target=_connect_worker, name="dingtalk-connect", daemon=True)
    thread.start()
    return DwsStatus(installed=current.installed, state="installing" if not current.installed else "authorizing")


def disconnect() -> DwsStatus:
    current = status()
    if not current.installed:
        return current
    if not current.connected:
        return current
    args = [current.executable, "auth", "logout"]
    if current.profile:
        args.extend(["--profile", current.profile])
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError(f"钉钉登出失败：{exc}") from exc
    if result.returncode != 0:
        raise ValueError(f"钉钉登出失败：{_bounded(result.stderr or result.stdout)}")
    return status()


def run_cli(args: list[str], timeout_seconds: int = _DEFAULT_TIMEOUT) -> ToolResult:
    current = status()
    if not current.connected:
        return ToolResult("钉钉未连接，请先在设置中完成授权。", "denied", {"connector": "dingtalk"})
    if not isinstance(args, list) or not args or any(not isinstance(item, str) or "\x00" in item for item in args):
        return ToolResult("dingtalk_cli 的 args 必须是非空字符串数组。", "error", {"connector": "dingtalk"})
    operation = describe_dingtalk_command(args)
    operation_meta = {
        "operation_id": operation.operation_id,
        "operation_effect": operation.effect,
        "goal_role": operation.goal_role,
        "domain": operation.domain,
        "operation": operation.operation,
        "resource_type": operation.resource_type,
    }
    timeout = max(1, min(int(timeout_seconds or _DEFAULT_TIMEOUT), 180))
    try:
        result = subprocess.run(
            [current.executable, *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except subprocess.TimeoutExpired:
        outcome = "in_doubt" if operation.effect != "read" else "timeout"
        return ToolResult(
            "钉钉操作未收到结果，可能已经生效。请先核验已有结果，不要重复执行写入操作。"
            if outcome == "in_doubt" else "钉钉 CLI 调用超时。",
            outcome,
            {"connector": "dingtalk", "argv": args[:12], **operation_meta, "no_replay": outcome == "in_doubt"},
        )
    except OSError as exc:
        return ToolResult(f"钉钉 CLI 启动失败：{exc}", "error", {"connector": "dingtalk"})
    content = _bounded(result.stdout or result.stderr)
    metadata = {"connector": "dingtalk", "argv": args[:12], "exit_code": result.returncode, **operation_meta}
    if result.returncode == 0:
        receipt = (
            receipt_from_output(operation, result.stdout or result.stderr)
            if operation.acceptance_type
            else None
        )
        if receipt:
            metadata["acceptance_receipt"] = receipt
        return ToolResult(content or "命令执行成功", "ok", metadata)
    if is_in_doubt_failure(operation, result.stdout or result.stderr):
        metadata["no_replay"] = True
        metadata["operation_outcome"] = "in_doubt"
        return ToolResult(
            (content or "钉钉 CLI 未确认执行结果")
            + "\n\n该操作可能已经生效。请先查询或读取已有结果核验，禁止重复执行同一写入操作。",
            "in_doubt",
            metadata,
        )
    return ToolResult(
        content or "钉钉 CLI 命令失败",
        "error",
        metadata,
    )
