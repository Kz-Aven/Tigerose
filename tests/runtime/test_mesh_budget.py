from __future__ import annotations

import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from server.db import mesh_repos, repos, schema
from server.db.connection import get_connection
from server.runtime import mesh_budget as budget


class MeshBudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {"TIGEROSE_HOME": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        from avent_paths import reset_path_cache
        reset_path_cache()
        self.addCleanup(reset_path_cache)
        journal = patch("server.runtime.migration_journal.append_journal_event")
        journal.start()
        self.addCleanup(journal.stop)
        schema.init_db()
        conn = get_connection()
        try:
            budget.migrate(conn)
            # Existing installations created this table while 200,000 was the
            # SQLite default. New workflows must not inherit that stale value.
            conn.execute("ALTER TABLE mesh_limits RENAME TO mesh_limits_current")
            conn.execute("""CREATE TABLE mesh_limits (
                workflow_id TEXT PRIMARY KEY,
                depth INTEGER NOT NULL DEFAULT 5,
                tasks INTEGER NOT NULL DEFAULT 32,
                messages INTEGER NOT NULL DEFAULT 100,
                revisions INTEGER NOT NULL DEFAULT 3,
                tokens INTEGER NOT NULL DEFAULT 200000,
                tools INTEGER NOT NULL DEFAULT 200,
                rounds INTEGER NOT NULL DEFAULT 100
            )""")
            conn.execute("DROP TABLE mesh_limits_current")
            conn.commit()
        finally:
            conn.close()
        a, b, c = [repos.create_template(name=name)["template_id"] for name in ("A", "B", "C")]
        mesh_repos.configure(a, [b, c])
        self.tasks = [mesh_repos.delegate(a, target, "task", ["done"], root_request_id="shared", idempotency_key=target) for target in (b, c)]
        self.workflow_id = self.tasks[0]["workflow_id"]
        self.assertEqual(budget.snapshot(self.workflow_id)["limits"]["tokens"], 1_000_000)
        self.works = [mesh_repos.claim_work("worker"), mesh_repos.claim_work("worker")]
        self.assertTrue(all(self.works))

    def limits(self, **values):
        conn = get_connection()
        try:
            for key, value in values.items():
                self.assertIn(key, budget.DEFAULT_LIMITS)
                conn.execute(f"UPDATE mesh_limits SET {key}=? WHERE workflow_id=?", (value, self.workflow_id))
            conn.commit()
        finally:
            conn.close()

    def test_parallel_branches_cannot_over_reserve(self):
        self.limits(tokens=1000)
        barrier = threading.Barrier(2)
        def reserve(work):
            barrier.wait()
            return budget.reserve_llm(work, 500, 100)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, self.works))
        self.assertEqual(sum(result["allowed"] for result in results), 1)
        state = budget.snapshot(self.workflow_id)["usage"]
        self.assertEqual(state["reserved_tokens"], 600)
        self.assertEqual(state["rounds"], 1)
        rejected = next(result for result in results if not result["allowed"])
        self.assertEqual(rejected["decision"]["payload"]["limit"], "tokens")

    def test_settle_releases_unused_allowance_once(self):
        reservation = budget.reserve_llm(self.works[0], 500, 100)
        budget.settle_llm(reservation["reservation_id"], 120, 30)
        budget.settle_llm(reservation["reservation_id"], 999, 999)
        state = budget.snapshot(self.workflow_id)["usage"]
        self.assertEqual(state["tokens"], 150)
        self.assertEqual(state["reserved_tokens"], 0)
        self.assertEqual(state["rounds"], 1)

    def test_missing_usage_and_restart_do_not_reset_allowance(self):
        reservation = budget.reserve_llm(self.works[0], 500, 100)
        budget.settle_llm(reservation["reservation_id"])
        # Fresh connections and idempotent migration retain all counters.
        conn = get_connection()
        try:
            budget.migrate(conn)
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(budget.snapshot(self.workflow_id)["usage"]["tokens"], 600)
        unconfirmed = budget.reserve_llm(self.works[1], 200, 100)
        self.assertTrue(unconfirmed["allowed"])
        self.assertEqual(budget.snapshot(self.workflow_id)["usage"]["reserved_tokens"], 300)

    def test_tools_and_rounds_are_shared_across_assistants(self):
        self.limits(tools=1, rounds=1)
        self.assertTrue(budget.reserve_tool(self.works[0], "read_file")["allowed"])
        denied = budget.reserve_tool(self.works[1], "write_file")
        self.assertFalse(denied["allowed"])
        self.assertEqual(denied["decision"]["payload"]["limit"], "tools")
        mesh_repos.decide(denied["decision"]["decision_id"], "raise_limit",
                          denied["decision"]["expected_task_revision"], {"value": 2}, "tools-increase")
        self.assertTrue(budget.reserve_tool(self.works[1], "write_file")["allowed"])
        self.assertTrue(budget.reserve_llm(self.works[0], 10, 10)["allowed"])
        denied = budget.reserve_llm(self.works[1], 10, 10)
        self.assertFalse(denied["allowed"])
        self.assertEqual(denied["decision"]["payload"]["limit"], "rounds")

    def test_cancelled_work_cannot_spend_but_can_settle(self):
        reservation = budget.reserve_llm(self.works[0], 500, 100)
        mesh_repos.cancel_task(self.works[0]["task_id"])
        with self.assertRaises(mesh_repos.MeshError) as error:
            budget.reserve_tool(self.works[0], "write_file")
        self.assertEqual(error.exception.code, "stale_revision")
        budget.settle_llm(reservation["reservation_id"], 200, 20)
        self.assertEqual(budget.snapshot(self.workflow_id)["usage"]["tokens"], 220)

    def test_provider_overage_stops_the_next_dispatch(self):
        self.limits(tokens=1000)
        reservation = budget.reserve_llm(self.works[0], 100, 100)
        budget.settle_llm(reservation["reservation_id"], 900, 200)
        denied = budget.reserve_llm(self.works[1], 1, 1)
        self.assertFalse(denied["allowed"])
        self.assertFalse(budget.reserve_tool(self.works[1], "write_file")["allowed"])
        self.assertEqual(budget.snapshot(self.workflow_id)["usage"]["tokens"], 1100)

    def test_token_limit_can_be_raised_above_default(self):
        denied = budget.reserve_llm(self.works[0], 1_000_000, 100)
        self.assertFalse(denied["allowed"])
        decision = denied["decision"]
        mesh_repos.decide(decision["decision_id"], "raise_limit", decision["expected_task_revision"],
                          {"value": 1_100_000}, "raise-token-budget")
        self.assertTrue(budget.reserve_llm(self.works[0], 1_000_000, 100)["allowed"])

    def test_budget_increase_preserves_submitted_task_for_review(self):
        task = self.tasks[0]
        mesh_repos.submit(task["task_id"], task["target_id"], {"summary": "done"})
        decision = mesh_repos.request_decision(task["task_id"], task["target_id"], "budget", ["raise_limit", "cancel"],
                                               {"limit": "tokens", "current": 1_000_000, "required": 1_000_001})
        mesh_repos.decide(decision["decision_id"], "raise_limit", decision["expected_task_revision"],
                           {"value": 1_000_001}, "resume-submitted-budget")
        self.assertEqual(mesh_repos.get_task(task["task_id"])["status"], "submitted")
        self.assertEqual(mesh_repos.review(task["task_id"], task["caller_id"], 1, "accept")["status"], "completed")

    def test_migration_upgrades_old_budget_decision_when_new_default_is_sufficient(self):
        task = self.tasks[0]
        self.limits(tokens=200_000)
        decision = mesh_repos.request_decision(task["task_id"], task["target_id"], "budget", ["raise_limit", "cancel"],
                                               {"limit": "tokens", "current": 200_000, "required": 202_257})
        conn = get_connection()
        try:
            budget.migrate(conn)
            conn.commit()
        finally:
            conn.close()
        restored = mesh_repos.get_task(task["task_id"])
        self.assertEqual(budget.snapshot(self.workflow_id)["limits"]["tokens"], 1_000_000)
        self.assertEqual(restored["status"], "queued")
        self.assertEqual(restored["decisions"][0]["status"], "resolved")
        self.assertEqual(restored["decisions"][0]["answer"]["payload"]["automatic"], True)

    def test_invalid_counts_and_forged_work_are_rejected(self):
        with self.assertRaises(mesh_repos.MeshError):
            budget.reserve_llm(self.works[0], -1, 100)
        forged = {**self.works[0], "task_id": self.works[1]["task_id"]}
        with self.assertRaises(mesh_repos.MeshError):
            budget.reserve_tool(forged, "read_file")
        self.assertEqual(budget.snapshot(self.workflow_id)["usage"]["tools"], 0)


if __name__ == "__main__":
    unittest.main()
