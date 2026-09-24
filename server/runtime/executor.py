"""ToolExecutor — safe tools in-process; side-effect tools via worker pool."""

from __future__ import annotations

import contextvars
import json
import re
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Any, Callable

from server.runtime.mcp import MCPCallResult, MCPClient, normalize_mcp_name
from server.runtime.tools.registry import DANGEROUS_TOOLS, resolve_tool_kind

_MCP_ERROR_PREFIX = re.compile(r"^MCP error:", re.IGNORECASE)
_MCP_IS_ERROR_JSON = re.compile(r'"isError"\s*:\s*true', re.IGNORECASE)


@dataclass
class ToolResult:
    """Protocol result. ``content`` is never used to infer ``outcome``."""

    content: str
    outcome: str = "ok"  # ok | error | in_doubt | denied | timeout | cancelled | waiting
    metadata: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        return self.content

    @property
    def status(self) -> str:
        """Alias for forward-compatible {status,data,meta} contract."""
        return {
            "ok": "success",
            "error": "failed",
            "in_doubt": "pending",
            "waiting": "pending",
        }.get(self.outcome, self.outcome)

    @property
    def data(self) -> str:
        return self.content

    @property
    def meta(self) -> dict[str, Any]:
        return self.metadata


def _extract_acceptance_receipt(
    structured_content: dict | None,
    provider_metadata: dict | None = None,
) -> dict[str, Any] | None:
    """Best-effort adapter for DingTalk send receipts from MCP structured content."""
    candidates: list[dict[str, Any]] = []
    if isinstance(structured_content, dict):
        candidates.append(structured_content)
        for key in ("data", "result", "response"):
            nested = structured_content.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)
    if isinstance(provider_metadata, dict):
        candidates.append(provider_metadata)

    for data in candidates:
        receipt_id = (
            data.get("processQueryKey")
            or data.get("process_query_key")
            or data.get("messageId")
            or data.get("message_id")
        )
        target_id = (
            data.get("openConversationId")
            or data.get("open_conversation_id")
            or data.get("conversationId")
            or data.get("conversation_id")
        )
        if receipt_id and target_id:
            return {
                "provider": "dingtalk",
                "receipt_id": str(receipt_id),
                "target_id": str(target_id),
            }
    return None


class ToolExecutor:
    def __init__(
        self,
        *,
        cwd: Path,
        safe_handlers: dict[str, Callable[..., str]] | None = None,
        dangerous_handlers: dict[str, Callable[..., str]] | None = None,
        all_handlers: dict[str, Callable[..., str]] | None = None,
        mcp_clients: dict[str, MCPClient] | None = None,
        plugin_handlers: dict[str, Any] | None = None,
        timeout_s: float = 180.0,
        skill_roots: tuple[Path, ...] = (),
        trusted_roots: tuple[Path, ...] = (),
        connector_executables: tuple[str, ...] = (),
    ):
        self.cwd = Path(cwd).resolve()
        self.skill_roots = tuple(Path(root).resolve() for root in skill_roots)
        self.trusted_roots = tuple(Path(root).resolve() for root in trusted_roots)
        self.safe_handlers = dict(safe_handlers or {})
        self.dangerous_handlers = dict(dangerous_handlers or {})
        self.all_handlers = dict(all_handlers or {})
        # merge for lookup
        for k, v in self.safe_handlers.items():
            self.all_handlers.setdefault(k, v)
        for k, v in self.dangerous_handlers.items():
            self.all_handlers[k] = v
        self.mcp_clients = mcp_clients or {}
        self.plugin_handlers = plugin_handlers or {}
        self.timeout_s = timeout_s
        self._quarantined: set[str] = set()
        self.connector_executables = tuple(connector_executables)

    def execute(
        self,
        name: str,
        arguments: dict[str, Any] | str | None,
        *,
        tool_call_id: str = "",
        scope_key: str = "",
    ) -> ToolResult:
        args = arguments or {}
        base_meta = {
            "tool_call_id": tool_call_id,
            "tool_name": name,
            "scope_key": scope_key,
        }
        if isinstance(args, str):
            try:
                args = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                return ToolResult(
                    f"Invalid tool arguments JSON: {args[:200]}",
                    "error",
                    {**base_meta, "tool_kind": "mutation"},
                )
        if not isinstance(args, dict):
            args = {}
        base_meta["tool_kind"] = resolve_tool_kind(name, args)

        try:
            if name in self._quarantined:
                return ToolResult(
                    f"Tool call quarantined after timeout: {name}",
                    "denied",
                    {**base_meta, "quarantined": True},
                )
            if name.startswith("mcp__"):
                return self._run_mcp(name, args, base_meta)
            if name in self.plugin_handlers:
                return self._run_worker(
                    name,
                    lambda: self.plugin_handlers[name](**args),
                    base_meta,
                )
            handler = self.all_handlers.get(name)
            if not handler:
                return ToolResult(
                    f"Unknown or disabled tool: {name}",
                    "denied",
                    base_meta,
                )
            if name == "ask_user_question":
                ignored = sorted(
                    key
                    for key in ("minItems", "maxItems", "min_items", "max_items")
                    if key in args
                )
                if ignored:
                    base_meta["ignored_schema_arguments"] = ignored
                    args = {key: value for key, value in args.items() if key not in ignored}
                args = {**args, "_tool_call_id": tool_call_id}
            if name in DANGEROUS_TOOLS or name in self.dangerous_handlers:
                return self._run_worker(name, lambda: handler(**args), base_meta)
            return self._normalize(handler(**args), base_meta)
        except TypeError as exc:
            return ToolResult(f"Tool argument error ({name}): {exc}", "error", base_meta)
        except Exception as exc:
            return ToolResult(f"Tool error ({name}): {exc}", "error", base_meta)

    def _mcp_client_for_tool(self, name: str) -> tuple[MCPClient, str] | None:
        """Resolve an ``mcp__{server}__{tool}`` tool name to (client, tool).

        Server names are normalized to ``[a-zA-Z0-9_-]`` when the tool surface
        is built, so a non-ASCII server name (e.g. 机器人消息 -> ``____``) can
        produce consecutive underscores that a naive ``split("__", 2)`` would
        mis-parse.  Match against every connected server by prefix instead.
        """
        if not name.startswith("mcp__"):
            return None
        for server, client in self.mcp_clients.items():
            prefix = f"mcp__{normalize_mcp_name(server)}__"
            if name.startswith(prefix):
                return client, name[len(prefix):]
        return None

    def _run_mcp(self, name: str, args: dict, metadata: dict[str, Any]) -> ToolResult:
        resolved = self._mcp_client_for_tool(name)
        if resolved is None:
            return ToolResult(f"MCP server not connected for tool: {name}", "error", metadata)
        client, tool = resolved
        return self._run_worker(name, lambda: client.call_tool(tool, args), metadata)

    def _run_worker(
        self,
        name: str,
        fn: Callable[[], Any],
        metadata: dict[str, Any],
    ) -> ToolResult:
        # Per-call isolation prevents timed-out handlers from starving a shared pool.
        # Copy the caller context: permission flags (allow_external,
        # allow_dangerous_bash, monitor flags) are contextvars set in the loop
        # thread; without this the worker thread would see defaults and
        # permission-granted external-path edits would fail with PermissionError.
        ctx = contextvars.copy_context()
        worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tool-exec")
        fut = worker.submit(lambda: ctx.run(fn))
        try:
            return self._normalize(fut.result(timeout=self.timeout_s), metadata)
        except FuturesTimeout:
            fut.cancel()
            self._quarantined.add(name)
            if name.startswith("mcp__"):
                resolved = self._mcp_client_for_tool(name)
                if resolved is not None:
                    client, _tool = resolved
                    try:
                        client.close()
                    except Exception:
                        pass
            return ToolResult(
                f"Tool worker timeout after {self.timeout_s}s",
                "timeout",
                {**metadata, "quarantined": True},
            )
        except Exception as exc:
            return ToolResult(f"Tool worker error: {exc}", "error", metadata)
        finally:
            worker.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _normalize(value: Any, metadata: dict[str, Any]) -> ToolResult:
        """Map handler returns to ToolResult. Never infer success from error-shaped content."""
        if isinstance(value, ToolResult):
            value.metadata = {**metadata, **value.metadata}
            value.metadata.setdefault("tool_kind", metadata.get("tool_kind"))
            return value
        if isinstance(value, MCPCallResult):
            meta = {**metadata}
            if not value.is_error:
                receipt = _extract_acceptance_receipt(
                    value.structured_content,
                    value.provider_metadata,
                )
                if receipt:
                    meta["acceptance_receipt"] = receipt
            outcome = "error" if value.is_error else "ok"
            meta.setdefault("tool_kind", metadata.get("tool_kind"))
            return ToolResult(value.content, outcome, meta)
        if isinstance(value, dict):
            if {"content", "outcome"} <= set(value):
                meta = {**metadata, **dict(value.get("metadata") or {})}
                meta.setdefault("tool_kind", metadata.get("tool_kind"))
                return ToolResult(
                    str(value.get("content") or ""),
                    str(value.get("outcome") or "ok"),
                    meta,
                )
            if "is_error" in value:
                meta = {
                    **metadata,
                    **dict(value.get("metadata") or value.get("provider_metadata") or {}),
                }
                if not value.get("is_error"):
                    receipt = _extract_acceptance_receipt(
                        value.get("structured_content")
                        if isinstance(value.get("structured_content"), dict)
                        else None,
                        value.get("provider_metadata")
                        if isinstance(value.get("provider_metadata"), dict)
                        else None,
                    )
                    if receipt:
                        meta["acceptance_receipt"] = receipt
                outcome = "error" if value.get("is_error") else "ok"
                meta.setdefault("tool_kind", metadata.get("tool_kind"))
                return ToolResult(str(value.get("content") or ""), outcome, meta)
        text = str(value)
        if _MCP_ERROR_PREFIX.match(text) or _MCP_IS_ERROR_JSON.search(text):
            return ToolResult(text, "error", dict(metadata))
        return ToolResult(text, "ok", dict(metadata))
