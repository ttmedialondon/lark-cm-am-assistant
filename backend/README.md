# Lark CM/AM Assistant（测试版）

这是一个独立于现有 `lark-user-ingress/` Node 骨架的 Python 服务。

它让 CM/AM 通过 Lark OAuth 授权后，同步自己当前可见的**群聊与私聊**：

- 已加入群聊和私聊数；
- 已同步消息总数；
- 当前授权用户发送的消息数；
- 每个可见群的 Lark 人类成员总数；
- 最近一次同步状态。
- 对单个会话手动请求重新扫描 Lark API 可返回的全部历史。
- 完成新版授权后，加密保存消息正文，并由 owner 在内部管理台按 CM/AM 与会话筛选查看。

## 数据边界

- 同步仅使用授权 CM/AM 自己的 user access token：`im:chat:read`、`im:message:readonly` 与 `offline_access`；`im:chat.members:read` 仅在管理员额外启用成员总数展示时使用。
- 不发送 Lark 消息。私聊会话发现使用 `types=p2p`，若 Lark 租户未返回该能力，会记录为不可用且不影响群聊同步。
- 旧的“仅统计”授权不会被静默升级：只有新版 OAuth 成功后才会读取私聊和保存正文。
- 正文使用独立 AES-GCM 加密域保存；不保存附件二进制、成员名单或完整发送者资料，且正文不进入日志。
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

仪表盘的“同步全部历史”按钮不会直接清除分页游标，也不会创建并发 worker。它只为当前授权用户的该会话写入一个请求标记；现有串行 worker 会在当前分页安全结束后，从头回填该会话。公司级 `chat_messages` 以消息 ID 去重，因此重扫或多人看到同一群都不会重复统计。

## 内部管理台

`/admin` 是独立的内部管理台，展示公司范围的授权、唯一群、去重消息与同步任务。它由数据库的 `admin_roles` 强制控制；无角色用户即使直接输入 URL 也会得到 403。当前部署只写入一个 `owner`，不自动授予其他 CM/AM。`/admin/messages` 仅 owner 可访问，每次查看均审计但不记录正文。

数据模型与页面口径见 [`docs/as-built/admin-console-v0.3.md`](../docs/as-built/admin-console-v0.3.md)。

## 本地开发

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env  # 仅本地；不要提交真实密钥
.venv/bin/python -m pytest
```

生产部署使用仓库根目录 `../infra/` 中的 systemd 和 Caddy 配置；具体步骤见 [`docs/runbooks/deploy-mvp.md`](../docs/runbooks/deploy-mvp.md)。App Secret 必须由管理员在服务器终端的隐藏输入提示中录入，不能提交到 Git 或发送到聊天。
