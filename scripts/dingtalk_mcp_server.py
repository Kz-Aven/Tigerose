#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""钉钉通讯录 MCP Server（stdio / NDJSON framing）。

暴露工具（供 agent 在「拉日程 / 创建日程」时罗列通讯录成员供用户选择）：

    mcp__dingtalk_contacts__list_dingtalk_contacts
        罗列通讯录成员；可选 keyword（姓名/手机号模糊过滤）、dept_id（部门过滤）。
    mcp__dingtalk_contacts__search_dingtalk_contacts
        按关键字搜索通讯录成员（姓名/手机号）。

协议：MCP 2024-11-05，newline-delimited JSON（与 server/runtime/mcp.py 的
StdioMCPClient 兼容）。零第三方依赖，仅用标准库。

凭证：DINGTALK_APP_KEY / DINGTALK_APP_SECRET（环境变量 > 仓库根 .env），
不硬编码任何密钥。
"""

from __future__ import annotations

import json
import os
import sys
import time

# 复用同目录 dingtalk_contacts.py 的凭证 / token / 部门 / 成员逻辑
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

import dingtalk_contacts as dc  # noqa: E402

SERVER_NAME = "dingtalk_contacts"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "list_dingtalk_contacts",
        "description": (
            "罗列钉钉企业通讯录成员（姓名/userId/手机号/部门），供创建日程选择参会人时使用。"
            "可选 keyword 按姓名或手机号模糊过滤，dept_id 按部门过滤。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "可选，按姓名或手机号模糊过滤（如「伍」「天文」「138」）",
                },
                "dept_id": {
                    "type": "integer",
                    "description": "可选，部门 ID；不传则列出全部部门成员",
                },
                "format": {
                    "type": "string",
                    "enum": ["markdown", "json"],
                    "description": "输出格式，默认 markdown（json 供程序化使用）",
                },
            },
        },
    },
    {
        "name": "search_dingtalk_contacts",
        "description": (
            "按关键字（姓名/手机号）搜索钉钉通讯录成员，返回匹配列表。"
            "创建日程需要 userId 时先用本工具确认成员。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键字，姓名或手机号（如「黄东生」「伍天文」）",
                }
            },
            "required": ["keyword"],
        },
    },
]


def _json_result(text: str, *, is_error: bool = False) -> dict:
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
    }


_CACHE_TTL_SECONDS = 60
_contacts_cache: dict = {"ts": 0.0, "depts": None, "dept_name": None, "users": None}


def _load_contacts():
    """返回 (depts, users)；权限错误时抛 RuntimeError(可读提示)。带 60s 进程内缓存，
    避免每次调用都全量遍历所有部门（拉日程罗列成员 / 搜索时显著提速）。"""
    now = time.time()
    if _contacts_cache["users"] is not None and now - _contacts_cache["ts"] < _CACHE_TTL_SECONDS:
        return (
            _contacts_cache["depts"],
            _contacts_cache["dept_name"],
            _contacts_cache["users"],
        )
    app_key, app_secret = dc.load_credentials()
    token = dc.get_access_token(app_key, app_secret)
    depts = dc.list_departments(token, app_key)
    dept_name = {d_id: name for d_id, name, _ in depts}
    dept_name[1] = "根部门"
    all_users = []
    for dept_id, name, _ in depts:
        all_users.extend(dc.list_users(token, dept_id, app_key))
    seen: dict = {}
    for u in all_users:
        seen.setdefault(u["userid"], u)
    users = list(seen.values())
    _contacts_cache.update(ts=now, depts=depts, dept_name=dept_name, users=users)
    return depts, dept_name, users


def _filter_users(users, keyword: str | None, dept_id: int | None):
    out = users
    if dept_id is not None:
        out = [u for u in out if u.get("dept_id") == dept_id]
    if keyword:
        kw = keyword.strip().lower()
        out = [
            u
            for u in out
            if kw in (u.get("name") or "").lower() or kw in (u.get("mobile") or "")
        ]
    return out


def _markdown(users, dept_name) -> str:
    if not users:
        return "（未找到匹配的通讯录成员）"
    lines = [f"## 钉钉通讯录（共 {len(users)} 人）\n", "| # | 姓名 | userId | 手机号 | 部门 |", "|---|------|--------|--------|------|"]
    for i, u in enumerate(sorted(users, key=lambda x: x["name"] or ""), 1):
        lines.append(
            f"| {i} | {u['name'] or '-'} | `{u['userid'] or '-'}` | "
            f"{u.get('mobile') or '-'} | {dept_name.get(u.get('dept_id'), '')} |"
        )
    return "\n".join(lines)


def _handle_call(name: str, arguments: dict) -> dict:
    args = arguments or {}
    try:
        depts, dept_name, users = _load_contacts()
        if name == "search_dingtalk_contacts":
            kw = str(args.get("keyword") or "").strip()
            if not kw:
                return _json_result("缺少必填参数 keyword", is_error=True)
            matched = _filter_users(users, kw, None)
            fmt = "markdown"
        else:  # list_dingtalk_contacts
            kw = args.get("keyword") or None
            dept_id = args.get("dept_id")
            if dept_id is not None:
                dept_id = int(dept_id)
            matched = _filter_users(users, kw, dept_id)
            fmt = str(args.get("format") or "markdown")

        if fmt == "json":
            return _json_result(
                json.dumps(
                    {
                        "departments": [
                            {"dept_id": d, "name": n, "parent_id": p} for d, n, p in depts
                        ],
                        "users": matched,
                    },
                    ensure_ascii=False,
                )
            )
        return _json_result(_markdown(matched, dept_name))
    except SystemExit as exc:
        return _json_result(str(exc), is_error=True)
    except RuntimeError as exc:
        return _json_result(str(exc), is_error=True)
    except Exception as exc:  # noqa: BLE001
        return _json_result(f"钉钉通讯录查询失败: {exc}", is_error=True)


def _handle(method: str, params: dict, msg_id) -> dict | None:
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "notifications/initialized":
        return None  # 通知无需响应
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        name = (params or {}).get("name", "")
        arguments = (params or {}).get("arguments") or {}
        result = _handle_call(name, arguments)
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"resources": []}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": {}}  # 未知方法容忍


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        resp = _handle(method, msg.get("params") or {}, msg_id)
        if resp is not None:
            print(json.dumps(resp, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
