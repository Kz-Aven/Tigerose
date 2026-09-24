"""Pending AskUserQuestion API."""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from server.api.sse_bus import bus
from server.db import repos
from server.runtime import ask_user_transcript as ask_tx

router = APIRouter(prefix="/api/questions", tags=["questions"])


class QuestionAnswer(BaseModel):
    question_id: str
    selected_labels: list[str] = Field(default_factory=list)
    other_text: str = ""


class AnswerBody(BaseModel):
    answers: list[QuestionAnswer]


def _validate_channel(channel: str) -> str:
    if not (channel.startswith("assistant:") or channel.startswith("group:")):
        raise HTTPException(400, "channel must be assistant:{id} or group:{id}")
    return channel


def _validate_answers(item: dict, answers: list[QuestionAnswer]) -> list[dict]:
    questions = (item.get("payload") or {}).get("questions") or []
    expected = {str(q.get("id") or ""): q for q in questions}
    if len(answers) != len(expected):
        raise HTTPException(400, "answers must contain exactly one item per question")
    seen: set[str] = set()
    normalized: list[dict] = []
    for answer in answers:
        qid = answer.question_id.strip()
        if qid not in expected or qid in seen:
            raise HTTPException(400, f"unknown or duplicate question_id: {qid}")
        question = expected[qid]
        selected = [str(v).strip() for v in answer.selected_labels if str(v).strip()]
        if len(selected) != len(set(selected)):
            raise HTTPException(400, f"duplicate selected_labels for {qid}")
        allowed = {
            str(option.get("label") or "")
            for option in question.get("options") or []
        }
        unknown = [label for label in selected if label not in allowed]
        if unknown:
            raise HTTPException(400, f"unknown labels for {qid}: {', '.join(unknown)}")
        if not question.get("multi_select") and len(selected) > 1:
            raise HTTPException(400, f"{qid} does not allow multiple selections")
        other_text = answer.other_text.strip()
        if not selected and not other_text:
            raise HTTPException(400, f"{qid} requires a selection or other_text")
        seen.add(qid)
        normalized.append(
            {
                "question_id": qid,
                "selected_labels": selected,
                "other_text": other_text,
            }
        )
    return normalized


async def _persist_resolution_transcript(
    item: dict[str, Any],
    *,
    status: str,
    answers: list[dict[str, Any]] | None = None,
) -> None:
    """Patch interrupt bubble + append user answer/cancel into chat history."""
    question_id = str(item.get("question_id") or "")
    channel = str(item.get("channel") or "")
    template_id = str(item.get("template_id") or "")
    session_id = str(item.get("session_id") or "")
    group_id = str(item.get("group_id") or "")
    ask_meta = ask_tx.build_ask_user_meta(item, status=status, answers=answers)
    content = (
        ask_tx.cancel_content()
        if status == "cancelled"
        else ask_tx.answer_content(item, answers)
    )

    if group_id:
        interrupt = repos.find_feed_event_for_ask_user(group_id, question_id)
        if interrupt:
            patched = repos.patch_feed_event_meta(
                group_id, str(interrupt["event_id"]), {"ask_user": ask_meta}
            )
            if patched:
                await bus.publish(channel, "feed.message", patched)
        user_event = repos.add_feed_event(
            group_id,
            speaker_type="user",
            speaker_id="local",
            content=content,
            visibility="L1",
            meta={
                "ask_user_answer": {
                    "question_id": question_id,
                    "status": status,
                    "answers": answers or [],
                }
            },
        )
        await bus.publish(channel, "feed.message", user_event)
        return

    if not template_id:
        return
    interrupt = repos.find_assistant_message_for_ask_user(
        template_id, session_id, question_id
    )
    if interrupt:
        patched = repos.patch_assistant_message_meta(
            template_id,
            str(interrupt["message_id"]),
            {"ask_user": ask_meta},
        )
        if patched:
            await bus.publish(channel, "assistant.message", patched)
    user_msg = repos.add_assistant_message(
        template_id,
        "user",
        content,
        session_id=session_id,
        meta={
            "ask_user_answer": {
                "question_id": question_id,
                "status": status,
                "answers": answers or [],
            }
        },
    )
    if session_id:
        from server.api import assistant_session_ops as sess

        sess.sync_after_message(template_id, session_id)
    await bus.publish(channel, "assistant.message", user_msg)


@router.get("/pending")
def pending_questions(channel: str = Query(...)):
    return {"items": repos.list_pending_questions(_validate_channel(channel))}


@router.post("/{question_id}/answer")
async def answer_question(question_id: str, body: AnswerBody):
    item = repos.get_pending_question(question_id)
    if not item:
        raise HTTPException(404, "question not found")
    if item.get("status") != "pending":
        raise HTTPException(409, f"question is already {item.get('status')}")
    answers = _validate_answers(item, body.answers)

    from server.runtime.run_coordinator import RunBusyError, coordinator

    try:
        run_id = coordinator.enqueue(
            str(item["session_id"]),
            agent_id=str(item.get("instance_id") or item.get("template_id") or ""),
        )
    except RunBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    answered = repos.answer_pending_question(question_id, answers)
    if not answered:
        coordinator.cancel(run_id)
        raise HTTPException(409, "question was resolved concurrently")
    try:
        await _persist_resolution_transcript(
            answered, status="answered", answers=answers
        )
    except Exception:
        # Transcript is best-effort; do not block resume.
        pass
    try:
        from server.scheduler.group_scheduler import resume_question_async

        resume_question_async(answered, run_id=run_id)
    except Exception as exc:
        coordinator.cancel(run_id)
        raise HTTPException(409, f"unable to resume question: {exc}") from exc
    runs = [
        {
            "agent_id": str(
                answered.get("instance_id") or answered.get("template_id") or ""
            ),
            "run_id": run_id,
        }
    ]
    await bus.publish(
        str(answered["channel"]),
        "ask_user_question_resolved",
        {**answered, "runs": runs},
    )
    return {"ok": True, "runs": runs}


@router.post("/{question_id}/cancel")
async def cancel_question(question_id: str):
    item = repos.cancel_pending_question(question_id)
    if not item:
        existing = repos.get_pending_question(question_id)
        if not existing:
            raise HTTPException(404, "question not found")
        raise HTTPException(409, f"question is already {existing.get('status')}")
    from server.runtime.turn import _get_sessions

    sm = _get_sessions()
    state = sm.load(str(item["session_id"]))
    if state:
        continuation = state.context.get("pending_question_continuation")
        if (
            isinstance(continuation, dict)
            and continuation.get("question_id") == question_id
        ):
            state.context.pop("pending_question_continuation", None)
        goal = state.context.get("goal")
        if (
            isinstance(goal, dict)
            and goal.get("status") == "waiting_for_user"
            and goal.get("pending_question_id") == question_id
        ):
            goal["status"] = "suspended"
            goal.pop("pending_question_id", None)
            goal["updated_at"] = time.time()
        sm.save(state, expected_version=state.version, retries=5)
    try:
        await _persist_resolution_transcript(item, status="cancelled", answers=[])
    except Exception:
        pass
    await bus.publish(
        str(item["channel"]),
        "ask_user_question_resolved",
        item,
    )
    return {"ok": True, "runs": []}
