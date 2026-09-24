"""Durable, atomic execution allowances shared by every branch of a Mesh DAG.

Token reservations use an estimate before dispatch and provider usage afterward.
Missing usage retains the reservation amount; a crashed call cannot silently regain
its allowance on restart. Provider token accounting remains authoritative.
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import contextmanager
from typing import Any

from server.db.connection import get_connection
from server.db import mesh_repos

DEFAULT_LIMITS = {"tokens": 1_000_000, "tools": 200, "rounds": 100}


def migrate(conn) -> None:
    """Called by schema.init_db after the base Mesh migration, before commit."""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(mesh_limits)")}
    for name, amount in DEFAULT_LIMITS.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE mesh_limits ADD COLUMN {name} INTEGER NOT NULL DEFAULT {amount}")
    # Keep explicit custom budgets intact while raising workflows created with
    # the former default to the current baseline.
    conn.execute("UPDATE mesh_limits SET tokens=? WHERE tokens=?", (DEFAULT_LIMITS["tokens"], 200_000))
    pending = conn.execute("""SELECT d.* FROM mesh_pending_decisions d
        JOIN mesh_task_bindings b USING(task_id)
        JOIN workflow_tasks t USING(task_id)
        WHERE d.status='pending' AND d.reason IN ('budget','limit_reached')
          AND json_extract(d.payload_json,'$.limit')='tokens'
          AND json_extract(d.payload_json,'$.current')=200000
          AND json_extract(d.payload_json,'$.required')<=?
          AND b.waiting_on_id=d.decision_id
          AND t.status NOT IN ('completed','failed','cancelled','timeout')""", (DEFAULT_LIMITS["tokens"],)).fetchall()
    for row in pending:
        decision = dict(row)
        payload = json.loads(decision["payload_json"])
        task = mesh_repos._task(conn, decision["task_id"])
        restored = "submitted" if payload.get("previous_status") == "submitted" else "queued"
        mesh_repos._state(conn, task, restored)
        conn.execute("UPDATE mesh_pending_decisions SET status='resolved',answer_json=? WHERE decision_id=?",
                     (json.dumps({"action":"raise_limit","payload":{"value":DEFAULT_LIMITS["tokens"],"automatic":True}}, ensure_ascii=False), decision["decision_id"]))
        requester = payload.get("requester_id", task["target_id"])
        sender = task["caller_id"] if requester == task["target_id"] else task["target_id"]
        mesh_repos._message(conn, task, sender, "budget_increased", {"limit":"tokens","value":DEFAULT_LIMITS["tokens"]})
    conn.execute("""CREATE TABLE IF NOT EXISTS mesh_budget_usage (
        workflow_id TEXT PRIMARY KEY REFERENCES workflow_runs(workflow_id),
        tokens INTEGER NOT NULL DEFAULT 0, reserved_tokens INTEGER NOT NULL DEFAULT 0,
        tools INTEGER NOT NULL DEFAULT 0, rounds INTEGER NOT NULL DEFAULT 0)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS mesh_budget_reservations (
        reservation_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL REFERENCES workflow_runs(workflow_id),
        work_id TEXT NOT NULL, estimated_tokens INTEGER NOT NULL,
        actual_tokens INTEGER, status TEXT NOT NULL DEFAULT 'reserved', created_at REAL NOT NULL)""")


@contextmanager
def _transaction():
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _scope(conn, work: dict) -> str:
    row = conn.execute("""SELECT t.workflow_id, t.status AS task_status
        FROM mesh_inbox i JOIN workflow_tasks t USING(task_id)
        WHERE i.work_id=? AND i.task_id=? AND i.agent_id=? AND i.lease_token=?
          AND i.fencing_token=? AND i.status='running' AND i.expires_at>?""",
        (work["work_id"], work["task_id"], work["agent_id"], work["lease_token"],
         work["fencing_token"], time.time())).fetchone()
    if row is None or row["task_status"] in mesh_repos.TERMINAL:
        raise mesh_repos.MeshError("stale_revision", "Budget dispatch requires a live task execution lease", 409)
    return row["workflow_id"]


def _usage(conn, workflow_id):
    conn.execute("INSERT OR IGNORE INTO mesh_budget_usage(workflow_id) VALUES (?)", (workflow_id,))
    return dict(conn.execute("SELECT * FROM mesh_budget_usage WHERE workflow_id=?", (workflow_id,)).fetchone())


def _blocked(work, limit, current, required):
    decision = mesh_repos.request_decision(
        work["task_id"], work["agent_id"], "budget", ["raise_limit", "cancel"],
        {"limit": limit, "current": current, "required": required},
    )
    return {"allowed": False, "decision": decision}


def estimate_input_tokens(messages: Any, tools: Any = None) -> int:
    """Conservative text estimate; multimodal provider accounting can differ."""
    serialized = json.dumps({"messages": messages, "tools": tools or []}, ensure_ascii=False, default=str)
    # Counting UTF-8 bytes rather than characters keeps Chinese/code estimates
    # conservative. Provider-specific image/audio charges still need settlement.
    return len(serialized.encode("utf-8")) + 256


def reserve_llm(work: dict, input_tokens: int, max_tokens: int) -> dict:
    """Reserve output + estimated input and one round in a single transaction."""
    if not isinstance(input_tokens, int) or not isinstance(max_tokens, int) or input_tokens < 0 or max_tokens < 1:
        raise mesh_repos.MeshError("invalid_input", "Token reservation must be nonnegative input and positive output")
    amount = input_tokens + max_tokens
    blocked = None
    with _transaction() as conn:
        workflow_id = _scope(conn, work)
        usage = _usage(conn, workflow_id)
        limits = conn.execute("SELECT * FROM mesh_limits WHERE workflow_id=?", (workflow_id,)).fetchone()
        for name, required in (("rounds", usage["rounds"] + 1),
                               ("tokens", usage["tokens"] + usage["reserved_tokens"] + amount)):
            if required > limits[name]:
                blocked = (name, limits[name], required)
                break
        if blocked is None:
            reservation_id = "mbr_" + uuid.uuid4().hex
            conn.execute("UPDATE mesh_budget_usage SET reserved_tokens=reserved_tokens+?,rounds=rounds+1 WHERE workflow_id=?", (amount, workflow_id))
            conn.execute("INSERT INTO mesh_budget_reservations(reservation_id,workflow_id,work_id,estimated_tokens,created_at) VALUES (?,?,?,?,?)", (reservation_id, workflow_id, work["work_id"], amount, time.time()))
            return {"allowed": True, "reservation_id": reservation_id, "estimated_tokens": amount}
    return _blocked(work, *blocked)


def settle_llm(reservation_id: str, input_tokens: int | None = None,
               output_tokens: int | None = None) -> dict:
    """Settle once; unknown usage conservatively consumes the whole reservation.

    Settlement is accounting only, so it remains legal after cancellation or an
    expired lease. It never changes task state or authorizes another dispatch.
    """
    for value in (input_tokens, output_tokens):
        if value is not None and (not isinstance(value, int) or value < 0):
            raise mesh_repos.MeshError("invalid_input", "Reported usage must be a nonnegative integer")
    with _transaction() as conn:
        row = conn.execute("SELECT * FROM mesh_budget_reservations WHERE reservation_id=?", (reservation_id,)).fetchone()
        if row is None:
            raise mesh_repos.MeshError("not_found", "Token reservation not found", 404)
        if row["status"] == "settled":
            return {"reservation_id": reservation_id, "actual_tokens": row["actual_tokens"]}
        actual = input_tokens + output_tokens if input_tokens is not None and output_tokens is not None else row["estimated_tokens"]
        conn.execute("UPDATE mesh_budget_usage SET reserved_tokens=reserved_tokens-?,tokens=tokens+? WHERE workflow_id=?", (row["estimated_tokens"], actual, row["workflow_id"]))
        conn.execute("UPDATE mesh_budget_reservations SET status='settled',actual_tokens=? WHERE reservation_id=?", (actual, reservation_id))
        return {"reservation_id": reservation_id, "actual_tokens": actual}


def reserve_tool(work: dict, tool_name: str = "") -> dict:
    """Charge one attempted tool dispatch, shared across callers and receivers."""
    blocked = None
    with _transaction() as conn:
        workflow_id = _scope(conn, work)
        usage = _usage(conn, workflow_id)
        limits = conn.execute("SELECT tokens,tools FROM mesh_limits WHERE workflow_id=?", (workflow_id,)).fetchone()
        for name, required in (("tokens", usage["tokens"] + usage["reserved_tokens"]),
                               ("tools", usage["tools"] + 1)):
            if required > limits[name]:
                blocked = (name, limits[name], required)
                break
        if blocked is None:
            conn.execute("UPDATE mesh_budget_usage SET tools=tools+1 WHERE workflow_id=?", (workflow_id,))
            return {"allowed": True}
    return _blocked(work, *blocked)


def snapshot(workflow_id: str) -> dict:
    with _transaction() as conn:
        limits = conn.execute("SELECT tokens,tools,rounds FROM mesh_limits WHERE workflow_id=?", (workflow_id,)).fetchone()
        if limits is None:
            raise mesh_repos.MeshError("not_found", "Collaboration not found", 404)
        return {"limits": dict(limits), "usage": _usage(conn, workflow_id)}
