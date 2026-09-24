"""Dependency-free, read-only stdio MCP server."""
from __future__ import annotations

import json
import re
import sys
from typing import Any

from .storage import AssetStore

PROTOCOL_VERSION = "2024-11-05"
_FORBIDDEN = re.compile(r";|--|/\*|\*/|\b(?:insert|update|delete|replace|drop|alter|create|pragma|attach|detach|vacuum|begin|commit|rollback)\b", re.I)
_SQLITE_ONLY = re.compile(r"\b(?:sqlite_|json_extract|group_concat|strftime|total_changes|last_insert_rowid)\b", re.I)
_SELECT = re.compile(r"^\s*(?:with\b[\s\S]+?\bselect\b|select\b)", re.I)
_TABLES = frozenset({"execution", "execution_span", "span_content", "config_snapshot", "execution_config_ref", "human_feedback", "eval_candidate"})


def _text(value: Any, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, default=str)}], "isError": error}


def _rows(cursor: Any) -> list[dict[str, Any]]:
    fields = [field[0] for field in cursor.description]
    return [dict(zip(fields, row)) for row in cursor.fetchall()]


class ReadonlyTools:
    def __init__(self, store: AssetStore | None = None) -> None:
        self.store = store or AssetStore()

    def assistants(self, _: dict[str, Any]) -> list[dict[str, Any]]:
        return self.store.list_assistants()

    def _keys(self, args: dict[str, Any]) -> list[str]:
        matches = self.store.list_assistants()
        name = str(args.get("assistant_name") or "").strip()
        platform = str(args.get("platform") or "").strip()
        if platform:
            matches = [item for item in matches if item["platform"] == platform]
        if name:
            exact = [item for item in matches if item["assistant_name"] == name]
            matches = exact or [item for item in matches if name.lower() in item["assistant_name"].lower()]
            if len(matches) != 1:
                raise ValueError("assistant_name must resolve to exactly one assistant")
        return [item["assistant_key"] for item in matches]

    def executions(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        limit = min(max(int(args.get("limit", 50)), 1), 500)
        status = str(args.get("status") or "")
        result: list[dict[str, Any]] = []
        for key in self._keys(args):
            with self.store.assistant_connection(key, readonly=True) as conn:
                sql = "SELECT execution_id,assistant_id,assistant_name,source_platform,status,termination,collection_mode,coverage,trace_integrity,total_tokens,duration_ms,started_at,ended_at FROM execution"
                parameters: tuple[Any, ...] = ()
                if status:
                    sql += " WHERE status=?"; parameters = (status,)
                sql += " ORDER BY started_at DESC LIMIT ?"
                result.extend(_rows(conn.execute(sql, parameters + (limit,))))
        return sorted(result, key=lambda item: item["started_at"], reverse=True)[:limit]

    def latest(self, args: dict[str, Any]) -> dict[str, Any]:
        items = self.executions({**args, "limit": 1})
        if not items:
            raise ValueError("no matching execution")
        return self.execution({"execution_id": items[0]["execution_id"]})

    def execution(self, args: dict[str, Any]) -> dict[str, Any]:
        key, conn = self.store.locate_execution(str(args["execution_id"]))
        with conn:
            cursor = conn.execute("SELECT * FROM execution WHERE execution_id=?", (args["execution_id"],))
            rows = _rows(cursor)
        if not rows:
            raise ValueError("execution metadata missing")
        return {"assistant_key": key, "execution": rows[0]}

    def trace(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        _, conn = self.store.locate_execution(str(args["execution_id"]))
        with conn:
            return _rows(conn.execute("SELECT * FROM execution_span WHERE execution_id=? ORDER BY started_at", (args["execution_id"],)))

    def content(self, args: dict[str, Any]) -> str:
        return self.store.read_content(str(args["content_ref"]))

    def metrics(self, args: dict[str, Any]) -> dict[str, Any]:
        items = self.executions({**args, "limit": 500})
        return {"execution_count": len(items), "completed_count": sum(item["status"] == "completed" for item in items), "strict_eval_eligible": sum(item["coverage"] == "full" and item["trace_integrity"] == "complete" for item in items), "total_tokens": sum(int(item["total_tokens"] or 0) for item in items), "total_duration_ms": sum(int(item["duration_ms"] or 0) for item in items)}

    def eval_candidates(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        return self.store.eval_candidates(bool(args.get("strict_only", True)), min(max(int(args.get("limit", 100)), 1), 500))

    def sql(self, args: dict[str, Any]) -> list[dict[str, Any]]:
        query = str(args["sql"])
        key = str(args["assistant_key"])
        if not _SELECT.search(query) or _FORBIDDEN.search(query) or _SQLITE_ONLY.search(query):
            raise ValueError("only one portable read-only SELECT is allowed")
        tables = {name.lower() for name in re.findall(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)", query, re.I)}
        if not tables or not tables <= _TABLES:
            raise ValueError("unknown table")
        with self.store.assistant_connection(key, readonly=True) as conn:
            return _rows(conn.execute(query, dict(args.get("parameters") or {})))[:500]


TOOLS = [
    {"name": "list_assistants", "description": "List local assistant data sources.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "list_executions", "description": "List executions by human-readable assistant name, platform, or status.", "inputSchema": {"type": "object", "properties": {"assistant_name": {"type": "string"}, "platform": {"type": "string"}, "status": {"type": "string"}, "limit": {"type": "integer"}}}},
    {"name": "get_latest_execution", "description": "Read the latest execution without requiring an ID.", "inputSchema": {"type": "object", "properties": {"assistant_name": {"type": "string"}, "platform": {"type": "string"}, "status": {"type": "string"}}}},
    {"name": "get_execution", "description": "Read one execution by ID.", "inputSchema": {"type": "object", "properties": {"execution_id": {"type": "string"}}, "required": ["execution_id"]}},
    {"name": "get_execution_trace", "description": "Read a normalized execution trace.", "inputSchema": {"type": "object", "properties": {"execution_id": {"type": "string"}}, "required": ["execution_id"]}},
    {"name": "read_content", "description": "Read a registered content reference.", "inputSchema": {"type": "object", "properties": {"content_ref": {"type": "string"}}, "required": ["content_ref"]}},
    {"name": "summarize_execution_metrics", "description": "Summarize local execution metrics and strict Eval eligibility.", "inputSchema": {"type": "object", "properties": {"assistant_name": {"type": "string"}, "platform": {"type": "string"}}}},
    {"name": "list_eval_candidates", "description": "Find failed or feedback-bearing executions for Eval review. Strict mode requires full coverage and complete storage.", "inputSchema": {"type": "object", "properties": {"strict_only": {"type": "boolean"}, "limit": {"type": "integer"}}}},
    {"name": "query_readonly_sql", "description": "Run one parameterized portable SELECT against a single assistant database.", "inputSchema": {"type": "object", "properties": {"assistant_key": {"type": "string"}, "sql": {"type": "string"}, "parameters": {"type": "object"}}, "required": ["assistant_key", "sql"]}},
]


def handle(message: dict[str, Any], tools: ReadonlyTools) -> dict[str, Any] | None:
    method, params, message_id = message.get("method", ""), message.get("params") or {}, message.get("id")
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": message_id, "result": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}, "resources": {}}, "serverInfo": {"name": "agent-asset-expert", "version": "0.1.0"}}}
    if method == "notifications/initialized": return None
    if method == "ping": return {"jsonrpc": "2.0", "id": message_id, "result": {}}
    if method == "tools/list": return {"jsonrpc": "2.0", "id": message_id, "result": {"tools": TOOLS}}
    if method == "resources/list": return {"jsonrpc": "2.0", "id": message_id, "result": {"resources": [{"uri": "agent-assets://assistants", "name": "Assistant data sources"}]}}
    if method == "resources/read":
        value = tools.assistants({}) if params.get("uri") == "agent-assets://assistants" else {"error": "unknown resource"}
        return {"jsonrpc": "2.0", "id": message_id, "result": {"contents": [{"uri": params.get("uri", ""), "mimeType": "application/json", "text": json.dumps(value, ensure_ascii=False)}]}}
    if method == "tools/call":
        handlers = {"list_assistants": tools.assistants, "list_executions": tools.executions, "get_latest_execution": tools.latest, "get_execution": tools.execution, "get_execution_trace": tools.trace, "read_content": tools.content, "summarize_execution_metrics": tools.metrics, "list_eval_candidates": tools.eval_candidates, "query_readonly_sql": tools.sql}
        try:
            result = _text(handlers[str(params["name"])](dict(params.get("arguments") or {})))
        except Exception as exc:
            result = _text({"error": str(exc)}, True)
        return {"jsonrpc": "2.0", "id": message_id, "result": result}
    return {"jsonrpc": "2.0", "id": message_id, "result": {}}


def main() -> None:
    tools = ReadonlyTools()
    for line in sys.stdin:
        try:
            response = handle(json.loads(line), tools)
            if response:
                print(json.dumps(response, ensure_ascii=False), flush=True)
        except json.JSONDecodeError:
            continue


if __name__ == "__main__":
    main()
