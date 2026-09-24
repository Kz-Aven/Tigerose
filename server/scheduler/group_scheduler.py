"""Per-group scheduler with per-instance locks + agent @ handoff relay."""

from __future__ import annotations

import asyncio
import re
import threading
from concurrent.futures import ThreadPoolExecutor

from server.api.sse_bus import bus
from server.db import repos
from server.runtime import turn as turn_runtime

MENTION_RE = re.compile(r"@([A-Za-z0-9_\u4e00-\u9fff.-]+)")
MAX_HANDOFF_HOPS = 5

_executor = ThreadPoolExecutor(max_workers=6, thread_name_prefix="agent-turn")
_instance_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None


def set_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


def get_event_loop() -> asyncio.AbstractEventLoop | None:
    return _loop


def _instance_lock(instance_id: str) -> threading.Lock:
    with _locks_guard:
        if instance_id not in _instance_locks:
            _instance_locks[instance_id] = threading.Lock()
        return _instance_locks[instance_id]


def parse_mentions(text: str) -> list[str]:
    """Return mention names in appearance order (deduped)."""
    seen: set[str] = set()
    out: list[str] = []
    for name in MENTION_RE.findall(text or ""):
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


def _remember_proposal_from_result(
    result: dict, *, source_groups: list[str] | None = None
) -> dict | None:
    """Normalize turn remember_proposal into meta object shape."""
    rp = result.get("remember_proposal")
    groups = [str(g) for g in (source_groups or []) if str(g).strip()]
    if isinstance(rp, str):
        body = rp.strip()
        if not body:
            return None
        return {"body": body, "source_groups": groups}
    if isinstance(rp, dict):
        body = str(rp.get("body") or "").strip()
        if not body:
            return None
        raw_groups = rp.get("source_groups")
        if isinstance(raw_groups, list) and raw_groups:
            groups = [str(g) for g in raw_groups if str(g).strip()]
        return {"body": body, "source_groups": groups}
    return None


def _execution_meta_from_result(result: dict) -> dict:
    """Keep the UI-sized execution summary with the persisted assistant message."""
    meta: dict = {}
    if "session_epoch" in result:
        meta["session_epoch"] = result["session_epoch"]
    capabilities = result.get("capabilities_used")
    if isinstance(capabilities, dict):
        meta["capabilities_used"] = capabilities
    invoked = result.get("capabilities_invoked")
    if isinstance(invoked, dict):
        meta["capabilities_invoked"] = invoked
    execution_chain = result.get("execution_chain")
    if isinstance(execution_chain, dict):
        meta["execution_chain"] = execution_chain
    for key in (
        "termination",
        "run_outcome_status",
        "execution_status",
        "evidence_status",
        "result_verdict",
        "classification_status",
        "intent",
    ):
        value = result.get(key)
        if value:
            meta[key] = value
    return meta


def _append_final_output_link(result: dict, message: dict, *, surface: str) -> None:
    run_id = str(result.get("run_id") or "")
    if not run_id:
        return
    try:
        from server.runtime.run_transcript import append_run_event

        append_run_event(
            run_id,
            {
                "kind": "audit",
                "stage": "final_output_persisted",
                "surface": surface,
                "message_id": message.get("message_id") or message.get("event_id") or "",
                "final_output": result.get("reply") or message.get("content") or "",
            },
        )
    except Exception:
        # Message persistence must not fail because the audit sidecar is unavailable.
        pass


def _publish(channel: str, event_type: str, data: dict) -> None:
    if _loop and _loop.is_running():
        bus.publish_threadsafe(_loop, channel, event_type, data)


def _publish_pending_question(result: dict) -> None:
    pending = result.get("pending_question")
    if not isinstance(pending, dict):
        return
    channel = str(pending.get("channel") or "").strip()
    if not channel:
        return
    _publish(channel, "ask_user_question", pending)


def _usage_run_ids(current_run_id: str, continuation: dict | None = None) -> list[str]:
    payload = continuation.get("payload") if isinstance(continuation, dict) else None
    prior_run_ids = payload.get("usage_run_ids") if isinstance(payload, dict) else []
    run_ids = (
        [str(run_id).strip() for run_id in prior_run_ids if str(run_id).strip()]
        if isinstance(prior_run_ids, list)
        else []
    )
    if current_run_id:
        run_ids.append(current_run_id)
    return list(dict.fromkeys(run_ids))


def _run_instance_turn(
    group_id: str,
    instance: dict,
    user_message: str,
    attachments: list[dict] | None = None,
    *,
    run_id: str = "",
    hop: int = 0,
    root_user_message: str | None = None,
    handoff_from: str | None = None,
    handoff_content: str | None = None,
    continuation: dict | None = None,
) -> None:
    iid = instance["instance_id"]
    display = instance["display_name"]
    root = root_user_message or user_message
    lock = _instance_lock(iid)
    with lock:
        repos.set_member_status(iid, "running")
        run_meta = {"instance_id": iid, "run_id": run_id}
        _publish(
            f"group:{group_id}",
            "instance.status",
            {**run_meta, "status": "running"},
        )
        _publish(
            f"group:{group_id}",
            "feed.trace",
            {
                "phase": "start",
                "instance_id": iid,
                "display_name": display,
                "run_id": run_id,
            },
        )
        try:
            tpl = repos.get_template(instance["template_id"])
            if not tpl:
                raise RuntimeError("template missing")
            template_id = tpl["template_id"]
            memories = turn_runtime.memory_summary_for_template(template_id)
            group = repos.get_group(group_id)
            extra_parts = [
                f"You are speaking in a project group as ONLY @{display}.",
                "Reply in first person as yourself only. Do not introduce, quote, or role-play other agents.",
                "Keep the reply focused on what YOU should say or do.",
                "If you need another teammate to continue after you finish, include @TheirName "
                "somewhere in your reply — the system will wake them with your message as context.",
                "If the message lists [附件] absolute paths, use those local files as context.",
            ]
            if group and group.get("workspace_path"):
                extra_parts.append(f"Project workspace: {group['workspace_path']}")

            if handoff_from and handoff_content:
                extra_parts.append(
                    f"This turn is a handoff from @{handoff_from} (hop {hop}/{MAX_HANDOFF_HOPS}). "
                    "Read their message carefully and continue the work they asked of you."
                )
                directed = (
                    f"[群内接力 · @{handoff_from} → @{display}]\n"
                    f"用户原话：\n{root}\n\n"
                    f"@{handoff_from} 的交接内容：\n{handoff_content}\n\n"
                    f"请只以 @{display} 的身份回答，不要替其他 Agent 发言。"
                )
            else:
                extra_parts.append(
                    "Other agents may also be @mentioned in the same user message — "
                    "ignore speaking for them."
                )
                directed = (
                    f"[群消息 · 仅 @{display} 需要回复]\n"
                    f"{user_message}\n\n"
                    f"请只以 @{display} 的身份回答，不要替其他 Agent 发言。"
                )

            result = turn_runtime.run_chat_turn(
                scope_key=turn_runtime.scope_group_agent(group_id, iid),
                surface="group",
                user_message=directed,
                template=tpl,
                memories=memories,
                extra_context="\n".join(extra_parts),
                title_hint=f"{group.get('name') if group else group_id} · {display}",
                attachments=attachments if not handoff_from else None,
                workspace_cwd=(group or {}).get("workspace_path") or None,
                group_id=group_id,
                instance_id=iid,
                on_trace=lambda step: _publish(
                    f"group:{group_id}",
                    "feed.trace",
                    {
                        "phase": "step",
                        "instance_id": iid,
                        "display_name": display,
                        "run_id": run_id,
                        "step": step,
                    },
                ),
                run_id=run_id or None,
                continuation=continuation,
            )
            if result.get("termination") == "waiting_for_user":
                from server.runtime import ask_user_transcript as ask_tx

                pending = result.get("pending_question") or {}
                meta = {
                    "display_name": display,
                    "theme_color": instance["theme_color"],
                    "template_id": template_id,
                    "model_profile_id": result.get("model_profile_id")
                    or tpl.get("model_profile_id")
                    or "",
                    "model_label": result.get("model_label")
                    or turn_runtime.profile_label(tpl.get("model_profile_id") or ""),
                    "handoff_hop": hop,
                    "handoff_from": handoff_from or "",
                    "tool_rounds": result.get("tool_rounds") or 0,
                    "run_id": result.get("run_id") or run_id,
                    "termination": "waiting_for_user",
                    "ask_user": ask_tx.build_ask_user_meta(pending, status="pending"),
                }
                if result.get("thinking"):
                    meta["thinking"] = result["thinking"]
                meta.update(_execution_meta_from_result(result))
                event = repos.add_feed_event(
                    group_id,
                    speaker_type="agent",
                    speaker_id=iid,
                    content=ask_tx.assistant_content(pending),
                    visibility="L1",
                    meta=meta,
                )
                _append_final_output_link(result, event, surface="group_feed")
                _publish(f"group:{group_id}", "feed.message", event)
                _publish_pending_question(result)
                _publish(
                    f"group:{group_id}",
                    "feed.trace",
                    {
                        "phase": "end",
                        "instance_id": iid,
                        "display_name": display,
                        "run_id": result.get("run_id") or run_id,
                        "duration_ms": (result.get("thinking") or {}).get("duration_ms") or 0,
                    },
                )
                return
            reply = result["reply"]
            meta = {
                "display_name": display,
                "theme_color": instance["theme_color"],
                "template_id": template_id,
                "model_profile_id": result.get("model_profile_id")
                or tpl.get("model_profile_id")
                or "",
                "model_label": result.get("model_label")
                or turn_runtime.profile_label(tpl.get("model_profile_id") or ""),
                "handoff_hop": hop,
                "handoff_from": handoff_from or "",
                "tool_rounds": result.get("tool_rounds") or 0,
                "run_id": result.get("run_id") or run_id,
            }
            if result.get("thinking"):
                meta["thinking"] = result["thinking"]
            meta.update(_execution_meta_from_result(result))
            usage_summary = repos.get_usage_summary_for_runs(
                _usage_run_ids(str(meta.get("run_id") or ""), continuation)
            )
            if usage_summary:
                meta["usage_summary"] = usage_summary
            rp_meta = _remember_proposal_from_result(result, source_groups=[group_id])
            if rp_meta:
                meta["remember_proposal"] = rp_meta
            event = repos.add_feed_event(
                group_id,
                speaker_type="agent",
                speaker_id=iid,
                content=reply,
                visibility="L1",
                meta=meta,
            )
            _append_final_output_link(result, event, surface="group_feed")
            _publish(f"group:{group_id}", "feed.message", event)
            if result.get("thinking"):
                _publish(
                    f"group:{group_id}",
                    "feed.trace",
                    {
                        "phase": "end",
                        "instance_id": iid,
                        "display_name": display,
                        "run_id": result.get("run_id") or run_id,
                        "duration_ms": (result.get("thinking") or {}).get("duration_ms") or 0,
                    },
                )
            _enqueue_handoffs_from_reply(
                group_id,
                from_instance=instance,
                reply=reply,
                root_user_message=root,
                hop=hop,
                origin_session_id=result.get("session_id") or "",
                source_run_id=result.get("run_id") or run_id or "",
                already_delegated="delegate_agent" in (result.get("capabilities_invoked") or {}).get("tools", []),
            )
        except Exception as e:
            event = repos.add_feed_event(
                group_id,
                speaker_type="system",
                speaker_id="system",
                content=f"{display} 执行失败: {e}",
                visibility="L1",
            )
            _publish(f"group:{group_id}", "feed.message", event)
            _publish(
                f"group:{group_id}",
                "error",
                {"message": str(e), "instance_id": iid, "run_id": run_id},
            )
        finally:
            repos.set_member_status(iid, "idle")
            _publish(
                f"group:{group_id}",
                "instance.status",
                {"instance_id": iid, "status": "idle", "run_id": run_id},
            )


def _enqueue_handoffs_from_reply(
    group_id: str,
    *,
    from_instance: dict,
    reply: str,
    root_user_message: str,
    hop: int,
    origin_session_id: str = "",
    source_run_id: str = "",
    already_delegated: bool = False,
) -> None:
    """Loose relay: any @Member in an agent reply wakes that member (scheme C)."""
    names = parse_mentions(reply)
    if not names:
        return

    next_hop = hop + 1
    if next_hop > MAX_HANDOFF_HOPS:
        event = repos.add_feed_event(
            group_id,
            speaker_type="system",
            speaker_id="system",
            content=(
                f"接力已达上限（{MAX_HANDOFF_HOPS} 跳），忽略 "
                f"@{from_instance['display_name']} 回复中的继续 @。"
            ),
            visibility="L1",
            meta={"handoff_capped": True},
        )
        _publish(f"group:{group_id}", "feed.message", event)
        return

    matched = repos.find_members_by_mention(group_id, names)
    from_iid = from_instance["instance_id"]
    targets: list[dict] = []
    seen: set[str] = set()
    for m in matched:
        mid = m["instance_id"]
        if mid == from_iid or mid in seen:
            continue
        seen.add(mid)
        targets.append(m)

    if not targets:
        return

    from server.runtime.feature_flags import flag_enabled

    if flag_enabled("agent_mesh_v1"):
        if already_delegated:
            return
        from server.db import mesh_repos

        for member in targets:
            # Only explicit @name: objective lines are delegation, not casual mentions.
            instruction = re.search(r"@" + re.escape(member["display_name"]) + r"\s*[:：]\s*([^\n]+)", reply)
            if not instruction:
                continue
            try:
                mesh_repos.delegate(
                    from_instance["template_id"], member["template_id"], instruction.group(1).strip(),
                    ["完成明确的派发目标，提供可核查的结果；缺少信息时在任务内追问。"],
                    origin_session_id=origin_session_id, origin_group_id=group_id,
                    root_request_id=source_run_id,
                    idempotency_key=f"handoff:{source_run_id}:{member['template_id']}",
                )
            except mesh_repos.MeshError as exc:
                event = repos.add_feed_event(
                    group_id, speaker_type="system", speaker_id="system", visibility="L1",
                    content=f"未向 {member['display_name']} 派发协作任务：{exc}",
                    meta={"mesh_error": exc.code},
                )
                _publish(f"group:{group_id}", "feed.message", event)
        return

    for m in targets:
        _executor.submit(
            _run_instance_turn,
            group_id,
            m,
            root_user_message,
            None,
            hop=next_hop,
            root_user_message=root_user_message,
            handoff_from=from_instance["display_name"],
            handoff_content=reply,
        )


def enqueue_group_mentions(
    group_id: str,
    user_message: str,
    members: list[dict],
    attachments: list[dict] | None = None,
    run_ids: dict[str, str] | None = None,
) -> None:
    """User @ fan-out: keep parallel (真人并行场景)."""
    for m in members:
        _executor.submit(
            _run_instance_turn,
            group_id,
            m,
            user_message,
            attachments,
            run_id=(run_ids or {}).get(m["instance_id"], ""),
            hop=0,
            root_user_message=user_message,
        )


def run_assistant_turn_async(
    template_id: str,
    user_message: str,
    *,
    aggregate_group_ids: list[str] | None = None,
    attachments: list[dict] | None = None,
    session_id: str | None = None,
    run_id: str = "",
) -> None:
    _executor.submit(
        _assistant_turn_worker,
        template_id,
        user_message,
        aggregate_group_ids or [],
        attachments or [],
        None,
        session_id,
        run_id,
    )


def run_im_assistant_turn_async(
    template_id: str,
    user_message: str,
    *,
    session_id: str,
    im_meta: dict,
    attachments: list[dict] | None = None,
    run_id: str = "",
) -> None:
    _executor.submit(
        _assistant_turn_worker,
        template_id,
        user_message,
        [],
        attachments or [],
        im_meta,
        session_id,
        run_id,
    )


def resume_question_async(item: dict, *, run_id: str) -> None:
    """Resume the exact DM assistant or group instance that asked the question."""
    if item.get("group_id"):
        instance = repos.get_member(str(item.get("instance_id") or ""))
        if (
            not instance
            or instance.get("group_id") != item.get("group_id")
            or instance.get("template_id") != item.get("template_id")
        ):
            raise RuntimeError("question group instance no longer exists")
        _executor.submit(
            _run_instance_turn,
            str(item["group_id"]),
            instance,
            "",
            None,
            run_id=run_id,
            continuation=item,
        )
        return
    _executor.submit(
        _assistant_turn_worker,
        str(item["template_id"]),
        "",
        [],
        [],
        None,
        str(item["session_id"]),
        run_id,
        item,
    )


def _assistant_turn_worker(
    template_id: str,
    user_message: str,
    aggregate_group_ids: list[str],
    attachments: list[dict] | None = None,
    im_meta: dict | None = None,
    session_id: str | None = None,
    run_id: str = "",
    continuation: dict | None = None,
) -> None:
    channel = f"assistant:{template_id}"
    _publish(
        channel,
        "assistant.status",
        {"template_id": template_id, "session_id": session_id or "", "status": "running", "run_id": run_id},
    )
    _publish(
        channel,
        "assistant.trace",
        {"phase": "start", "template_id": template_id, "session_id": session_id or "", "run_id": run_id},
    )
    reply_text = ""
    reply_attachments: list[dict] = []
    deliver_im_reply = True
    try:
        tpl = repos.get_template(template_id)
        if not tpl:
            raise RuntimeError("template not found")
        memories = turn_runtime.memory_summary_for_template(template_id)
        extra = ""
        if aggregate_group_ids:
            chunks = []
            for gid in aggregate_group_ids:
                g = repos.get_group(gid)
                if not g:
                    continue
                l1 = repos.list_l1_feed(gid, limit=15)
                stats = repos.task_stats(gid)
                lines = [f"### {g['name']} ({gid})", f"Tasks: {stats}"]
                for ev in l1:
                    lines.append(f"- [{ev['speaker_type']}] {ev['content'][:400]}")
                chunks.append("\n".join(lines))
            extra = "Cross-project summaries (L1 + task stats only):\n\n" + "\n\n".join(chunks)

        from avent_config import cfg_get, get_config

        cfg = get_config()
        workspace = (
            str(cfg_get(cfg, "avent", "default_workspace", default="") or "").strip()
            or str(cfg_get(cfg, "terminal", "cwd", default="") or "").strip()
            or None
        )

        def publish_intermediate(content: str, thinking_steps: list[dict]) -> None:
            sid = session_id or ""
            if not sid:
                from server.runtime.turn import scope_assistant, _get_sessions

                sid = _get_sessions().resolve_session_id(scope_assistant(template_id)) or ""
            meta: dict = {
                "intermediate": True,
                "model_profile_id": tpl.get("model_profile_id") or "",
                "model_label": turn_runtime.profile_label(tpl.get("model_profile_id") or ""),
                "run_id": run_id,
            }
            if thinking_steps:
                meta["thinking"] = {"duration_ms": 0, "steps": thinking_steps}
            msg = repos.add_assistant_message(
                template_id,
                "assistant",
                content,
                session_id=sid,
                meta=meta,
            )
            _append_final_output_link({"run_id": run_id}, msg, surface="assistant_dm")
            if sid:
                from server.api import assistant_session_ops as sess

                sess.sync_after_message(template_id, sid)
            _publish(channel, "assistant.message", msg)

        result = turn_runtime.run_chat_turn(
            scope_key=turn_runtime.scope_assistant(template_id),
            surface="assistant_dm",
            user_message=user_message,
            template=tpl,
            memories=memories,
            extra_context=extra,
            title_hint=user_message[:48],
            attachments=attachments,
            workspace_cwd=workspace,
            on_trace=lambda step: _publish(
                channel,
                "assistant.trace",
                {
                    "phase": "step",
                    "template_id": template_id,
                    "session_id": session_id or "",
                    "run_id": run_id,
                    "step": step,
                },
            ),
            on_intermediate_content=publish_intermediate,
            run_id=run_id or None,
            continuation=continuation,
            session_id=session_id,
        )
        if im_meta and result.get("termination") == "cancelled":
            deliver_im_reply = False
            return
        if result.get("termination") == "waiting_for_user":
            from server.runtime import ask_user_transcript as ask_tx

            pending = result.get("pending_question") or {}
            meta = {
                "model_profile_id": result.get("model_profile_id")
                or tpl.get("model_profile_id")
                or "",
                "model_label": result.get("model_label")
                or turn_runtime.profile_label(tpl.get("model_profile_id") or ""),
                "run_id": result.get("run_id") or run_id,
                "termination": "waiting_for_user",
                "ask_user": ask_tx.build_ask_user_meta(pending, status="pending"),
            }
            if result.get("thinking"):
                meta["thinking"] = result["thinking"]
            meta.update(_execution_meta_from_result(result))
            sid = session_id or result.get("session_id") or ""
            if not sid:
                from server.runtime.turn import scope_assistant, _get_sessions

                sid = _get_sessions().resolve_session_id(scope_assistant(template_id)) or ""
            msg = repos.add_assistant_message(
                template_id,
                "assistant",
                ask_tx.assistant_content(pending),
                session_id=sid,
                meta=meta,
            )
            if sid:
                from server.api import assistant_session_ops as sess

                sess.sync_after_message(template_id, sid)
            _publish(channel, "assistant.message", msg)
            _publish_pending_question(result)
            _publish(
                channel,
                "assistant.trace",
                {
                    "phase": "end",
                    "template_id": template_id,
                    "session_id": sid,
                    "run_id": result.get("run_id") or run_id,
                    "duration_ms": (result.get("thinking") or {}).get("duration_ms") or 0,
                },
            )
            return
        reply_text = str(result.get("reply") or "")
        reply_attachments = list(result.get("reply_attachments") or [])
        meta: dict = {
            "model_profile_id": result.get("model_profile_id")
            or tpl.get("model_profile_id")
            or "",
            "model_label": result.get("model_label")
            or turn_runtime.profile_label(tpl.get("model_profile_id") or ""),
            "run_id": result.get("run_id") or run_id,
        }
        if im_meta and deliver_im_reply:
            meta.update(
                {
                    "origin": "im",
                    "via": im_meta.get("platform"),
                    "im_conversation_id": im_meta.get("conversation_id"),
                    "external_delivery": "reply_to_inbound_im",
                    "provider_message_id": im_meta.get("provider_message_id"),
                }
            )
        rp_meta = _remember_proposal_from_result(
            result, source_groups=list(aggregate_group_ids or [])
        )
        if rp_meta:
            if aggregate_group_ids:
                rp_meta["source_groups"] = list(aggregate_group_ids)
            meta["remember_proposal"] = rp_meta
        if result.get("thinking"):
            meta["thinking"] = result["thinking"]
        meta.update(_execution_meta_from_result(result))
        usage_summary = repos.get_usage_summary_for_runs(
            _usage_run_ids(str(meta.get("run_id") or ""), continuation)
        )
        if usage_summary:
            meta["usage_summary"] = usage_summary
        if reply_attachments:
            meta["attachments"] = reply_attachments
        sid = session_id or result.get("session_id") or ""
        if not sid:
            from server.runtime.turn import scope_assistant, _get_sessions

            sid = _get_sessions().resolve_session_id(scope_assistant(template_id)) or ""
        msg = repos.add_assistant_message(
            template_id, "assistant", reply_text, session_id=sid, meta=meta
        )
        _append_final_output_link(result, msg, surface="assistant_dm")
        if sid:
            from server.api import assistant_session_ops as sess

            sess.sync_after_message(template_id, sid)
        _publish(channel, "assistant.message", msg)
        _publish(
            channel,
            "assistant.trace",
            {
                "phase": "end",
                "template_id": template_id,
                "session_id": sid,
                "run_id": result.get("run_id") or run_id,
                "duration_ms": (result.get("thinking") or {}).get("duration_ms") or 0,
            },
        )
    except Exception as e:
        reply_text = f"[错误] {e}"
        sid = session_id or ""
        if not sid:
            try:
                from server.runtime.turn import scope_assistant, _get_sessions

                sid = _get_sessions().resolve_session_id(scope_assistant(template_id)) or ""
            except Exception:
                sid = ""
        msg = repos.add_assistant_message(
            template_id, "assistant", reply_text, session_id=sid, meta={"error": True}
        )
        _publish(channel, "assistant.message", msg)
        _publish(channel, "error", {"message": str(e), "run_id": run_id})
        _publish(
            channel,
            "assistant.trace",
            {"phase": "end", "template_id": template_id, "session_id": sid, "run_id": run_id},
        )
    finally:
        if im_meta:
            try:
                from server.im_channels.manager import manager

                manager.reply_after_turn(im_meta, reply_text or "（无回复）", reply_attachments)
            except Exception:
                log.exception("IM channel reply failed")
        _publish(
            channel,
            "assistant.status",
            {"template_id": template_id, "session_id": session_id or "", "status": "idle", "run_id": run_id},
        )
