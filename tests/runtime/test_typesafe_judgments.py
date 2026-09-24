from __future__ import annotations

import unittest
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from server.runtime.task_intent import (
    TaskIntentResult,
    classify_task_intent,
    should_activate_goal,
)
from server.runtime.goal import Evaluation
from server.runtime.typesafe_judgments import (
    TypeSafeUnavailable,
    classify_task_intent_with_typesafe,
    evaluate_goal_with_typesafe,
)


class FakeChoice:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeNoul:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.state = None
        self.questions = None

    def system_one(self, *, state, questions):
        self.state = state
        self.questions = questions
        return self.response


def response(*, choices: dict[str, tuple[str, float]], nouls: dict[str, float]):
    return SimpleNamespace(
        answers={
            key: SimpleNamespace(type="choice", choice=value, confidence=confidence)
            for key, (value, confidence) in choices.items()
        } | {
            key: SimpleNamespace(type="noul", noul=value) for key, value in nouls.items()
        },
    )


def factory(client):
    return lambda _timeout: (client, FakeChoice, FakeNoul)


class TypeSafeJudgmentTests(unittest.TestCase):
    def test_durable_intent_uses_valid_typesafe_result(self):
        client = FakeClient(
            response(
                choices={
                    "task_intent": ("durable_goal", 0.91),
                    "goal_relationship": ("none", 0.98),
                },
                nouls={
                    "has_independent_verifiable_work": 0.82,
                    "requires_iterative_delivery": 0.78,
                },
            )
        )

        result = classify_task_intent_with_typesafe(
            "逐个修复失败用例，验证后输出报告",
            client_factory=factory(client),
        )

        self.assertEqual(result.intent, "durable_goal")
        self.assertTrue(result.authoritative)
        self.assertTrue(result.classified)
        self.assertTrue(should_activate_goal(result))
        self.assertEqual(client.state["active_goal"], {"active": False})
        self.assertEqual(set(client.questions), {
            "task_intent",
            "goal_relationship",
            "has_independent_verifiable_work",
            "requires_iterative_delivery",
        })

    def test_low_confidence_is_valid_and_does_not_activate_goal(self):
        client = FakeClient(
            response(
                choices={
                    "task_intent": ("durable_goal", 0.42),
                    "goal_relationship": ("none", 0.99),
                },
                nouls={
                    "has_independent_verifiable_work": 0.95,
                    "requires_iterative_delivery": 0.95,
                },
            )
        )

        result = classify_task_intent_with_typesafe("完成多个任务", client_factory=factory(client))

        self.assertTrue(result.classified)
        self.assertTrue(result.authoritative)
        self.assertFalse(should_activate_goal(result))

    def test_invalid_typesafe_response_is_contract_failure(self):
        client = FakeClient(
            response(
                choices={
                    "task_intent": ("unknown", 0.91),
                    "goal_relationship": ("none", 0.99),
                },
                nouls={
                    "has_independent_verifiable_work": 0.8,
                    "requires_iterative_delivery": 0.8,
                },
            )
        )

        with self.assertRaisesRegex(TypeSafeUnavailable, "response_contract"):
            classify_task_intent_with_typesafe("test", client_factory=factory(client))

    def test_goal_completion_requires_all_typesafe_evidence_thresholds(self):
        client = FakeClient(
            response(
                choices={"completion_state": ("completed", 0.92)},
                nouls={
                    "claim_supported_by_evidence": 0.91,
                    "deliverable_or_verification_present": 0.89,
                },
            )
        )

        result = evaluate_goal_with_typesafe(
            {"condition": "完成报告", "final_claim": "报告已完成", "evidence": []},
            client_factory=factory(client),
        )

        self.assertTrue(result.completed)
        self.assertEqual(result.model_profile_id, "typesafe:jev")

    def test_goal_low_confidence_blocks_without_error_or_fallback_signal(self):
        client = FakeClient(
            response(
                choices={"completion_state": ("completed", 0.45)},
                nouls={
                    "claim_supported_by_evidence": 0.91,
                    "deliverable_or_verification_present": 0.89,
                },
            )
        )

        result = evaluate_goal_with_typesafe(
            {"condition": "完成报告", "final_claim": "报告已完成", "evidence": []},
            client_factory=factory(client),
        )

        self.assertFalse(result.completed)
        self.assertIn("门槛不足", result.reason)
        self.assertEqual(result.missing_evidence, ["goal_completion_evidence"])

    def test_goal_state_is_redacted_and_bounded(self):
        client = FakeClient(
            response(
                choices={"completion_state": ("incomplete", 0.92)},
                nouls={
                    "claim_supported_by_evidence": 0.1,
                    "deliverable_or_verification_present": 0.1,
                },
            )
        )

        evaluate_goal_with_typesafe(
            {
                "condition": "finish " * 1000,
                "final_claim": "done " * 1000,
                "evidence": [{"api_key": "must-not-leak", "summary": "x" * 3000}] * 20,
                "recent_messages": [{"content": "y" * 3000}] * 8,
            },
            client_factory=factory(client),
        )

        serialized = json.dumps(client.state, ensure_ascii=False)
        self.assertNotIn("must-not-leak", serialized)
        self.assertLessEqual(len(serialized), 16000)

    def test_intent_wrapper_falls_back_only_on_typesafe_unavailable(self):
        expected = TaskIntentResult("one_shot_action", 0.9, authoritative=True)
        legacy = MagicMock(return_value=expected)
        with patch(
            "server.runtime.typesafe_judgments.classify_task_intent_with_typesafe",
            side_effect=TypeSafeUnavailable("timeout"),
        ), patch(
            "server.runtime.task_intent.classify_task_intent_with_llm",
            legacy,
        ):
            result = classify_task_intent("send reminder", {}, MagicMock())

        self.assertIs(result, expected)
        legacy.assert_called_once()

    def test_intent_wrapper_does_not_fallback_for_valid_low_confidence(self):
        valid = TaskIntentResult(
            "durable_goal",
            0.2,
            durable_signals=[],
            authoritative=True,
            classified=True,
        )
        legacy = MagicMock()
        with patch(
            "server.runtime.typesafe_judgments.classify_task_intent_with_typesafe",
            return_value=valid,
        ), patch(
            "server.runtime.task_intent.classify_task_intent_with_llm",
            legacy,
        ):
            result = classify_task_intent("do work", {}, MagicMock())

        self.assertIs(result, valid)
        legacy.assert_not_called()

    def test_disabled_flag_uses_legacy_without_importing_typesafe(self):
        expected = TaskIntentResult("read_only", 0.9, authoritative=True)
        legacy = MagicMock(return_value=expected)
        with patch("server.runtime.feature_flags.flag_enabled", return_value=False), patch(
            "server.runtime.task_intent.classify_task_intent_with_llm",
            legacy,
        ):
            result = classify_task_intent("explain this", {}, MagicMock())

        self.assertIs(result, expected)
        legacy.assert_called_once()

    def test_goal_factory_keeps_valid_low_confidence_typesafe_result(self):
        from server.runtime.turn import _build_goal_evaluator

        typesafe_result = Evaluation(
            False,
            "TypeSafe：完成判断或证据门槛不足",
            ["goal_completion_evidence"],
            "collect evidence",
            "typesafe:jev",
        )
        legacy_client = MagicMock()
        with patch("server.runtime.feature_flags.flag_enabled", return_value=True), patch(
            "server.runtime.turn._client_for_profile", return_value=legacy_client
        ), patch(
            "server.runtime.typesafe_judgments.evaluate_goal_with_typesafe",
            return_value=typesafe_result,
        ) as typesafe:
            evaluate = _build_goal_evaluator({"id": "legacy"}, {})
            result = evaluate({"condition": "finish"})

        self.assertIs(result, typesafe_result)
        typesafe.assert_called_once()
        legacy_client.with_options.assert_not_called()

    def test_goal_factory_falls_back_once_after_typesafe_failure(self):
        from server.runtime.turn import _build_goal_evaluator

        legacy_client = MagicMock()
        legacy_response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            '{"completed":false,"reason":"missing test",'
                            '"missing_evidence":["test"],"next_action":"run test"}'
                        )
                    ),
                    finish_reason="stop",
                )
            ]
        )
        with patch("server.runtime.feature_flags.flag_enabled", return_value=True), patch(
            "server.runtime.turn._client_for_profile", return_value=legacy_client
        ), patch(
            "server.runtime.typesafe_judgments.evaluate_goal_with_typesafe",
            side_effect=TypeSafeUnavailable("timeout"),
        ) as typesafe, patch(
            "server.runtime.usage.create_completion", return_value=legacy_response
        ) as legacy_call:
            evaluate = _build_goal_evaluator({"id": "legacy"}, {})
            result = evaluate({"condition": "finish"})

        self.assertFalse(result.completed)
        self.assertEqual(result.reason, "missing test")
        typesafe.assert_called_once()
        legacy_call.assert_called_once()


if __name__ == "__main__":
    unittest.main()
