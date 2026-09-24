"""Provider usage normalization and durable metering for LLM calls."""

from __future__ import annotations

import json
import math
import time
import uuid
from typing import Any

from server.db import repos


def _get(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _number(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _estimate_tokens(value: Any) -> int:
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    return max(1, cjk + math.ceil((len(text) - cjk) / 4))


def _response_output(response: Any) -> Any:
    choices = _get(response, "choices") or []
    if not choices:
        return ""
    message = _get(choices[0], "message")
    if message is None:
        return ""
    if hasattr(message, "model_dump"):
        return message.model_dump()
    return message


def _normalized_usage(response: Any, request: dict[str, Any]) -> dict[str, Any]:
    usage = _get(response, "usage")
    input_tokens = _number(_get(usage, "prompt_tokens"))
    if input_tokens is None:
        input_tokens = _number(_get(usage, "input_tokens"))
    output_tokens = _number(_get(usage, "completion_tokens"))
    if output_tokens is None:
        output_tokens = _number(_get(usage, "output_tokens"))
    if input_tokens is None or output_tokens is None:
        input_tokens = _estimate_tokens({"messages": request.get("messages"), "tools": request.get("tools")})
        output_tokens = _estimate_tokens(_response_output(response))
        return {
            "input_tokens": input_tokens,
            "cached_input_tokens": 0,
            "uncached_input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "reasoning_tokens": None,
            "cache_creation_tokens": None,
            "usage_source": "estimated",
            "cache_usage_source": "unavailable",
        }

    prompt_details = _get(usage, "prompt_tokens_details") or _get(usage, "input_tokens_details")
    cached = _number(_get(prompt_details, "cached_tokens"))
    if cached is None:
        cached = _number(_get(prompt_details, "cache_read_input_tokens"))
    cache_source = "provider" if cached is not None else "unavailable"
    cached = min(cached or 0, input_tokens)
    completion_details = _get(usage, "completion_tokens_details") or _get(usage, "output_tokens_details")
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "uncached_input_tokens": input_tokens - cached,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "reasoning_tokens": _number(_get(completion_details, "reasoning_tokens")),
        "cache_creation_tokens": _number(_get(prompt_details, "cache_creation_input_tokens")),
        "usage_source": "provider",
        "cache_usage_source": cache_source,
    }


def create_completion(
    client: Any,
    *,
    profile: dict[str, Any],
    call_kind: str,
    run_id: str = "",
    session_id: str = "",
    agent_id: str = "",
    **request: Any,
) -> Any:
    """Call a provider and record successful usage without affecting reply delivery."""
    started_at = time.time()
    call_id = f"llm_{uuid.uuid4().hex}"
    try:
        response = client.chat.completions.create(**request)
    except Exception as exc:
        try:
            from server.agent_assets import recorder

            recorder.record_llm(
                assistant_id=agent_id, run_id=run_id, call_id=call_id, call_kind=call_kind,
                profile=profile, request=request, error=exc, started_at=started_at,
            )
        except Exception:
            pass
        raise
    try:
        normalized = _normalized_usage(response, request)
        completed_at = time.time()
        repos.add_llm_usage_event(
            {
                "event_id": f"usage_{uuid.uuid4().hex}",
                "call_id": call_id,
                "run_id": run_id,
                "session_id": session_id,
                "agent_id": agent_id,
                "call_kind": call_kind,
                "provider": str(profile.get("provider") or "custom"),
                "model": str(request.get("model") or profile.get("id") or ""),
                "status": "completed",
                "started_at": started_at,
                "completed_at": completed_at,
                "created_at": completed_at,
                **normalized,
            }
        )
        try:
            from server.agent_assets import recorder

            recorder.record_llm(
                assistant_id=agent_id, run_id=run_id, call_id=call_id, call_kind=call_kind,
                profile=profile, request=request, response=_response_output(response),
                usage=normalized, started_at=started_at,
            )
        except Exception:
            pass
    except Exception:
        # Metering must never discard a successful model response.
        pass
    return response
