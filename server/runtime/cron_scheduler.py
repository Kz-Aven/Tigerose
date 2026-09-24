"""Background cron ticker + autorun into assistant/group turns."""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from server.runtime.tools import cron as cron_store

if TYPE_CHECKING:
    pass

_thread: threading.Thread | None = None
_stop = threading.Event()


def _fire_job(job: cron_store.CronJob) -> None:
    from server.db import repos
    from server.scheduler import group_scheduler as scheduler

    prompt = f"[Scheduled] {job.prompt}"
    if job.group_id and job.instance_id:
        member = None
        for m in repos.list_members(job.group_id):
            if m["instance_id"] == job.instance_id:
                member = m
                break
        if member:
            scheduler.enqueue_group_mentions(job.group_id, prompt, [member])
            return
        # fallback: any member with template
        members = [m for m in repos.list_members(job.group_id) if m["template_id"] == job.template_id]
        if members:
            scheduler.enqueue_group_mentions(job.group_id, prompt, members[:1])
            return

    # Assistant DM path: persist user-visible scheduled message then run turn
    from server.api import assistant_session_ops as sess

    sid = sess.resolve_session_for_post(job.template_id, None, title_hint=prompt)
    repos.add_assistant_message(
        job.template_id,
        "user",
        prompt,
        session_id=sid,
        meta={"scheduled": True, "job_id": job.id},
    )
    sess.sync_after_message(job.template_id, sid)
    scheduler.run_assistant_turn_async(job.template_id, prompt, session_id=sid)


def _loop() -> None:
    while not _stop.is_set():
        try:
            cron_store.tick_and_enqueue()
            for job in cron_store.consume_queue():
                try:
                    _fire_job(job)
                except Exception:
                    pass
        except Exception:
            pass
        _stop.wait(1.0)


def start() -> None:
    global _thread
    cron_store.load_durable()
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="cron-scheduler", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
