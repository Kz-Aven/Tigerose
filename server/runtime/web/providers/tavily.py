"""Tavily extract (and optional search) via HTTP API."""

from __future__ import annotations

import logging
import os
from typing import Any

from server.runtime.web.provider import WebSearchProvider

logger = logging.getLogger(__name__)


def _api_key() -> str:
    return (os.environ.get("TAVILY_API_KEY") or "").strip()


def _tavily_request(endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
    import httpx

    api_key = _api_key()
    if not api_key:
        raise ValueError(
            "TAVILY_API_KEY not set. Add it in Settings → 通用, or TIGEROSE_HOME/.env"
        )
    base_url = (os.environ.get("TAVILY_BASE_URL") or "https://api.tavily.com").rstrip("/")
    body = dict(payload)
    body["api_key"] = api_key
    url = f"{base_url}/{endpoint.lstrip('/')}"
    response = httpx.post(url, json=body, timeout=60)
    response.raise_for_status()
    return response.json()


def _normalize_documents(
    response: dict[str, Any], fallback_url: str = ""
) -> list[dict[str, Any]]:
    documents: list[dict[str, Any]] = []
    for result in response.get("results", []):
        url = result.get("url", fallback_url)
        raw = result.get("raw_content", "") or result.get("content", "")
        documents.append(
            {
                "url": url,
                "title": result.get("title", ""),
                "content": raw,
                "raw_content": raw,
                "metadata": {"sourceURL": url, "title": result.get("title", "")},
            }
        )
    for fail in response.get("failed_results", []):
        documents.append(
            {
                "url": fail.get("url", fallback_url),
                "title": "",
                "content": "",
                "raw_content": "",
                "error": fail.get("error", "extraction failed"),
                "metadata": {"sourceURL": fail.get("url", fallback_url)},
            }
        )
    for fail_url in response.get("failed_urls", []):
        url_str = fail_url if isinstance(fail_url, str) else str(fail_url)
        documents.append(
            {
                "url": url_str,
                "title": "",
                "content": "",
                "raw_content": "",
                "error": "extraction failed",
                "metadata": {"sourceURL": url_str},
            }
        )
    return documents


class TavilyWebSearchProvider(WebSearchProvider):
    @property
    def name(self) -> str:
        return "tavily"

    def is_available(self) -> bool:
        return bool(_api_key())

    def supports_search(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        try:
            raw = _tavily_request(
                "search",
                {
                    "query": query,
                    "max_results": min(max(1, int(limit or 5)), 20),
                    "include_raw_content": False,
                    "include_images": False,
                },
            )
            web = []
            for i, result in enumerate(raw.get("results", [])):
                web.append(
                    {
                        "title": result.get("title", ""),
                        "url": result.get("url", ""),
                        "description": result.get("content", ""),
                        "position": i + 1,
                    }
                )
            return {"success": True, "data": {"web": web}}
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Tavily search error: %s", exc)
            return {"success": False, "error": f"Tavily search failed: {exc}"}

    def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        _ = kwargs
        try:
            raw = _tavily_request("extract", {"urls": urls, "include_images": False})
            return _normalize_documents(raw, fallback_url=urls[0] if urls else "")
        except ValueError as exc:
            return [{"url": u, "title": "", "content": "", "error": str(exc)} for u in urls]
        except Exception as exc:  # noqa: BLE001
            logger.warning("Tavily extract error: %s", exc)
            return [
                {
                    "url": u,
                    "title": "",
                    "content": "",
                    "error": f"Tavily extract failed: {exc}",
                }
                for u in urls
            ]
