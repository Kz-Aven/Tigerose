"""Tools for durable collaboration between existing assistant identities."""

from __future__ import annotations

import json
from typing import Any

def _spec(description: str, properties: dict, required: list[str]) -> dict:
    return {"description": description, "dangerous": False,
            "parameters": {"type": "object", "properties": properties,
                           "required": required, "additionalProperties": False}}


_TEXT = {"type": "string"}
_IDS = {"type": "array", "items": _TEXT}
_TASK = {"task_id": _TEXT}
_KEY = {"idempotency_key": {"type": "string", "description": "Stable unique key for this action; reuse when retrying."}}

MESH_TOOL_SPECS = {
    "discover_agents": _spec("Find existing peer assistants you are allowed to contact. No permission is implied by a name or group membership.",
                             {"query": _TEXT, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}, []),
    "get_agent_profile": _spec("Read the public capability contract of an authorized peer.", {"agent_id": _TEXT}, ["agent_id"]),
    "delegate_agent": _spec("Create a durable task for an existing peer. All assistants are peers. Supply an explicit objective and acceptance criteria; you must review the eventual result. Calls may run in parallel. A task grants replies within that task, not permanent reverse calling permission.",
                            {"agent_id": _TEXT, "title": {"type": "string", "description": "Short task title, at most 40 characters. Put full instructions and source material in objective."}, "objective": _TEXT, "acceptance_criteria": _IDS,
                             "context": {"type": "object"}, "artifact_refs": _IDS,
                             "parent_task_id": _TEXT, "depends_on": _IDS, **_KEY},
                            ["agent_id", "objective", "acceptance_criteria", "idempotency_key"]),
    "list_agent_tasks": _spec("List your own sent and received collaboration tasks.",
                              {"role": {"type": "string", "enum": ["all", "sent", "received"]}, "status": _TEXT, "cursor": _TEXT}, []),
    "get_agent_task": _spec("Read a task you participate in, its submissions and messages. Other tasks' private messages are not shared.",
                           {**_TASK, "after_sequence": {"type": "integer", "minimum": 0}}, ["task_id"]),
    "send_task_message": _spec("Communicate with the other participant inside an existing task. Use clarification_request, information_request or decision_request when an answer is needed; then yield. Progress does not wake the recipient. Final results must use submit_agent_task.",
                              {**_TASK, "kind": {"type": "string", "enum": ["message", "clarification_request", "information_request", "decision_request", "progress", "artifact"]},
                               "body": _TEXT, "reply_to": _TEXT, "artifact_refs": _IDS, **_KEY},
                              ["task_id", "kind", "body", "idempotency_key"]),
    "yield_agent_task": _spec("Persist that you are waiting for a reply or child tasks and end this execution turn. No polling or occupied worker is needed.",
                             {**_TASK, "reason": {"type": "string", "enum": ["children", "child_tasks", "clarification", "information", "message", "decision", "user_decision", "reconciliation", "permission"]}, "request_id": _TEXT, "child_task_ids": _IDS}, ["task_id", "reason"]),
    "submit_agent_task": _spec("Submit your final result for the task caller to review. Submission is NOT acceptance. Required child tasks must first be accepted.",
                              {**_TASK, "summary": _TEXT, "findings": {"type": "array", "items": {"type": "object"}},
                               "sources": _IDS, "warnings": _IDS, "artifact_refs": _IDS, **_KEY}, ["task_id", "summary", "idempotency_key"]),
    "review_agent_task": _spec("As the caller, review the current submission against its acceptance criteria. Accept it or request specific changes. Only your own outgoing tasks may be reviewed.",
                              {**_TASK, "submission_revision": {"type": "integer", "minimum": 1},
                               "decision": {"type": "string", "enum": ["accept", "request_changes"]}, "reason": _TEXT, **_KEY},
                              ["task_id", "submission_revision", "decision", "reason", "idempotency_key"]),
    "decide_agent_task": _spec("As the parent task caller, explicitly allow a partial delivery despite the named failed children. This does not accept results or mark failed dependencies successful.",
                              {**_TASK, "failed_child_ids": _IDS, "reason": _TEXT}, ["task_id", "failed_child_ids", "reason"]),
    "fail_agent_task": _spec("Report an unrecoverable failure as the receiver. Unfinished delegated descendants are cancelled; completed evidence is preserved.",
                            {**_TASK, "error_code": _TEXT, "reason": _TEXT}, ["task_id", "error_code", "reason"]),
    "cancel_agent_task": _spec("Cancel a task you initiated, including unfinished delegated descendants. Does not undo external effects.",
                              {**_TASK, "reason": _TEXT}, ["task_id", "reason"]),
    "publish_task_artifact": _spec("Register a file produced in this task's workspace as an artifact. As its creator you may pass its artifact_id in delegate_agent.artifact_refs to a peer authorized by your own ACL. Complete requested downstream work before submitting this task. Do not invent artifact IDs.",
                                  {**_TASK, "path": _TEXT, "name": _TEXT, "mime": _TEXT}, ["task_id", "path"]),
    "read_task_artifact": _spec("Materialize an artifact explicitly authorized for this task into its inputs directory. Read the returned local path with your normal file tools. This does not grant access to unrelated tasks or permission to forward the artifact.",
                               {"artifact_id": _TEXT}, ["artifact_id"]),
}

QUERY_NAMES = frozenset({"discover_agents", "get_agent_profile", "list_agent_tasks", "get_agent_task"})
CONTROL_NAMES = frozenset(MESH_TOOL_SPECS) - QUERY_NAMES


def handlers(ctx) -> dict[str, Any]:
    from server.db import mesh_repos as repo
    from server.runtime.executor import ToolResult

    actor = ctx.template_id

    def wrap(fn, *, end_turn=False, require_key=False):
        def invoke(**kwargs):
            try:
                if require_key and not str(kwargs.get("idempotency_key") or "").strip():
                    raise repo.MeshError("invalid_input", "A stable idempotency_key is required")
                value = fn(**kwargs)
                paused = isinstance(value, dict) and bool(value.get("limit_reached"))
                return ToolResult(json.dumps(value, ensure_ascii=False, default=str), "ok",
                                  {"mesh_yield": True, "mesh_task_id": ctx.mesh_task_id} if paused or (end_turn and ctx.mesh_task_id) else {})
            except repo.MeshError as exc:
                return ToolResult(json.dumps({"error": exc.code, "message": str(exc)}, ensure_ascii=False), "error")
        return invoke

    def delegate(agent_id, objective, acceptance_criteria, context=None, parent_task_id="", depends_on=None, idempotency_key="", artifact_refs=None, title=""):
        if ctx.mesh_task_id:
            if parent_task_id and parent_task_id != ctx.mesh_task_id:
                raise repo.MeshError("permission_denied", "Delegation must belong to the current task", 403)
            parent_task_id = ctx.mesh_task_id
        return repo.delegate(actor, agent_id, objective, acceptance_criteria, context=context,
                             parent_task_id=parent_task_id, origin_session_id=ctx.session_id,
                             origin_group_id=ctx.group_id or "", root_request_id=ctx.mesh_task_id or ctx.run_id or ctx.session_id,
                             depends_on=depends_on, idempotency_key=idempotency_key, artifact_refs=artifact_refs, title=title)

    def get(task_id, after_sequence=0):
        return {"task": repo.get_task(task_id, actor_id=actor),
                "messages": repo.messages(task_id, actor_id=actor, after_sequence=after_sequence)}

    def submit(task_id, summary, findings=None, sources=None, warnings=None, artifact_refs=None, idempotency_key=""):
        task = repo.get_task(task_id, actor_id=actor)
        if task['executor_kind'] == 'mesh_coordination':
            raise repo.MeshError('invalid_input', 'This is the source coordination task. Return the final result as your final reply; do not submit it for another review.')
        return repo.submit(task_id, actor, {"summary": summary, "findings": findings or [],
                                          "sources": sources or [], "warnings": warnings or [],
                                          "artifact_refs": artifact_refs or []}, idempotency_key=idempotency_key)

    def read_artifact(artifact_id):
        import hashlib
        import os
        import tempfile
        from pathlib import Path

        if not ctx.mesh_task_id:
            raise repo.MeshError("permission_denied", "Read artifacts inside their authorized task execution", 403)
        repo.get_task(ctx.mesh_task_id, actor_id=actor)
        artifact = repo.artifact(artifact_id, actor_id=actor)
        if artifact["task_id"] != ctx.mesh_task_id and ctx.mesh_task_id not in artifact["metadata"].get("forwarded_to_tasks", []):
            raise repo.MeshError("permission_denied", "Artifact has not been forwarded to this task", 403)
        root = Path(repo.task_workspace(ctx.mesh_task_id)).resolve()
        inputs = root / "inputs"
        inputs.mkdir(parents=True, exist_ok=True)
        if not inputs.resolve().is_relative_to(root):
            raise repo.MeshError("permission_denied", "Artifact inputs must stay inside the task workspace", 403)
        name = Path(artifact.get("name") or "artifact").name
        if name in ("", ".", ".."):
            name = "artifact"
        name = name.encode("utf-8")[:180].decode("utf-8", errors="ignore")
        target = inputs / (hashlib.sha256(artifact_id.encode()).hexdigest()[:16] + "-" + name)
        temporary = None
        try:
            digest = hashlib.sha256()
            with open(artifact["path"], "rb") as source, tempfile.NamedTemporaryFile(dir=inputs, delete=False) as output:
                temporary = Path(output.name)
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
                    output.write(chunk)
            if digest.hexdigest() != artifact["sha256"]:
                raise repo.MeshError("stale_revision", "Artifact content changed while being read", 409)
            os.replace(temporary, target)
            return {"artifact_id": artifact_id, "path": str(target), "name": name,
                    "mime": artifact.get("mime", "application/octet-stream")}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    return {
        "discover_agents": wrap(lambda query="", limit=5: repo.discover(actor, query, limit)),
        "get_agent_profile": wrap(lambda agent_id: repo.profile(agent_id, actor_id=actor)),
        "delegate_agent": wrap(delegate, require_key=True),
        "list_agent_tasks": wrap(lambda role="all", status="", cursor="": repo.list_tasks(actor_id=actor, role=role, status=status, cursor=cursor)),
        "get_agent_task": wrap(get),
        "send_task_message": wrap(lambda task_id, kind, body, reply_to="", artifact_refs=None, idempotency_key="": repo.send_message(task_id, actor, kind, body, reply_to=reply_to, artifact_refs=artifact_refs, idempotency_key=idempotency_key), require_key=True),
        "yield_agent_task": wrap(lambda task_id, reason, request_id="", child_task_ids=None: repo.yield_task(task_id, actor, reason, request_id=request_id, child_task_ids=child_task_ids), end_turn=True),
        "submit_agent_task": wrap(submit, end_turn=True, require_key=True),
        "review_agent_task": wrap(lambda task_id, submission_revision, decision, reason="", idempotency_key="": repo.review(task_id, actor, submission_revision, decision, reason, idempotency_key=idempotency_key), end_turn=True, require_key=True),
        "decide_agent_task": wrap(lambda task_id, failed_child_ids, reason: repo.allow_partial(task_id, actor, failed_child_ids, reason)),
        "fail_agent_task": wrap(lambda task_id, error_code, reason="": repo.fail_task(task_id, actor, error_code, reason), end_turn=True),
        "cancel_agent_task": wrap(lambda task_id, reason="": repo.cancel_task(task_id, actor_id=actor, reason=reason)),
        "publish_task_artifact": wrap(lambda task_id, path, name="", mime="": repo.register_artifact(task_id, actor, str((ctx.cwd / path).resolve()), name=name, mime=mime)),
        "read_task_artifact": wrap(read_artifact),
    }
