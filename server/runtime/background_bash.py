"""Owned background shell jobs with bounded waits and process-group cleanup."""

from __future__ import annotations

import atexit
import os
import signal
import subprocess
import tempfile
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from server.runtime.tools import files


@dataclass
class _Job:
    job_id: str
    owner: tuple[str, str]
    process: subprocess.Popen
    output_path: Path
    timeout_s: float
    status: str = "running"
    lock: threading.RLock = field(default_factory=threading.RLock)
    done: threading.Event = field(default_factory=threading.Event)


_jobs: dict[str, _Job] = {}
_lock = threading.RLock()
_OUTPUT_LIMIT = 50_000


def _terminal_timeout() -> float:
    from avent_config import cfg_get, get_config

    try:
        return max(1.0, float(cfg_get(get_config(), "terminal", "timeout", default=180)))
    except (TypeError, ValueError):
        return 180.0


def _owned(ctx, job_id: str) -> _Job:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise KeyError("Background job not found")
        if job.owner != (ctx.scope_key, ctx.template_id):
            raise PermissionError("Background job belongs to another assistant or task scope")
        return job


def _signal_group(job: _Job, sig: int) -> None:
    try:
        os.killpg(job.process.pid, sig)
    except ProcessLookupError:
        pass


def _stop(job: _Job, reason: str) -> None:
    with job.lock:
        if job.status != "running":
            return
        job.status = reason
    _signal_group(job, signal.SIGTERM)
    try:
        job.process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    # The shell may exit before its children; kill the group even in that case.
    _signal_group(job, signal.SIGKILL)
    job.process.wait(timeout=2)
    job.done.set()


def _watch(job: _Job) -> None:
    try:
        code = job.process.wait(timeout=job.timeout_s)
        with job.lock:
            if job.status == "running":
                job.status = "completed" if code == 0 else "failed"
    except subprocess.TimeoutExpired:
        _stop(job, "timeout")
    finally:
        job.done.set()


def _snapshot(job: _Job) -> dict:
    with job.lock:
        state = job.status
        exit_code = job.process.poll()
        try:
            with job.output_path.open("rb") as stream:
                size = stream.seek(0, os.SEEK_END)
                stream.seek(max(0, size - _OUTPUT_LIMIT))
                output = stream.read(_OUTPUT_LIMIT).decode("utf-8", errors="replace")
        except FileNotFoundError:
            size, output = 0, ""
    return {"job_id": job.job_id, "status": state, "exit_code": exit_code,
            "output": output, "output_truncated": size > _OUTPUT_LIMIT,
            "process_id": job.process.pid}


def start(ctx, command: str) -> dict:
    if not isinstance(command, str) or not command.strip():
        raise ValueError("A nonempty shell command is required")
    # Read authorization in the original tool invocation, before creating threads.
    if (files.bash_needs_permission(command) and files._monitor_dangerous_bash.get()
            and not files._allow_dangerous_bash.get()):
        raise PermissionError("bash blocked: dangerous command pattern (denied by policy or awaiting permission)")
    if not ctx.scope_key or not ctx.template_id:
        raise PermissionError("Background shell requires an assistant and task scope")
    timeout_s = _terminal_timeout()
    with tempfile.NamedTemporaryFile(prefix="tigerose-bash-", suffix=".log", delete=False) as output:
        path = Path(output.name)
        try:
            process = subprocess.Popen(command, shell=True, cwd=str(ctx.cwd), stdin=subprocess.DEVNULL,
                                       stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        except Exception:
            path.unlink(missing_ok=True)
            raise
    job = _Job("bash_" + uuid.uuid4().hex, (ctx.scope_key, ctx.template_id), process, path, timeout_s)
    with _lock:
        _jobs[job.job_id] = job
    threading.Thread(target=_watch, args=(job,), name=f"background-{job.job_id}", daemon=True).start()
    return _snapshot(job)


def status(ctx, job_id: str) -> dict:
    return _snapshot(_owned(ctx, job_id))


def wait(ctx, job_id: str, timeout_s: float = 20) -> dict:
    job = _owned(ctx, job_id)
    job.done.wait(timeout=max(0.0, min(20.0, float(timeout_s))))
    return _snapshot(job)


def cancel(ctx, job_id: str) -> dict:
    job = _owned(ctx, job_id)
    _stop(job, "cancelled")
    return _snapshot(job)


def cleanup_scope(scope_key: str, template_id: str) -> None:
    with _lock:
        owned = [job for job in _jobs.values() if job.owner == (scope_key, template_id)]
        for job in owned:
            _jobs.pop(job.job_id, None)
    for job in owned:
        _stop(job, "cancelled")
        job.done.wait(timeout=3)
        job.output_path.unlink(missing_ok=True)


def _cleanup_all() -> None:
    with _lock:
        owners = {job.owner for job in _jobs.values()}
    for scope_key, template_id in owners:
        cleanup_scope(scope_key, template_id)


atexit.register(_cleanup_all)
