"""DuckDuckGo search via the optional ``ddgs`` package."""

from __future__ import annotations

import concurrent.futures as _cf
import logging
from typing import Any

from server.runtime.web.provider import WebSearchProvider

logger = logging.getLogger(__name__)
_SEARCH_TIMEOUT_SECS = 30


def _run_ddgs_search(query: str, safe_limit: int) -> list[dict[str, Any]]:
    from ddgs import DDGS  # type: ignore

    results: list[dict[str, Any]] = []
    with DDGS(timeout=10) as client:
        for i, hit in enumerate(client.text(query, max_results=safe_limit)):
            if i >= safe_limit:
                break
            url = str(hit.get("href") or hit.get("url") or "")
            results.append(
                {
                    "title": str(hit.get("title", "")),
                    "url": url,
                    "description": str(hit.get("body", "")),
                    "position": i + 1,
                }
            )
    return results


class DDGSWebSearchProvider(WebSearchProvider):
    @property
    def name(self) -> str:
        return "ddgs"

    def is_available(self) -> bool:
        try:
            import ddgs  # noqa: F401

            return True
        except ImportError:
            return False

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return False

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        if not self.is_available():
            return {
                "success": False,
                "error": "ddgs package is not installed — run `pip install ddgs`",
            }
        safe_limit = max(1, min(int(limit or 5), 20))
        pool = _cf.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(_run_ddgs_search, query, safe_limit)
            try:
                web_results = future.result(timeout=_SEARCH_TIMEOUT_SECS)
            except _cf.TimeoutError:
                return {
                    "success": False,
                    "error": (
                        f"DuckDuckGo search timed out after {_SEARCH_TIMEOUT_SECS}s"
                    ),
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("DDGS search error: %s", exc)
            return {"success": False, "error": f"DuckDuckGo search failed: {exc}"}
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return {"success": True, "data": {"web": web_results}}
