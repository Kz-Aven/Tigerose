import os
import tempfile
from pathlib import Path


def test_assets_are_partitioned_redacted_and_queryable(monkeypatch):
    with tempfile.TemporaryDirectory() as raw:
        monkeypatch.setenv("TIGEROSE_HOME", raw)
        from avent_paths import reset_path_cache
        reset_path_cache()
        from server.agent_assets.store import assets_root, store
        from server.agent_assets import mcp_server

        first = store.start_execution(assistant_id="assistant_a", run_id="run_a", session_id="s", surface="dm", user_input={"token": "secret", "body": "visible"})
        store.finish_execution(assistant_id="assistant_a", run_id="run_a", status="completed", termination="completed", output="done")
        store.start_execution(assistant_id="assistant_b", run_id="run_b", session_id="s", surface="dm", user_input="two")
        assert (assets_root() / "assistants" / "assistant_a.db").is_file()
        assert (assets_root() / "assistants" / "assistant_b.db").is_file()
        assert len(mcp_server.list_executions({"assistant_ids": ["assistant_a", "assistant_b"], "limit": 10})) == 2
        execution = mcp_server.get_execution({"execution_id": first})["execution"]
        assert execution["status"] == "completed"
        assert "secret" not in mcp_server.read_content({"content_ref": execution["input_content_ref"]})
        try:
            mcp_server.readonly_sql({"assistant_id": "assistant_a", "sql": "DELETE FROM execution", "parameters": {}})
        except ValueError:
            pass
        else:
            raise AssertionError("write SQL was accepted")
        reset_path_cache()


def test_runtime_facade_writes_to_shared_agent_asset_expert_root(tmp_path, monkeypatch):
    monkeypatch.setenv("TIGEROSE_HOME", str(tmp_path / "tigerose"))
    monkeypatch.setenv("AGENT_ASSET_EXPERT_HOME", str(tmp_path / "shared-assets"))
    from avent_paths import reset_path_cache
    from agent_asset_expert.storage import AssetStore
    from server.agent_assets.recorder import ObservationRecorder

    reset_path_cache()
    recorder = ObservationRecorder()
    recorder.start_execution(
        assistant_id="analysis", run_id="run-1", session_id="session-1",
        surface="chat", user_input="summarize this",
    )
    recorder.snapshot(
        assistant_id="analysis", run_id="run-1", asset_type="agent",
        asset_id="analysis", value={"name": "数据分析阿喵"}, purpose="primary_agent",
    )
    recorder.record_llm(
        assistant_id="analysis", run_id="run-1", call_id="llm-1", call_kind="chat",
        profile={"id": "model"}, request={"messages": []}, response="plan",
        usage={"input_tokens": 12, "output_tokens": 8, "total_tokens": 20},
    )
    recorder.record_tool(
        assistant_id="analysis", run_id="run-1", call_id="tool-1", name="bash",
        args={"cmd": "pwd"}, result="ok", outcome="ok",
    )
    execution_id = recorder.finish_execution(
        assistant_id="analysis", run_id="run-1", status="completed",
        termination="normal_stop", output="done",
    )

    store = AssetStore()
    key, conn = store.locate_execution(execution_id)
    with conn:
        execution = conn.execute(
            "SELECT assistant_name,input_tokens,output_tokens,total_tokens FROM execution WHERE execution_id=?",
            (execution_id,),
        ).fetchone()
        spans = conn.execute("SELECT COUNT(*) FROM execution_span WHERE execution_id=?", (execution_id,)).fetchone()[0]
    assert key == "tigerose--analysis"
    assert execution == ("数据分析阿喵", 12, 8, 20)
    assert spans == 2
    assert not (tmp_path / "tigerose" / "data" / "agent-assets").exists()
    reset_path_cache()
