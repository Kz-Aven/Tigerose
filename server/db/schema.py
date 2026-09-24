"""SQLite schema for Project Group collaboration."""

from __future__ import annotations

import hashlib

from .connection import get_connection

SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_templates (
  template_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT '',
  system_prompt TEXT NOT NULL DEFAULT '',
  tools_allowlist TEXT NOT NULL DEFAULT '[]',
  capabilities TEXT NOT NULL DEFAULT '{}',
  theme_color TEXT NOT NULL DEFAULT '#5db8a6',
  avatar_id TEXT NOT NULL DEFAULT '',
  model_profile_id TEXT NOT NULL DEFAULT '',
  config_meta TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS capability_registry (
  capability_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  source_path TEXT NOT NULL DEFAULT '',
  display_name TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  meta TEXT NOT NULL DEFAULT '{}',
  hidden INTEGER NOT NULL DEFAULT 0,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS assistant_messages (
  message_id TEXT PRIMARY KEY,
  template_id TEXT NOT NULL,
  role TEXT NOT NULL,
  content TEXT NOT NULL,
  ts REAL NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}',
  session_id TEXT NOT NULL DEFAULT '',
  FOREIGN KEY (template_id) REFERENCES agent_templates(template_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS assistant_memories (
  memory_id TEXT PRIMARY KEY,
  template_id TEXT NOT NULL,
  body TEXT NOT NULL,
  source_groups TEXT NOT NULL DEFAULT '[]',
  source TEXT NOT NULL DEFAULT 'manual',
  ts REAL NOT NULL,
  FOREIGN KEY (template_id) REFERENCES agent_templates(template_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS project_groups (
  group_id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  workspace_path TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  settings TEXT NOT NULL DEFAULT '{}',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS group_memberships (
  instance_id TEXT PRIMARY KEY,
  group_id TEXT NOT NULL,
  template_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  theme_color TEXT NOT NULL DEFAULT '#5db8a6',
  is_coordinator INTEGER NOT NULL DEFAULT 0,
  runtime_status TEXT NOT NULL DEFAULT 'idle',
  created_at REAL NOT NULL,
  FOREIGN KEY (group_id) REFERENCES project_groups(group_id) ON DELETE CASCADE,
  FOREIGN KEY (template_id) REFERENCES agent_templates(template_id),
  UNIQUE(group_id, template_id)
);

CREATE TABLE IF NOT EXISTS feed_events (
  event_id TEXT PRIMARY KEY,
  group_id TEXT NOT NULL,
  speaker_type TEXT NOT NULL,
  speaker_id TEXT NOT NULL,
  visibility TEXT NOT NULL DEFAULT 'L2',
  content TEXT NOT NULL,
  ts REAL NOT NULL,
  meta TEXT NOT NULL DEFAULT '{}',
  FOREIGN KEY (group_id) REFERENCES project_groups(group_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS group_tasks (
  task_id TEXT PRIMARY KEY,
  group_id TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  owner_instance_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  blocked_by TEXT NOT NULL DEFAULT '[]',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  FOREIGN KEY (group_id) REFERENCES project_groups(group_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS group_summaries (
  summary_id TEXT PRIMARY KEY,
  group_id TEXT NOT NULL,
  content TEXT NOT NULL,
  ts REAL NOT NULL,
  FOREIGN KEY (group_id) REFERENCES project_groups(group_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS runtime_tasks (
  task_id TEXT PRIMARY KEY,
  scope_key TEXT NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  owner TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'pending',
  blocked_by TEXT NOT NULL DEFAULT '[]',
  worktree TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_runs (
  run_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  agent_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_usage_events (
  event_id TEXT PRIMARY KEY,
  call_id TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL DEFAULT '',
  session_id TEXT NOT NULL DEFAULT '',
  agent_id TEXT NOT NULL DEFAULT '',
  call_kind TEXT NOT NULL,
  provider TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  usage_source TEXT NOT NULL,
  cache_usage_source TEXT NOT NULL,
  input_tokens INTEGER NOT NULL,
  cached_input_tokens INTEGER NOT NULL,
  uncached_input_tokens INTEGER NOT NULL,
  output_tokens INTEGER NOT NULL,
  total_tokens INTEGER NOT NULL,
  reasoning_tokens INTEGER,
  cache_creation_tokens INTEGER,
  started_at REAL NOT NULL,
  completed_at REAL NOT NULL,
  created_at REAL NOT NULL,
  CHECK (input_tokens >= 0 AND cached_input_tokens >= 0 AND uncached_input_tokens >= 0 AND output_tokens >= 0 AND total_tokens >= 0),
  CHECK (cached_input_tokens <= input_tokens),
  CHECK (uncached_input_tokens = input_tokens - cached_input_tokens),
  CHECK (total_tokens = input_tokens + output_tokens)
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_completed_at ON llm_usage_events(completed_at);
CREATE INDEX IF NOT EXISTS idx_llm_usage_completed_model ON llm_usage_events(completed_at, model);
CREATE INDEX IF NOT EXISTS idx_llm_usage_completed_agent ON llm_usage_events(completed_at, agent_id);
CREATE INDEX IF NOT EXISTS idx_llm_usage_run ON llm_usage_events(run_id);

CREATE TABLE IF NOT EXISTS runtime_goal_tasks (
  goal_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  scope_key TEXT NOT NULL,
  run_id TEXT NOT NULL DEFAULT '',
  bound_at REAL NOT NULL,
  PRIMARY KEY (goal_id, task_id)
);

CREATE TABLE IF NOT EXISTS pending_questions (
  question_id TEXT PRIMARY KEY,
  tool_call_id TEXT NOT NULL,
  goal_id TEXT NOT NULL DEFAULT '',
  run_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  channel TEXT NOT NULL,
  surface TEXT NOT NULL,
  template_id TEXT NOT NULL,
  group_id TEXT NOT NULL DEFAULT '',
  instance_id TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  answer TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  answered_at REAL,
  cancelled_at REAL
);

CREATE INDEX IF NOT EXISTS idx_assistant_messages_tid ON assistant_messages(template_id, ts);
CREATE INDEX IF NOT EXISTS idx_feed_group_ts ON feed_events(group_id, ts);
CREATE INDEX IF NOT EXISTS idx_tasks_group ON group_tasks(group_id, status);
CREATE INDEX IF NOT EXISTS idx_memberships_group ON group_memberships(group_id);
CREATE INDEX IF NOT EXISTS idx_runtime_tasks_scope ON runtime_tasks(scope_key, status);
CREATE INDEX IF NOT EXISTS idx_runtime_runs_session ON runtime_runs(session_id, status);
CREATE INDEX IF NOT EXISTS idx_runtime_goal_tasks_goal ON runtime_goal_tasks(goal_id);
CREATE INDEX IF NOT EXISTS idx_pending_questions_channel_status
  ON pending_questions(channel, status, created_at);
CREATE INDEX IF NOT EXISTS idx_pending_questions_session_status
  ON pending_questions(session_id, status);

CREATE TABLE IF NOT EXISTS pending_actions (
  action_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  session_id TEXT NOT NULL,
  objective_id TEXT NOT NULL DEFAULT '',
  objective_revision INTEGER NOT NULL DEFAULT 1,
  attempt_id TEXT NOT NULL DEFAULT '',
  tool_name TEXT NOT NULL DEFAULT '',
  frozen_args_digest TEXT NOT NULL DEFAULT '',
  continuation_ref TEXT NOT NULL DEFAULT '',
  run_id TEXT NOT NULL DEFAULT '',
  expires_at REAL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  decided_at REAL,
  consumed_at REAL
);

CREATE INDEX IF NOT EXISTS idx_pending_actions_session_status
  ON pending_actions(session_id, status);

CREATE TABLE IF NOT EXISTS runtime_budgets (
  root_budget_id TEXT PRIMARY KEY,
  profile TEXT NOT NULL,
  limits_json TEXT NOT NULL DEFAULT '{}',
  consumed_json TEXT NOT NULL DEFAULT '{}',
  reserved_json TEXT NOT NULL DEFAULT '{}',
  active_execution_ms INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_reservations (
  reservation_id TEXT PRIMARY KEY,
  root_budget_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  amount REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'reserved',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_budget_reservations_root
  ON budget_reservations(root_budget_id, status);

CREATE TABLE IF NOT EXISTS app_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT '',
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS im_channel_applications (
  application_id TEXT PRIMARY KEY,
  platform TEXT NOT NULL,
  provider_application_id TEXT NOT NULL,
  display_name TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'connected',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(platform, provider_application_id)
);

CREATE TABLE IF NOT EXISTS im_bots (
  bot_id TEXT PRIMARY KEY,
  application_id TEXT NOT NULL,
  template_id TEXT,
  provider_bot_id TEXT NOT NULL,
  display_name TEXT NOT NULL DEFAULT '',
  secret_ref TEXT NOT NULL DEFAULT '',
  health TEXT NOT NULL DEFAULT 'stopped',
  last_error TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  FOREIGN KEY (application_id) REFERENCES im_channel_applications(application_id) ON DELETE CASCADE,
  FOREIGN KEY (template_id) REFERENCES agent_templates(template_id) ON DELETE SET NULL,
  UNIQUE(application_id, provider_bot_id)
);

CREATE TABLE IF NOT EXISTS im_conversations (
  conversation_id TEXT PRIMARY KEY,
  bot_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  remote_conversation_id TEXT NOT NULL,
  session_id TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL DEFAULT '',
  last_inbound_at REAL,
  FOREIGN KEY (bot_id) REFERENCES im_bots(bot_id) ON DELETE CASCADE,
  UNIQUE(bot_id, kind, remote_conversation_id)
);

CREATE TABLE IF NOT EXISTS session_unreads (
  session_id TEXT PRIMARY KEY,
  unread_count INTEGER NOT NULL DEFAULT 0,
  last_unread_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS im_message_receipts (
  platform TEXT NOT NULL,
  bot_id TEXT NOT NULL,
  provider_message_id TEXT NOT NULL,
  conversation_id TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL,
  PRIMARY KEY(platform, bot_id, provider_message_id)
);

CREATE INDEX IF NOT EXISTS idx_im_bots_template ON im_bots(template_id);
CREATE INDEX IF NOT EXISTS idx_im_conversations_bot ON im_conversations(bot_id, last_inbound_at);
CREATE INDEX IF NOT EXISTS idx_session_unreads_unread ON session_unreads(unread_count, last_unread_at);

CREATE TABLE IF NOT EXISTS schema_migrations (
  migration_id TEXT PRIMARY KEY,
  checksum TEXT NOT NULL,
  applied_at REAL NOT NULL
);
"""

_WORKFLOW_V0_MIGRATION_ID = "durable_workflow_v0_expand"
_WORKFLOW_V0_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS workflow_runs (
      workflow_id TEXT PRIMARY KEY,
      revision INTEGER NOT NULL DEFAULT 1,
      kind TEXT NOT NULL,
      status TEXT NOT NULL,
      owner_session_id TEXT NOT NULL DEFAULT '',
      root_budget_id TEXT NOT NULL DEFAULT '',
      input_snapshot_json TEXT NOT NULL DEFAULT '{}',
      legacy_source TEXT NOT NULL DEFAULT '',
      legacy_id TEXT NOT NULL DEFAULT '',
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL,
      terminal_at REAL
    )""",
    """CREATE TABLE IF NOT EXISTS workflow_tasks (
      task_id TEXT PRIMARY KEY,
      workflow_id TEXT NOT NULL,
      revision INTEGER NOT NULL DEFAULT 1,
      title TEXT NOT NULL DEFAULT '',
      intent_json TEXT NOT NULL DEFAULT '{}',
      status TEXT NOT NULL,
      priority INTEGER NOT NULL DEFAULT 0,
      executor_kind TEXT NOT NULL DEFAULT '',
      acceptance_policy TEXT NOT NULL DEFAULT '',
      next_run_at REAL,
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL,
      FOREIGN KEY (workflow_id) REFERENCES workflow_runs(workflow_id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS workflow_task_deps (
      task_id TEXT NOT NULL,
      depends_on_task_id TEXT NOT NULL,
      dependency_kind TEXT NOT NULL DEFAULT 'hard',
      PRIMARY KEY (task_id, depends_on_task_id),
      FOREIGN KEY (task_id) REFERENCES workflow_tasks(task_id) ON DELETE CASCADE,
      FOREIGN KEY (depends_on_task_id) REFERENCES workflow_tasks(task_id) ON DELETE CASCADE,
      CHECK (dependency_kind IN ('hard', 'soft')),
      CHECK (task_id != depends_on_task_id)
    )""",
    """CREATE TABLE IF NOT EXISTS workflow_attempts (
      attempt_id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL,
      sequence INTEGER NOT NULL,
      status TEXT NOT NULL,
      intent_revision INTEGER NOT NULL DEFAULT 1,
      idempotency_key TEXT NOT NULL DEFAULT '',
      dispatch_json TEXT NOT NULL DEFAULT '{}',
      receipt_json TEXT NOT NULL DEFAULT '{}',
      error_code TEXT NOT NULL DEFAULT '',
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL,
      FOREIGN KEY (task_id) REFERENCES workflow_tasks(task_id) ON DELETE CASCADE,
      UNIQUE(task_id, sequence),
      UNIQUE(task_id, idempotency_key)
    )""",
    """CREATE TABLE IF NOT EXISTS workflow_leases (
      task_id TEXT PRIMARY KEY,
      owner_id TEXT NOT NULL,
      lease_token TEXT NOT NULL,
      fencing_token INTEGER NOT NULL,
      expires_at REAL NOT NULL,
      heartbeat_at REAL NOT NULL,
      FOREIGN KEY (task_id) REFERENCES workflow_tasks(task_id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS workflow_events (
      event_id TEXT PRIMARY KEY,
      workflow_id TEXT NOT NULL,
      task_id TEXT,
      attempt_id TEXT,
      sequence INTEGER NOT NULL,
      type TEXT NOT NULL,
      dedupe_key TEXT NOT NULL DEFAULT '',
      payload_json TEXT NOT NULL DEFAULT '{}',
      created_at REAL NOT NULL,
      FOREIGN KEY (workflow_id) REFERENCES workflow_runs(workflow_id) ON DELETE CASCADE,
      FOREIGN KEY (task_id) REFERENCES workflow_tasks(task_id) ON DELETE SET NULL,
      FOREIGN KEY (attempt_id) REFERENCES workflow_attempts(attempt_id) ON DELETE SET NULL,
      UNIQUE(workflow_id, sequence)
    )""",
    """CREATE TABLE IF NOT EXISTS workflow_outbox (
      outbox_id TEXT PRIMARY KEY,
      event_id TEXT NOT NULL,
      workflow_id TEXT NOT NULL,
      attempt_id TEXT,
      kind TEXT NOT NULL,
      payload_json TEXT NOT NULL DEFAULT '{}',
      status TEXT NOT NULL DEFAULT 'pending',
      attempts INTEGER NOT NULL DEFAULT 0,
      available_at REAL NOT NULL,
      created_at REAL NOT NULL,
      updated_at REAL NOT NULL,
      FOREIGN KEY (event_id) REFERENCES workflow_events(event_id) ON DELETE CASCADE,
      FOREIGN KEY (workflow_id) REFERENCES workflow_runs(workflow_id) ON DELETE CASCADE,
      FOREIGN KEY (attempt_id) REFERENCES workflow_attempts(attempt_id) ON DELETE SET NULL,
      UNIQUE(event_id, kind)
    )""",
    """CREATE TABLE IF NOT EXISTS workflow_context_packs (
      pack_id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL,
      revision INTEGER NOT NULL,
      summary TEXT NOT NULL DEFAULT '',
      open_items_json TEXT NOT NULL DEFAULT '[]',
      artifact_refs_json TEXT NOT NULL DEFAULT '[]',
      source_event_seq INTEGER NOT NULL,
      created_at REAL NOT NULL,
      FOREIGN KEY (task_id) REFERENCES workflow_tasks(task_id) ON DELETE CASCADE,
      UNIQUE(task_id, revision, source_event_seq)
    )""",
    """CREATE TABLE IF NOT EXISTS workflow_artifacts (
      artifact_id TEXT PRIMARY KEY,
      task_id TEXT NOT NULL,
      kind TEXT NOT NULL,
      uri TEXT NOT NULL DEFAULT '',
      sha256 TEXT NOT NULL DEFAULT '',
      metadata_json TEXT NOT NULL DEFAULT '{}',
      created_at REAL NOT NULL,
      FOREIGN KEY (task_id) REFERENCES workflow_tasks(task_id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_workflow_runs_status ON workflow_runs(status, updated_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_runs_legacy ON workflow_runs(legacy_source, legacy_id) WHERE legacy_source != '' AND legacy_id != ''",
    "CREATE INDEX IF NOT EXISTS idx_workflow_tasks_runnable ON workflow_tasks(status, next_run_at, priority)",
    "CREATE INDEX IF NOT EXISTS idx_workflow_tasks_workflow ON workflow_tasks(workflow_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_workflow_task_deps_dependency ON workflow_task_deps(depends_on_task_id)",
    "CREATE INDEX IF NOT EXISTS idx_workflow_leases_expiry ON workflow_leases(expires_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_workflow_events_dedupe ON workflow_events(workflow_id, dedupe_key) WHERE dedupe_key != ''",
    "CREATE INDEX IF NOT EXISTS idx_workflow_events_task ON workflow_events(task_id, sequence)",
    "CREATE INDEX IF NOT EXISTS idx_workflow_outbox_delivery ON workflow_outbox(status, available_at)",
    "CREATE INDEX IF NOT EXISTS idx_workflow_artifacts_task ON workflow_artifacts(task_id, created_at)",
)
_WORKFLOW_V0_CHECKSUM = hashlib.sha256(
    "\n".join(_WORKFLOW_V0_STATEMENTS).encode("utf-8")
).hexdigest()

_WORKFLOW_V2_MIGRATION_ID = "durable_workflow_v2_graph"
_WORKFLOW_V2_STATEMENTS = (
    """CREATE TABLE IF NOT EXISTS workflow_milestones (
      milestone_id TEXT PRIMARY KEY,
      workflow_id TEXT NOT NULL,
      title TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',
      acceptance_policy TEXT NOT NULL DEFAULT 'all_required',
      created_at REAL NOT NULL,
      accepted_at REAL,
      FOREIGN KEY (workflow_id) REFERENCES workflow_runs(workflow_id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS workflow_milestone_tasks (
      milestone_id TEXT NOT NULL,
      task_id TEXT NOT NULL,
      required INTEGER NOT NULL DEFAULT 1,
      PRIMARY KEY (milestone_id, task_id),
      FOREIGN KEY (milestone_id) REFERENCES workflow_milestones(milestone_id) ON DELETE CASCADE,
      FOREIGN KEY (task_id) REFERENCES workflow_tasks(task_id) ON DELETE CASCADE
    )""",
    """CREATE TABLE IF NOT EXISTS workflow_merge_gates (
      gate_id TEXT PRIMARY KEY,
      workflow_id TEXT NOT NULL,
      task_id TEXT NOT NULL UNIQUE,
      merge_target TEXT NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',
      decision_by TEXT NOT NULL DEFAULT '',
      decision_at REAL,
      metadata_json TEXT NOT NULL DEFAULT '{}',
      created_at REAL NOT NULL,
      FOREIGN KEY (workflow_id) REFERENCES workflow_runs(workflow_id) ON DELETE CASCADE,
      FOREIGN KEY (task_id) REFERENCES workflow_tasks(task_id) ON DELETE CASCADE
    )""",
    "CREATE INDEX IF NOT EXISTS idx_workflow_milestones_workflow ON workflow_milestones(workflow_id, status)",
    "CREATE INDEX IF NOT EXISTS idx_workflow_merge_gates_target ON workflow_merge_gates(merge_target, status)",
)
_WORKFLOW_V2_CHECKSUM = hashlib.sha256(
    "\n".join(_WORKFLOW_V2_STATEMENTS).encode("utf-8")
).hexdigest()


def _append_migration_journal(stage: str) -> None:
    try:
        from server.runtime.migration_journal import append_journal_event

        append_journal_event(
            _WORKFLOW_V0_MIGRATION_ID,
            stage,
            checksum=_WORKFLOW_V0_CHECKSUM,
        )
    except Exception:
        # The SQLite record below is authoritative; audit JSONL is best effort.
        pass


def _add_column_if_missing(conn, table: str, definition: str) -> None:
    name = definition.split()[0]
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if name not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")


def _apply_workflow_v0_migration(conn) -> None:
    row = conn.execute(
        "SELECT checksum FROM schema_migrations WHERE migration_id = ?",
        (_WORKFLOW_V0_MIGRATION_ID,),
    ).fetchone()
    if row:
        if str(row[0]) != _WORKFLOW_V0_CHECKSUM:
            raise RuntimeError("workflow V0 migration checksum mismatch")
        return

    _append_migration_journal("prepared")
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in _WORKFLOW_V0_STATEMENTS:
            conn.execute(statement)
        _add_column_if_missing(conn, "pending_actions", "workflow_id TEXT")
        _add_column_if_missing(conn, "pending_actions", "task_id TEXT")
        _add_column_if_missing(conn, "pending_actions", "attempt_id TEXT")
        _add_column_if_missing(conn, "pending_actions", "decision_type TEXT")
        _add_column_if_missing(conn, "pending_actions", "decision_payload_json TEXT")
        _add_column_if_missing(conn, "pending_actions", "resolved_by TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_actions_workflow_status "
            "ON pending_actions(workflow_id, status)"
        )
        conn.execute(
            "INSERT INTO schema_migrations (migration_id, checksum, applied_at) "
            "VALUES (?, ?, strftime('%s', 'now'))",
            (_WORKFLOW_V0_MIGRATION_ID, _WORKFLOW_V0_CHECKSUM),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        _append_migration_journal("rolled_back")
        raise
    _append_migration_journal("committed")


def _apply_workflow_v2_migration(conn) -> None:
    row = conn.execute(
        "SELECT checksum FROM schema_migrations WHERE migration_id = ?",
        (_WORKFLOW_V2_MIGRATION_ID,),
    ).fetchone()
    if row:
        if str(row[0]) != _WORKFLOW_V2_CHECKSUM:
            raise RuntimeError("workflow V2 migration checksum mismatch")
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in _WORKFLOW_V2_STATEMENTS:
            conn.execute(statement)
        _add_column_if_missing(conn, "workflow_tasks", "parent_task_id TEXT")
        _add_column_if_missing(conn, "workflow_tasks", "retry_policy_json TEXT NOT NULL DEFAULT '{}'")
        _add_column_if_missing(conn, "workflow_tasks", "blocked_reason TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "workflow_tasks", "workspace_path TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "workflow_tasks", "worktree_path TEXT NOT NULL DEFAULT ''")
        _add_column_if_missing(conn, "workflow_tasks", "merge_target TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_workflow_tasks_parent ON workflow_tasks(parent_task_id)"
        )
        conn.execute(
            "INSERT INTO schema_migrations (migration_id, checksum, applied_at) VALUES (?, ?, strftime('%s', 'now'))",
            (_WORKFLOW_V2_MIGRATION_ID, _WORKFLOW_V2_CHECKSUM),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def _apply_mesh_migration(conn) -> None:
    from .mesh_repos import MIGRATION_STATEMENTS

    checksum = hashlib.sha256("\n".join(MIGRATION_STATEMENTS).encode()).hexdigest()
    row = conn.execute("SELECT checksum FROM schema_migrations WHERE migration_id='agent_mesh_v1'").fetchone()
    if row:
        if row[0] != checksum:
            raise RuntimeError("Agent Mesh migration checksum mismatch")
        return
    try:
        conn.execute("BEGIN IMMEDIATE")
        for statement in MIGRATION_STATEMENTS:
            conn.execute(statement)
        conn.execute("INSERT INTO schema_migrations VALUES ('agent_mesh_v1', ?, strftime('%s','now'))", (checksum,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        _apply_workflow_v0_migration(conn)
        _apply_workflow_v2_migration(conn)
        _apply_mesh_migration(conn)
        from server.runtime.mesh_budget import migrate as migrate_mesh_budget
        migrate_mesh_budget(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_templates)").fetchall()}
        if "model_profile_id" not in cols:
            conn.execute(
                "ALTER TABLE agent_templates ADD COLUMN model_profile_id TEXT NOT NULL DEFAULT ''"
            )
        if "capabilities" not in cols:
            conn.execute(
                "ALTER TABLE agent_templates ADD COLUMN capabilities TEXT NOT NULL DEFAULT '{}'"
            )
        if "avatar_id" not in cols:
            conn.execute(
                "ALTER TABLE agent_templates ADD COLUMN avatar_id TEXT NOT NULL DEFAULT ''"
            )
        mem_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(assistant_memories)").fetchall()
        }
        if "source" not in mem_cols:
            conn.execute(
                "ALTER TABLE assistant_memories ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'"
            )
        msg_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(assistant_messages)").fetchall()
        }
        if "session_id" not in msg_cols:
            conn.execute(
                "ALTER TABLE assistant_messages ADD COLUMN session_id TEXT NOT NULL DEFAULT ''"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_assistant_messages_session "
            "ON assistant_messages(template_id, session_id, ts)"
        )
        # Migrate tools_allowlist → capabilities.tools when capabilities.tools empty
        from server.capabilities.catalog import normalize_capabilities
        import json

        rows = conn.execute(
            "SELECT template_id, tools_allowlist, capabilities FROM agent_templates"
        ).fetchall()
        for row in rows:
            tid, tools_raw, caps_raw = row[0], row[1], row[2]
            try:
                tools = json.loads(tools_raw or "[]")
            except Exception:
                tools = []
            try:
                caps = json.loads(caps_raw or "{}")
            except Exception:
                caps = {}
            caps = normalize_capabilities(caps)
            if not caps.get("tools") and tools:
                caps["tools"] = [str(t) for t in tools]
                conn.execute(
                    "UPDATE agent_templates SET capabilities = ? WHERE template_id = ?",
                    (json.dumps(caps, ensure_ascii=False), tid),
                )
        conn.commit()
        from server.db.memory_migration import migrate as migrate_memory_system

        migrate_memory_system(conn)
        conn.commit()
    finally:
        conn.close()
