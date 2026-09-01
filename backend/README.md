# Lark CM/AM Assistant（测试版）

这是一个独立于现有 `lark-user-ingress/` Node 骨架的 Python 服务。

它让 CM/AM 通过 Lark OAuth 授权后，同步自己当前可见的**群聊与私聊**：

- 个人“消息提醒中心”只展示接入状态和最近检查时间；
- 个人页面不展示会话名称、成员人数或消息数量；
- 完成新版授权后，加密保存消息正文，并由 owner 在内部管理台按 CM/AM 与会话筛选查看；同步时还会保存消息发送者的 Lark 身份元数据。

## 数据边界

- 同步仅使用授权 CM/AM 自己的 user access token：`im:chat:read`、`im:message:readonly`、`offline_access` 与 `im:chat.members:read`。最后一项会分页保存群成员的最新 Lark 身份快照（稳定 ID、名称、租户属性），不保存成员历史版本。
- 不发送 Lark 消息。私聊会话发现使用 `types=p2p`，若 Lark 租户未返回该能力，会记录为不可用且不影响群聊同步。
- 旧的“仅统计”授权不会被静默升级：只有新版 OAuth 成功后才会读取私聊和保存正文。
- 正文使用独立 AES-GCM 加密域保存；不保存附件二进制，正文不进入日志。消息发送者 `open_id` / `tenant_key` / 类型与群成员快照会被保存，用于归属和提醒。规则 V1 会从所有已同意正文同步的消息中记录明确的平台主页链接或带平台标签的账号；它只表示“账号出现在该消息中”，不会把发言者自动认定为达人、经纪人或账号主人。
- 为了分页断点、去重和权限过滤，数据库将公司级 `chat_messages` 与授权用户级 observation 分开保存。共享群的正文只保存一份，但管理台只会按所选授权用户在新版授权后重新观察到的记录展示。

## 架构

```text
浏览器 / Lark 网页应用
        │ OAuth
        ▼
FastAPI + Jinja2 + HTMX + Tabler    ←→ PostgreSQL
        │                                      ▲
        └── 独立 systemd worker ─── Lark Open API（用户令牌）
```

`app/main.py` 只处理网页和 OAuth；`app/worker.py` 作为独立进程领取 PostgreSQL 任务。历史回填不使用 FastAPI `BackgroundTasks`。

个人页面不是会话管理台。首次回填和后续增量同步均由串行 worker 自动完成；公司级 `chat_messages` 以消息 ID 去重，因此多人看到同一群不会重复存储正文。

## 内部管理台

`/admin` 是独立的内部管理台，展示公司范围的授权、唯一群、去重消息与同步任务。它由数据库的 `admin_roles` 强制控制；无角色用户即使直接输入 URL 也会得到 403。当前部署只写入一个 `owner`，不自动授予其他 CM/AM。`/admin/messages`、`/admin/contacts`、`/admin/accounts` 和 `/admin/creators` 仅 owner 可访问；联系人页展示稳定 Lark 身份，账号页展示原始出现记录。每次查看正文或账号证据均审计但不记录正文。

### OpenAI 功能凭证

仅 owner 可访问的 `/admin/ai` 当前只配置一个功能：`contact_assessment`（联系人/账号关系的影子判定）。owner 会在此一次性粘贴该功能所属 OpenAI Project 的 service-account key；服务端会立即以独立加密域保存密钥，页面和审计记录都不会保留或显示明文，只显示必要的遮罩尾号。

页面会展示 Project ID、模型、启用状态、密钥轮换状态和最近测试时间/状态；空白密钥输入表示保留当前密钥，替换输入即轮换，关闭功能不必删除配置。浏览器不会直接请求 OpenAI，也不会保存 OpenAI Organization Admin Key。模型仅在 owner 从 `/admin/contacts/{open_id}` 明确点击“AI 影子判定”后由 worker 调用：任务只使用一个仍有效授权范围内的最多 23 条消息片段，使用 `store: false` 和严格 JSON Schema，保存角色/关系、置信度和消息引用而不保存 Prompt 或模型自由文本。现有 Lark 同步优先于判定任务；每一次人工确认都会重新校验证据授权仍有效。

成本归属以 OpenAI 的 Project 为单位：生产环境为每个成本中心/功能建立独立 Project，并为该 Project 建立独立 service account 与 key。例如本功能可使用 `TT Lark – Contact Assessment – Production` / `contact-assessment-worker`。这样 OpenAI 后台的 Project 用量、预算和告警可以直接对应功能；以后翻译、回复建议应各自使用新的 Project 和 key。

数据模型与页面口径见 [`docs/as-built/admin-console-v0.3.md`](../docs/as-built/admin-console-v0.3.md)。

## 本地开发

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env  # 仅本地；不要提交真实密钥
.venv/bin/python -m pytest
```

生产部署使用仓库根目录 `../infra/` 中的 systemd 和 Caddy 配置；具体步骤见 [`docs/runbooks/deploy-mvp.md`](../docs/runbooks/deploy-mvp.md)。App Secret 必须由管理员在服务器终端的隐藏输入提示中录入，不能提交到 Git 或发送到聊天。
