from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from server.runtime.task_intent import (
    classify_task_intent_with_llm,
    heuristic_intent,
    should_activate_goal,
)


class TaskIntentSemanticTests(unittest.TestCase):
    def _client_for(self, content: str) -> MagicMock:
        client = MagicMock()
        client.with_options.return_value.chat.completions.create.return_value = (
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])
        )
        return client

    def test_keyword_compatibility_hook_never_classifies(self):
        for text in (
            "给钉钉日程测试群发消息",
            "测试一下这个函数并修复失败测试",
            "批量迁移 30 个项目，逐个验证并汇总",
        ):
            self.assertIsNone(heuristic_intent(text))

    def test_llm_one_shot_is_authoritative(self):
        client = self._client_for(
            '{"intent":"one_shot_action","confidence":0.91,'
            '"durable_signals":[],"reason":"one bounded external action",'
            '"goal_relationship":"none"}'
        )
        result = classify_task_intent_with_llm(
            "私聊联系人提醒会议快开始了",
            {"id": "mock-model"},
            lambda _profile: client,
        )
        self.assertEqual(result.intent, "one_shot_action")
        self.assertTrue(result.classified)
        self.assertTrue(result.authoritative)
        self.assertFalse(should_activate_goal(result))

    def test_llm_durable_with_semantic_facts_can_activate(self):
        client = self._client_for(
            '{"intent":"durable_goal","confidence":0.9,'
            '"durable_signals":["eight independently scored cases",'
            '"report depends on completed verification"],'
            '"reason":"multiple verifiable work items and a final aggregate",'
            '"goal_relationship":"none"}'
        )
        result = classify_task_intent_with_llm(
            "重跑未通过用例，完成全部评分并输出报告",
            {"id": "mock-model"},
            lambda _profile: client,
        )
        self.assertEqual(result.intent, "durable_goal")
        self.assertTrue(result.classified)
        self.assertTrue(should_activate_goal(result))

    def test_invalid_classifier_response_does_not_downgrade_to_one_shot(self):
        client = self._client_for("not json")
        result = classify_task_intent_with_llm(
            "重跑未通过用例，完成全部评分并输出报告",
            {"id": "mock-model"},
            lambda _profile: client,
        )
        self.assertFalse(result.classified)
        self.assertEqual(result.intent, "read_only")
        self.assertEqual(result.reason, "任务类型识别中")
        self.assertIn("invalid JSON", result.classification_error)

    def test_durable_without_semantic_facts_is_unavailable_not_one_shot(self):
        client = self._client_for(
            '{"intent":"durable_goal","confidence":0.9,'
            '"durable_signals":[],"reason":"long",'
            '"goal_relationship":"none"}'
        )
        result = classify_task_intent_with_llm(
            "重跑未通过用例，完成全部评分并输出报告",
            {"id": "mock-model"},
            lambda _profile: client,
        )
        self.assertFalse(result.classified)
        self.assertNotEqual(result.intent, "one_shot_action")
        self.assertIn("missing semantic", result.classification_error)


if __name__ == "__main__":
    unittest.main()
