"""Bounded scope-filtered recall, with a deterministic Chinese-aware fallback."""

import json
import queue
import threading
import time

from memory_core import estimate_tokens, relevance_score
from server.db import memory_store, repos

_SELECTOR_SLOTS = threading.BoundedSemaphore(2)


def recall(template_id, query, group_id=None, selector=None):
    started = time.monotonic()
    audit = {"selection_status": "no_match", "items": [], "elapsed_ms": 0}
    conn = repos.get_connection()
    try:
        candidates = memory_store.authorized(conn, template_id, group_id, index_only=True)
    except Exception as exc:
        conn.close()
        audit.update(selection_status="failed", error=type(exc).__name__)
        return {"items": [], "audit": audit}
    conn.close()
    ranked = sorted(candidates, key=lambda m: (-relevance_score(query, m["summary"]), m["memory_id"]))
    fallback = [m["memory_id"] for m in ranked if relevance_score(query, m["summary"]) > 0][:5]
    ids = fallback
    status = "text" if ids else "no_match"
    if selector and candidates:
        bounded, cost = [], 0
        for candidate in ranked:
            size = estimate_tokens(json.dumps(candidate, ensure_ascii=False))
            if cost + size > 6000:
                continue
            bounded.append(candidate)
            cost += size
        outcome = queue.Queue(maxsize=1)

        def select():
            try:
                outcome.put((True, selector(query, bounded)))
            except Exception as exc:
                outcome.put((False, type(exc).__name__))
            finally:
                _SELECTOR_SLOTS.release()

        # Daemon work cannot hold up shutdown or mutate the already chosen result.
        acquired = _SELECTOR_SLOTS.acquire(blocking=False)
        if acquired:
            threading.Thread(target=select, name="memory-selector", daemon=True).start()
        try:
            if not acquired:
                raise ValueError("selector capacity exhausted")
            ok, value = outcome.get(timeout=max(0, 1.5 - (time.monotonic() - started)))
            if isinstance(value, str) and ok:
                value = json.loads(value)
            if not ok or not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError("invalid selector result")
            allowed = {m["memory_id"] for m in bounded}
            ids = list(dict.fromkeys(v for v in value if v in allowed))[:5]
            status = "model" if ids else "no_match"
        except (queue.Empty, ValueError, TypeError):
            status = "degraded"
            ids = fallback
    items, used = [], 0
    conn = repos.get_connection()
    try:
        # Scope and status are checked again after selection, including membership.
        if group_id:
            memory_store.validate_scope(conn, template_id, "group", group_id)
        indexed = {m["memory_id"]: m for m in candidates}
        for mid in ids:
            row = memory_store.get(conn, template_id, mid)
            if not row or row["version"] != indexed[mid]["version"]:
                continue
            if not ((row["scope_kind"] == "assistant" and row["scope_id"] == template_id)
                    or (group_id and row["scope_kind"] == "group" and row["scope_id"] == group_id)):
                continue
            item = dict(row)
            size = estimate_tokens(json.dumps(item, ensure_ascii=False))
            if size > 2000 - used:
                item["body"] = item["summary"]
                item["summary_only"] = True
                item["detail_ref"] = f"/api/assistants/{template_id}/memories/{mid}"
                size = estimate_tokens(json.dumps(item, ensure_ascii=False))
            if size > 2000 - used:
                continue
            items.append(item)
            used += size
    except Exception as exc:
        items = []
        status = "failed"
        audit["error"] = type(exc).__name__
    finally:
        conn.close()
    audit.update(selection_status=status, elapsed_ms=round((time.monotonic() - started) * 1000),
                 items=[{k: m[k] for k in ("memory_id", "version", "scope_kind", "scope_id")} for m in items])
    return {"items": items, "audit": audit}
