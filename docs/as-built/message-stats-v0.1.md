# Lark Message Stats：测试版技术说明

状态：已部署；Lark App Secret 已完成服务器端安全配置，等待真实 CM/AM 对当前 Python 版本完成 OAuth 与消息统计端到端验收。  
入口：`https://larkbot.ttapp.help/`

## 范围

- 仅群聊；不包含 P2P 单聊。
- 每个 CM/AM 通过 Lark OAuth 授权后，只统计该用户当前可见的群。
- 默认历史范围为 Lark API 可返回的全部历史（`SYNC_LOOKBACK_DAYS=0`）；可改为正整数限制最近天数。
- 不发送 Lark 消息，不运行 AI。
- 不保存消息正文、附件、提及、完整发送者资料。

## 展示口径

| 卡片 | 口径 |
|---|---|
| 已加入群聊 | 当前可见且 `chat_status=normal` 的群数量。 |
| 已同步消息总数 | Lark API 返回、未撤回、且属于当前可见群的唯一消息 ID 数。 |
| 我发送的消息数 | 发送者为 `user`、`id_type=open_id` 且 ID 与 OAuth 用户 `open_id` 一致的未撤回消息。 |
| 最近一次同步 | 某个可读群最后完成回填或增量同步的时间。 |

`message_ledger` 仅保存：`authorization_id`、`chat_id`、`message_id`、创建时间、本人发送布尔值与撤回布尔值。它用于分页断点后的去重和可重复统计，绝不保存 `content`。

## 服务组件

```text
浏览器 / Lark Web App
       │ OAuth + HTTPS
       ▼
Caddy → FastAPI Web（127.0.0.1:8100）
       │               │
       │               └─ Jinja2 + HTMX + Tabler 仪表盘
       ▼
PostgreSQL ← 独立 Python worker（systemd） ← Lark User Access Token
```

| 部件 | 名称 |
|---|---|
| 系统账户 | `larkstats` |
| 数据库 | `lark_message_stats` |
| Web 服务 | `lark-message-stats-web.service` |
| 同步服务 | `lark-message-stats-worker.service` |
| Web 本机端口 | `127.0.0.1:8100` |
| 运行配置 | `/etc/lark-message-stats/app.env`，权限 `640 root:larkstats` |
| App Secret 安全录入器 | `/usr/local/sbin/lark-message-stats-set-secret` |

PostgreSQL、FastAPI 均不监听公网；仅 Caddy 暴露 80/443。Uvicorn 已关闭 access log，避免 OAuth 授权码出现在日志中。

## OAuth 与令牌

授权页使用 Lark OAuth v2 与 PKCE：

1. 生成一次性 `state` 与 PKCE `code_verifier`。
2. 浏览器用 Secure + HttpOnly `__Host-` Cookie 保存 `state`；数据库只保存其 SHA-256 与加密后的 verifier，10 分钟过期。
3. 回调校验 Cookie 和一次性数据库状态，再用 code 换取令牌。
4. `access_token`、`refresh_token` 使用服务器独有的 AES-256-GCM 密钥加密后写入 PostgreSQL。
5. Worker 在 access token 临近过期时，对授权行加锁并原子替换 Lark 单次使用的 refresh token。

实际调用的最小用户权限：

```text
im:chat:read
im:message:readonly
offline_access
```

参考：

- [Lark 获取 OAuth 授权码](https://open.larksuite.com/document/common-capabilities/sso/api/obtain-oauth-code)
- [Lark 获取 user_access_token](https://open.larksuite.com/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/get-user-access-token)
- [Lark 刷新 user_access_token](https://open.larksuite.com/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/refresh-user-access-token)
- [Lark 群列表](https://open.larksuite.com/document/server-docs/group/chat/list)
- [Lark 会话历史消息](https://open.larksuite.com/document/server-docs/im-v1/message/list)

## 同步模型

- OAuth 成功后创建 `backfill` job。
- Worker 以用户 token 用稳定的创建时间排序分页获取群（每页最多 100）。
- 每群消息以创建时间顺序分页（每页最多 50），固定首次回填的时间窗口，避免新消息改变分页边界。
- 每页把 ledger 去重、游标和状态写在同一 PostgreSQL 事务；服务重启后安全继续。
- 首次回填完成后，每 180 秒创建一次增量 job；增量窗口包含 10 分钟重叠，以防边界漏记，重复消息不会重复计数。
- 若单个群不可读，标记为 `not_readable`，不把它误计为“零消息”；重新 OAuth 后会重新尝试。

## 验证结果

- 2026-08-27 本机复核：Ruff 通过。
- 2026-08-27 单元测试复核：4/4 通过（AES-GCM、OAuth URL/PKCE、本人发送判定、毫秒时间解析）。
- Hetzner：Python、PostgreSQL、Caddy、Web、Worker 已启用且开机自启。
- 2026-08-27 公网 `GET /healthz` 返回 200，数据库为 `ok`，Secret 状态为 `configured`；首页授权按钮已启用。

早期 Spike 已验证用户身份可读取测试外部群及其历史消息；当前 Python 版本的完整 OAuth、历史回填、增量同步和看板数字，仍需由应用可用范围内的真实 CM/AM 完成一次授权后验收。
