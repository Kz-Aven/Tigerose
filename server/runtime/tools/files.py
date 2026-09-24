"""File / shell tools with workspace path guards."""

from __future__ import annotations

import contextvars
import os
import re
import signal
import shlex
import subprocess
from pathlib import Path

# Elevated for a single approved tool call (set by permission gate).
_allow_external = contextvars.ContextVar("avent_allow_external", default=False)
_allow_dangerous_bash = contextvars.ContextVar("avent_allow_dangerous_bash", default=False)
# When False, dangerous bash patterns are not blocked in run_bash (rule disabled).
_monitor_dangerous_bash = contextvars.ContextVar("avent_monitor_dangerous_bash", default=True)

# Ask user (or hard-block if denied). Catastrophic / elevated patterns.
_BASH_DANGEROUS = re.compile(
    r"(rm\s+-rf\s+/|"
    r"sudo\b|"
    r"shutdown\b|"
    r"reboot\b|"
    r"mkfs\b|"
    r"dd\s+if=|"
    r"curl\s+[^\n|]*\|\s*(ba)?sh|"
    r"wget\s+[^\n|]*\|\s*(ba)?sh|"
    r"chmod\s+-R\s+777|"
    r">\s*/etc/|"
    r"rm\s+-rf\s+~)",
    re.IGNORECASE,
)


def allow_external(enabled: bool = True):
    return _allow_external.set(enabled)


def reset_allow_external(token) -> None:
    _allow_external.reset(token)


def allow_dangerous_bash(enabled: bool = True):
    return _allow_dangerous_bash.set(enabled)


def reset_allow_dangerous_bash(token) -> None:
    _allow_dangerous_bash.reset(token)


def set_monitor_dangerous_bash(enabled: bool = True):
    return _monitor_dangerous_bash.set(enabled)


def reset_monitor_dangerous_bash(token) -> None:
    _monitor_dangerous_bash.reset(token)


def resolve_path(cwd: Path, path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = cwd / p
    return p.resolve()


def is_under_cwd(cwd: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(Path(cwd).resolve())
        return True
    except ValueError:
        return False


def is_under_roots(path: Path, roots: tuple[Path, ...] = ()) -> bool:
    return any(is_under_cwd(root, path) for root in roots)


def is_trusted_path(cwd: Path, path: Path, trusted_roots: tuple[Path, ...] = ()) -> bool:
    return is_under_cwd(cwd, path) or is_under_roots(path, trusted_roots)


def ensure_under(cwd: Path, path: Path, trusted_roots: tuple[Path, ...] = ()) -> Path:
    cwd_r = cwd.resolve()
    if is_trusted_path(cwd_r, path, trusted_roots):
        return path
    if _allow_external.get():
        return path
    raise PermissionError(f"path escapes workspace: {path}")


def bash_needs_permission(command: str) -> bool:
    return bool(_BASH_DANGEROUS.search(command or ""))


def is_skill_resource(path: Path, skill_roots: tuple[Path, ...]) -> bool:
    """Only normalized descendants of enabled skill roots are resources."""
    return any(is_under_cwd(root, path) for root in skill_roots)


def skill_script_argument_index(
    command: str, *, cwd: Path, skill_roots: tuple[Path, ...]
) -> int | None:
    """Recognize a single interpreter invocation without shell expansion."""
    if not skill_roots or re.search(r"[;&|<>`$\n\r(){}\[\]*?~]", command):
        return None
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return None
    if len(tokens) < 2:
        return None
    interpreter = tokens[0]
    if interpreter not in {"python", "python3", "node"}:
        return None
    index = 2 if interpreter != "node" and tokens[1] == "-u" else 1
    if len(tokens) <= index or tokens[index].startswith("-"):
        return None
    script = resolve_path(cwd, tokens[index])
    suffixes = {".js", ".mjs", ".cjs"} if interpreter == "node" else {".py"}
    if script.suffix not in suffixes or not script.is_file():
        return None
    return index if is_skill_resource(script, skill_roots) else None


def bash_absolute_paths(
    command: str, *, skip_argument_index: int | None = None, cwd: Path | None = None
) -> list[Path]:
    """Extract absolute path arguments so bash cannot bypass file policy."""
    try:
        tokens = shlex.split(command or "", posix=True)
    except ValueError:
        return []
    paths: list[Path] = []
    separators = {"|", "||", "&&", ";"}
    for index, token in enumerate(tokens):
        if index == skip_argument_index:
            continue
        candidate = re.sub(r"^[0-9]*[<>]+", "", token).rstrip(";")
        if candidate.startswith("-") and "=" in candidate:
            candidate = candidate.split("=", 1)[1]
        if candidate.startswith("~/"):
            candidate = str(Path(candidate).expanduser())
        if cwd is not None and ".." in Path(candidate).parts:
            candidate = str(resolve_path(cwd, candidate))
        if not candidate.startswith("/") or candidate == "/dev/null":
            continue
        if index == 0 or tokens[index - 1] in separators:
            # Absolute executable, not a file argument.
            continue
        paths.append(Path(candidate).expanduser().resolve())
    return paths


def read_file(cwd: Path, path: str, limit: int | None = None, offset: int | None = None,
              trusted_roots: tuple[Path, ...] = ()):
    from server.runtime.executor import ToolResult

    try:
        target = ensure_under(cwd, resolve_path(cwd, path), trusted_roots)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(f"Path not allowed: {path} ({exc})", "denied", {"path": path})
    if target.suffix.lower() == ".xlsx":
        return ToolResult(
            f"Refusing to read '{path}' with read_file: .xlsx is a binary Office format. "
            "Use excel_read instead.",
            "denied",
            {"path": path},
        )
    try:
        if not target.is_file():
            return ToolResult(f"File not found: {path}", "error", {"path": str(target)})
        lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    except PermissionError as exc:
        return ToolResult(f"Permission denied: {path} ({exc})", "denied", {"path": path})
    except OSError as exc:
        return ToolResult(f"OS error reading {path}: {exc}", "error", {"path": path})
    start = max(0, int(offset or 0))
    chunk = lines[start:]
    if limit is not None:
        chunk = chunk[: max(0, int(limit))]
    body = "\n".join(chunk)[:50000]
    return ToolResult(body, "ok", {"path": str(target)})


def write_file(cwd: Path, path: str, content: str,
               trusted_roots: tuple[Path, ...] = ()) -> str:
    from server.runtime.executor import ToolResult

    target = ensure_under(cwd, resolve_path(cwd, path), trusted_roots)
    if target.suffix.lower() == ".xlsx":
        return ToolResult(
            f"Refusing to write '{path}' with write_file: .xlsx is a binary Office format. "
            "Use excel_write instead.",
            "denied",
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return ToolResult(
        f"Wrote {len(content)} bytes to {target}",
        "ok",
        {"path": str(target), "bytes_written": len(content)},
    )


def edit_file(cwd: Path, path: str, old_text: str, new_text: str,
              trusted_roots: tuple[Path, ...] = ()) -> str:
    from server.runtime.executor import ToolResult

    target = ensure_under(cwd, resolve_path(cwd, path), trusted_roots)
    if target.suffix.lower() == ".xlsx":
        return ToolResult(
            f"Refusing to edit '{path}' with edit_file: .xlsx is a binary Office format. "
            "Use excel_write instead.",
            "denied",
        )
    if not target.is_file():
        return ToolResult(f"File not found: {path}", "error")
    text = target.read_text(encoding="utf-8")
    if old_text not in text:
        return ToolResult("old_text not found (exact match required)", "error")
    if text.count(old_text) > 1:
        return ToolResult("old_text matched multiple times; make it unique", "error")
    target.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    return ToolResult(f"Edited {target}", "ok", {"path": str(target)})


def glob_files(cwd: Path, pattern: str) -> str:
    ignored_parts = {
        ".build",
        ".git",
        ".venv",
        "__pycache__",
        "node_modules",
        "dist",
    }
    matches = sorted(
        str(p.relative_to(cwd))
        for p in cwd.glob(pattern)
        if p.is_file() and not any(part in ignored_parts for part in p.parts)
    )
    visible = matches[:200]
    if not visible:
        return "(no matches)"
    suffix = (
        f"\n… ({len(matches) - len(visible)} more matches; narrow the pattern)"
        if len(matches) > len(visible)
        else ""
    )
    return "\n".join(visible) + suffix


def run_bash(cwd: Path, command: str) -> str:
    from avent_config import cfg_get, get_config
    from server.runtime.executor import ToolResult

    if (
        bash_needs_permission(command)
        and _monitor_dangerous_bash.get()
        and not _allow_dangerous_bash.get()
    ):
        return ToolResult(
            "bash blocked: dangerous command pattern (denied by policy or awaiting permission)",
            "denied",
        )
    try:
        timeout_s = float(cfg_get(get_config(), "terminal", "timeout", default=180))
    except (TypeError, ValueError):
        timeout_s = 180.0
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        stdout, stderr = proc.communicate(timeout=max(1.0, timeout_s))
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=2)
        except Exception:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                pass
        return ToolResult(
            f"bash timeout after {timeout_s:g}s",
            "timeout",
            {"process_id": proc.pid},
        )
    out = (stdout or "") + (("\n" + stderr) if stderr else "")
    return ToolResult(
        (out.strip() or f"(exit {proc.returncode})")[:50000],
        "ok" if proc.returncode == 0 else "error",
        {"exit_code": proc.returncode, "process_id": proc.pid},
    )


def run_verified_readonly_commands(cwd: Path, argv_groups: tuple[tuple[str, ...], ...]):
    """Run a parser-verified read-only command plan without a shell."""
    from avent_config import cfg_get, get_config
    from server.runtime.executor import ToolResult

    try:
        timeout_s = float(cfg_get(get_config(), "terminal", "timeout", default=180))
    except (TypeError, ValueError):
        timeout_s = 180.0
    output: list[str] = []
    for argv in argv_groups:
        try:
            proc = subprocess.run(
                list(argv),
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=max(1.0, timeout_s),
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(f"bash timeout after {timeout_s:g}s", "timeout")
        body = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
        output.append(body.strip() or f"(exit {proc.returncode})")
        if proc.returncode != 0:
            return ToolResult("\n\n".join(output)[:50000], "error", {"exit_code": proc.returncode})
    return ToolResult("\n\n".join(output)[:50000], "ok", {"verified_readonly": True})
