from __future__ import annotations

import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo


class UsageRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.env = patch.dict(os.environ, {"TIGEROSE_HOME": str(self.home)})
        self.env.start()
        self.addCleanup(self.env.stop)
        from avent_paths import reset_path_cache
        from server.db import schema

        reset_path_cache()
        self.addCleanup(reset_path_cache)
        schema.init_db()

    def _event(
        self,
        call_id: str,
        *,
        agent_id: str = "agent_a",
        source: str = "provider",
        run_id: str = "run_1",
    ) -> dict:
        now = time.time()
        return {
            "event_id": f"event_{call_id}", "call_id": call_id, "run_id": run_id,
            "session_id": "session_1", "agent_id": agent_id, "call_kind": "agent_loop",
            "provider": "openai", "model": "test-model", "status": "completed",
            "usage_source": source, "cache_usage_source": "provider" if source == "provider" else "unavailable",
            "input_tokens": 100, "cached_input_tokens": 40 if source == "provider" else 0,
            "uncached_input_tokens": 60 if source == "provider" else 100,
            "output_tokens": 20, "total_tokens": 120, "started_at": now - 1,
            "completed_at": now, "created_at": now,
        }

    def test_events_are_idempotent_and_aggregate_by_run_and_agent(self):
        from server.db import repos

        self.assertTrue(repos.add_llm_usage_event(self._event("one")))
        self.assertFalse(repos.add_llm_usage_event(self._event("one")))
        self.assertTrue(repos.add_llm_usage_event(self._event("two", agent_id="", source="estimated")))
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()

        summary = repos.get_run_usage_summary("run_1")
        self.assertEqual(summary["total_tokens"], 240)
        self.assertEqual(summary["usage_source"], "mixed")
        overview = repos.usage_overview(today, today)
        self.assertEqual(overview["call_count"], 2)
        self.assertEqual(overview["cached_input_tokens"], 40)
        by_agent = repos.usage_by_agent(today, today)
        internal = next(item for item in by_agent if item["agent_id"] == "")
        self.assertEqual(internal["agent_name"], "平台内部调用")
        agent = next(item for item in by_agent if item["agent_id"] == "agent_a")
        self.assertEqual(agent["uncached_input_tokens"], 60)
        self.assertEqual(agent["cached_input_tokens"], 40)
        self.assertEqual(
            agent["uncached_input_tokens"] + agent["cached_input_tokens"] + agent["output_tokens"],
            agent["total_tokens"],
        )

    def test_usage_summary_combines_distinct_question_continuation_runs(self):
        from server.db import repos
        from server.scheduler.group_scheduler import _usage_run_ids

        self.assertTrue(repos.add_llm_usage_event(self._event("question", run_id="run_question")))
        self.assertTrue(
            repos.add_llm_usage_event(
                self._event("reply", run_id="run_reply", source="estimated")
            )
        )

        run_ids = _usage_run_ids(
            "run_reply",
            {"payload": {"usage_run_ids": ["run_question", "run_question"]}},
        )
        summary = repos.get_usage_summary_for_runs(run_ids)

        self.assertEqual(run_ids, ["run_question", "run_reply"])
        self.assertEqual(summary["call_count"], 2)
        self.assertEqual(summary["total_tokens"], 240)
        self.assertEqual(summary["usage_source"], "mixed")

    def test_usage_api_adds_priced_amounts_to_every_usage_view(self):
        from server.api import usage
        from server.db import repos

        event = self._event("priced", agent_id="agent_priced")
        event["model"] = "deepseek-v4-flash"
        self.assertTrue(repos.add_llm_usage_event(event))
        today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()

        overview = usage.get_overview(today, today)["data"]
        trend = usage.get_trend(today, today)["items"]
        by_model = usage.get_by_model(today, today)["items"]
        by_agent = usage.get_by_agent(today, today)["items"]

        expected = (60 * 3.0 + 40 * 0.1 + 20 * 9.0) / 1_000_000
        self.assertAlmostEqual(overview["spend_cny"], expected)
        self.assertAlmostEqual(trend[0]["spend_cny"], expected)
        self.assertAlmostEqual(by_model[0]["spend_cny"], expected)
        self.assertAlmostEqual(by_agent[0]["spend_cny"], expected)

    def test_collector_uses_provider_usage_then_estimates_missing_usage(self):
        from server.db import repos
        from server.runtime.usage import create_completion

        exact = SimpleNamespace(
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=20,
                prompt_tokens_details=SimpleNamespace(cached_tokens=40),
                completion_tokens_details=SimpleNamespace(reasoning_tokens=5),
            ),
            choices=[SimpleNamespace(message=SimpleNamespace(content="done"))],
        )
        estimated = SimpleNamespace(
            usage=None,
            choices=[SimpleNamespace(message=SimpleNamespace(content="估算回复"))],
        )
        client = MagicMock()
        client.chat.completions.create.side_effect = [exact, estimated]
        request = {"model": "test-model", "messages": [{"role": "user", "content": "你好"}]}

        create_completion(client, profile={"id": "test-model", "provider": "openai"}, call_kind="agent_loop", run_id="run_exact", **request)
        create_completion(client, profile={"id": "test-model", "provider": "custom"}, call_kind="agent_loop", run_id="run_estimated", **request)

        exact_summary = repos.get_run_usage_summary("run_exact")
        estimated_summary = repos.get_run_usage_summary("run_estimated")
        self.assertEqual(exact_summary["input_tokens"], 100)
        self.assertEqual(exact_summary["cached_input_tokens"], 40)
        self.assertEqual(exact_summary["output_tokens"], 20)
        self.assertEqual(estimated_summary["usage_source"], "estimated")
        self.assertGreater(estimated_summary["total_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
