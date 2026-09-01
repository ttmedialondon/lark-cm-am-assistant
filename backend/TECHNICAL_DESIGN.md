# Lark CM/AM Assistant：当前实现说明

入口：`https://larkbot.ttapp.help/`

## 当前范围

- 每位 CM/AM 以自己的 Lark OAuth user access token 授权；系统只读取该用户原本可见、且 Lark API 实际返回的会话与历史消息。
- 群聊和私聊使用同一套 `chats`、`authorization_chats`、`chat_messages` 与同步队列。群聊成员人数仅保存 Lark 返回的总数；私聊显示为无成员数。
- OAuth 成功后回填 Lark API 可返回的历史，并每 5 分钟增量同步、回看 10 分钟以避免边界漏记。
- 不发送 Lark 消息，不运行 AI，不保存附件二进制或成员名单。

`GET /im/v1/chats?types=p2p` 的用户身份支持在当前租户已做 metadata-only 预检；因公开网页文档仍有滞后描述，Worker 将 P2P 发现状态单独记为 `succeeded`、`unavailable` 或 `attention_required`。P2P 不可用不会阻塞群聊同步。

## 授权与正文边界

旧版本 OAuth 明确承诺只做统计，因此旧授权不会被静默升级：

1. `body_consent_at` 为空的既有用户继续同步原有群聊统计，不读取私聊，也不保存正文。
2. 用户在更新后的授权页重新 OAuth 后，服务设置 `body_consent_at`、重新开始会话回填，并收集正文。
3. 只有在新版授权时间之后重新观察到的消息，才会在该 CM/AM 的管理台筛选范围内显示正文。

消息正文存入公司级去重表 `chat_messages.body_ciphertext`，使用从服务器主密钥派生的独立 AES-256-GCM 加密域。共享群的正文只存一份；`authorization_message_observations` 决定某一 CM/AM 是否可在自己的授权范围中看到该消息。令牌、密钥、正文都不写入普通日志。

## 页面口径

个人仪表盘展示：

| 项目 | 口径 |
|---|---|
| 已加入群聊 / 私聊 | 当前可见、`chat_status=normal` 的会话数。 |
| 已同步消息总数 | 当前可见会话中 Lark 返回、未撤回的唯一消息 ID。 |
| 我发送的消息数 | 发送者的 `open_id` 与当前 OAuth 用户一致。 |
| 最近一次同步 | 某个当前可见会话最后完成回填或增量同步的时间。 |

`/admin` 展示公司级统计、人员、群聊和同步任务。`/admin/messages` 仅数据库 `admin_roles` 中的 `owner` 可访问：可用 CM/AM、会话类型和会话下拉筛选，展示最近 100 条已加密存储的正文。每次内容页查看写入 `audit_events`，但审计记录不含消息正文。

## 同步模型

1. OAuth 成功后创建一个 `backfill` job。
2. Worker 使用稳定创建时间顺序分页发现会话（每页 100）并读取消息（每页 50）。
3. 一页中的 canonical 消息、用户观察关系与游标在同一 PostgreSQL 事务写入，重启可安全续跑。
4. 首次回填完成后，每 300 秒创建增量 job；每次重叠 600 秒，重复消息按 `(chat_id, message_id)` 去重。
5. “同步全部历史”只为当前用户—会话关系写入请求标记；同一用户仍只有一个活动 job，旧 worker 不能覆盖新游标。

## 运行组件

```text
浏览器
  │ OAuth + HTTPS
  ▼
Caddy → FastAPI + Jinja2/HTMX/Tabler → PostgreSQL
                       ▲
                systemd Python worker → Lark Open API
```

| 部件 | 值 |
|---|---|
| Web 服务 | `lark-message-stats-web.service` |
| Worker | `lark-message-stats-worker.service` |
| Web 本机端口 | `127.0.0.1:8100` |
| 运行配置 | `/etc/lark-message-stats/app.env`，`root:larkstats` / `0640` |

实际 OAuth scopes 保持当前已验证的最小运行集：`offline_access`、`im:chat:read`、`im:message:readonly`。群成员总数需要时可额外加入 `im:chat.members:read`。

参考：

- [Lark OAuth 授权](https://open.larksuite.com/document/common-capabilities/sso/api/obtain-oauth-code)
- [Lark user access token](https://open.larksuite.com/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/get-user-access-token)
- [Lark 会话历史消息](https://open.larksuite.com/document/server-docs/im-v1/message/list)
- [Lark IM CLI 的 chat list 参数说明](https://github.com/larksuite/cli/blob/main/skills/lark-im/references/lark-im-chat-list.md)
