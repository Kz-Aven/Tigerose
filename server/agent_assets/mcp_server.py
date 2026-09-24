"""Zero-dependency read-only MCP server for local Agent data assets."""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .store import assets_root, store

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "agent_assets"
_FORBIDDEN = re.compile(r";|--|/\*|\*/|\b(?:insert|update|delete|replace|drop|alter|create|pragma|attach|detach|vacuum|begin|commit|rollback)\b", re.I)
_SQLITE_ONLY = re.compile(r"\b(?:sqlite_|json_extract|group_concat|strftime|total_changes|last_insert_rowid)\b", re.I)
_SELECT = re.compile(r"^\s*(?:with\b[\s\S]+?\bselect\b|select\b)", re.I)
_TABLES = frozenset({"execution", "execution_span", "span_content", "config_snapshot", "execution_config_ref", "human_feedback", "eval_candidate"})


def _result(value: Any, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}], "isError": error}


def _registry() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{store.registry_path()}?mode=ro", uri=True)


def _assistant(assistant_id: str) -> sqlite3.Connection:
    with _registry() as registry:
        row = registry.execute("SELECT db_path FROM assistant_registry WHERE assistant_id=?", (assistant_id,)).fetchone()
    if not row: raise ValueError("unknown assistant_id")
    return sqlite3.connect(f"file:{assets_root() / row[0]}?mode=ro", uri=True)


def list_assistants(_: dict[str, Any]) -> list[dict[str, Any]]:
    from server.db import repos
    names = {str(item["template_id"]): str(item.get("name") or "") for item in repos.list_templates()}
    with _registry() as conn:
        return [{"assistant_id": row[0], "assistant_name": names.get(row[0], row[0]), "last_write_at": row[1]} for row in conn.execute("SELECT assistant_id,last_write_at FROM assistant_registry ORDER BY assistant_id")]


def _assistant_ids(args: dict[str, Any]) -> list[str]:
    ids = [str(item) for item in (args.get("assistant_ids") or [])]
    if args.get("assistant_id"):
        ids = [str(args["assistant_id"])]
    name = str(args.get("assistant_name") or "").strip()
    if name:
        matches = [item["assistant_id"] for item in list_assistants({}) if name == item["assistant_name"]]
        if not matches:
            matches = [item["assistant_id"] for item in list_assistants({}) if name in item["assistant_name"]]
        if len(matches) != 1: raise ValueError("assistant_name must match exactly one registered assistant")
        ids = matches
    return ids or [item["assistant_id"] for item in list_assistants({})]


def list_executions(args: dict[str, Any]) -> list[dict[str, Any]]:
    assistant_ids = _assistant_ids(args)
    limit = min(max(int(args.get("limit", 100)), 1), 500); rows: list[dict[str, Any]] = []
    for assistant_id in assistant_ids:
        with _assistant(str(assistant_id)) as conn:
            cursor = conn.execute("SELECT execution_id,assistant_id,status,termination,total_tokens,duration_ms,started_at,ended_at FROM execution ORDER BY started_at DESC LIMIT ?", (limit,))
            rows.extend(dict(zip(("execution_id", "assistant_id", "status", "termination", "total_tokens", "duration_ms", "started_at", "ended_at"), row)) for row in cursor)
    return sorted(rows, key=lambda item: item["started_at"], reverse=True)[:limit]


def latest_execution(args: dict[str, Any]) -> dict[str, Any]:
    rows = [item for item in list_executions({**args, "limit": 100}) if item["status"] == str(args.get("status") or "completed")]
    if not rows: raise ValueError("no matching execution")
    return get_execution({"execution_id": rows[0]["execution_id"]})


def latest_trace(args: dict[str, Any]) -> list[dict[str, Any]]:
    execution = latest_execution(args)["execution"]
    return get_trace({"execution_id": execution["execution_id"]})


def _locate_execution(execution_id: str) -> tuple[str, sqlite3.Connection]:
    with _registry() as conn:
        row = conn.execute("SELECT assistant_id FROM execution_locator WHERE execution_id=?", (execution_id,)).fetchone()
    if not row: raise ValueError("unknown execution_id")
    return row[0], _assistant(row[0])


def get_execution(args: dict[str, Any]) -> dict[str, Any]:
    assistant_id, conn = _locate_execution(str(args["execution_id"]))
    with conn:
        row = conn.execute("SELECT * FROM execution WHERE execution_id=?", (args["execution_id"],)).fetchone()
        fields = [item[0] for item in conn.execute("SELECT * FROM execution WHERE 1=0").description]
    return {"assistant_id": assistant_id, "execution": dict(zip(fields, row))}


def get_trace(args: dict[str, Any]) -> list[dict[str, Any]]:
    _, conn = _locate_execution(str(args["execution_id"]))
    with conn:
        cursor = conn.execute("SELECT * FROM execution_span WHERE execution_id=? ORDER BY started_at", (args["execution_id"],))
        fields = [item[0] for item in cursor.description]
        return [dict(zip(fields, row)) for row in cursor]


def read_content(args: dict[str, Any]) -> str:
    content_ref = str(args["content_ref"])
    with _registry() as conn:
        row = conn.execute("SELECT assistant_id FROM content_locator WHERE content_ref=?", (content_ref,)).fetchone()
    if not row: raise ValueError("unknown content_ref")
    with _assistant(row[0]) as conn:
        item = conn.execute("SELECT content_sha256 FROM span_content WHERE content_ref=?", (content_ref,)).fetchone()
    if not item: raise ValueError("content metadata missing")
    path = assets_root() / "assistants" / row[0] / "contents" / "sha256" / item[0][:2] / item[0]
    return path.read_text(encoding="utf-8")


def metrics(_: dict[str, Any]) -> dict[str, Any]:
    items = list_executions({"limit": 500}); count = len(items)
    return {"execution_count": count, "success_count": sum(item["status"] == "completed" for item in items), "total_tokens": sum(item["total_tokens"] for item in items), "total_duration_ms": sum(item["duration_ms"] for item in items)}


def readonly_sql(args: dict[str, Any]) -> list[dict[str, Any]]:
    sql = str(args["sql"]); assistant_id = str(args["assistant_id"])
    if not _SELECT.search(sql) or _FORBIDDEN.search(sql) or _SQLITE_ONLY.search(sql): raise ValueError("only one portable read-only SELECT is allowed")
    referenced = {name.lower() for name in re.findall(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)", sql, re.I)}
    if not referenced or not referenced <= _TABLES: raise ValueError("unknown table")
    with _assistant(assistant_id) as conn:
        cursor = conn.execute(sql, dict(args.get("parameters") or {})); fields = [item[0] for item in cursor.description]
        return [dict(zip(fields, row)) for row in cursor.fetchmany(500)]


_HANDLERS = {"list_assistants": list_assistants, "list_executions": list_executions, "get_latest_execution": latest_execution, "get_latest_execution_trace": latest_trace, "get_execution": get_execution, "get_execution_trace": get_trace, "summarize_execution_metrics": metrics, "read_content": read_content, "query_readonly_sql": readonly_sql}
TOOLS = [
    {"name": "list_assistants", "description": "List registered assistant asset databases.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_executions", "description": "List executions, optionally across assistants.", "inputSchema": {"type": "object", "properties": {"assistant_ids": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer"}}}},
    *[{"name": name, "description": "Get the latest completed execution or its trace by human-readable assistant_name.", "inputSchema": {"type": "object", "properties": {"assistant_name": {"type": "string"}, "assistant_id": {"type": "string"}, "status": {"type": "string"}}, "required": ["assistant_name"]}} for name in ("get_latest_execution", "get_latest_execution_trace")],
    *[{"name": name, "description": "Read-only Agent execution data assets.", "inputSchema": {"type": "object", "properties": {"execution_id": {"type": "string"}}}} for name in ("get_execution", "get_execution_trace")],
    {"name": "summarize_execution_metrics", "description": "Summarize local execution metrics.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "read_content", "description": "Read a registered content reference.", "inputSchema": {"type": "object", "properties": {"content_ref": {"type": "string"}}, "required": ["content_ref"]}},
    {"name": "query_readonly_sql", "description": "Run one parameterized portable SELECT against one assistant.", "inputSchema": {"type": "object", "properties": {"assistant_id": {"type": "string"}, "sql": {"type": "string"}, "parameters": {"type": "object"}}, "required": ["assistant_id", "sql"]}},
]


def handle(method: str, params: dict[str, Any], msg_id: Any) -> dict[str, Any] | None:
    if method == "initialize": return {"jsonrpc": "2.0", "id": msg_id, "result": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}, "resources": {}}, "serverInfo": {"name": SERVER_NAME, "version": "0.1.0"}}}
    if method == "notifications/initialized": return None
    if method == "ping": return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list": return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "resources/list": return {"jsonrpc": "2.0", "id": msg_id, "result": {"resources": [{"uri": "agent-assets://assistants", "name": "Assistants"}]}}
    if method == "resources/read":
        uri = str(params.get("uri") or "")
        value = list_assistants({}) if uri == "agent-assets://assistants" else {"error": "unknown resource"}
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"contents": [{"uri": uri, "mimeType": "application/json", "text": json.dumps(value, ensure_ascii=False)}]}}
    if method == "tools/call":
        try: result = _result(_HANDLERS[str(params.get("name"))](dict(params.get("arguments") or {})))
        except Exception as exc: result = _result({"error": str(exc)}, True)
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    return {"jsonrpc": "2.0", "id": msg_id, "result": {}}


def main() -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line); response = handle(message.get("method", ""), message.get("params") or {}, message.get("id"))
            if response: print(json.dumps(response, ensure_ascii=False), flush=True)
        except json.JSONDecodeError: continue


if __name__ == "__main__": main()
