"""In-process teammates: MessageBus + background workers."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from server.runtime.context import TurnContext

_bus_lock = threading.Lock()
_inboxes: dict[str, list[dict[str, Any]]] = defaultdict(list)
_teammates: dict[str, "Teammate"] = {}
_plans: dict[str, dict[str, Any]] = {}


@dataclass
class Teammate:
    name: str
    prompt: str
    template_id: str
    scope_key: str
    requires_plan: bool = False
    plan_approved: bool = False
    shutdown: bool = False
    thread: threading.Thread | None = None
    created_at: float = field(default_factory=time.time)


def _key(template_id: str, name: str) -> str:
    return f"{template_id}::{name}"


def send_message(from_name: str, to: str, content: str, *, template_id: str) -> str:
    with _bus_lock:
        _inboxes[_key(template_id, to)].append(
            {"from": from_name, "content": content, "ts": time.time()}
        )
    return f"Sent to {to}"


def check_inbox(name: str, *, template_id: str) -> str:
    with _bus_lock:
        msgs = list(_inboxes.get(_key(template_id, name), []))
        _inboxes[_key(template_id, name)] = []
    if not msgs:
        return "Inbox empty."
    return "\n".join(f"[{m['from']}] {m['content']}" for m in msgs)


def request_shutdown(name: str, *, template_id: str) -> str:
    with _bus_lock:
        tm = _teammates.get(_key(template_id, name))
        if not tm:
            return f"Teammate not found: {name}"
        tm.shutdown = True
    return f"Shutdown requested for {name}"


def request_plan(name: str, plan: str, *, template_id: str) -> str:
    with _bus_lock:
        _plans[_key(template_id, name)] = {
            "plan": plan,
            "status": "pending",
            "ts": time.time(),
        }
        tm = _teammates.get(_key(template_id, name))
        if tm:
            tm.plan_approved = False
    return f"Plan submitted for {name}; awaiting review_plan."


def review_plan(name: str, approve: bool, note: str = "", *, template_id: str) -> str:
    with _bus_lock:
        plan = _plans.get(_key(template_id, name))
        if not plan:
            return f"No pending plan for {name}"
        plan["status"] = "approved" if approve else "rejected"
        plan["note"] = note
        tm = _teammates.get(_key(template_id, name))
        if tm and approve:
            tm.plan_approved = True
            tm.requires_plan = False
    return f"Plan for {name} {'approved' if approve else 'rejected'}."


def _teammate_loop(tm: Teammate) -> None:
    """Lightweight loop: wait for plan if needed, then one LLM turn with tools."""
    from server.db import repos
    from server.runtime import turn as turn_runtime

    # Wait for plan approval if required
    deadline = time.time() + 3600
    while tm.requires_plan and not tm.plan_approved and not tm.shutdown:
        if time.time() > deadline:
            send_message(tm.name, "lead", "Plan approval timed out.", template_id=tm.template_id)
            return
        time.sleep(1.0)
    if tm.shutdown:
        return

    tpl = repos.get_template(tm.template_id)
    if not tpl:
        return
    # Restrict tools for teammate: no spawn recursion
    caps = dict(tpl.get("capabilities") or {})
    tools = list(caps.get("tools") or [])
    blocked = {"spawn_teammate", "task", "schedule_cron", "review_plan"}
    caps["tools"] = [t for t in tools if t not in blocked]
    tpl = {**tpl, "capabilities": caps}

    try:
        result = turn_runtime.run_chat_turn(
            scope_key=f"{tm.scope_key}:teammate:{tm.name}",
            surface="teammate",
            user_message=tm.prompt,
            template=tpl,
            memories=turn_runtime.memory_summary_for_template(tm.template_id),
            extra_context=(
                f"You are teammate '{tm.name}'. Do the assigned work, then stop. "
                "Use send_message to report to lead if needed. Do not spawn further teammates."
            ),
            title_hint=f"teammate:{tm.name}",
        )
        send_message(
            tm.name,
            "lead",
            f"[teammate done] {result.get('reply', '')[:2000]}",
            template_id=tm.template_id,
        )
        # Surface on assistant channel
        sid = str(result.get("session_id") or "")
        repos.add_assistant_message(
            tm.template_id,
            "assistant",
            f"[Teammate {tm.name}]\n{result.get('reply', '')}",
            session_id=sid,
            meta={"teammate": tm.name},
        )
        from server.scheduler import group_scheduler as scheduler

        scheduler._publish(
            f"assistant:{tm.template_id}",
            "assistant.message",
            {
                "role": "assistant",
                "content": f"[Teammate {tm.name} finished]",
                "meta": {"teammate": tm.name},
            },
        )
    except Exception as exc:
        send_message(tm.name, "lead", f"[teammate error] {exc}", template_id=tm.template_id)
    finally:
        with _bus_lock:
            _teammates.pop(_key(tm.template_id, tm.name), None)


def spawn_teammate(
    ctx: TurnContext,
    name: str,
    prompt: str,
    *,
    requires_plan: bool = False,
) -> str:
    name = (name or "").strip()
    if not name:
        return "name is required"
    key = _key(ctx.template_id, name)
    with _bus_lock:
        if key in _teammates:
            return f"Teammate already running: {name}"
        tm = Teammate(
            name=name,
            prompt=prompt,
            template_id=ctx.template_id,
            scope_key=ctx.scope_key,
            requires_plan=bool(requires_plan),
            plan_approved=not requires_plan,
        )
        _teammates[key] = tm
        t = threading.Thread(target=_teammate_loop, args=(tm,), name=f"tm-{name}", daemon=True)
        tm.thread = t
        t.start()
    return (
        f"Spawned teammate '{name}'."
        + (" Waiting for review_plan before work." if requires_plan else "")
    )
