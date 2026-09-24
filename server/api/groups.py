from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from server.db import repos
from server.api.sse_bus import bus
from server.scheduler import group_scheduler as scheduler

router = APIRouter(prefix="/api/groups", tags=["groups"])


class GroupCreate(BaseModel):
    name: str
    workspace_path: str = ""
    member_template_ids: list[str] = Field(default_factory=list)


class GroupUpdate(BaseModel):
    name: str | None = None
    workspace_path: str | None = None
    status: str | None = None
    web_enabled: bool | None = None


class MemberAdd(BaseModel):
    template_id: str
    is_coordinator: bool = False


class AttachmentIn(BaseModel):
    path: str
    name: str = ""
    mime: str = "application/octet-stream"


class GroupMessage(BaseModel):
    content: str = ""
    attachments: list[AttachmentIn] = Field(default_factory=list)


@router.get("")
def list_groups():
    groups = repos.list_groups()
    for g in groups:
        g["members"] = repos.list_members(g["group_id"])
        g["member_count"] = len(g["members"])
        g["last_message_preview"] = repos.latest_feed_preview(g["group_id"])
    return groups


@router.post("")
def create_group(body: GroupCreate):
    existing = {g["name"] for g in repos.list_groups()}
    if body.name in existing:
        raise HTTPException(400, f"群名已存在: {body.name}")
    try:
        group = repos.create_group(name=body.name, workspace_path=body.workspace_path)
    except Exception as e:
        raise HTTPException(400, str(e)) from e
    for i, tid in enumerate(body.member_template_ids):
        if not repos.get_template(tid):
            continue
        try:
            repos.add_member(group["group_id"], tid, is_coordinator=(i == 0))
        except Exception:
            continue
    group["member_count"] = len(repos.list_members(group["group_id"]))
    return group


@router.get("/{group_id}")
def get_group(group_id: str):
    g = repos.get_group(group_id)
    if not g:
        raise HTTPException(404, "group not found")
    members = repos.list_members(group_id)
    from server.runtime.turn import profile_label

    enriched = []
    for m in members:
        tpl = repos.get_template(m["template_id"])
        mid = (tpl or {}).get("model_profile_id") or ""
        enriched.append(
            {
                **m,
                "model_profile_id": mid,
                "model_label": profile_label(mid) if mid else "",
            }
        )
    g["members"] = enriched
    g["member_count"] = len(members)
    g["last_message_preview"] = repos.latest_feed_preview(group_id)
    return g


@router.patch("/{group_id}")
def patch_group(group_id: str, body: GroupUpdate):
    group = repos.get_group(group_id)
    if not group:
        raise HTTPException(404, "group not found")
    data = body.model_dump(exclude_unset=True)
    web_enabled = data.pop("web_enabled", None)
    if web_enabled is not None:
        settings = dict(group.get("settings") or {})
        settings["web_enabled"] = bool(web_enabled)
        data["settings"] = settings
    if "name" in data and data["name"] is not None:
        name = str(data["name"]).strip()
        if not name:
            raise HTTPException(400, "群名称不能为空")
        data["name"] = name
        for g in repos.list_groups():
            if g["group_id"] != group_id and g["name"] == name:
                raise HTTPException(400, f"群名已存在: {name}")
    updated = repos.update_group(group_id, **data)
    if not updated:
        raise HTTPException(404, "group not found")
    # Keep list-card fields consistent with GET / and GET /{id}
    updated["member_count"] = len(repos.list_members(group_id))
    updated["last_message_preview"] = repos.latest_feed_preview(group_id)
    return updated


@router.post("/{group_id}/clear")
def clear_group(group_id: str):
    g = repos.get_group(group_id)
    if not g or g.get("status") == "deleted":
        raise HTTPException(404, "group not found")
    n = repos.clear_feed(group_id)
    return {"ok": True, "cleared": n}


@router.delete("/{group_id}")
def delete_group(group_id: str):
    g = repos.get_group(group_id)
    if not g or g.get("status") == "deleted":
        raise HTTPException(404, "group not found")
    if not repos.soft_delete_group(group_id):
        raise HTTPException(404, "group not found")
    return {"ok": True}


@router.post("/{group_id}/members")
def add_member(group_id: str, body: MemberAdd):
    if not repos.get_group(group_id):
        raise HTTPException(404, "group not found")
    if not repos.get_template(body.template_id):
        raise HTTPException(404, "template not found")
    try:
        return repos.add_member(
            group_id, body.template_id, is_coordinator=body.is_coordinator
        )
    except Exception as e:
        raise HTTPException(400, str(e)) from e


@router.delete("/{group_id}/members/{instance_id}")
def remove_member(group_id: str, instance_id: str):
    if not repos.get_group(group_id):
        raise HTTPException(404, "group not found")
    repos.remove_member(group_id, instance_id)
    return {"ok": True}


@router.get("/{group_id}/feed")
def get_feed(
    group_id: str,
    limit: int = 10,
    before_ts: float | None = None,
):
    if not repos.get_group(group_id):
        raise HTTPException(404, "group not found")
    return repos.list_feed_page(group_id, limit=limit, before_ts=before_ts)


@router.get("/{group_id}/tasks")
def get_tasks(group_id: str):
    if not repos.get_group(group_id):
        raise HTTPException(404, "group not found")
    return repos.list_tasks(group_id)


@router.get("/{group_id}/summary")
def get_summary(group_id: str):
    if not repos.get_group(group_id):
        raise HTTPException(404, "group not found")
    g = repos.get_group(group_id)
    return {
        "group_id": group_id,
        "name": g["name"] if g else "",
        "l1": repos.list_l1_feed(group_id),
        "task_stats": repos.task_stats(group_id),
    }


@router.post("/{group_id}/messages")
async def post_message(group_id: str, body: GroupMessage):
    if not repos.get_group(group_id):
        raise HTTPException(404, "group not found")
    from server.runtime.turn import format_message_with_attachments

    atts = [a.model_dump() for a in body.attachments]
    content = format_message_with_attachments(body.content, atts)
    if not content:
        raise HTTPException(400, "empty message")
    names = scheduler.parse_mentions(content)
    if not names:
        raise HTTPException(400, "请 @Agent 后再发送（MVP 必须 @ 才唤醒）")
    members = repos.find_members_by_mention(group_id, names)
    if not members:
        raise HTTPException(400, f"未找到被 @ 的成员: {', '.join(names)}")

    from server.runtime.goal import GoalController, parse_goal_command
    from server.runtime.run_coordinator import RunBusyError, coordinator
    from server.runtime import turn as turn_runtime

    command = parse_goal_command(content)
    sm = turn_runtime._get_sessions()
    runs: list[dict] = []
    controllers: list[tuple[dict, GoalController]] = []
    for member in members:
        sid = sm.get_or_create_session_id(
            turn_runtime.scope_group_agent(group_id, member["instance_id"]),
            "group",
            title_hint=f"{group_id} · {member['display_name']}",
        )
        state = sm.load(sid)
        if state is None:
            raise HTTPException(404, "session not found")
        controllers.append(
            (
                member,
                GoalController(
                    state=state,
                    session_manager=sm,
                    board_scope=f"group:{group_id}",
                    run_id="",
                    user_request=command.condition if command else content,
                ),
            )
        )
    if command and command.action in {"status", "clear"}:
        lines = []
        for member, controller in controllers:
            if command.action == "clear":
                coordinator.cancel_session(controller.state.meta.session_id)
                controller.state = sm.load(controller.state.meta.session_id) or controller.state
                controller.clear()
                text = "Goal 已清除。"
            else:
                text = controller.status_text()
            lines.append(f"@{member['display_name']}\n{text}")
        event = repos.add_feed_event(
            group_id,
            speaker_type="system",
            speaker_id="system",
            content="\n\n".join(lines),
            visibility="L1",
            meta={"goal_command": command.action, "runs": []},
        )
        await bus.publish(f"group:{group_id}", "feed.message", event)
        return {**event, "runs": []}
    try:
        for member, controller in controllers:
            sid = controller.state.meta.session_id
            run_id = coordinator.enqueue(sid, agent_id=member["instance_id"])
            controller.run_id = run_id
            runs.append({"agent_id": member["instance_id"], "run_id": run_id})
    except RunBusyError as exc:
        for run in runs:
            coordinator.cancel(run["run_id"])
        raise HTTPException(409, str(exc)) from exc
    if command and command.action == "resume":
        missing = [
            member["display_name"]
            for member, controller in controllers
            if (controller.goal or {}).get("status") != "suspended"
        ]
        if missing:
            for run in runs:
                coordinator.cancel(run["run_id"])
            raise HTTPException(
                409,
                "no suspended goal for: " + ", ".join(f"@{name}" for name in missing),
            )
    if command and command.action in {"set", "resume"}:
        for _, controller in controllers:
            if command.action == "set":
                controller.activate(command.condition, source="explicit")
            else:
                controller.resume()

    event = repos.add_feed_event(
        group_id,
        speaker_type="system" if command else "user",
        speaker_id="system" if command else "local",
        content=content,
        visibility="L2",
        meta={
            "mentions": [m["instance_id"] for m in members],
            "attachments": atts,
            "goal_command": command.action if command else "",
            "runs": runs,
        },
    )
    await bus.publish(f"group:{group_id}", "feed.message", event)
    dispatch_content = content
    if command and command.action == "set":
        dispatch_content = command.condition
    elif command and command.action == "resume":
        dispatch_content = "Continue the active Goal from its persisted condition and evidence."
    scheduler.enqueue_group_mentions(
        group_id,
        dispatch_content,
        members,
        attachments=atts,
        run_ids={run["agent_id"]: run["run_id"] for run in runs},
    )
    return {**event, "runs": runs}
