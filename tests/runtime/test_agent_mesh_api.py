from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient


class AgentMeshAPITests(unittest.TestCase):
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
        from server.db import schema
        schema.init_db()
        from server.api import agent_mesh, workflows
        self.mesh = agent_mesh
        self.app = FastAPI()
        self.app.state.mesh_admin_token = "test-only-secret"
        self.app.include_router(agent_mesh.router)
        self.app.include_router(workflows.router)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer test-only-secret"}

    def test_management_auth_cannot_be_spoofed_by_actor(self):
        with patch.object(self.mesh.mesh_repos, "list_tasks", return_value={"items": [], "next_cursor": ""}) as listing:
            for headers in ({}, {"x-avent-actor": "local"}, {"Authorization": "Bearer wrong"}):
                response = self.client.get("/api/agent-mesh/tasks?actor_id=admin", headers=headers)
                self.assertEqual(response.status_code, 401)
            listing.assert_not_called()
            response = self.client.get("/api/agent-mesh/tasks", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn("test-only-secret", response.text)

    def test_unconfigured_management_api_fails_closed(self):
        self.app.state.mesh_admin_token = ""
        response = self.client.get("/api/agent-mesh/tasks", headers=self.headers)
        self.assertEqual(response.status_code, 401)

    def test_pagination_is_bounded_and_forwards_filters(self):
        with patch.object(self.mesh.mesh_repos, "list_tasks", return_value={"items": [], "next_cursor": "next"}) as listing:
            response = self.client.get("/api/agent-mesh/tasks?assistant_id=A&role=sent&limit=5&cursor=abc", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            listing.assert_called_once_with(actor_id="A", role="sent", status="", group_id="", cursor="abc", limit=5)
            self.assertEqual(self.client.get("/api/agent-mesh/tasks?limit=10000", headers=self.headers).status_code, 422)

    def test_messages_incremental_page(self):
        with patch.object(self.mesh.mesh_repos, "messages", return_value=[{"sequence": 2}, {"sequence": 3}]):
            response = self.client.get("/api/agent-mesh/tasks/t/messages?after_sequence=1&limit=1", headers=self.headers)
            self.assertEqual(response.json(), {"items": [{"sequence": 2}], "next_sequence": 2})

    def test_old_routes_reject_mesh_creation_and_revision(self):
        from server.db import workflow_repos
        workflow = workflow_repos.create_workflow(kind="agent_mesh", owner_session_id="scope", title="mesh")
        task = workflow_repos.get_root_task(workflow["workflow_id"])
        with patch("server.api.workflows.flag_enabled", return_value=True):
            response = self.client.post("/api/workflows", json={"session_id": "scope", "title": "bypass", "kind": "agent_mesh"})
            self.assertEqual(response.status_code, 409)
            response = self.client.post(f"/api/workflows/tasks/{task['task_id']}/revision", json={"session_id": "scope", "intent": {"bypass": True}})
            self.assertEqual(response.status_code, 409)
            response = self.client.post(f"/api/workflows/{workflow['workflow_id']}/tasks", json={"session_id": "scope", "title": "bypass"})
            self.assertEqual(response.status_code, 409)
        self.assertEqual(workflow_repos.get_task(task["task_id"])["revision"], task["revision"])

    def test_artifact_download_requires_credential_and_hides_path(self):
        path = Path(self.tmp.name) / "result.txt"
        path.write_text("delivered result", encoding="utf-8")
        with patch.object(self.mesh.mesh_repos, "artifact", return_value={"path": str(path), "name": "result.txt", "mime": "text/plain"}) as read:
            self.assertEqual(self.client.get("/api/agent-mesh/artifacts/art").status_code, 401)
            read.assert_not_called()
            response = self.client.get("/api/agent-mesh/artifacts/art", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.text, "delivered result")
            self.assertNotIn(self.tmp.name, str(response.headers))

    def test_old_pending_action_and_merge_gate_reject_mesh(self):
        from server.db import workflow_repos
        from server.db.connection import get_connection
        workflow = workflow_repos.create_workflow(kind="agent_mesh", owner_session_id="scope", title="mesh")
        task = workflow_repos.get_root_task(workflow["workflow_id"])
        gate = workflow_repos.create_merge_gate(task["task_id"], merge_target="test")
        conn = get_connection()
        try:
            conn.execute("INSERT INTO pending_actions (action_id,kind,session_id,created_at,updated_at,workflow_id,task_id) VALUES ('test-action','workflow_reconciliation','scope',0,0,?,?)", (workflow["workflow_id"], task["task_id"]))
            conn.commit()
        finally:
            conn.close()
        with patch("server.api.workflows.flag_enabled", return_value=True), \
             patch.object(workflow_repos, "decide_pending_action") as action, \
             patch.object(workflow_repos, "decide_merge_gate") as merge:
            response = self.client.post("/api/workflows/pending-actions/test-action/decision", json={"session_id": "scope", "decision": "confirm_succeeded"})
            self.assertEqual(response.status_code, 409)
            response = self.client.post(f"/api/workflows/merge-gates/{gate['gate_id']}/decision", json={"session_id": "scope", "approved": True})
            self.assertEqual(response.status_code, 409)
            action.assert_not_called()
            merge.assert_not_called()

    def test_decision_requires_revision_and_idempotency_key(self):
        with patch.object(self.mesh.mesh_repos, "decide", return_value={"status": "resolved"}) as decide:
            body = {"action": "approve", "expected_task_revision": 3, "idempotency_key": "key", "payload": {"answer": "ok"}}
            response = self.client.post("/api/agent-mesh/decisions/d", headers=self.headers, json=body)
            self.assertEqual(response.status_code, 200)
            decide.assert_called_once_with("d", **body)
            body.pop("expected_task_revision")
            self.assertEqual(self.client.post("/api/agent-mesh/decisions/d", headers=self.headers, json=body).status_code, 422)

    def test_real_collaboration_configuration_revision_conflict(self):
        from server.db import repos
        first = repos.create_template(name="first")["template_id"]
        second = repos.create_template(name="second")["template_id"]
        path = f"/api/agent-mesh/assistants/{first}/collaboration"
        before = self.client.get(path, headers=self.headers)
        self.assertEqual(before.status_code, 200)
        body = {"outgoing": [second], "accepting_tasks": True, "expected_revision": before.json()["revision"]}
        saved = self.client.put(path, headers=self.headers, json=body)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()["outgoing"], [second])
        conflict = self.client.put(path, headers=self.headers, json=body)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["detail"]["code"], "stale_revision")

    def test_unknown_task_returns_structured_not_found(self):
        response = self.client.get("/api/agent-mesh/tasks/missing", headers=self.headers)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "not_found")

    def test_pending_permission_inbox_requires_management_credential(self):
        with patch.object(self.mesh.mesh_repos, "pending_permission_decisions", return_value={"items": []}) as read:
            response = self.client.get("/api/agent-mesh/decisions/pending", headers={"x-avent-actor": "local"})
            self.assertEqual(response.status_code, 401)
            read.assert_not_called()
            self.assertEqual(self.client.get("/api/agent-mesh/decisions/pending", headers=self.headers).json(), {"items": []})

    def test_pending_permission_inbox_includes_coordination_and_excludes_resolved(self):
        from server.db import repos, mesh_repos as mesh
        a = repos.create_template(name="initiator")["template_id"]
        b = repos.create_template(name="receiver")["template_id"]
        mesh.configure(a, [b])
        task = mesh.delegate(a, b, "Check evidence", ["done"], idempotency_key="permission-inbox")
        permission = mesh.request_decision(task["task_id"], b, "permission", ["approve", "deny", "cancel"],
                                           {"tool": "write_file", "args": {"path": "evidence.txt"}})
        root_permission = mesh.request_decision(task["parent_task_id"], a, "permission", ["approve", "deny", "cancel"],
                                                {"tool": "write_file", "args": {"path": "summary.txt"}})
        path = "/api/agent-mesh/decisions/pending"
        items = self.client.get(path, headers=self.headers).json()["items"]
        self.assertEqual({item["decision_id"] for item in items}, {permission["decision_id"], root_permission["decision_id"]})
        item = next(item for item in items if item["decision_id"] == permission["decision_id"])
        self.assertEqual(item["task_title"], "Check evidence")
        self.assertEqual(item["requester_name"], "receiver")
        self.assertEqual(item["requester_id"], b)
        self.assertEqual(item["expected_task_revision"], permission["expected_task_revision"])
        mesh.decide(permission["decision_id"], "approve", permission["expected_task_revision"], idempotency_key="approve-test")
        questions=[{"id":"choice","question":"请选择交付格式","options":[{"label":"Markdown"},{"label":"PDF"}]}]
        question = mesh.request_decision(task["task_id"], b, "user_input", ["answer", "cancel"], {"questions": questions})
        items = self.client.get(path, headers=self.headers).json()["items"]
        self.assertEqual({item["decision_id"] for item in items}, {root_permission["decision_id"],question["decision_id"]})
        self.assertEqual(next(item for item in items if item['decision_id']==question['decision_id'])['payload']['questions'],questions)
        mesh.cancel_task(task["parent_task_id"])
        self.assertEqual(self.client.get(path, headers=self.headers).json(), {"items": []})

    def test_system_decisions_appear_in_global_dialog_inbox(self):
        from server.db import repos, mesh_repos as mesh
        a=repos.create_template(name='caller')['template_id']
        b=repos.create_template(name='receiver')['template_id']
        mesh.configure(a,[b])
        for reason,actions in [('user_decision',['answer','cancel']),('reconciliation',['confirm_success','retry','cancel'])]:
            with self.subTest(reason=reason):
                task=mesh.delegate(a,b,'Review result',['evidence'])
                decision=mesh.request_decision(task['task_id'],a,reason,actions,{'reason':'Execution ended without submit or explicit wait'})
                items=self.client.get('/api/agent-mesh/decisions/pending',headers=self.headers).json()['items']
                self.assertIn(decision['decision_id'],[item['decision_id'] for item in items])
                mesh.cancel_task(task['task_id'])

    def test_budget_request_appears_in_global_dialog_inbox(self):
        from server.db import repos, mesh_repos as mesh
        a = repos.create_template(name="caller")["template_id"]
        b = repos.create_template(name="worker")["template_id"]
        mesh.configure(a, [b])
        task = mesh.delegate(a, b, "Generate image", ["image"])
        decision = mesh.request_decision(task['task_id'], b, 'budget', ['raise_limit', 'cancel'],
            {'limit': 'tokens', 'current': 200000, 'required': 215106})
        items = self.client.get('/api/agent-mesh/decisions/pending', headers=self.headers).json()['items']
        self.assertEqual(items[0]['decision_id'], decision['decision_id'])
        self.assertEqual(items[0]['payload']['required'], 215106)
        mesh.cancel_task(task['task_id'])
        self.assertEqual(self.client.get('/api/agent-mesh/decisions/pending', headers=self.headers).json()['items'], [])

    def test_active_assistant_cannot_be_deleted(self):
        from server.api.assistants import delete_assistant
        from fastapi import HTTPException
        with patch("server.api.assistants.repos.get_template", return_value={"template_id": "A"}), \
             patch.object(self.mesh.mesh_repos, "has_active_tasks", return_value=True), \
             patch("server.api.assistants.repos.delete_template") as delete:
            with self.assertRaises(HTTPException) as raised:
                delete_assistant("A")
            self.assertEqual(raised.exception.status_code, 409)
            delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
