import copy
from types import SimpleNamespace

import pytest

from server.runtime.session_compaction import SUMMARY_HEADER, compact_history
from server.runtime.tools.session_ops import compact
from session_store import SessionManager


def history(count=12):
    messages = []
    for i in range(count):
        messages += [{"role": "user", "content": f"User request {i}", "source_id": f"msg_{i}"},
                     {"role": "assistant", "content": f"Completed item {i}."}]
    return messages


def test_complete_user_answer_url_and_assistant_role():
    messages = history()
    address = "https://example.com/api/" + "a" * 400
    answer = "用户限制：不可发布。\n" + "完整要求" * 80 + "\n接口文档 " + address
    messages[6].update(content=answer, question_id="question_1234")
    result = compact_history(messages)
    assert result["changed"]
    assert answer in result["summary"]
    assert address in result["summary"]
    node = result["messages"][4]
    assert node["role"] == "assistant" and node["content"] == result["summary"]
    assert "question_1234" in node["content"]
    assert result["messages"][:4] == messages[:4]
    assert result["messages"][-12:] == messages[-12:]


def test_closed_tool_chain_stays_in_complete_turn():
    messages = history()
    messages[17:17] = [{"role": "assistant", "content": "", "tool_calls": [{"id": "call_1"}]},
                       {"role": "tool", "tool_call_id": "call_1", "content": "https://example.com/tool"}]
    result = compact_history(messages)
    assert result["changed"]
    assert result["messages"][-14:] == messages[-14:]


@pytest.mark.parametrize("records", [
    [{"role": "assistant", "tool_calls": [{"id": "call_open"}], "content": ""}],
    [{"role": "tool", "tool_call_id": "orphan", "content": "result"}],
    [{"role": "assistant", "tool_calls": [{"id": "cross"}], "content": ""}, {"role": "user", "content": "new turn"}],
])
def test_open_or_invalid_tool_chain_rejects_without_mutation(records):
    messages = history() + records
    before = copy.deepcopy(messages)
    result = compact_history(messages)
    assert not result["changed"]
    assert messages == before


def test_over_budget_keeps_original_protected_message():
    messages = history()
    messages[6]["content"] = "完整约束不得截断" * 3000
    before = copy.deepcopy(messages)
    result = compact_history(messages, max_summary_tokens=200)
    assert not result["changed"] and "protected" in result["reason"]
    assert messages == before


def test_repeat_compaction_keeps_one_summary_without_nested_header():
    first = compact_history(history())
    second_messages = first["messages"] + history(4)
    second = compact_history(second_messages, first["summary"])
    assert second["changed"]
    assert second["summary"].count(SUMMARY_HEADER) == 1
    assert sum(m.get("conversation_summary", False) for m in second["messages"]) == 1
    assert first["summary"].removeprefix(SUMMARY_HEADER).strip() in second["summary"]
    assert "User request 2" in second["summary"]


def test_legacy_saved_summary_is_not_truncated_again():
    saved = "Original full historical answer: " + "约束" * 800 + " https://example.com/late"
    messages = history()
    messages.insert(4, {"role": "user", "content": "[Compacted 22 messages]\n\n" + saved[:1500]})
    result = compact_history(messages, saved)
    assert result["changed"]
    assert saved in result["summary"]
    assert "[Compacted 22 messages]" not in result["summary"]


def test_summarizer_receives_no_user_and_cannot_drop_resource():
    messages = history()
    messages[7]["content"] = "工具确认资源 /workspace/docs/api.md 和 https://example.com/result 任务 task_123456"
    def summarizer(records):
        assert all(m["role"] != "user" for m in records)
        return "Assistant completed checks."
    result = compact_history(messages, summarizer=summarizer)
    assert result["changed"]
    for fact in ("/workspace/docs/api.md", "https://example.com/result", "task_123456", "User request 3"):
        assert fact in result["summary"]


def test_summary_failure_and_mismatch_preserve_original():
    messages = history()
    result = compact_history(messages, summarizer=lambda _: "")
    assert not result["changed"]
    messages.insert(4, {"role": "assistant", "content": "different", "conversation_summary": True})
    assert not compact_history(messages, "saved")["changed"]


def test_tool_persists_full_summary_for_next_turn(tmp_path):
    manager = SessionManager(tmp_path, {"session": {"persist_dir": ".sessions"}})
    state = manager.create_session("assistant_dm", "assistant:test")
    state.messages = history()
    ctx = SimpleNamespace(session_state=state, session_manager=manager)
    assert compact(ctx).startswith("Compacted")
    restored = manager.load(state.meta.session_id)
    node = next(m for m in restored.messages if m.get("conversation_summary"))
    assert node["content"] == restored.compact_summary
    assert len(node["content"]) > 0


def test_tool_save_failure_restores_in_memory_history():
    messages = history()
    state = SimpleNamespace(messages=messages, compact_summary="", version=1)
    def fail(*args, **kwargs):
        raise OSError("disk full")
    ctx = SimpleNamespace(session_state=state, session_manager=SimpleNamespace(save=fail))
    with pytest.raises(OSError):
        compact(ctx)
    assert state.messages is messages and state.compact_summary == ""
