---
name: dingtalk-calendar-flow
description: 钉钉日程创建标准流程。当用户要求创建/安排/「拉」钉钉日程、添加参会人、选择会议成员时使用。流程为：先用通讯录 MCP 罗列/搜索成员 → 用提问卡让用户确认参会人 → 再调用日历 MCP 创建日程。避免在不知道 userId 的情况下直接创建或瞎猜。
---

# 钉钉日程创建流程

## 工具速查

| 用途 | 工具 |
|------|------|
| 罗列/搜索通讯录成员 | `mcp__dingtalk_contacts__list_dingtalk_contacts` / `mcp__dingtalk_contacts__search_dingtalk_contacts` |
| 创建日程（含参会人） | `mcp__dingtalk_calendar__create_calendar_event` |
| 给已有日程加参会人 | `mcp__dingtalk_calendar__add_calendar_participant` |

## 标准流程（创建日程时必走）

1. **罗列通讯录**：调用 `mcp__dingtalk_contacts__list_dingtalk_contacts`（不传参 = 全量；传 `keyword` 可按姓名/手机号过滤，如「伍」「天文」）。用户给了名字但人很多时，先用 `search_dingtalk_contacts` 精确找人。
2. **确认参会人**：把工具返回的「姓名 + userId」整理给用户，用 `ask_user_question` 让用户勾选要拉进日程的人（选项中带 userId，便于下一步直接使用）。
3. **创建日程**：调用 `mcp__dingtalk_calendar__create_calendar_event`，`attendees` 传用户确认的 userId 列表；时间按用户要求转成 ISO-8601 带时区格式（如 `2026-08-05T16:00:00+08:00`，时区 `Asia/Shanghai`）。
4. **收尾**：创建成功后在回复中列出日程标题、时间、参会人姓名与日程 ID；需要提醒/会议室时按用户要求补充。

## 常见问题

- **通讯录工具返回权限错误**（`qyapi_get_department_list` / `qyapi_get_department_member` 60011）：这是应用未开通通讯录权限，请用户/管理员在钉钉开发者后台开通，工具返回里自带申请链接（`https://open-dev.dingtalk.com/appscope/apply?content=<appkey>%23<权限名>`）。开通前不要强行创建日程或猜 userId。
- **userId 与姓名对不上**：以通讯录工具返回的 `userid` 为准，不要凭手机号或昵称猜。
- **参会人已在日程中**：组织者（当前账号本人）不需要重复添加；`add_calendar_participant` 返回失败时检查是否已是参会人。
- **凭证**：读环境变量或仓库根 `.env` 的 `DINGTALK_APP_KEY` / `DINGTALK_APP_SECRET`，不要把密钥写进代码或回复里。
