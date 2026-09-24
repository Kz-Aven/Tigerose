"""MCP clients: stdio JSON-RPC + optional in-process mocks."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from server.capabilities.mcp_config import resolve_mcp_launch

_DISALLOWED = re.compile(r"[^a-zA-Z0-9_-]")


@dataclass
class MCPCallResult:
    is_error: bool
    content: str
    content_blocks: list[dict] = field(default_factory=list)
    structured_content: dict | None = None
    provider_metadata: dict = field(default_factory=dict)


def _mcp_result_from_protocol(result: Any) -> MCPCallResult:
    """Parse MCP tools/call result payload into MCPCallResult."""
    if not isinstance(result, dict):
        return MCPCallResult(is_error=False, content=str(result))

    content_blocks = [
        block for block in (result.get("content") or []) if isinstance(block, dict)
    ]
    structured = result.get("structuredContent")
    structured_content = structured if isinstance(structured, dict) else None
    provider_metadata = dict(result.get("_meta") or result.get("meta") or {})

    parts: list[str] = []
    for block in content_blocks:
        if block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        else:
            parts.append(json.dumps(block, ensure_ascii=False))
    text = "\n".join(parts) if parts else json.dumps(result, ensure_ascii=False)

    if result.get("isError"):
        return MCPCallResult(
            is_error=True,
            content=f"MCP error: {result}",
            content_blocks=content_blocks,
            structured_content=structured_content,
            provider_metadata=provider_metadata,
        )
    return MCPCallResult(
        is_error=False,
        content=text,
        content_blocks=content_blocks,
        structured_content=structured_content,
        provider_metadata=provider_metadata,
    )


def normalize_mcp_name(name: str) -> str:
    return _DISALLOWED.sub("_", name)


class MCPClient:
    def __init__(self, name: str):
        self.name = name
        self.tools: list[dict] = []
        self._handlers: dict[str, Callable[..., str]] = {}

    def register(self, tool_defs: list[dict], handlers: dict[str, Callable[..., str]]) -> None:
        self.tools = tool_defs
        self._handlers = handlers

    def call_tool(self, tool_name: str, args: dict) -> MCPCallResult:
        handler = self._handlers.get(tool_name)
        if not handler:
            return MCPCallResult(
                is_error=True,
                content=f"MCP error: unknown tool '{tool_name}'",
            )
        try:
            return MCPCallResult(is_error=False, content=str(handler(**(args or {}))))
        except TypeError:
            # Some handlers expect a single dict
            try:
                return MCPCallResult(is_error=False, content=str(handler(args or {})))
            except Exception as exc:
                return MCPCallResult(is_error=True, content=f"MCP error: {exc}")
        except Exception as exc:
            return MCPCallResult(is_error=True, content=f"MCP error: {exc}")

    def close(self) -> None:
        pass


class StdioMCPClient(MCPClient):
    """Minimal MCP stdio client.

    Supports both newline-delimited JSON (NDJSON, used by codegraph) and
    Content-Length framed JSON-RPC.
    """

    def __init__(
        self,
        name: str,
        *,
        command: str,
        args: list[str],
        env: dict[str, str] | None = None,
        timeout: float = 180.0,
    ):
        super().__init__(name)
        self._timeout = timeout
        self._id = 0
        self._lock = threading.Lock()
        self._buf = b""
        self._framing: str | None = None  # "ndjson" | "content-length"
        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)
        self._proc = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=proc_env,
            bufsize=0,
        )
        self._stderr_thread = threading.Thread(target=self._drain_stderr, daemon=True)
        self._stderr_thread.start()
        self._initialize()
        self._list_tools()

    def _drain_stderr(self) -> None:
        try:
            assert self._proc.stderr is not None
            while True:
                chunk = self._proc.stderr.read(1024)
                if not chunk:
                    break
        except Exception:
            pass

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _write(self, message: dict[str, Any]) -> None:
        assert self._proc.stdin is not None
        data = json.dumps(message, ensure_ascii=False).encode("utf-8")
        # Prefer NDJSON (codegraph / many Node MCP servers); Content-Length also OK for others.
        if self._framing == "content-length":
            header = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
            self._proc.stdin.write(header + data)
        else:
            self._proc.stdin.write(data + b"\n")
        self._proc.stdin.flush()

    def _read_message(self) -> dict[str, Any]:
        assert self._proc.stdout is not None
        deadline = time.time() + self._timeout
        while time.time() < deadline:
            if b"\n" in self._buf or b"\r\n\r\n" in self._buf:
                break
            chunk = self._proc.stdout.read(1)
            if not chunk:
                if self._proc.poll() is not None:
                    raise RuntimeError(f"MCP '{self.name}' exited ({self._proc.returncode})")
                time.sleep(0.01)
                continue
            self._buf += chunk
        else:
            raise RuntimeError(f"MCP '{self.name}' read timeout")

        # Detect framing from first response
        if self._framing is None:
            if self._buf.lower().startswith(b"content-length:"):
                self._framing = "content-length"
            else:
                self._framing = "ndjson"

        if self._framing == "content-length":
            while b"\r\n\r\n" not in self._buf and time.time() < deadline:
                chunk = self._proc.stdout.read(1)
                if not chunk:
                    break
                self._buf += chunk
            header, _, rest = self._buf.partition(b"\r\n\r\n")
            length = 0
            for line in header.decode("utf-8", errors="replace").split("\r\n"):
                if line.lower().startswith("content-length:"):
                    length = int(line.split(":", 1)[1].strip())
            body = rest
            while len(body) < length and time.time() < deadline:
                chunk = self._proc.stdout.read(length - len(body))
                if not chunk:
                    break
                body += chunk
            self._buf = body[length:]
            return json.loads(body[:length].decode("utf-8"))

        # NDJSON
        while b"\n" not in self._buf and time.time() < deadline:
            chunk = self._proc.stdout.read(1)
            if not chunk:
                break
            self._buf += chunk
        line, _, rest = self._buf.partition(b"\n")
        self._buf = rest
        line = line.strip()
        if not line:
            return self._read_message()
        return json.loads(line.decode("utf-8"))

    def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        with self._lock:
            req_id = self._next_id()
            msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params is not None:
                msg["params"] = params
            self._write(msg)
            while True:
                resp = self._read_message()
                if resp.get("id") == req_id:
                    if "error" in resp:
                        err = resp["error"]
                        raise RuntimeError(f"MCP error: {err}")
                    return resp.get("result")

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        with self._lock:
            msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                msg["params"] = params
            self._write(msg)

    def _initialize(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "avent-agent", "version": "0.1.2"},
            },
        )
        self._notify("notifications/initialized")

    def _list_tools(self) -> None:
        result = self._request("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        self.tools = list(tools or [])

    def call_tool(self, tool_name: str, args: dict) -> MCPCallResult:
        try:
            result = self._request(
                "tools/call",
                {"name": tool_name, "arguments": args or {}},
            )
        except Exception as exc:
            return MCPCallResult(is_error=True, content=f"MCP error: {exc}")
        return _mcp_result_from_protocol(result)

    def close(self) -> None:
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
        except Exception:
            pass


class HttpMCPClient(MCPClient):
    """Minimal MCP Streamable HTTP client (JSON-RPC over POST).

    Supports both plain `application/json` responses (e.g. DingTalk gateway)
    and `text/event-stream` (SSE) responses per the MCP streamable-http spec.
    No OAuth flow: auth is expected via URL query params or pre-set headers.
    """

    def __init__(
        self,
        name: str,
        *,
        url: str,
        headers: dict[str, str] | None = None,
        timeout: float = 180.0,
    ):
        super().__init__(name)
        self._url = url
        self._headers = dict(headers or {})
        self._timeout = timeout
        self._id = 0
        self._lock = threading.Lock()
        self._session_id: str | None = None
        self._initialize()
        self._list_tools()

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _post(self, message: dict[str, Any]) -> dict[str, Any] | None:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        req = urllib.request.Request(
            self._url,
            data=json.dumps(message, ensure_ascii=False).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                ctype = resp.headers.get("Content-Type", "") or ""
                sid = resp.headers.get("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raise RuntimeError(
                f"MCP '{self.name}' HTTP {exc.code}: {exc.reason} {detail}".strip()
            ) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"MCP '{self.name}' HTTP error: {exc.reason}") from exc
        return self._parse_response(body, ctype)

    @staticmethod
    def _parse_response(body: str, ctype: str) -> dict[str, Any] | None:
        body = (body or "").strip()
        if not body:
            return None
        if "text/event-stream" in ctype:
            # SSE: collect `data:` JSON payloads, return the first non-empty one.
            for line in body.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    payload = line[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        return json.loads(payload)
                    except json.JSONDecodeError:
                        continue
            return None
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"MCP response is not JSON: {body[:200]}") from exc

    def _request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        with self._lock:
            req_id = self._next_id()
            msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params is not None:
                msg["params"] = params
            resp = self._post(msg)
            if resp is None:
                raise RuntimeError(f"MCP '{self.name}' empty response for {method}")
            if resp.get("id") != req_id:
                raise RuntimeError(
                    f"MCP '{self.name}' response id mismatch for {method}: {resp}"
                )
            if "error" in resp:
                err = resp["error"]
                raise RuntimeError(f"MCP error: {err}")
            return resp.get("result")

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        with self._lock:
            msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                msg["params"] = params
            try:
                self._post(msg)
            except Exception:
                # Notifications are fire-and-forget; a failing ACK is not fatal.
                pass

    def _initialize(self) -> None:
        self._request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "avent-agent", "version": "0.1.2"},
            },
        )
        self._notify("notifications/initialized")

    def _list_tools(self) -> None:
        result = self._request("tools/list", {})
        tools = result.get("tools") if isinstance(result, dict) else None
        self.tools = list(tools or [])

    def call_tool(self, tool_name: str, args: dict) -> MCPCallResult:
        try:
            result = self._request(
                "tools/call",
                {"name": tool_name, "arguments": args or {}},
            )
        except Exception as exc:
            return MCPCallResult(is_error=True, content=f"MCP error: {exc}")
        return _mcp_result_from_protocol(result)

    def close(self) -> None:
        pass


def _mock_docs() -> MCPClient:
    c = MCPClient("docs")
    c.register(
        [
            {
                "name": "search",
                "description": "Search documentation. (readOnly)",
                "inputSchema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
            {
                "name": "get_version",
                "description": "Get API version. (readOnly)",
                "inputSchema": {"type": "object", "properties": {}, "required": []},
            },
        ],
        {
            "search": lambda query: f"[docs] Found 3 results for '{query}'",
            "get_version": lambda: "[docs] API v2.1.0",
        },
    )
    return c


def _mock_deploy() -> MCPClient:
    c = MCPClient("deploy")
    c.register(
        [
            {
                "name": "trigger",
                "description": "Trigger deployment. (destructive)",
                "inputSchema": {
                    "type": "object",
                    "properties": {"service": {"type": "string"}},
                    "required": ["service"],
                },
            },
            {
                "name": "status",
                "description": "Check deployment status. (readOnly)",
                "inputSchema": {
                    "type": "object",
                    "properties": {"service": {"type": "string"}},
                    "required": ["service"],
                },
            },
        ],
        {
            "trigger": lambda service: f"[deploy] Triggered: {service}",
            "status": lambda service: f"[deploy] {service}: running (v1.4.2)",
        },
    )
    return c


MOCK_MCP_SERVERS: dict[str, Callable[[], MCPClient]] = {
    "docs": _mock_docs,
    "deploy": _mock_deploy,
}


def mcp_tool_openai(server_name: str, tool_def: dict) -> dict[str, Any]:
    schema = tool_def.get("inputSchema") or tool_def.get("input_schema") or {
        "type": "object",
        "properties": {},
    }
    # Prefer stable server_id for wire names; display name only in description.
    try:
        from server.capabilities.mcp_config import (
            KNOWN_SERVER_ID_MIGRATIONS,
            is_valid_server_id,
        )
        from server.runtime.feature_flags import flag_enabled

        if flag_enabled("server_id_v2"):
            sid = KNOWN_SERVER_ID_MIGRATIONS.get(server_name, server_name)
            if not is_valid_server_id(sid):
                sid = normalize_mcp_name(server_name)
            else:
                # Still normalize if somehow invalid chars slipped through
                pass
            wire_server = sid if is_valid_server_id(sid) else normalize_mcp_name(sid)
            return {
                "type": "function",
                "function": {
                    "name": f"mcp__{wire_server}__{tool_def['name']}",
                    "description": (
                        f"[MCP:{server_name}] {tool_def.get('description') or tool_def['name']}"
                    ),
                    "parameters": schema,
                },
            }
    except Exception:
        pass
    safe = normalize_mcp_name(server_name)
    return {
        "type": "function",
        "function": {
            "name": f"mcp__{safe}__{tool_def['name']}",
            "description": f"[MCP:{server_name}] {tool_def.get('description') or tool_def['name']}",
            "parameters": schema,
        },
    }


def connect_mcp_servers(
    server_ids: list[str],
    *,
    workspace: Path | str | None = None,
    timeout: float = 180.0,
) -> dict[str, Any]:
    """Connect enabled MCP servers (stdio config first, then in-process mocks).

    Returns:
      clients: canonical server_id → MCPClient (unique; use for OpenAI tool schemas)
      lookup: alias/display/request name → same MCPClient (executor resolution only)
      warnings: connection warnings
    """
    clients: dict[str, MCPClient] = {}
    lookup: dict[str, MCPClient] = {}
    warnings: list[str] = []

    def _index_alias(alias: str, canonical: str, client: MCPClient) -> None:
        alias = (alias or "").strip()
        if not alias or alias == canonical:
            return
        lookup[alias] = client

    for raw in server_ids:
        name = raw.removeprefix("mcp:").strip()
        if not name:
            continue
        if name in clients or name in lookup:
            continue
        launch = resolve_mcp_launch(name, workspace=workspace)
        key = str((launch or {}).get("server_id") or name)
        if key in clients:
            existing = clients[key]
            _index_alias(name, key, existing)
            if launch:
                _index_alias(str(launch.get("display_name") or ""), key, existing)
                for extra in launch.get("aliases") or []:
                    _index_alias(str(extra), key, existing)
            continue
        if launch:
            try:
                if launch.get("url"):
                    client = HttpMCPClient(
                        key,
                        url=launch["url"],
                        timeout=timeout,
                    )
                else:
                    client = StdioMCPClient(
                        key,
                        command=launch["command"],
                        args=list(launch.get("args") or []),
                        env=dict(launch.get("env") or {}),
                        timeout=timeout,
                    )
                clients[key] = client
                lookup[key] = client
                _index_alias(name, key, client)
                _index_alias(str(launch.get("display_name") or ""), key, client)
                for extra in launch.get("aliases") or []:
                    _index_alias(str(extra), key, client)
                continue
            except Exception as exc:
                warnings.append(f"MCP '{name}' connect failed: {exc}")
        factory = MOCK_MCP_SERVERS.get(name) or MOCK_MCP_SERVERS.get(key)
        if factory:
            try:
                client = factory()
                clients[key] = client
                lookup[key] = client
                _index_alias(name, key, client)
            except Exception as exc:
                warnings.append(f"MCP '{name}' mock connect failed: {exc}")
        else:
            if not any(name in w for w in warnings):
                warnings.append(f"MCP server '{name}' not found")
    return {"clients": clients, "lookup": lookup, "warnings": warnings}


def test_mcp_server(
    name: str,
    *,
    workspace: Path | str | None = None,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Handshake + tools/list for Settings test button."""
    if cfg and cfg.get("command"):
        from server.capabilities.mcp_config import substitute_workspace

        command = substitute_workspace(str(cfg["command"]), workspace)
        import shutil

        which = shutil.which(command) if "/" not in command else command
        args = [substitute_workspace(str(a), workspace) for a in (cfg.get("args") or [])]
        env = {
            k: substitute_workspace(str(v), workspace)
            for k, v in (cfg.get("env") or {}).items()
        }
        client = StdioMCPClient(
            name,
            command=which or command,
            args=args,
            env=env,
            timeout=20.0,
        )
    elif cfg and cfg.get("url"):
        from server.capabilities.mcp_config import substitute_workspace

        client = HttpMCPClient(
            name,
            url=substitute_workspace(str(cfg["url"]), workspace),
            timeout=20.0,
        )
    else:
        connected = connect_mcp_servers([name], workspace=workspace)
        if name not in connected["clients"]:
            return {
                "ok": False,
                "error": "; ".join(connected["warnings"]) or "connect failed",
                "tools": [],
            }
        client = connected["clients"][name]
    try:
        tools = [
            {"name": t.get("name"), "description": t.get("description") or ""}
            for t in client.tools
        ]
        return {"ok": True, "tools": tools, "tool_count": len(tools)}
    finally:
        client.close()
