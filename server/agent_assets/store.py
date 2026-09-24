"""MySQL-first SQLite persistence for local Agent execution assets."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from avent_paths import data_root

_SECRET_KEY = re.compile(r"(api[_-]?key|secret|token|cookie|password|authorization|private[_-]?key)", re.I)
_SECRET_VALUE = re.compile(r"\b(?:Bearer|Basic|Token)\s+[A-Za-z0-9._~+/-]+=*", re.I)


def _utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def redact(value: Any) -> Any:
    """Remove only credential material before it reaches any persistence path."""
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if _SECRET_KEY.search(str(k)) else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_VALUE.sub("[REDACTED]", value)
    return value


def assets_root() -> Path:
    return data_root() / "data" / "agent-assets"


def _safe_assistant_id(assistant_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", assistant_id):
        raise ValueError("invalid assistant_id")
    return assistant_id


_ASSISTANT_DDL = """
CREATE TABLE IF NOT EXISTS schema_migration (version INT NOT NULL PRIMARY KEY, applied_at DATETIME(3) NOT NULL);
CREATE TABLE IF NOT EXISTS execution (
 execution_id CHAR(36) NOT NULL PRIMARY KEY, trace_id CHAR(36) NOT NULL, source_platform VARCHAR(64) NOT NULL,
 source_execution_id VARCHAR(128) NOT NULL, assistant_id VARCHAR(128) NOT NULL, session_id VARCHAR(128) NOT NULL,
 surface VARCHAR(64) NOT NULL, status VARCHAR(32) NOT NULL, termination VARCHAR(64) NOT NULL,
 termination_detail TEXT, input_content_ref VARCHAR(64), output_content_ref VARCHAR(64), input_tokens BIGINT NOT NULL DEFAULT 0,
 cached_input_tokens BIGINT NOT NULL DEFAULT 0, output_tokens BIGINT NOT NULL DEFAULT 0, total_tokens BIGINT NOT NULL DEFAULT 0,
 duration_ms BIGINT NOT NULL DEFAULT 0, retry_count INT NOT NULL DEFAULT 0, error_code VARCHAR(64), error_message TEXT,
 trace_completeness VARCHAR(16) NOT NULL DEFAULT 'complete', started_at DATETIME(3) NOT NULL, ended_at DATETIME(3),
 created_at DATETIME(3) NOT NULL, UNIQUE(source_platform, source_execution_id));
CREATE TABLE IF NOT EXISTS execution_span (
 span_id CHAR(36) NOT NULL PRIMARY KEY, execution_id CHAR(36) NOT NULL, parent_span_id CHAR(36), source_span_key VARCHAR(160) NOT NULL,
 span_type VARCHAR(32) NOT NULL, name VARCHAR(255) NOT NULL, status VARCHAR(32) NOT NULL, call_kind VARCHAR(64), tool_name VARCHAR(255),
 model_id VARCHAR(255), input_content_ref VARCHAR(64), output_content_ref VARCHAR(64), input_tokens BIGINT NOT NULL DEFAULT 0,
 cached_input_tokens BIGINT NOT NULL DEFAULT 0, output_tokens BIGINT NOT NULL DEFAULT 0, total_tokens BIGINT NOT NULL DEFAULT 0,
 duration_ms BIGINT NOT NULL DEFAULT 0, error_code VARCHAR(64), error_message TEXT, metadata_json TEXT,
 started_at DATETIME(3) NOT NULL, ended_at DATETIME(3), created_at DATETIME(3) NOT NULL,
 UNIQUE(execution_id, source_span_key), FOREIGN KEY(execution_id) REFERENCES execution(execution_id));
CREATE TABLE IF NOT EXISTS span_content (
 content_ref VARCHAR(64) NOT NULL PRIMARY KEY, execution_id CHAR(36) NOT NULL, span_id CHAR(36), role VARCHAR(32) NOT NULL,
 content_sha256 CHAR(64) NOT NULL, content_summary TEXT NOT NULL, byte_size BIGINT NOT NULL, truncated TINYINT(1) NOT NULL DEFAULT 0,
 created_at DATETIME(3) NOT NULL, FOREIGN KEY(execution_id) REFERENCES execution(execution_id));
CREATE TABLE IF NOT EXISTS config_snapshot (
 snapshot_id CHAR(36) NOT NULL PRIMARY KEY, asset_type VARCHAR(64) NOT NULL, asset_id VARCHAR(255) NOT NULL, version VARCHAR(128) NOT NULL,
 content_sha256 CHAR(64) NOT NULL, content_ref VARCHAR(64), captured_at DATETIME(3) NOT NULL,
 UNIQUE(asset_type, asset_id, version, content_sha256));
CREATE TABLE IF NOT EXISTS execution_config_ref (
 execution_id CHAR(36) NOT NULL, snapshot_id CHAR(36) NOT NULL, purpose VARCHAR(64) NOT NULL,
 PRIMARY KEY(execution_id, snapshot_id, purpose), FOREIGN KEY(execution_id) REFERENCES execution(execution_id), FOREIGN KEY(snapshot_id) REFERENCES config_snapshot(snapshot_id));
CREATE TABLE IF NOT EXISTS human_feedback (
 feedback_id CHAR(36) NOT NULL PRIMARY KEY, execution_id CHAR(36) NOT NULL, feedback_type VARCHAR(32) NOT NULL,
 reason TEXT, content_ref VARCHAR(64), created_at DATETIME(3) NOT NULL, FOREIGN KEY(execution_id) REFERENCES execution(execution_id));
CREATE INDEX IF NOT EXISTS idx_execution_started_status ON execution(started_at, status);
CREATE INDEX IF NOT EXISTS idx_execution_session_started ON execution(session_id, started_at);
CREATE INDEX IF NOT EXISTS idx_span_execution_started ON execution_span(execution_id, started_at);
CREATE INDEX IF NOT EXISTS idx_span_tool_status ON execution_span(tool_name, status);
"""

_REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS assistant_registry (assistant_id VARCHAR(128) NOT NULL PRIMARY KEY, db_path VARCHAR(512) NOT NULL,
 content_path VARCHAR(512) NOT NULL, schema_version INT NOT NULL, registered_at DATETIME(3) NOT NULL, last_write_at DATETIME(3));
CREATE TABLE IF NOT EXISTS execution_locator (execution_id CHAR(36) NOT NULL PRIMARY KEY, assistant_id VARCHAR(128) NOT NULL);
CREATE TABLE IF NOT EXISTS content_locator (content_ref VARCHAR(64) NOT NULL PRIMARY KEY, assistant_id VARCHAR(128) NOT NULL);
CREATE TABLE IF NOT EXISTS collector_health (assistant_id VARCHAR(128) NOT NULL PRIMARY KEY, failure_count BIGINT NOT NULL DEFAULT 0,
 last_error TEXT, last_success_at DATETIME(3));
"""


class AssetStore:
    def registry_path(self) -> Path:
        return assets_root() / "registry.db"

    def _registry(self) -> sqlite3.Connection:
        path = self.registry_path(); path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        conn.executescript(_REGISTRY_DDL)
        return conn

    def _assistant(self, assistant_id: str) -> sqlite3.Connection:
        assistant_id = _safe_assistant_id(assistant_id)
        root = assets_root() / "assistants"; root.mkdir(parents=True, exist_ok=True)
        path = root / f"{assistant_id}.db"
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(_ASSISTANT_DDL)
        relative_db = str(path.relative_to(assets_root()))
        content = root / assistant_id / "contents"; content.mkdir(parents=True, exist_ok=True)
        with self._registry() as registry:
            existing = registry.execute("SELECT assistant_id FROM assistant_registry WHERE assistant_id=?", (assistant_id,)).fetchone()
            values = (relative_db, str(content.relative_to(assets_root())), 1, _utc(), assistant_id)
            if existing:
                registry.execute("UPDATE assistant_registry SET db_path=?,content_path=?,schema_version=?,last_write_at=? WHERE assistant_id=?", values)
            else:
                registry.execute("INSERT INTO assistant_registry(assistant_id,db_path,content_path,schema_version,registered_at,last_write_at) VALUES(?,?,?,?,?,?)", (assistant_id, relative_db, str(content.relative_to(assets_root())), 1, _utc(), _utc()))
        return conn

    def _content(self, assistant_id: str, execution_id: str, role: str, value: Any, span_id: str | None = None) -> str:
        raw = json.dumps(redact(value), ensure_ascii=False, sort_keys=True, default=str) if not isinstance(value, str) else redact(value)
        encoded = raw.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest(); content_ref = f"cnt_{uuid.uuid4().hex}"
        directory = assets_root() / "assistants" / _safe_assistant_id(assistant_id) / "contents" / "sha256" / digest[:2]
        directory.mkdir(parents=True, exist_ok=True); target = directory / digest
        if not target.exists():
            fd, temp_name = tempfile.mkstemp(dir=directory); os.write(fd, encoded); os.close(fd); os.replace(temp_name, target)
        with self._assistant(assistant_id) as conn:
            conn.execute("INSERT INTO span_content(content_ref,execution_id,span_id,role,content_sha256,content_summary,byte_size,truncated,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (content_ref, execution_id, span_id, role, digest, raw[:2000], len(encoded), 0, _utc()))
        with self._registry() as registry:
            registry.execute("INSERT INTO content_locator(content_ref,assistant_id) VALUES(?,?)", (content_ref, assistant_id))
        return content_ref

    def start_execution(self, *, assistant_id: str, run_id: str, session_id: str, surface: str, user_input: Any) -> str:
        execution_id = str(uuid.uuid4()); trace_id = str(uuid.uuid4()); now = _utc()
        with self._assistant(assistant_id) as conn:
            row = conn.execute("SELECT execution_id FROM execution WHERE source_platform=? AND source_execution_id=?", ("tigerose", run_id)).fetchone()
            if row:
                execution_id = row[0]
            else:
                conn.execute("INSERT INTO execution(execution_id,trace_id,source_platform,source_execution_id,assistant_id,session_id,surface,status,termination,started_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (execution_id, trace_id, "tigerose", run_id, assistant_id, session_id, surface, "running", "", now, now))
        with self._registry() as registry:
            if not registry.execute("SELECT execution_id FROM execution_locator WHERE execution_id=?", (execution_id,)).fetchone():
                registry.execute("INSERT INTO execution_locator(execution_id,assistant_id) VALUES(?,?)", (execution_id, assistant_id))
        ref = self._content(assistant_id, execution_id, "user_input", user_input)
        with self._assistant(assistant_id) as conn:
            conn.execute("UPDATE execution SET input_content_ref=? WHERE execution_id=?", (ref, execution_id))
        return execution_id

    def finish_execution(self, *, assistant_id: str, run_id: str, status: str, termination: str, output: Any = "", error: Exception | None = None) -> None:
        with self._assistant(assistant_id) as conn:
            row = conn.execute("SELECT execution_id,started_at FROM execution WHERE source_platform=? AND source_execution_id=?", ("tigerose", run_id)).fetchone()
        if not row: return
        execution_id, started = row; output_ref = self._content(assistant_id, execution_id, "agent_output", output) if output else None
        ended = _utc(); start_dt = datetime.strptime(started, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)
        duration = max(0, int((datetime.now(timezone.utc) - start_dt).total_seconds() * 1000))
        with self._assistant(assistant_id) as conn:
            conn.execute("UPDATE execution SET status=?,termination=?,output_content_ref=?,error_code=?,error_message=?,duration_ms=?,ended_at=? WHERE execution_id=?", (status, termination, output_ref, "SYSTEM_ERROR" if error else None, str(redact(str(error))) if error else None, duration, ended, execution_id))

    def snapshot(self, *, assistant_id: str, run_id: str, asset_type: str, asset_id: str, value: Any, purpose: str) -> None:
        with self._assistant(assistant_id) as conn:
            row = conn.execute("SELECT execution_id FROM execution WHERE source_platform=? AND source_execution_id=?", ("tigerose", run_id)).fetchone()
        if not row: return
        execution_id = row[0]; safe = redact(value); raw = json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(raw.encode()).hexdigest(); version = digest[:16]
        with self._assistant(assistant_id) as conn:
            existing = conn.execute("SELECT snapshot_id FROM config_snapshot WHERE asset_type=? AND asset_id=? AND version=? AND content_sha256=?", (asset_type, asset_id, version, digest)).fetchone()
        snapshot_id = existing[0] if existing else str(uuid.uuid4())
        if not existing:
            ref = self._content(assistant_id, execution_id, "config_snapshot", safe)
            with self._assistant(assistant_id) as conn:
                conn.execute("INSERT INTO config_snapshot(snapshot_id,asset_type,asset_id,version,content_sha256,content_ref,captured_at) VALUES(?,?,?,?,?,?,?)", (snapshot_id, asset_type, asset_id, version, digest, ref, _utc()))
        with self._assistant(assistant_id) as conn:
            if not conn.execute("SELECT 1 FROM execution_config_ref WHERE execution_id=? AND snapshot_id=? AND purpose=?", (execution_id, snapshot_id, purpose)).fetchone():
                conn.execute("INSERT INTO execution_config_ref(execution_id,snapshot_id,purpose) VALUES(?,?,?)", (execution_id, snapshot_id, purpose))

    def feedback(self, *, assistant_id: str, execution_id: str, feedback_type: str, reason: str = "", content: Any = None) -> None:
        with self._assistant(assistant_id) as conn:
            if not conn.execute("SELECT 1 FROM execution WHERE execution_id=?", (execution_id,)).fetchone(): raise ValueError("unknown execution_id")
        ref = self._content(assistant_id, execution_id, "feedback", content) if content is not None else None
        with self._assistant(assistant_id) as conn:
            conn.execute("INSERT INTO human_feedback(feedback_id,execution_id,feedback_type,reason,content_ref,created_at) VALUES(?,?,?,?,?,?)", (str(uuid.uuid4()), execution_id, feedback_type, str(redact(reason)), ref, _utc()))

    def record_llm(self, *, assistant_id: str, run_id: str, call_id: str, call_kind: str, profile: dict[str, Any], request: dict[str, Any], response: Any = None, usage: dict[str, Any] | None = None, error: Exception | None = None, started_at: float | None = None) -> None:
        with self._assistant(assistant_id) as conn:
            row = conn.execute("SELECT execution_id FROM execution WHERE source_platform=? AND source_execution_id=?", ("tigerose", run_id)).fetchone()
        if not row: return
        execution_id = row[0]; span_id = str(uuid.uuid4()); now = _utc(); input_ref = self._content(assistant_id, execution_id, "llm_input", request, span_id)
        output_ref = self._content(assistant_id, execution_id, "llm_output", response, span_id) if response is not None else None
        usage = usage or {}; duration = max(0, int((__import__("time").time() - (started_at or __import__("time").time())) * 1000))
        with self._assistant(assistant_id) as conn:
            if not conn.execute("SELECT span_id FROM execution_span WHERE execution_id=? AND source_span_key=?", (execution_id, call_id)).fetchone():
                conn.execute("INSERT INTO execution_span(span_id,execution_id,source_span_key,span_type,name,status,call_kind,model_id,input_content_ref,output_content_ref,input_tokens,cached_input_tokens,output_tokens,total_tokens,duration_ms,error_code,error_message,started_at,ended_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (span_id, execution_id, call_id, "llm", call_kind, "error" if error else "success", call_kind, str(profile.get("id") or request.get("model") or ""), input_ref, output_ref, int(usage.get("input_tokens") or 0), int(usage.get("cached_input_tokens") or 0), int(usage.get("output_tokens") or 0), int(usage.get("total_tokens") or 0), duration, "LLM_ERROR" if error else None, str(redact(str(error))) if error else None, now, _utc(), now))
            conn.execute("UPDATE execution SET input_tokens=input_tokens+?,cached_input_tokens=cached_input_tokens+?,output_tokens=output_tokens+?,total_tokens=total_tokens+? WHERE execution_id=?", (int(usage.get("input_tokens") or 0), int(usage.get("cached_input_tokens") or 0), int(usage.get("output_tokens") or 0), int(usage.get("total_tokens") or 0), execution_id))

    def record_tool(self, *, assistant_id: str, run_id: str, call_id: str, name: str, args: Any, result: Any, outcome: str, metadata: Any = None) -> None:
        with self._assistant(assistant_id) as conn:
            row = conn.execute("SELECT execution_id FROM execution WHERE source_platform=? AND source_execution_id=?", ("tigerose", run_id)).fetchone()
        if not row: return
        execution_id = row[0]; span_id = str(uuid.uuid4()); now = _utc()
        input_ref = self._content(assistant_id, execution_id, "tool_input", args, span_id)
        output_ref = self._content(assistant_id, execution_id, "tool_output", result, span_id)
        status = "success" if outcome == "ok" else outcome
        with self._assistant(assistant_id) as conn:
            if conn.execute("SELECT span_id FROM execution_span WHERE execution_id=? AND source_span_key=?", (execution_id, call_id)).fetchone(): return
            conn.execute("INSERT INTO execution_span(span_id,execution_id,source_span_key,span_type,name,status,tool_name,input_content_ref,output_content_ref,error_code,error_message,metadata_json,started_at,ended_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (span_id, execution_id, call_id, "tool", name, status, name, input_ref, output_ref, None if outcome == "ok" else "TOOL_ERROR", None if outcome == "ok" else str(redact(str(result))), json.dumps(redact(metadata or {}), ensure_ascii=False), now, now, now))


store = AssetStore()
