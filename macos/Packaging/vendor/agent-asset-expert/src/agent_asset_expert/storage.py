"""MySQL-first SQLite persistence. SQLite is an engine, not the data contract."""
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
from typing import Any, Iterator

from .redaction import redact

_SAFE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")

ASSISTANT_DDL = """
CREATE TABLE IF NOT EXISTS schema_migration (version INT NOT NULL PRIMARY KEY, applied_at DATETIME(3) NOT NULL);
CREATE TABLE IF NOT EXISTS execution (
 execution_id CHAR(36) NOT NULL PRIMARY KEY, source_platform VARCHAR(64) NOT NULL, source_execution_id VARCHAR(255) NOT NULL,
 assistant_id VARCHAR(128) NOT NULL, assistant_name VARCHAR(255) NOT NULL, session_id VARCHAR(255) NOT NULL,
 status VARCHAR(32) NOT NULL, termination VARCHAR(64) NOT NULL, termination_detail TEXT,
 collection_mode VARCHAR(32) NOT NULL, coverage VARCHAR(16) NOT NULL, coverage_reason TEXT,
 trace_integrity VARCHAR(16) NOT NULL, integrity_reason TEXT,
 input_content_ref VARCHAR(64), output_content_ref VARCHAR(64), input_tokens BIGINT NOT NULL DEFAULT 0,
 cached_input_tokens BIGINT NOT NULL DEFAULT 0, output_tokens BIGINT NOT NULL DEFAULT 0, total_tokens BIGINT NOT NULL DEFAULT 0,
 duration_ms BIGINT NOT NULL DEFAULT 0, retry_count INT NOT NULL DEFAULT 0, error_code VARCHAR(64), error_message TEXT,
 started_at DATETIME(3) NOT NULL, ended_at DATETIME(3), created_at DATETIME(3) NOT NULL,
 UNIQUE(source_platform, source_execution_id));
CREATE TABLE IF NOT EXISTS execution_span (
 span_id CHAR(36) NOT NULL PRIMARY KEY, execution_id CHAR(36) NOT NULL, parent_span_id CHAR(36), source_span_key VARCHAR(255) NOT NULL,
 span_type VARCHAR(32) NOT NULL, name VARCHAR(255) NOT NULL, status VARCHAR(32) NOT NULL, tool_name VARCHAR(255), model_id VARCHAR(255),
 input_content_ref VARCHAR(64), output_content_ref VARCHAR(64), input_tokens BIGINT NOT NULL DEFAULT 0,
 cached_input_tokens BIGINT NOT NULL DEFAULT 0, output_tokens BIGINT NOT NULL DEFAULT 0, total_tokens BIGINT NOT NULL DEFAULT 0,
 duration_ms BIGINT NOT NULL DEFAULT 0, error_code VARCHAR(64), error_message TEXT, metadata_json TEXT,
 started_at DATETIME(3) NOT NULL, ended_at DATETIME(3), created_at DATETIME(3) NOT NULL,
 UNIQUE(execution_id, source_span_key), FOREIGN KEY(execution_id) REFERENCES execution(execution_id));
CREATE TABLE IF NOT EXISTS span_content (
 content_ref VARCHAR(64) NOT NULL PRIMARY KEY, execution_id CHAR(36) NOT NULL, span_id CHAR(36), role VARCHAR(32) NOT NULL,
 content_sha256 CHAR(64) NOT NULL, content_summary TEXT NOT NULL, byte_size BIGINT NOT NULL, truncated TINYINT(1) NOT NULL DEFAULT 0, created_at DATETIME(3) NOT NULL, FOREIGN KEY(execution_id) REFERENCES execution(execution_id));
CREATE TABLE IF NOT EXISTS config_snapshot (
 snapshot_id CHAR(36) NOT NULL PRIMARY KEY, asset_type VARCHAR(64) NOT NULL, asset_id VARCHAR(255) NOT NULL, version VARCHAR(128) NOT NULL,
 content_sha256 CHAR(64) NOT NULL, content_ref VARCHAR(64), captured_at DATETIME(3) NOT NULL, UNIQUE(asset_type, asset_id, version, content_sha256));
CREATE TABLE IF NOT EXISTS execution_config_ref (execution_id CHAR(36) NOT NULL, snapshot_id CHAR(36) NOT NULL, purpose VARCHAR(64) NOT NULL, PRIMARY KEY(execution_id, snapshot_id, purpose), FOREIGN KEY(execution_id) REFERENCES execution(execution_id), FOREIGN KEY(snapshot_id) REFERENCES config_snapshot(snapshot_id));
CREATE TABLE IF NOT EXISTS human_feedback (feedback_id CHAR(36) NOT NULL PRIMARY KEY, execution_id CHAR(36) NOT NULL, feedback_type VARCHAR(32) NOT NULL, reason TEXT, content_ref VARCHAR(64), created_at DATETIME(3) NOT NULL, FOREIGN KEY(execution_id) REFERENCES execution(execution_id));
CREATE TABLE IF NOT EXISTS eval_candidate (candidate_id CHAR(36) NOT NULL PRIMARY KEY, execution_id CHAR(36) NOT NULL, reason_code VARCHAR(64) NOT NULL, strict_eligible TINYINT(1) NOT NULL, created_at DATETIME(3) NOT NULL, FOREIGN KEY(execution_id) REFERENCES execution(execution_id));
CREATE INDEX IF NOT EXISTS idx_execution_started_status ON execution(started_at, status);
CREATE INDEX IF NOT EXISTS idx_execution_session_started ON execution(session_id, started_at);
CREATE INDEX IF NOT EXISTS idx_span_execution_started ON execution_span(execution_id, started_at);
CREATE INDEX IF NOT EXISTS idx_span_tool_status ON execution_span(tool_name, status);
"""
REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS assistant_registry (assistant_key VARCHAR(255) NOT NULL PRIMARY KEY, platform VARCHAR(64) NOT NULL, assistant_id VARCHAR(128) NOT NULL, assistant_name VARCHAR(255) NOT NULL, db_path VARCHAR(512) NOT NULL, content_path VARCHAR(512) NOT NULL, schema_version INT NOT NULL, registered_at DATETIME(3) NOT NULL, last_write_at DATETIME(3));
CREATE TABLE IF NOT EXISTS execution_locator (execution_id CHAR(36) NOT NULL PRIMARY KEY, assistant_key VARCHAR(255) NOT NULL);
CREATE TABLE IF NOT EXISTS content_locator (content_ref VARCHAR(64) NOT NULL PRIMARY KEY, assistant_key VARCHAR(255) NOT NULL);
CREATE TABLE IF NOT EXISTS collector_health (assistant_key VARCHAR(255) NOT NULL PRIMARY KEY, failure_count BIGINT NOT NULL DEFAULT 0, last_error TEXT, last_success_at DATETIME(3));
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _dict_rows(cursor: Any) -> list[dict[str, Any]]:
    fields = [field[0] for field in cursor.description]
    return [dict(zip(fields, row)) for row in cursor.fetchall()]


def data_root() -> Path:
    override = os.environ.get("AGENT_ASSET_EXPERT_HOME")
    return Path(override).expanduser() if override else Path.home() / "Library" / "Application Support" / "AgentAssetExpert"


class AssetStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or data_root()

    @staticmethod
    def assistant_key(platform: str, assistant_id: str) -> str:
        if not _SAFE.fullmatch(platform) or not _SAFE.fullmatch(assistant_id):
            raise ValueError("platform and assistant_id must be alphanumeric, underscore, or hyphen")
        return f"{platform}--{assistant_id}"

    def registry_path(self) -> Path:
        return self.root / "data" / "registry.db"

    def _connect(self, path: Path, readonly: bool = False) -> sqlite3.Connection:
        if readonly:
            return sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(path)

    def _registry(self) -> sqlite3.Connection:
        conn = self._connect(self.registry_path())
        conn.executescript(REGISTRY_DDL)
        return conn

    def register(self, platform: str, assistant_id: str, assistant_name: str) -> str:
        key = self.assistant_key(platform, assistant_id)
        db_path = self.root / "data" / "assistants" / f"{key}.db"
        content_path = self.root / "data" / "assistants" / key / "contents"
        content_path.mkdir(parents=True, exist_ok=True)
        with self._connect(db_path) as conn:
            conn.executescript(ASSISTANT_DDL)
            migrated = conn.execute("SELECT version FROM schema_migration WHERE version=?", (1,)).fetchone()
            if not migrated:
                conn.execute("INSERT INTO schema_migration(version,applied_at) VALUES(?,?)", (1, utc_now()))
        with self._registry() as registry:
            row = registry.execute("SELECT assistant_key FROM assistant_registry WHERE assistant_key=?", (key,)).fetchone()
            values = (platform, assistant_id, assistant_name, str(db_path.relative_to(self.root / "data")), str(content_path.relative_to(self.root / "data")), 1, utc_now(), key)
            if row:
                registry.execute("UPDATE assistant_registry SET platform=?,assistant_id=?,assistant_name=?,db_path=?,content_path=?,schema_version=?,last_write_at=? WHERE assistant_key=?", values)
            else:
                registry.execute("INSERT INTO assistant_registry(assistant_key,platform,assistant_id,assistant_name,db_path,content_path,schema_version,registered_at,last_write_at) VALUES(?,?,?,?,?,?,?,?,?)", (key, platform, assistant_id, assistant_name, str(db_path.relative_to(self.root / "data")), str(content_path.relative_to(self.root / "data")), 1, utc_now(), utc_now()))
        return key

    def assistant_connection(self, key: str, readonly: bool = False) -> sqlite3.Connection:
        with self._registry() as registry:
            row = registry.execute("SELECT db_path FROM assistant_registry WHERE assistant_key=?", (key,)).fetchone()
        if not row:
            raise ValueError("unknown assistant")
        return self._connect(self.root / "data" / row[0], readonly)

    def list_assistants(self) -> list[dict[str, Any]]:
        with self._registry() as conn:
            fields = ("assistant_key", "platform", "assistant_id", "assistant_name", "last_write_at")
            return [dict(zip(fields, row)) for row in conn.execute("SELECT assistant_key,platform,assistant_id,assistant_name,last_write_at FROM assistant_registry ORDER BY platform,assistant_name")]

    def put_content(self, key: str, execution_id: str, role: str, value: Any, span_id: str | None = None) -> str:
        safe = redact(value)
        raw = safe if isinstance(safe, str) else json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str)
        encoded = raw.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        ref = f"cnt_{uuid.uuid4()}"
        with self._registry() as registry:
            row = registry.execute("SELECT content_path FROM assistant_registry WHERE assistant_key=?", (key,)).fetchone()
        if not row:
            raise ValueError("unknown assistant")
        directory = self.root / "data" / row[0] / "sha256" / digest[:2]
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / digest
        if not target.exists():
            fd, temp = tempfile.mkstemp(dir=directory)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(encoded)
                os.replace(temp, target)
            finally:
                if os.path.exists(temp):
                    os.unlink(temp)
        with self.assistant_connection(key) as conn:
            conn.execute("INSERT INTO span_content(content_ref,execution_id,span_id,role,content_sha256,content_summary,byte_size,truncated,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (ref, execution_id, span_id, role, digest, raw[:2000], len(encoded), 0, utc_now()))
        with self._registry() as registry:
            registry.execute("INSERT INTO content_locator(content_ref,assistant_key) VALUES(?,?)", (ref, key))
        return ref

    def read_content(self, ref: str) -> str:
        with self._registry() as registry:
            row = registry.execute("SELECT assistant_key FROM content_locator WHERE content_ref=?", (ref,)).fetchone()
            if not row:
                raise ValueError("unknown content_ref")
            path_row = registry.execute("SELECT content_path FROM assistant_registry WHERE assistant_key=?", (row[0],)).fetchone()
        with self.assistant_connection(row[0], readonly=True) as conn:
            digest_row = conn.execute("SELECT content_sha256 FROM span_content WHERE content_ref=?", (ref,)).fetchone()
        if not digest_row:
            raise ValueError("content metadata missing")
        return (self.root / "data" / path_row[0] / "sha256" / digest_row[0][:2] / digest_row[0]).read_text(encoding="utf-8")

    def locate_execution(self, execution_id: str) -> tuple[str, sqlite3.Connection]:
        with self._registry() as registry:
            row = registry.execute("SELECT assistant_key FROM execution_locator WHERE execution_id=?", (execution_id,)).fetchone()
        if not row:
            raise ValueError("unknown execution_id")
        return row[0], self.assistant_connection(row[0], readonly=True)

    def snapshot(self, execution_id: str, asset_type: str, asset_id: str, value: Any, purpose: str) -> str:
        key, lookup = self.locate_execution(execution_id)
        lookup.close()
        safe = redact(value)
        raw = safe if isinstance(safe, str) else json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        version = digest[:16]
        with self.assistant_connection(key) as conn:
            row = conn.execute("SELECT snapshot_id FROM config_snapshot WHERE asset_type=? AND asset_id=? AND version=? AND content_sha256=?", (asset_type, asset_id, version, digest)).fetchone()
        snapshot_id = row[0] if row else str(uuid.uuid4())
        if not row:
            ref = self.put_content(key, execution_id, "config_snapshot", safe)
            with self.assistant_connection(key) as conn:
                conn.execute("INSERT INTO config_snapshot(snapshot_id,asset_type,asset_id,version,content_sha256,content_ref,captured_at) VALUES(?,?,?,?,?,?,?)", (snapshot_id, asset_type, asset_id, version, digest, ref, utc_now()))
        with self.assistant_connection(key) as conn:
            exists = conn.execute("SELECT execution_id FROM execution_config_ref WHERE execution_id=? AND snapshot_id=? AND purpose=?", (execution_id, snapshot_id, purpose)).fetchone()
            if not exists:
                conn.execute("INSERT INTO execution_config_ref(execution_id,snapshot_id,purpose) VALUES(?,?,?)", (execution_id, snapshot_id, purpose))
        return snapshot_id

    def feedback(self, execution_id: str, feedback_type: str, reason: str = "", content: Any = None) -> str:
        key, lookup = self.locate_execution(execution_id)
        lookup.close()
        ref = self.put_content(key, execution_id, "feedback", content) if content is not None else None
        feedback_id = str(uuid.uuid4())
        with self.assistant_connection(key) as conn:
            conn.execute("INSERT INTO human_feedback(feedback_id,execution_id,feedback_type,reason,content_ref,created_at) VALUES(?,?,?,?,?,?)", (feedback_id, execution_id, feedback_type, str(redact(reason)), ref, utc_now()))
        return feedback_id

    def eval_candidates(self, strict_only: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for assistant in self.list_assistants():
            key = assistant["assistant_key"]
            with self.assistant_connection(key, readonly=True) as conn:
                where = "WHERE (e.status <> ? OR f.feedback_id IS NOT NULL)"
                params: tuple[Any, ...] = ("completed",)
                if strict_only:
                    where += " AND e.coverage=? AND e.trace_integrity=?"; params += ("full", "complete")
                cursor = conn.execute(f"SELECT DISTINCT e.execution_id,e.assistant_name,e.source_platform,e.status,e.coverage,e.trace_integrity,CASE WHEN e.status <> 'completed' THEN 'failure' ELSE 'feedback' END AS reason_code FROM execution e LEFT JOIN human_feedback f ON f.execution_id=e.execution_id {where} ORDER BY e.started_at DESC LIMIT ?", params + (limit,))
                candidates.extend(_dict_rows(cursor))
        return candidates[:limit]

    def record_execution(self, *, platform: str, assistant_id: str, assistant_name: str, source_execution_id: str, session_id: str, user_input: Any, output: Any = "", status: str = "completed", termination: str = "completed", collection_mode: str = "runtime_adapter", coverage: str = "full", coverage_reason: str = "", trace_integrity: str = "complete", integrity_reason: str = "", spans: list[dict[str, Any]] | None = None, started_at: str | None = None, ended_at: str | None = None, error_code: str | None = None, error_message: str | None = None) -> str:
        key = self.register(platform, assistant_id, assistant_name)
        with self.assistant_connection(key) as conn:
            existing = conn.execute("SELECT execution_id FROM execution WHERE source_platform=? AND source_execution_id=?", (platform, source_execution_id)).fetchone()
        if existing:
            return existing[0]
        execution_id = str(uuid.uuid4())
        now = utc_now()
        started = started_at or now
        ended = ended_at or now
        prepared_spans = list(spans or [])
        input_tokens = sum(int(span.get("input_tokens") or 0) for span in prepared_spans)
        cached_input_tokens = sum(int(span.get("cached_input_tokens") or 0) for span in prepared_spans)
        output_tokens = sum(int(span.get("output_tokens") or 0) for span in prepared_spans)
        total_tokens = sum(int(span.get("total_tokens") or 0) for span in prepared_spans)
        duration_ms = sum(int(span.get("duration_ms") or 0) for span in prepared_spans)
        with self.assistant_connection(key) as conn:
            conn.execute("INSERT INTO execution(execution_id,source_platform,source_execution_id,assistant_id,assistant_name,session_id,status,termination,collection_mode,coverage,coverage_reason,trace_integrity,integrity_reason,input_tokens,cached_input_tokens,output_tokens,total_tokens,duration_ms,error_code,error_message,started_at,ended_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (execution_id, platform, source_execution_id, assistant_id, assistant_name, session_id, status, termination, collection_mode, coverage, coverage_reason, trace_integrity, integrity_reason, input_tokens, cached_input_tokens, output_tokens, total_tokens, duration_ms, error_code, error_message, started, ended, now))
        try:
            input_ref = self.put_content(key, execution_id, "user_input", user_input)
            output_ref = self.put_content(key, execution_id, "agent_output", output) if output != "" else None
            persisted_spans = []
            for index, span in enumerate(prepared_spans):
                span_id = str(uuid.uuid4())
                persisted_spans.append((
                    span_id,
                    span,
                    self.put_content(key, execution_id, "span_input", span.get("input", ""), span_id),
                    self.put_content(key, execution_id, "span_output", span.get("output", ""), span_id),
                    index,
                ))
            with self.assistant_connection(key) as conn:
                conn.execute("UPDATE execution SET input_content_ref=?,output_content_ref=? WHERE execution_id=?", (input_ref, output_ref, execution_id))
                for span_id, span, input_span, output_span, index in persisted_spans:
                    conn.execute("INSERT INTO execution_span(span_id,execution_id,source_span_key,span_type,name,status,tool_name,model_id,input_content_ref,output_content_ref,input_tokens,cached_input_tokens,output_tokens,total_tokens,duration_ms,error_code,error_message,metadata_json,started_at,ended_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (span_id, execution_id, str(span.get("key", index)), str(span.get("type", "tool")), str(span.get("name", "unknown")), str(span.get("status", "success")), span.get("tool_name"), span.get("model_id"), input_span, output_span, int(span.get("input_tokens") or 0), int(span.get("cached_input_tokens") or 0), int(span.get("output_tokens") or 0), int(span.get("total_tokens") or 0), int(span.get("duration_ms") or 0), span.get("error_code"), span.get("error_message"), json.dumps(redact(span.get("metadata", {})), ensure_ascii=False), span.get("started_at") or started, span.get("ended_at") or ended, now))
            with self._registry() as registry:
                registry.execute("INSERT INTO execution_locator(execution_id,assistant_key) VALUES(?,?)", (execution_id, key))
            return execution_id
        except Exception as exc:
            with self.assistant_connection(key) as conn:
                conn.execute("UPDATE execution SET trace_integrity=?,integrity_reason=? WHERE execution_id=?", ("incomplete", str(exc), execution_id))
            with self._registry() as registry:
                existing_locator = registry.execute("SELECT execution_id FROM execution_locator WHERE execution_id=?", (execution_id,)).fetchone()
                if not existing_locator:
                    registry.execute("INSERT INTO execution_locator(execution_id,assistant_key) VALUES(?,?)", (execution_id, key))
            raise
