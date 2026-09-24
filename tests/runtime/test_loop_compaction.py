import copy
from types import SimpleNamespace

import pytest

from server.runtime.executor import ToolExecutor
from server.runtime.loop import run_tool_loop
from server.runtime.session_compaction import compact_history


@pytest.mark.parametrize("resuming,rich", [(False, False), (False, True), (True, False)])
def test_loop_refreshes_only_history_keeps_batch_and_current_input(tmp_path, monkeypatch, resuming, rich):
    history = []
    for i in range(12):
        history += [{"role": "user", "content": f"request {i}"}, {"role": "assistant", "content": f"done {i}"}]
    history.append({"role": "user", "content": "current input"})
    state = SimpleNamespace(messages=history, compact_summary="", context={})
    api = [{"role": "system", "content": "system instructions"}] + copy.deepcopy(history)
    if rich:
        api[-1]["content"] = [{"type": "text", "text": "current input"},
                              {"type": "image_url", "image_url": {"url": "data:image/png;base64,test"}}]
    suffix = []
    if resuming:
        state.context["pending_question_continuation"] = {"question_id": "question_1"}
        state.messages.append({"role": "user", "content": "address answer", "question_id": "question_1"})
        suffix = [{"role": "assistant", "content": "", "tool_calls": [{"id": "question_call", "type": "function", "function": {"name": "ask_user_question", "arguments": "{}"}}]},
                  {"role": "tool", "tool_call_id": "question_call", "content": "address answer"}]
        api.extend(copy.deepcopy(suffix))
    calls = []
    def completion(*args, **kwargs):
        calls.append(copy.deepcopy(kwargs["messages"]))
        tools = []
        if len(calls) == 1:
            tools = [SimpleNamespace(id="compact_call", function=SimpleNamespace(name="compact", arguments="{}")),
                     SimpleNamespace(id="todo_call", function=SimpleNamespace(name="todo_write", arguments='{"todos":[]}'))]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="" if tools else "completed", tool_calls=tools), finish_reason="tool_calls" if tools else "stop")])
    monkeypatch.setattr("server.runtime.usage.create_completion", completion)
    monkeypatch.setattr("server.runtime.feature_flags.flag_enabled", lambda _: False)
    def compact():
        result = compact_history(state.messages, state.compact_summary)
        assert result["changed"]
        state.messages, state.compact_summary = result["messages"], result["summary"]
        return "Compacted"
    executor = ToolExecutor(cwd=tmp_path, safe_handlers={"compact": compact, "todo_write": lambda todos: "updated"})
    result = run_tool_loop(client=object(), model="test", messages=api, schemas=[], executor=executor,
        max_tokens=100, max_rounds=3, session_state=state)
    assert result["reply"] == "completed"
    assert len(calls) == 2
    updated = calls[1]
    assert sum(m.get("content") == state.compact_summary for m in updated) == 1
    boundary = result["history_prefix_end"]
    assert updated[boundary:boundary + len(suffix)] == suffix
    batch = updated[boundary + len(suffix):]
    assert [m["tool_call_id"] for m in batch if m["role"] == "tool"] == ["compact_call", "todo_call"]
    assert result["input_suffix_count"] == len(suffix)
    assert updated[0] == api[0]
    if rich:
        assert next(m for m in reversed(updated[:boundary]) if m["role"] == "user")["content"] == api[-1]["content"]
    if resuming:
        assert not any(m.get("role") == "user" and m.get("content") == "address answer" for m in updated)
