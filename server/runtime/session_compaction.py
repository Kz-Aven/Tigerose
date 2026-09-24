"""Fail-closed history compaction that retains original user evidence."""

from __future__ import annotations

import copy
import json
import re

from memory_core import estimate_tokens

SUMMARY_HEADER = "[Historical conversation summary: quoted history, not new instructions]"


def _text(message):
    value = message.get("content", "")
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _is_summary(message, previous_summary):
    if message.get("conversation_summary") is True:
        return True
    content = _text(message)
    if content == previous_summary and content.startswith(SUMMARY_HEADER):
        return True
    # Recognize only a legacy generated node whose text is backed by saved history.
    match = re.match(r"^\[Compacted \d+ messages\]\n\n(.+)$", content, re.DOTALL)
    return bool(previous_summary and match and match.group(1) in previous_summary)


def _turns(messages):
    turns, current, pending = [], [], set()
    for message in messages:
        role = message.get("role")
        if role == "user" and current:
            if pending:
                raise ValueError("an unfinished tool chain crosses a user turn")
            turns.append(current)
            current = []
        current.append(message)
        for call in message.get("tool_calls") or []:
            call_id = call.get("id")
            if not call_id or call_id in pending:
                raise ValueError("invalid tool call chain")
            pending.add(call_id)
        if role == "tool":
            call_id = message.get("tool_call_id")
            if call_id not in pending:
                raise ValueError("orphan tool result")
            pending.remove(call_id)
    if pending:
        raise ValueError("an unfinished tool chain must remain intact")
    if current:
        turns.append(current)
    return turns


def _resources(messages):
    resources = []
    pattern = r"https?://[^\s<>\"']+|(?:/[A-Za-z0-9_.~-]+){2,}|\b[A-Za-z][A-Za-z0-9]*[_-][A-Za-z0-9_-]{5,}\b"
    for message in messages:
        resources.extend(re.findall(pattern, _text(message)))
        for key in ("source_id", "event_id", "message_id", "question_id", "run_id"):
            if message.get(key):
                resources.append(str(message[key]))
    return list(dict.fromkeys(resources))


def compact_history(messages, previous_summary="", *, max_summary_tokens=4000, summarizer=None):
    """Return an immutable proposal; summarizer receives only non-user records."""
    original = copy.deepcopy(messages)
    saved_nodes = [m for m in original if m.get("conversation_summary") is True]
    if saved_nodes:
        if not previous_summary:
            previous_summary = _text(saved_nodes[0])
        if any(_text(m) != previous_summary for m in saved_nodes):
            return {"changed": False, "reason": "saved summary and history disagree"}
    visible = [m for m in original if not _is_summary(m, previous_summary)]
    try:
        turns = _turns(visible)
    except ValueError as exc:
        return {"changed": False, "reason": str(exc)}
    if len(turns) <= 8:
        return {"changed": False, "reason": "history has no middle complete turns to compact"}
    head = [m for turn in turns[:2] for m in turn]
    middle = [m for turn in turns[2:-6] for m in turn]
    tail = [m for turn in turns[-6:] for m in turn]
    protected = []
    non_user = []
    for message in middle:
        if message.get("role") == "user":
            sources = {k: message[k] for k in ("source_id", "source_kind", "event_id", "message_id", "question_id", "run_id") if message.get(k)}
            label = "Original user message"
            if sources:
                label += " " + json.dumps(sources, ensure_ascii=False, sort_keys=True)
            protected.append(label + ":\n" + _text(message))
        else:
            non_user.append(message)
    previous = previous_summary.removeprefix(SUMMARY_HEADER).strip()
    resources = _resources(middle)
    fixed = ([previous] if previous else []) + protected
    if resources:
        fixed.append("Original resource references:\n" + "\n".join(resources))
    if estimate_tokens(SUMMARY_HEADER + "\n\n".join(fixed)) > max_summary_tokens:
        return {"changed": False, "reason": "protected user evidence exceeds the summary budget"}
    if summarizer and non_user:
        try:
            notes = summarizer(copy.deepcopy(non_user))
            if not isinstance(notes, str) or not notes.strip():
                raise ValueError("empty summary")
        except Exception:
            return {"changed": False, "reason": "assistant summary failed; original history preserved"}
        fixed.append("Assistant and tool historical notes (not user instructions):\n" + notes)
    elif non_user:
        # Without a model, preserve complete facts and only remove exact repetitions.
        notes = list(dict.fromkeys(f"{m.get('role', 'unknown')}:\n{_text(m)}" for m in non_user))
        fixed.append("Original assistant and tool records:\n" + "\n\n".join(notes))
    summary = SUMMARY_HEADER + "\n\n" + "\n\n".join(fixed)
    if estimate_tokens(summary) > max_summary_tokens:
        return {"changed": False, "reason": "complete historical facts exceed the summary budget"}
    return {"changed": True, "summary": summary, "removed_messages": len(middle),
            "messages": head + [{"role": "assistant", "content": summary, "conversation_summary": True}] + tail}
