from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

import avent_config
from server.api import models


class GoalEvaluatorModelsAPITest(unittest.TestCase):
    def setUp(self) -> None:
        self._old_cfg = avent_config._CFG
        self._old_config_path = avent_config._CONFIG_PATH
        self._old_active_profile_id = avent_config._ACTIVE_PROFILE_ID
        self._tempdir = tempfile.TemporaryDirectory()
        self.config_path = Path(self._tempdir.name) / "config.yaml"
        self._write_config()

        app = FastAPI()
        app.include_router(models.router)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        avent_config._CFG = self._old_cfg
        avent_config._CONFIG_PATH = self._old_config_path
        avent_config._ACTIVE_PROFILE_ID = self._old_active_profile_id
        self._tempdir.cleanup()

    def _write_config(self, configured_profile_id: str | None = None) -> None:
        data = {
            "_config_version": 1,
            "model": {
                "default": "primary",
                "profiles": [
                    {
                        "id": "primary",
                        "label": "Primary",
                        "provider": "custom",
                        "base_url": "http://localhost:1234/v1",
                        "api_key": "secret-primary",
                    },
                    {
                        "id": "reviewer",
                        "label": "Reviewer",
                        "provider": "custom",
                        "base_url": "http://localhost:1234/v1",
                        "api_key": "secret-reviewer",
                    },
                ],
            },
        }
        if configured_profile_id is not None:
            data["agent"] = {"goal_evaluator_model": configured_profile_id}
        self.config_path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        avent_config.init_config(
            config_path=self.config_path,
            workdir=self.config_path.parent,
        )

    def test_get_default_follows_global_default_for_display(self) -> None:
        response = self.client.get("/api/models/goal-evaluator")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "profile_id": "",
                "configured_profile_id": "",
                "effective_profile_id": "primary",
                "fallback": False,
            },
        )

    def test_get_valid_selection(self) -> None:
        self._write_config("reviewer")

        response = self.client.get("/api/models/goal-evaluator")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "profile_id": "reviewer",
                "configured_profile_id": "reviewer",
                "effective_profile_id": "reviewer",
                "fallback": False,
            },
        )

    def test_get_dangling_selection_falls_back_without_losing_original(self) -> None:
        self._write_config("deleted-profile")

        response = self.client.get("/api/models/goal-evaluator")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "profile_id": "",
                "configured_profile_id": "deleted-profile",
                "effective_profile_id": "primary",
                "fallback": True,
            },
        )

    def test_put_empty_value_follows_current_run_profile(self) -> None:
        self._write_config("reviewer")

        response = self.client.put(
            "/api/models/goal-evaluator",
            json={"profile_id": ""},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["profile_id"], "")
        self.assertEqual(response.json()["effective_profile_id"], "primary")
        persisted = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["agent"]["goal_evaluator_model"], "")

    def test_put_valid_global_profile(self) -> None:
        response = self.client.put(
            "/api/models/goal-evaluator",
            json={"profile_id": "reviewer"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["profile_id"], "reviewer")
        self.assertEqual(response.json()["effective_profile_id"], "reviewer")
        persisted = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual(
            persisted["agent"]["goal_evaluator_model"],
            "reviewer",
        )

    def test_put_unknown_profile_returns_400(self) -> None:
        response = self.client.put(
            "/api/models/goal-evaluator",
            json={"profile_id": "missing"},
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("model profile not found: missing", response.json()["detail"])
        persisted = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.assertNotIn("agent", persisted)


if __name__ == "__main__":
    unittest.main()
