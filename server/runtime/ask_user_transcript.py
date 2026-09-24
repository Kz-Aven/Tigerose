"""Persist AskUserQuestion turns into chat / feed transcript."""

from __future__ import annotations

from typing import Any


def questions_from_pending(pending: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(pending, dict):
        return []
    questions = pending.get("questions")
    if isinstance(questions, list) and questions:
        return questions
    payload = pending.get("payload")
    if isinstance(payload, dict):
        nested = payload.get("questions")
        if isinstance(nested, list):
            return nested
    return []


def assistant_content(pending: dict[str, Any] | None) -> str:
    """Short bubble text for sidebar preview + accessibility."""
    questions = questions_from_pending(pending)
    if not questions:
        return "需要你的回答"
    headers = [
        str(q.get("header") or "").strip()
        for q in questions
        if isinstance(q, dict) and str(q.get("header") or "").strip()
    ]
    if len(headers) == 1:
        return f"需要你的回答：{headers[0]}"
    if headers:
        return f"需要你的回答（{len(headers)} 个问题）"
    return "需要你的回答"


def answer_content(pending: dict[str, Any] | None, answers: list[dict[str, Any]] | None) -> str:
    questions = {
        str(q.get("id") or ""): q
        for q in questions_from_pending(pending)
        if isinstance(q, dict) and str(q.get("id") or "").strip()
    }
    lines: list[str] = []
    for raw in answers or []:
        if not isinstance(raw, dict):
            continue
        qid = str(raw.get("question_id") or "").strip()
        question = questions.get(qid) or {}
        header = str(question.get("header") or qid or "问题").strip()
        labels = [
            str(label).strip()
            for label in (raw.get("selected_labels") or [])
            if str(label).strip()
        ]
        other = str(raw.get("other_text") or "").strip()
        parts = list(labels)
        if other:
            parts.append(f"其他：{other}")
        choice = "、".join(parts) if parts else "（未选）"
        lines.append(f"{header}：{choice}")
    if not lines:
        return "已提交回答"
    if len(lines) == 1:
        return lines[0]
    return "\n".join(lines)


def cancel_content() -> str:
    return "已取消回答"


def build_ask_user_meta(
    pending: dict[str, Any] | None,
    *,
    status: str = "pending",
    answers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    pending = pending if isinstance(pending, dict) else {}
    meta: dict[str, Any] = {
        "question_id": str(pending.get("question_id") or ""),
        "status": status,
        "questions": questions_from_pending(pending),
    }
    if answers is not None:
        meta["answers"] = answers
    return meta
