"""Builtin web_search / web_extract tool handlers."""

from __future__ import annotations

import json
from typing import Any

from server.runtime.web.registry import get_extract_provider, get_search_provider


def web_search(query: str, limit: int = 5) -> str:
    q = (query or "").strip()
    if not q:
        return json.dumps({"success": False, "error": "query is required"}, ensure_ascii=False)
    result = get_search_provider().search(q, limit=int(limit or 5))
    return json.dumps(result, ensure_ascii=False)


def web_extract(urls: Any = None, url: str = "") -> str:
    """Accept ``urls`` list or a single ``url`` string from the model."""
    collected: list[str] = []
    if isinstance(urls, str) and urls.strip():
        collected.append(urls.strip())
    elif isinstance(urls, list):
        collected.extend(str(u).strip() for u in urls if str(u).strip())
    if url and str(url).strip():
        collected.append(str(url).strip())
    # de-dupe preserve order
    seen: set[str] = set()
    ordered: list[str] = []
    for u in collected:
        if u not in seen:
            seen.add(u)
            ordered.append(u)
    if not ordered:
        return json.dumps(
            [{"url": "", "error": "at least one url is required", "title": "", "content": ""}],
            ensure_ascii=False,
        )
    docs = get_extract_provider().extract(ordered)
    return json.dumps(docs, ensure_ascii=False)
