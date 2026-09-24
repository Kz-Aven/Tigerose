import json
import sqlite3
import threading
import time

from memory_core import estimate_tokens, make_summary, relevance_score
from server.db import memory_store as store
from server.runtime.memory_recall import recall
from test_memory_store import database


def wire(monkeypatch, path):
    def connect():
        conn = sqlite3.connect(path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    monkeypatch.setattr("server.db.repos.get_connection", connect)


def test_old_chinese_memory_is_recalled(database, monkeypatch):
    conn, path = database
    for i in range(35):
        store.create(conn, "a", f"办公桌颜色偏好记录{i}")
    conn.commit()
    wire(monkeypatch, path)
    result = recall("a", "接口地址在哪里")
    assert [m["memory_id"] for m in result["items"]] == ["old"]
    assert recall("a", "午餐餐厅推荐")["items"] == []


def test_selector_index_excludes_body_and_foreign_ids(database, monkeypatch):
    conn, path = database
    other = store.create(conn, "b", "another owner")
    conn.commit()
    wire(monkeypatch, path)
    def selector(query, index):
        assert all("body" not in row for row in index)
        return [other["memory_id"], "old", "old", "nonexistent"]
    assert [m["memory_id"] for m in recall("a", "接口", selector=selector)["items"]] == ["old"]
    assert recall("a", "接口", selector=lambda q, i: [])["items"] == []


def test_selector_mutation_is_rechecked(database, monkeypatch):
    conn, path = database
    wire(monkeypatch, path)
    def selector(query, index):
        other = sqlite3.connect(path)
        other.row_factory = sqlite3.Row
        try:
            with other:
                store.update(other, "a", "old", body="已经更改", expected_version=1)
        finally:
            other.close()
        return ["old"]
    assert recall("a", "接口", selector=selector)["items"] == []


def test_timeout_fallback_is_bounded(database, monkeypatch):
    _, path = database
    wire(monkeypatch, path)
    release = threading.Event()
    def selector(query, index):
        release.wait(3)
        return []
    start = time.monotonic()
    try:
        result = recall("a", "接口地址", selector=selector)
    finally:
        release.set()
    assert time.monotonic() - start < 2
    assert result["audit"]["selection_status"] == "degraded"
    assert result["items"][0]["memory_id"] == "old"


def test_budget_uses_summary_without_cutting_urls(database, monkeypatch):
    conn, path = database
    long = store.create(conn, "a", "接口参考 " + "内容" * 8000 + " https://example.com/api", summary="接口参考 https://example.com/api")
    conn.commit()
    wire(monkeypatch, path)
    result = recall("a", "接口", selector=lambda q, i: [long["memory_id"]])
    assert result["items"][0]["summary_only"]
    assert result["items"][0]["body"].endswith("https://example.com/api")
    assert sum(estimate_tokens(json.dumps(m, ensure_ascii=False)) for m in result["items"]) <= 2000


def test_chinese_terms_and_summary_url_boundaries():
    assert relevance_score("供应商接口地址", "供应商接口说明") > 0
    assert relevance_score("接口", "会议安排") == 0
    summary = make_summary("见 https://example.com/" + "a" * 300)
    assert "https://" not in summary
