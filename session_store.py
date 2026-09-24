"""Session isolation — per-channel conversation state on disk."""

from __future__ import annotations

import json
import hashlib
import fcntl
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

from avent_config import cfg_get, get_config

VALID_SESSION_ID = re.compile(r"^sess_[a-f0-9]{12}$")


class SessionVersionConflict(RuntimeError):
    """Optimistic concurrency failure on session state.json."""


@dataclass
class SessionMeta:
    session_id: str
    surface: str
    scope_key: str
    title: str = "New session"
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    status: str = "active"  # active | archived
    message_count: int = 0


@dataclass
class SessionState:
    meta: SessionMeta
    version: int = 0
    messages: list = field(default_factory=list)
    context: dict = field(default_factory=dict)
    todos: list = field(default_factory=list)
    compact_summary: str = ""
    _base_content: str = field(default="", repr=False, compare=False)

    def content_fingerprint(self) -> str:
        value = [self.messages, self.context, self.todos, self.compact_summary]
        return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


class SessionManager:
    """Load/save isolated sessions under .sessions/."""

    def __init__(self, workdir: Path, config: dict | None = None):
        self.workdir = workdir
        self.config = config or get_config()
        persist = str(cfg_get(self.config, "session", "persist_dir", default=".sessions"))
        self.root = (workdir / persist).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.scope_map_path = self.root / "scope_map.json"
        self._lock = threading.RLock()
        self.active: SessionState | None = None
        self._scope_map: dict[str, str] = {}
        self._load_scope_map()

    def _load_scope_map(self) -> None:
        if self.scope_map_path.exists():
            try:
                data = json.loads(self.scope_map_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self._scope_map = {str(k): str(v) for k, v in data.items()}
            except (json.JSONDecodeError, OSError):
                self._scope_map = {}

    def _save_scope_map(self) -> None:
        self._atomic_write(
            self.scope_map_path,
            json.dumps(self._scope_map, indent=2, ensure_ascii=False) + "\n",
        )

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def _session_dir(self, session_id: str) -> Path:
        if not VALID_SESSION_ID.match(session_id):
            raise ValueError(f"Invalid session_id: {session_id}")
        return self.root / session_id

    def _new_session_id(self) -> str:
        return f"sess_{uuid.uuid4().hex[:12]}"

    def memory_dir(self, session_id: str) -> Path:
        path = self._session_dir(session_id) / "memory"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def scope_key_for_feishu(self, chat_id: str) -> str:
        return f"feishu:chat:{chat_id}"

    def scope_key_for_cli(self) -> str:
        default = str(cfg_get(self.config, "session", "cli_default_scope", default="cli:default"))
        return default

    def scope_key_for_cron(self, job_id: str | None = None) -> str:
        if job_id:
            return f"cron:job:{job_id}"
        return self.scope_key_for_cli()

    def resolve_session_id(self, scope_key: str) -> str | None:
        return self._scope_map.get(scope_key)

    def bind_scope(self, scope_key: str, session_id: str) -> None:
        self._scope_map[scope_key] = session_id
        self._save_scope_map()

    def load(self, session_id: str) -> SessionState | None:
        sdir = self._session_dir(session_id)
        state_path = sdir / "state.json"
        if state_path.exists():
            data = json.loads(state_path.read_text(encoding="utf-8"))
            state = SessionState(
                meta=SessionMeta(**data["meta"]),
                version=int(data.get("version") or 0),
                messages=data.get("messages") if isinstance(data.get("messages"), list) else [],
                context=data.get("context") if isinstance(data.get("context"), dict) else {},
                todos=data.get("todos") if isinstance(data.get("todos"), list) else [],
                compact_summary=str(data.get("compact_summary") or ""),
            )
            state._base_content = state.content_fingerprint()
            return state
        meta_path = sdir / "meta.json"
        if not meta_path.exists():
            return None
        meta_data = json.loads(meta_path.read_text(encoding="utf-8"))
        meta = SessionMeta(**meta_data)
        messages_path = sdir / "messages.json"
        messages = []
        if messages_path.exists():
            messages = json.loads(messages_path.read_text(encoding="utf-8"))
        context = {}
        ctx_path = sdir / "context.json"
        if ctx_path.exists():
            context = json.loads(ctx_path.read_text(encoding="utf-8"))
        todos = []
        todos_path = sdir / "todos.json"
        if todos_path.exists():
            todos = json.loads(todos_path.read_text(encoding="utf-8"))
        summary = ""
        sum_path = sdir / "compact_summary.md"
        if sum_path.exists():
            summary = sum_path.read_text(encoding="utf-8")
        state = SessionState(
            meta=meta,
            messages=messages if isinstance(messages, list) else [],
            context=context if isinstance(context, dict) else {},
            todos=todos if isinstance(todos, list) else [],
            compact_summary=summary,
        )
        state._base_content = state.content_fingerprint()
        return state

    def _read_disk_version(self, session_id: str) -> int:
        state_path = self._session_dir(session_id) / "state.json"
        if not state_path.exists():
            return 0
        try:
            return int(json.loads(state_path.read_text(encoding="utf-8")).get("version") or 0)
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            return 0

    @contextmanager
    def _disk_lock(self):
        with (self.root / ".write.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def save(
        self,
        state: SessionState | None = None,
        *,
        expected_version: int | None = None,
        touch: bool = True,
        retries: int = 0,
        _fence_connection=None,
    ) -> int | None:
        """Persist session state.

        When ``expected_version`` is set, performs optimistic concurrency (CAS).
        Authoritative writers (active turns / goal) may pass ``retries`` so a
        racing meta-sync that only advanced the version does not fail the turn.
        """
        state = state or self.active
        if not state:
            return None
        attempts = max(0, int(retries)) + 1
        last_conflict: SessionVersionConflict | None = None
        cas_expected = state.version if expected_version is None else expected_version
        for attempt in range(attempts):
            try:
                return self._save_once(
                    state,
                    expected_version=cas_expected,
                    touch=touch,
                    fence_connection=_fence_connection,
                )
            except SessionVersionConflict as exc:
                last_conflict = exc
                if attempt + 1 >= attempts or cas_expected is None:
                    raise
                # Only retry when the winner changed metadata, not model history.
                disk = self.load(state.meta.session_id)
                if disk is None:
                    raise
                if disk.content_fingerprint() != state._base_content:
                    raise SessionVersionConflict(
                        "stale session writer aborted; disk content changed"
                    )
                # Only metadata changed; preserve it while retrying the content commit.
                state.meta = disk.meta
                state.version = disk.version
                cas_expected = disk.version
        if last_conflict:
            raise last_conflict
        return None

    def _save_once(
        self,
        state: SessionState,
        *,
        expected_version: int | None,
        touch: bool,
        fence_connection=None,
    ) -> int:
        guard = nullcontext()
        if "session_epoch" in state.context:
            from server.runtime.session_history import commit_guard

            guard = commit_guard(state, conn=fence_connection)
        with self._lock, guard, self._disk_lock():
            sdir = self._session_dir(state.meta.session_id)
            state_path = sdir / "state.json"
            if state.version > 0 and not state_path.exists():
                raise SessionVersionConflict("session was removed; stale writer aborted")
            sdir.mkdir(parents=True, exist_ok=True)
            disk_version = 0
            if state_path.exists():
                try:
                    disk_version = int(
                        json.loads(state_path.read_text(encoding="utf-8")).get("version") or 0
                    )
                except (json.JSONDecodeError, OSError, TypeError, ValueError):
                    disk_version = int(state.version or 0)
            if expected_version is not None and disk_version != expected_version:
                raise SessionVersionConflict(
                    f"session version conflict: expected {expected_version}, found {disk_version}"
                )
            state.meta.message_count = len(state.messages)
            if touch:
                state.meta.last_active_at = time.time()
            state.version = max(int(state.version or 0), disk_version) + 1
            canonical = {
                "version": state.version,
                "meta": asdict(state.meta),
                "messages": state.messages,
                "context": state.context,
                "todos": state.todos,
                "compact_summary": state.compact_summary,
            }
            self._atomic_write(
                state_path,
                json.dumps(canonical, ensure_ascii=False, default=str) + "\n",
            )
            # Compatibility exports. state.json is the sole read authority after migration.
            (sdir / "meta.json").write_text(
                json.dumps(asdict(state.meta), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            (sdir / "messages.json").write_text(
                json.dumps(state.messages, ensure_ascii=False, default=str) + "\n",
                encoding="utf-8",
            )
            (sdir / "context.json").write_text(
                json.dumps(state.context, ensure_ascii=False, default=str) + "\n",
                encoding="utf-8",
            )
            (sdir / "todos.json").write_text(
                json.dumps(state.todos, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            if state.compact_summary:
                (sdir / "compact_summary.md").write_text(state.compact_summary, encoding="utf-8")
            state._base_content = state.content_fingerprint()
            return state.version

    def patch_meta(self, session_id: str, **changes) -> SessionState | None:
        for attempt in range(5):
            with self._lock:
                state = self.load(session_id)
                if state is None:
                    return None
                for key, value in changes.items():
                    if key not in {"title", "status", "last_active_at", "message_count"}:
                        raise ValueError(f"not a mutable session metadata field: {key}")
                    setattr(state.meta, key, value)
                try:
                    self.save(state, expected_version=state.version, touch=False)
                    return state
                except SessionVersionConflict:
                    if attempt == 4:
                        raise

    def create_session(
        self, surface: str, scope_key: str, *, title: str = "", bind_scope: bool = True
    ) -> SessionState:
        session_id = self._new_session_id()
        meta = SessionMeta(
            session_id=session_id,
            surface=surface,
            scope_key=scope_key,
            title=title or "New session",
        )
        state = SessionState(meta=meta)
        if bind_scope:
            self.bind_scope(scope_key, session_id)
        self.memory_dir(session_id)
        self.save(state)
        return state

    def archive_session(self, session_id: str) -> None:
        state = self.patch_meta(session_id, status="archived")
        if not state:
            return
        for scope, sid in list(self._scope_map.items()):
            if sid == session_id:
                del self._scope_map[scope]
        self._save_scope_map()

    def ensure_session(self, scope_key: str, surface: str, *, title_hint: str = "") -> SessionState:
        with self._lock:
            if self.active and self.active.meta.scope_key == scope_key:
                self.active.meta.last_active_at = time.time()
                return self.active

            if self.active:
                self.save(self.active)

            session_id = self.resolve_session_id(scope_key)
            state = self.load(session_id) if session_id else None
            if state is None or state.meta.status == "archived":
                title = _title_from_hint(title_hint) if title_hint else "New session"
                state = self.create_session(surface, scope_key, title=title)
            elif title_hint and state.meta.title in ("", "New session"):
                state.meta.title = _title_from_hint(title_hint)

            self.active = state
            self.active.meta.last_active_at = time.time()
            return state

    def get_or_create_session_id(
        self, scope_key: str, surface: str, *, title_hint: str = "",
    ) -> str:
        """Resolve scope → session_id without switching the active session."""
        with self._lock:
            session_id = self.resolve_session_id(scope_key)
            state = self.load(session_id) if session_id else None
            if state is None or state.meta.status == "archived":
                title = _title_from_hint(title_hint) if title_hint else "New session"
                state = self.create_session(surface, scope_key, title=title)
            elif title_hint and state.meta.title in ("", "New session"):
                state.meta.title = _title_from_hint(title_hint)
                self.save(state)
            return state.meta.session_id

    def switch_session(self, session_id: str) -> SessionState | None:
        state = self.load(session_id)
        if not state:
            return None
        with self._lock:
            if self.active:
                self.save(self.active)
            self.active = state
            self.bind_scope(state.meta.scope_key, session_id)
            state.meta.status = "active"
            state.meta.last_active_at = time.time()
            return state

    def new_cli_session(self) -> SessionState:
        scope_key = self.scope_key_for_cli()
        old_id = self.resolve_session_id(scope_key)
        if old_id:
            self.archive_session(old_id)
        if self.active:
            self.save(self.active)
            self.active = None
        state = self.create_session("cli", scope_key, title="New session")
        self.active = state
        return state

    def resume_cli_session(self, session_id: str) -> SessionState | None:
        state = self.load(session_id)
        if not state:
            return None
        scope_key = self.scope_key_for_cli()
        current_id = self.resolve_session_id(scope_key)
        if current_id and current_id != session_id:
            self.archive_session(current_id)
        state.meta.status = "active"
        state.meta.scope_key = scope_key
        state.meta.surface = "cli"
        self.bind_scope(scope_key, session_id)
        with self._lock:
            if self.active and self.active.meta.session_id != session_id:
                self.save(self.active)
            self.active = state
        self.save(state)
        return state

    def bootstrap_cli_session(self) -> SessionState:
        scope_key = self.scope_key_for_cli()
        return self.ensure_session(scope_key, "cli")

    def list_sessions(self, *, limit: int = 20, include_archived: bool = False) -> list[SessionMeta]:
        metas: list[SessionMeta] = []
        for path in sorted(self.root.glob("sess_*"), key=lambda p: p.stat().st_mtime, reverse=True):
            if not path.is_dir():
                continue
            meta_path = path / "meta.json"
            if not meta_path.exists():
                continue
            try:
                data = json.loads(meta_path.read_text(encoding="utf-8"))
                meta = SessionMeta(**data)
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
            if not include_archived and meta.status == "archived":
                continue
            metas.append(meta)
            if len(metas) >= limit:
                break
        metas.sort(key=lambda m: m.last_active_at, reverse=True)
        return metas

    def active_session_id(self) -> str | None:
        return self.active.meta.session_id if self.active else None

    def unbind_scope(self, scope_key: str) -> None:
        with self._lock:
            if scope_key in self._scope_map:
                del self._scope_map[scope_key]
                self._save_scope_map()

    def purge_session(self, session_id: str) -> None:
        """Remove session directory and any scope bindings pointing at it."""
        import shutil

        with self._lock, self._disk_lock():
            sdir = self._session_dir(session_id)
            if sdir.exists():
                shutil.rmtree(sdir, ignore_errors=True)
            for scope, sid in list(self._scope_map.items()):
                if sid == session_id:
                    del self._scope_map[scope]
            self._save_scope_map()
            if self.active and self.active.meta.session_id == session_id:
                self.active = None

    def clear_session_contents(
        self, session_id: str, *, title: str = "新对话", session_epoch: int | None = None,
        _fence_connection=None,
    ) -> SessionState | None:
        state = self.load(session_id)
        if not state:
            return None
        state.messages = []
        state.todos = []
        state.compact_summary = ""
        state.context = {"session_epoch": session_epoch, "history_cursor": 0} if session_epoch is not None else {}
        state.meta.message_count = 0
        state.meta.title = title
        state.meta.last_active_at = time.time()
        self.save(state, _fence_connection=_fence_connection)
        if self.active and self.active.meta.session_id == session_id:
            self.active = state
        return state

    def list_sessions_for_scope(
        self,
        scope_key: str,
        *,
        include_empty: bool = False,
        include_archived: bool = True,
    ) -> list[SessionMeta]:
        metas = self.list_sessions(limit=500, include_archived=include_archived)
        out = [m for m in metas if m.scope_key == scope_key]
        if not include_empty:
            out = [m for m in out if m.message_count > 0]
        out.sort(key=lambda m: m.last_active_at, reverse=True)
        return out

    def sync_meta_counts(
        self,
        session_id: str,
        *,
        message_count: int,
        last_active_at: float | None = None,
        title: str | None = None,
    ) -> None:
        state = self.load(session_id)
        if not state:
            return
        changed = False
        desired_count = int(message_count)
        if state.meta.message_count != desired_count:
            state.meta.message_count = desired_count
            changed = True
        if last_active_at is not None:
            desired_active_at = float(last_active_at)
            if state.meta.last_active_at != desired_active_at:
                state.meta.last_active_at = desired_active_at
                changed = True
        if title is not None and state.meta.title != title:
            state.meta.title = title
            changed = True
        if changed:
            try:
                self.save(
                    state,
                    expected_version=state.version,
                    touch=False,
                )
            except SessionVersionConflict:
                # Active turn / goal already advanced the snapshot; meta can
                # catch up on the next idle refresh.
                return
        if self.active and self.active.meta.session_id == session_id:
            self.active = state

    def check_idle_reset(self) -> SessionState | None:
        """Phase C: auto-new CLI session after idle timeout."""
        mode = str(cfg_get(self.config, "session", "reset", "mode", default="idle")).lower()
        if mode not in ("idle", "daily"):
            return None
        if not self.active or self.active.meta.surface != "cli":
            return None
        idle_minutes = int(cfg_get(self.config, "session", "reset", "idle_minutes", default=1440))
        if idle_minutes <= 0:
            return None
        elapsed = time.time() - self.active.meta.last_active_at
        if elapsed < idle_minutes * 60:
            return None
        return self.new_cli_session()

    def session_summary_line(self) -> str:
        if not self.active:
            return "[session] (none)"
        m = self.active.meta
        return (
            f"[session] {m.session_id} · {m.surface} · {m.title[:40]} "
            f"({len(self.active.messages)} msgs)"
        )


def _title_from_hint(text: str, max_len: int = 48) -> str:
    clean = re.sub(r"\[via[^\]]+\]\s*", "", text).strip()
    clean = clean.split("\n", 1)[0].strip()
    if not clean:
        return "New session"
    if len(clean) <= max_len:
        return clean
    return clean[: max_len - 1] + "…"
