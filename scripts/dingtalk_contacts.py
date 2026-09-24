#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""钉钉通讯录成员罗列工具（供「拉日程时选择参会人」使用）。

用法:
    python3 scripts/dingtalk_contacts.py            # 列出所有部门 + 成员(markdown)
    python3 scripts/dingtalk_contacts.py --json     # 输出 JSON (供程序化调用)

凭证来源（按优先级）:
    1. 环境变量 DINGTALK_APP_KEY / DINGTALK_APP_SECRET
    2. 仓库根目录 .env 中的 DINGTALK_APP_KEY / DINGTALK_APP_SECRET

说明:
    - 需要应用开通通讯录权限: qyapi_get_department_list, qyapi_get_department_member
      （未开通时接口会返回 errcode=88 / sub_code=60011，脚本会给出申请链接）
    - 本脚本不包含任何密钥硬编码。
"""
import json
import os
import sys
import urllib.request
import urllib.parse

API_BASE = "https://oapi.dingtalk.com"


def load_credentials():
    """按优先级加载凭证：环境变量 > 仓库 .env。"""
    key = os.environ.get("DINGTALK_APP_KEY")
    secret = os.environ.get("DINGTALK_APP_SECRET")
    if key and secret:
        return key, secret
    # 从仓库根 .env 读取
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k == "DINGTALK_APP_KEY" and not key:
                    key = v
                elif k == "DINGTALK_APP_SECRET" and not secret:
                    secret = v
    if not key or not secret:
        sys.exit("缺少凭证：请设置环境变量 DINGTALK_APP_KEY / DINGTALK_APP_SECRET，或在 .env 中配置。")
    return key, secret


def get_access_token(app_key, app_secret):
    url = f"{API_BASE}/gettoken?appkey={urllib.parse.quote(app_key)}&appsecret={urllib.parse.quote(app_secret)}"
    with urllib.request.urlopen(url, timeout=20) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if data.get("errcode") != 0:
        sys.exit(f"获取 access_token 失败: {data}")
    return data["access_token"]


def api_post(token, path, body):
    url = f"{API_BASE}{path}?access_token={token}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def permission_error_hint(data, app_key):
    sub_code = data.get("sub_code", "")
    if sub_code == "60011":
        scope = data.get("sub_msg", "")
        # 提取形如 [qyapi_get_department_list] 的权限名
        start = scope.find("[")
        end = scope.find("]")
        if start != -1 and end != -1:
            perm = scope[start + 1:end]
            url = f"https://open-dev.dingtalk.com/appscope/apply?content={app_key}%23{perm}"
            return f"应用缺少权限 {perm}，请管理员开通：{url}"
    return f"接口调用失败: {data}"


def list_departments(token, app_key):
    """递归列出全部部门，返回 [(dept_id, name, parent_id)]"""
    depts = []

    def walk(dept_id):
        data = api_post(token, "/topapi/v2/department/listsub", {"dept_id": dept_id})
        if data.get("errcode") != 0:
            raise RuntimeError(permission_error_hint(data, app_key))
        for d in data.get("result", []) or []:
            depts.append((d["dept_id"], d.get("name", ""), dept_id))
            walk(d["dept_id"])

    walk(1)
    return depts


def list_users(token, dept_id, app_key, page_size=100):
    """分页获取某部门下所有成员。"""
    users = []
    cursor = 0
    while True:
        data = api_post(token, "/topapi/v2/user/list",
                        {"dept_id": dept_id, "cursor": cursor, "size": page_size})
        if data.get("errcode") != 0:
            raise RuntimeError(permission_error_hint(data, app_key))
        result = data.get("result") or {}
        for u in result.get("list") or []:
            users.append({
                "userid": u.get("userid"),
                "name": u.get("name"),
                "mobile": u.get("mobile"),
                "position": u.get("position", ""),
                "dept_id": dept_id,
            })
        if result.get("has_more"):
            cursor = result.get("next_cursor", cursor + page_size)
        else:
            break
    return users


def main():
    as_json = "--json" in sys.argv
    app_key, app_secret = load_credentials()
    token = get_access_token(app_key, app_secret)
    try:
        depts = list_departments(token, app_key)
    except RuntimeError as e:
        sys.exit(str(e))

    dept_name = {d_id: name for d_id, name, _ in depts}
    dept_name[1] = "根部门"

    all_users = []
    for dept_id, name, _ in depts:
        try:
            all_users.extend(list_users(token, dept_id, app_key))
        except RuntimeError as e:
            sys.exit(str(e))

    # 去重（同一人可能出现在多个部门）
    seen = {}
    for u in all_users:
        seen.setdefault(u["userid"], u)

    if as_json:
        print(json.dumps({
            "departments": [{"dept_id": d, "name": n, "parent_id": p} for d, n, p in depts],
            "users": list(seen.values()),
        }, ensure_ascii=False, indent=2))
        return

    print(f"## 钉钉通讯录（共 {len(seen)} 人 / {len(depts)} 个部门）\n")
    print("| # | 姓名 | userId | 手机号 | 部门 |")
    print("|---|------|--------|--------|------|")
    for i, u in enumerate(sorted(seen.values(), key=lambda x: x["name"] or ""), 1):
        dept = dept_name.get(u["dept_id"], "")
        print(f"| {i} | {u['name'] or '-'} | `{u['userid'] or '-'}` | {u.get('mobile') or '-'} | {dept} |")


if __name__ == "__main__":
    main()
