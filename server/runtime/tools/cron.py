"""Cron / one-shot delay job store + matching (server runtime)."""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from avent_paths import data_root, ensure_data_dirs

_lock = threading.Lock()
_jobs: dict[str, "CronJob"] = {}
_queue: list["CronJob"] = []
_last_fired: dict[str, str] = {}


def _durable_path() -> Path:
    ensure_data_dirs()
    return data_root() / "data" / "scheduled_tasks.json"

@dataclass
class CronJob:
    id: str
    prompt: str
    template_id: str
    scope_key: str
    group_id: str | None = None
    instance_id: str | None = None
    cron: str = ""
    run_at: float | None = None  # unix ts for one-shot
    recurring: bool = True
    durable: bool = True
    created_at: float = field(default_factory=time.time)


def _cron_field_matches(field: str, value: int) -> bool:
    if field == "*":
        return True
    if field.startswith("*/"):
        step = int(field[2:])
        return step > 0 and value % step == 0
    if "," in field:
        return any(_cron_field_matches(f.strip(), value) for f in field.split(","))
    if "-" in field:
        lo, hi = field.split("-", 1)
        return int(lo) <= value <= int(hi)
    return value == int(field)


def cron_matches(cron_expr: str, dt: datetime) -> bool:
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return False
    minute, hour, dom, month, dow = fields
    dow_val = (dt.weekday() + 1) % 7
    if not (
        _cron_field_matches(minute, dt.minute)
        and _cron_field_matches(hour, dt.hour)
        and _cron_field_matches(month, dt.month)
    ):
        return False
    dom_ok = _cron_field_matches(dom, dt.day)
    dow_ok = _cron_field_matches(dow, dow_val)
    if dom == "*" and dow == "*":
        return True
    if dom == "*":
        return dow_ok
    if dow == "*":
        return dom_ok
    return dom_ok or dow_ok


def _validate_cron_field(field: str, lo: int, hi: int) -> str | None:
    if field == "*":
        return None
    if field.startswith("*/"):
        step_str = field[2:]
        if not step_str.isdigit() or int(step_str) <= 0:
            return f"Invalid step: {field}"
        return None
    if "," in field:
        for part in field.split(","):
            err = _validate_cron_field(part.strip(), lo, hi)
            if err:
                return err
        return None
    if "-" in field:
        parts = field.split("-", 1)
        if not parts[0].isdigit() or not parts[1].isdigit():
            return f"Invalid range: {field}"
        a, b = int(parts[0]), int(parts[1])
        if a < lo or a > hi or b < lo or b > hi or a > b:
            return f"Range {field} out of bounds [{lo}-{hi}]"
        return None
    if not field.isdigit():
        return f"Invalid field: {field}"
    val = int(field)
    if val < lo or val > hi:
        return f"Value {val} out of bounds [{lo}-{hi}]"
    return None


def validate_cron(cron_expr: str) -> str | None:
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        return f"Expected 5 fields, got {len(fields)}"
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
    names = ["minute", "hour", "day-of-month", "month", "day-of-week"]
    for field, (lo, hi), name in zip(fields, bounds, names):
        err = _validate_cron_field(field, lo, hi)
        if err:
            return f"{name}: {err}"
    return None


def save_durable() -> None:
    path = _durable_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    durable = [asdict(j) for j in _jobs.values() if j.durable]
    path.write_text(json.dumps(durable, indent=2, ensure_ascii=False), encoding="utf-8")


def load_durable() -> None:
    path = _durable_path()
    if not path.exists():
        return
    try:
        items = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return
    with _lock:
        for item in items:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            try:
                job = CronJob(**{k: v for k, v in item.items() if k in CronJob.__dataclass_fields__})
            except TypeError:
                continue
            if job.cron:
                err = validate_cron(job.cron)
                if err:
                    continue
            _jobs[job.id] = job


def schedule_job(
    *,
    prompt: str,
    template_id: str,
    scope_key: str,
    group_id: str | None = None,
    instance_id: str | None = None,
    cron: str = "",
    delay_seconds: int | None = None,
    recurring: bool | None = None,
    durable: bool = True,
) -> CronJob | str:
    prompt = (prompt or "").strip()
    if not prompt:
        return "prompt is required"
    run_at = None
    cron = (cron or "").strip()
    if delay_seconds is not None:
        try:
            delay = int(delay_seconds)
        except (TypeError, ValueError):
            return "delay_seconds must be an integer"
        if delay < 1:
            return "delay_seconds must be >= 1"
        run_at = time.time() + delay
        cron = ""
        if recurring is None:
            recurring = False
    elif cron:
        err = validate_cron(cron)
        if err:
            return err
        if recurring is None:
            recurring = True
    else:
        return "Provide cron or delay_seconds"
    if recurring is None:
        recurring = True

    job = CronJob(
        id=f"cron_{random.randint(0, 999999):06d}",
        prompt=prompt,
        template_id=template_id,
        scope_key=scope_key,
        group_id=group_id,
        instance_id=instance_id,
        cron=cron,
        run_at=run_at,
        recurring=bool(recurring),
        durable=durable,
    )
    with _lock:
        _jobs[job.id] = job
    if durable:
        save_durable()
    return job


def cancel_job(job_id: str, *, template_id: str | None = None) -> str:
    with _lock:
        job = _jobs.get(job_id)
        if not job:
            return f"Job {job_id} not found"
        if template_id and job.template_id != template_id:
            return f"Job {job_id} not owned by this assistant"
        _jobs.pop(job_id, None)
    if job.durable:
        save_durable()
    return f"Cancelled {job_id}"


def list_jobs(template_id: str | None = None) -> list[CronJob]:
    with _lock:
        jobs = list(_jobs.values())
    if template_id:
        jobs = [j for j in jobs if j.template_id == template_id]
    return jobs


def format_jobs(template_id: str) -> str:
    jobs = list_jobs(template_id)
    if not jobs:
        return "No cron jobs. Use schedule_cron to add one."
    lines = []
    for j in jobs:
        if j.run_at:
            when = f"once@{datetime.fromtimestamp(j.run_at).isoformat(timespec='seconds')}"
        else:
            when = f"cron'{j.cron}'"
        tag = "recurring" if j.recurring else "one-shot"
        lines.append(f"  {j.id}: {when} → {j.prompt[:60]} [{tag}]")
    return "\n".join(lines)


def tick_and_enqueue() -> None:
    """Called every second by scheduler thread."""
    now = time.time()
    dt = datetime.now()
    marker = dt.strftime("%Y-%m-%d %H:%M")
    with _lock:
        for job in list(_jobs.values()):
            try:
                fire = False
                if job.run_at is not None:
                    if now >= job.run_at:
                        fire = True
                elif job.cron and cron_matches(job.cron, dt) and _last_fired.get(job.id) != marker:
                    fire = True
                    _last_fired[job.id] = marker
                if not fire:
                    continue
                _queue.append(job)
                if not job.recurring or job.run_at is not None:
                    _jobs.pop(job.id, None)
                    if job.durable:
                        # defer save outside? keep simple
                        pass
            except Exception:
                continue
        # persist after removals
        durable = [asdict(j) for j in _jobs.values() if j.durable]
    path = _durable_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(durable, indent=2, ensure_ascii=False), encoding="utf-8")


def consume_queue() -> list[CronJob]:
    with _lock:
        fired = list(_queue)
        _queue.clear()
    return fired
