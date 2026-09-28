from __future__ import annotations

import unittest
from types import SimpleNamespace

from server.runtime.bash_semantic import decide_bash_semantics


class BashSemanticTests(unittest.TestCase):
    def test_restricted_semantic_reviewer_requires_closed_json_contract(self):
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kwargs: SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content='{"choice":"deny","confidence":0.93}'))]
                    )
                )
            )
        )
        result = decide_bash_semantics(client, "test", {"unknowns": ["DYNAMIC_SHELL_SYNTAX"]})
        self.assertEqual(result, ("deny", 0.93))

    def test_invalid_semantic_contract_does_not_produce_a_decision(self):
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=lambda **kwargs: SimpleNamespace(
                        choices=[SimpleNamespace(message=SimpleNamespace(content='{"choice":"execute","confidence":2}'))]
                    )
                )
            )
        )
        self.assertIsNone(decide_bash_semantics(client, "test", {}))

