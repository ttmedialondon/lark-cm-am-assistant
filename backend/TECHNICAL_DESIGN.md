# Lark CM/AM Assistant：当前实现说明

入口：`https://larkbot.ttapp.help/`

## 当前范围

- 每位 CM/AM 以自己的 Lark OAuth user access token 授权；系统只读取该用户原本可见、且 Lark API 实际返回的会话与历史消息。
- 群聊和私聊使用同一套 `chats`、`authorization_chats`、`chat_messages` 与同步队列。每条消息保存 Lark 返回的发送者 ID、ID 类型、`open_id`（适用时）、`tenant_key`、发送者类型、回复链/话题/`@` 关系及可用的 Lark 跳转定位字段。
- 群聊在成员数刷新时分页保存最新成员快照：`open_id`、名称、`tenant_key` 与成员类型；不把 API 未返回的人误判为已离群，也不保存成员历史版本。私聊的对端稳定 ID 保存在该授权用户的 `authorization_chats` 关系上。
- 联系人以稳定 Lark 身份聚合，`creator`、经纪/机构、品牌/商家等都是联系人角色。独立的联系人自动识别任务只使用当前授权范围内的消息、会话上下文、群名和 Lark 身份元数据构造受限证据包调用 Luna；仅达到 owner 配置阈值、引用可核验且无事实冲突的角色建议才能自动确认，其他建议进入人工队列。
- OAuth 成功后回填 Lark API 可返回的历史，并每 5 分钟增量同步、回看 10 分钟以避免边界漏记。
- 不发送 Lark 消息，不保存附件二进制；成员快照和消息发送者身份仅用于公司内部的后续归属与提醒链路。Luna 仅用于联系人自动识别，使用 `store: false`、严格 JSON Schema 和消息引用校验；具体口径见 [`contact-assessment-automatic-v0.2.md`](../docs/as-built/contact-assessment-automatic-v0.2.md)。

`GET /im/v1/chats?types=p2p` 的用户身份支持在当前租户已做 metadata-only 预检；因公开网页文档仍有滞后描述，Worker 将 P2P 发现状态单独记为 `succeeded`、`unavailable` 或 `attention_required`。P2P 不可用不会阻塞群聊同步。

## 授权与正文边界

旧版本 OAuth 明确承诺只做统计，因此旧授权不会被静默升级：

1. `body_consent_at` 为空的既有用户继续同步原有群聊统计，不读取私聊，也不保存正文。
2. 用户在更新后的授权页重新 OAuth 后，服务设置 `body_consent_at`、重新开始会话回填，并收集正文。
3. 只有在新版授权时间之后重新观察到的消息，才会在该 CM/AM 的管理台筛选范围内显示正文。

消息正文存入公司级去重表 `chat_messages.body_ciphertext`，使用从服务器主密钥派生的独立 AES-256-GCM 加密域。共享群的正文只存一份；`authorization_message_observations` 决定某一 CM/AM 是否可在自己的授权范围中看到该消息。令牌、密钥、正文都不写入普通日志。

## 页面口径

CM/AM 的个人页面是“消息提醒中心”：仅展示接入状态和最近检查时间，不展示会话名称、成员人数、消息数量或消息正文，也不提供手动历史回填。页面保留可折叠、明确的“权限与数据使用”说明：系统会在用户授权范围内读取群聊和私聊内容用于分析，正文会加密保存并由内部 owner 按该用户授权范围查看。

`/admin` 展示公司级统计、人员、群聊和同步任务。`/admin/messages` 仅数据库 `admin_roles` 中的 `owner` 可访问：可用 CM/AM、会话类型和会话下拉筛选，展示最近 100 条已加密存储的正文。发送者名称优先使用消息返回的名称和最近成员快照。`/admin/contacts` 可按已确认或未确认筛选；每次人工核对都从该联系人详情进入。`/admin/creators` 只展示已确认拥有 `creator` 角色的联系人，并链接到同一位外部联系人的详情。每次正文或 AI 证据页查看均写入 `audit_events`，但审计记录不含消息正文。

管理台的人员状态以一个主阶段展示：`正在发现会话`、`首次同步中`、`已接入 · 定时同步`、`定时更新中` 或需要处理。该主阶段由授权状态、可同步会话数、首次回填完成数、当前 job 和下一次计划时间派生，不会把“已发现私聊目录”“群成员快照”或“下一轮定时 job 已排程”误显示成消息同步失败。页面另列出群聊/私聊的正文回填进度；P2P 的目录发现与 P2P 正文回填始终分开。

## 同步模型

1. OAuth 成功后创建一个 `backfill` job。
2. Worker 使用稳定创建时间顺序分页发现会话（每页 100）并读取消息（每页 50）。
3. 一页中的 canonical 消息、用户观察关系与游标在同一 PostgreSQL 事务写入，重启可安全续跑。
4. 首次回填完成后，每 300 秒创建增量 job；每次重叠 600 秒，重复消息按 `(chat_id, message_id)` 去重。
5. 同一用户同一时刻最多一个 worker job；个人页面不创建手动同步任务。
6. 每次重新观察到消息都会更新可用的发送者 Lark 身份和会话上下文；联系人角色评估由独立的持久化队列按授权范围处理，不把单一消息、昵称、普通 `@` 提及或群成员人数直接写成角色事实。
7. `MAX_PAGES_PER_JOB` 到达时是正常分页让出：同一 job 会立刻续跑，但不增加 `attempts` 或写入 `last_error`。`attempts` 只记录实际的临时错误、租约过期或需要退避的重试。
8. Lark 返回 `chat_status` 非 `normal` 的会话（例如已归档/解散）仍保留发现事实，但不纳入回填进度，也不计划请求消息正文。`topic` 会话计入群聊并在 UI 标为“专题群”。

## 后续紧急提醒跳转

Lark 的消息模型可返回 `message_app_link`、`message_position` 与 `thread_message_position`。真正实现紧急提醒时，worker 应原样保存 `message_app_link`，提醒卡使用普通 HTTPS 链接“在 Lark 中打开”；不要用 `message_id` 猜 URL。无精确消息链接时，可用会话 fallback：`https://applink.larksuite.com/client/chat/open?openChatId=oc_xxx`。点击人仍必须拥有该会话权限。

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

实际 OAuth scopes 为：`offline_access`、`im:chat:read`、`im:message:readonly`、`im:chat.members:read`。最后一项用于群成员快照；消息的回复链、线程、`@` 提及随消息对象保存，本版本不需要为它们新增 Lark 权限。群详情仅在实际需要时用真实 user token 预检，明确缺 scope 后再申请。

参考：

- [Lark OAuth 授权](https://open.larksuite.com/document/common-capabilities/sso/api/obtain-oauth-code)
- [Lark user access token](https://open.larksuite.com/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/get-user-access-token)
- [Lark 会话历史消息](https://open.larksuite.com/document/server-docs/im-v1/message/list)
- [Lark IM CLI 的 chat list 参数说明](https://github.com/larksuite/cli/blob/main/skills/lark-im/references/lark-im-chat-list.md)
- [Lark SDK 的 Message AppLink 字段](https://pkg.go.dev/github.com/larksuite/oapi-sdk-go/v3/service/im/v1)
- [Lark CLI 的 AppLink 说明](https://github.com/larksuite/cli/blob/main/skills/lark-im/SKILL.md)
